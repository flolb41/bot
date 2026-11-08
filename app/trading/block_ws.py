"""Écoute WebSocket des nouveaux blocs Base (eth_subscribe newHeads).

Objectif : remplacer le polling REST pur par une détection réactive des
nouveaux blocs, afin de lancer un scan d'arbitrage dans les secondes suivant
un bloc plutôt que d'attendre le prochain tick du scheduler (jusqu'à 60 s de
latence).

Principe :
- Connexion WebSocket à un nœud Base (URL configurées, rotation en cas de
  panne/429/reconnexion forcée).
- Abonnement `eth_subscribe` sur `newHeads` : chaque nouveau bloc émet une
  notification JSON-RPC.
- À chaque nouveau bloc, si l'intervalle minimal depuis le dernier scan est
  respecté (garde-fou DexScreener ~1.1s/appel, ~14 appels/scan → ≥~15s par
  cycle), on déclenche `run_arbitrage_scan_job`.
- Le scheduler interval (`app.scheduler.build_scheduler`) reste actif en
  fallback : si le WebSocket est coupé, les scans continuent aux intervalles
  configurés.

Ce module ne fait QUE détecter les blocs et déclencher des scans. Il n'envoie
jamais de transaction — l'exécution reste gérée par `app.trading.executor` /
`app.trading.flashloan` avec tous les garde-fous existants.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import websocket  # websocket-client

from app.trading.rpc import DEFAULT_RPC_URLS, mask_rpc_url

logger = logging.getLogger(__name__)

# Intervalle minimal entre deux scans déclenchés par WebSocket (secondes).
# DexScreener throttlé à 1.1s/appel, ~14 appels par cycle (1 par token seed)
# → ~15s minimum par cycle. 30s laisse une marge de sécurité et évite de
# saturer l'API publique en cas de rafale de blocs.
MIN_SCAN_INTERVAL_SECONDS = 30.0

# Délai max d'attente d'un message WebSocket avant ping/keepalive (secondes).
WS_PING_INTERVAL = 20.0
WS_PING_TIMEOUT = 10.0

# Délai entre deux tentatives de reconnexion WebSocket (secondes).
WS_RECONNECT_DELAY = 5.0

# Nombre d'URL WebSocket à tenter en rotation (d'abord celles de RPC_BASE,
# puis des endpoints publics gratuits).
DEFAULT_WS_URLS: tuple[str, ...] = (
    "wss://base-rpc.publicnode.com",
    "wss://base.blockpi.network/v1/rpc/public",
)


class BlockWebSocketListener:
    """Écoute les nouveaux blocs Base via WebSocket et déclenche un callback
    à chaque bloc (respectant l'intervalle minimal entre scans).

    Utilise `websocket-client` (déjà présent via httpx/ws dépendances ? non,
    ajouté explicitement). Rotation d'URL en cas de déconnexion/429.
    """

    def __init__(
        self,
        on_new_block: Callable[[int], None],
        ws_urls: Optional[list[str]] = None,
        min_scan_interval_seconds: float = MIN_SCAN_INTERVAL_SECONDS,
    ):
        self._on_new_block = on_new_block
        self._urls = ws_urls or list(DEFAULT_WS_URLS)
        self._min_interval = min_scan_interval_seconds
        self._last_scan_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None

    # ------------------------------------------------------------------ start / stop

    def start(self) -> None:
        """Démarre l'écoute WebSocket dans un thread daemon."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="base-ws-listener", daemon=True)
        self._thread.start()
        logger.info("WebSocket block listener démarré (%d URL configurées).", len(self._urls))

    def stop(self, timeout: float = 10.0) -> None:
        """Arrête l'écoute WebSocket et attend la fin du thread (max `timeout` s)."""
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("WebSocket block listener arrêté.")

    # ------------------------------------------------------------------ internal loop

    def _run_loop(self) -> None:
        """Boucle de reconnexion : tente chaque URL en rotation jusqu'à stop()."""
        url_index = 0
        while not self._stop.is_set():
            url = self._urls[url_index % len(self._urls)]
            try:
                self._connect_once(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Erreur WebSocket sur %s : %s — reconnexion dans %.0fs",
                               mask_rpc_url(url), exc, WS_RECONNECT_DELAY)
            if self._stop.is_set():
                break
            time.sleep(WS_RECONNECT_DELAY)
            url_index += 1

    def _connect_once(self, url: str) -> None:
        """Ouvre une connexion WebSocket, souscrit à newHeads, boucle sur les messages."""
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # `run_forever` est bloquant : retourne seulement à la fermeture.
        self._ws.run_forever(
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
            reconnect=0,  # on gère nous-mêmes la reconnexion dans _run_loop
        )
        self._ws = None

    # ------------------------------------------------------------------ callbacks

    def _on_open(self, _ws) -> None:
        logger.info("WebSocket connecté : %s", mask_rpc_url(self._ws.url if self._ws else "?"))
        # Abonnement aux nouveaux blocs (eth_subscribe newHeads).
        sub_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["newHeads"],
        })
        self._ws.send(sub_msg)

    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        # Réponse d'abonnement (id=1, result=subscription id) — ignorer.
        if data.get("id") == 1:
            return
        # Notification newHeads : params.subscription == notre id, params.result = bloc.
        params = data.get("params") or {}
        if params.get("subscription") and "result" in params:
            block = params["result"]
            block_number = int(block.get("number", "0x0"), 16) if isinstance(block.get("number"), str) else int(block.get("number", 0))
            self._maybe_trigger_scan(block_number)

    def _on_error(self, _ws, error) -> None:
        logger.warning("Erreur WebSocket : %s", error)

    def _on_close(self, _ws, code, reason) -> None:
        logger.info("WebSocket fermé (code=%s, reason=%s) — reconnexion.", code, reason)

    # ------------------------------------------------------------------ scan trigger

    def _maybe_trigger_scan(self, block_number: int) -> None:
        """Déclenche le scan si l'intervalle minimal est respecté."""
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_scan_at
            if elapsed < self._min_interval:
                return
            self._last_scan_at = now
        logger.info("Nouveau bloc Base #%d — déclenchement du scan d'arbitrage.", block_number)
        try:
            self._on_new_block(block_number)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec du scan déclenché par WebSocket (bloc #%d) : %s", block_number, exc)
