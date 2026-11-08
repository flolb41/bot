"""Validated runtime overrides for live trading strategy thresholds."""
from __future__ import annotations

import json
import logging
import math
from copy import deepcopy

from app.database import Database

logger = logging.getLogger("app.trading.runtime_settings")

STATE_KEY = "runtime_trading_settings"
SETTINGS_LIMITS = {
    "min_net_profit_usd": (0.01, 5.0),
    "min_net_profit_pct": (0.5, 1.0),
    "flashloan_min_net_profit_usd": (0.25, 10.0),
    "min_liquidity_usd": (10_000.0, 1_000_000.0),
}
DEFAULTS = {
    "min_net_profit_usd": 0.01,
    "min_net_profit_pct": 0.5,
    "flashloan_min_net_profit_usd": 0.25,
    "min_liquidity_usd": 10_000.0,
    "flashloan_enabled": False,
}


def validate_runtime_settings(settings: dict) -> dict:
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ValueError("Réglages incomplets ou inconnus.")
    validated: dict[str, float | bool] = {}
    for name, (minimum, maximum) in SETTINGS_LIMITS.items():
        if isinstance(settings[name], bool):
            raise ValueError(f"Valeur invalide pour {name}.")
        try:
            value = float(settings[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valeur invalide pour {name}.") from exc
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{name} doit être compris entre {minimum:g} et {maximum:g}.")
        validated[name] = value
    if not isinstance(settings["flashloan_enabled"], bool):
        raise ValueError("flashloan_enabled doit être un booléen.")
    validated["flashloan_enabled"] = settings["flashloan_enabled"]
    return validated


def get_runtime_settings(db: Database, config: dict) -> dict:
    trading = (config or {}).get("trading") or {}
    defaults = {
        "min_net_profit_usd": trading.get("min_net_profit_usd", DEFAULTS["min_net_profit_usd"]),
        "min_net_profit_pct": trading.get("min_net_profit_pct", DEFAULTS["min_net_profit_pct"]),
        "flashloan_min_net_profit_usd": trading.get(
            "flashloan_min_net_profit_usd", DEFAULTS["flashloan_min_net_profit_usd"],
        ),
        "min_liquidity_usd": trading.get("min_liquidity_usd", DEFAULTS["min_liquidity_usd"]),
        "flashloan_enabled": trading.get("flashloan_enabled", DEFAULTS["flashloan_enabled"]),
    }
    raw = db.get_state(STATE_KEY)
    if raw is not None:
        try:
            overrides = json.loads(raw)
            defaults.update(validate_runtime_settings(overrides))
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Réglages live persistés invalides ; utilisation de la configuration statique."
            )
    validated_numeric = {
        name: value for name, value in defaults.items() if name in SETTINGS_LIMITS
    }
    for name, value in validated_numeric.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = DEFAULTS[name]
        minimum, maximum = SETTINGS_LIMITS[name]
        defaults[name] = numeric if math.isfinite(numeric) and minimum <= numeric <= maximum else DEFAULTS[name]
    defaults["flashloan_enabled"] = bool(defaults["flashloan_enabled"])
    return defaults


def save_runtime_settings(db: Database, settings: dict) -> dict:
    validated = validate_runtime_settings(settings)
    db.set_state(STATE_KEY, json.dumps(validated, sort_keys=True, separators=(",", ":")))
    return validated


def apply_runtime_settings(db: Database, config: dict) -> dict:
    raw = config.raw if hasattr(config, "raw") else (config or {})
    effective = deepcopy(raw)
    trading = effective.setdefault("trading", {})
    trading.update(get_runtime_settings(db, effective))
    return effective
