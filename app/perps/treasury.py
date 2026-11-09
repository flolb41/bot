"""Trésorerie (papier) : ce que le capital oisif pourrait rapporter.

Observe périodiquement (1) le taux de dépôt USDC d'Aave V3 sur Base, lu
on-chain par un `eth_call` brut (le venv perps n'a pas web3), (2) les
rendements du vault LP Veranta (ERC-4626, contrepartie des traders — risque
de drawdown en cas de pertes des LP) via le SDK, (3) le solde USDC oisif du
wallet, et cumule le « manque à gagner » = USDC oisif × meilleur taux × durée.
Aucun dépôt réel : c'est la référence que toute stratégie doit battre.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from app.perps.models import TreasuryConfig
from app.perps.storage import PerpsStore

logger = logging.getLogger("app.perps.treasury")

_GET_RESERVE_DATA_SELECTOR = "35ea6a75"  # getReserveData(address)
_BALANCE_OF_SELECTOR = "70a08231"        # balanceOf(address)
_RAY = 10 ** 27


def _encode_address_arg(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def decode_aave_liquidity_rate(return_data: str) -> float:
    """Taux de dépôt annualisé (%) depuis la réponse brute de `getReserveData` :
    3e mot de la struct `ReserveData` (`currentLiquidityRate`, en ray)."""
    payload = return_data.removeprefix("0x")
    if len(payload) < 3 * 64:
        raise ValueError("Réponse getReserveData trop courte.")
    rate_ray = int(payload[2 * 64:3 * 64], 16)
    return rate_ray / _RAY * 100


def decode_uint(return_data: str) -> int:
    payload = return_data.removeprefix("0x")
    return int(payload[:64], 16) if payload else 0


def resolve_rpc_url(config: TreasuryConfig) -> str:
    if config.rpc_url:
        return config.rpc_url
    env_urls = [u.strip() for u in os.environ.get("RPC_BASE", "").split(",") if u.strip()]
    return env_urls[0] if env_urls else "https://mainnet.base.org"


class TreasuryDataProvider(Protocol):
    async def aave_usdc_apr_pct(self) -> float | None: ...

    async def vault_returns_pct(self) -> dict[str, float | None]: ...

    async def idle_usdc(self) -> float | None: ...

    async def deposited_usdc(self) -> float | None: ...


class VerantaTreasuryData:
    """Fournisseur réel : JSON-RPC brut (httpx) pour Aave/USDC, SDK pour le vault."""

    def __init__(self, config: TreasuryConfig, market_data=None):
        self._config = config
        self._market_data = market_data   # VerantaMarketData : réutilise son client SDK
        self._rpc_url = resolve_rpc_url(config)

    async def _eth_call(self, to: str, data: str) -> str:
        import httpx

        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": "0x" + data}, "latest"]}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self._rpc_url, json=payload)
            response.raise_for_status()
            body = response.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    async def aave_usdc_apr_pct(self) -> float | None:
        try:
            raw = await self._eth_call(
                self._config.aave_pool_address, _GET_RESERVE_DATA_SELECTOR + _encode_address_arg(self._config.usdc_address),
            )
            apr = decode_aave_liquidity_rate(raw)
            return apr if 0 <= apr < 100 else None
        except Exception as exc:  # noqa: BLE001
            logger.info("Taux Aave indisponible : %s", exc)
            return None

    async def vault_returns_pct(self) -> dict[str, float | None]:
        try:
            client = await self._market_data._get_client()  # noqa: SLF001 - même client SDK que le marché
            payload = await client.info.vault_share_rate_returns()
            returns = ((payload or {}).get("returns") or {}).get("jr") or {}
            return {
                "7d": returns.get("last7d"), "30d": returns.get("last30d"), "12m": returns.get("last12m"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("Rendements du vault Veranta indisponibles : %s", exc)
            return {"7d": None, "30d": None, "12m": None}

    async def idle_usdc(self) -> float | None:
        return await self._erc20_balance(self._config.usdc_address, "Solde USDC")

    async def deposited_usdc(self) -> float | None:
        """USDC déposés sur Aave V3 = solde d'aUSDC (il grossit avec les intérêts)."""
        return await self._erc20_balance(self._config.ausdc_address, "Solde aUSDC")

    async def _erc20_balance(self, token: str, label: str) -> float | None:
        if not self._config.wallet_address:
            return None
        try:
            raw = await self._eth_call(token, _BALANCE_OF_SELECTOR + _encode_address_arg(self._config.wallet_address))
            return decode_uint(raw) / 1e6
        except Exception as exc:  # noqa: BLE001
            logger.info("%s indisponible : %s", label, exc)
            return None


def build_observation(
    store: PerpsStore, now: float, *, idle_usdc: float | None, aave_apr: float | None,
    vault: dict[str, float | None], max_gap_seconds: float, deposited_usdc: float | None = None,
) -> dict[str, Any]:
    """Ligne d'observation + manque à gagner cumulé (fonction pure hors lecture
    de la dernière observation). Le vault est comparé sur 30 j : le 7 j est trop
    bruité, le 12 m lisse les régimes. Le manque à gagner ne porte que sur
    l'USDC liquide : ce qui est déposé sur Aave travaille déjà."""
    candidates = [("aave_usdc", aave_apr), ("veranta_vault_30d", vault.get("30d"))]
    candidates = [(venue, apr) for venue, apr in candidates if apr is not None]
    best_venue, best_apr = max(candidates, key=lambda c: c[1]) if candidates else (None, None)
    previous = store.latest_treasury_observation()
    missed = float(previous["missed_yield_usd"]) if previous else 0.0
    if previous and idle_usdc is not None and best_apr is not None:
        elapsed = min(max(0.0, now - float(previous["observed_at"])), max_gap_seconds)
        missed += idle_usdc * best_apr / 100 * elapsed / (365 * 86400)
    return {
        "observed_at": now, "idle_usdc": idle_usdc, "deposited_usdc": deposited_usdc, "aave_usdc_apr_pct": aave_apr,
        "vault_7d_apr_pct": vault.get("7d"), "vault_30d_apr_pct": vault.get("30d"), "vault_12m_apr_pct": vault.get("12m"),
        "best_apr_pct": best_apr, "best_venue": best_venue, "missed_yield_usd": missed,
    }


async def run_treasury_observation(store: PerpsStore, config: TreasuryConfig, provider: TreasuryDataProvider,
                                   now: float) -> dict[str, Any]:
    idle = await provider.idle_usdc()
    deposited = await provider.deposited_usdc()
    aave = await provider.aave_usdc_apr_pct()
    vault = await provider.vault_returns_pct()
    row = build_observation(
        store, now, idle_usdc=idle, aave_apr=aave, vault=vault, max_gap_seconds=2 * config.interval_minutes * 60,
        deposited_usdc=deposited,
    )
    store.record_treasury_observation(row)
    logger.info(
        "Trésorerie : USDC liquide %s | déposé Aave %s | Aave %s | vault Veranta 7j/30j/12m %s/%s/%s | manque à gagner cumulé %.4f $",
        f"{idle:.2f}" if idle is not None else "n/a", f"{deposited:.2f}" if deposited is not None else "n/a",
        f"{aave:.2f} %" if aave is not None else "n/a",
        *(f"{v:.1f} %" if v is not None else "n/a" for v in (vault.get("7d"), vault.get("30d"), vault.get("12m"))),
        row["missed_yield_usd"],
    )
    return row
