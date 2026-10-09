"""Tests de `app.trading.rpc` : ordre de priorité des endpoints et basculement
automatique du `RotatingHTTPProvider` (sans réseau : le gestionnaire de
session HTTP de web3 est remplacé par un faux)."""
from __future__ import annotations

import requests

from app.trading import rpc

INFURA = "https://base-mainnet.infura.io/v3/abcdef123456"
ALCHEMY = "https://base-mainnet.g.alchemy.com/v2/GiA9GH_xxxxxxxx"
OK_BODY = b'{"jsonrpc":"2.0","id":1,"result":"0x2105"}'


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


class _FakeSessionManager:
    """Remplace `HTTPProvider._request_session_manager` : `answers` associe une
    URL à une liste de réponses successives (bytes) ou d'exceptions à lever."""

    def __init__(self, answers: dict[str, list]):
        self.answers = {url: list(seq) for url, seq in answers.items()}
        self.calls: list[str] = []

    def make_post_request(self, endpoint_uri, request_data, **kwargs):  # noqa: ARG002
        self.calls.append(endpoint_uri)
        value = self.answers[endpoint_uri].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _provider(urls, answers):
    provider_class = rpc._rotating_provider_class()
    provider_class._cooldown_until.clear()
    provider_class._consecutive_failures.clear()
    provider = provider_class(urls, request_kwargs={"timeout": 5})
    provider._request_session_manager = _FakeSessionManager(answers)
    return provider


def test_rpc_urls_keeps_env_order_then_appends_public_defaults(monkeypatch):
    monkeypatch.setenv("RPC_BASE", f" {INFURA} , {ALCHEMY},{rpc.DEFAULT_RPC_URLS[0]} ")
    urls = rpc.rpc_urls()
    assert urls[:2] == [INFURA, ALCHEMY]
    assert urls[2:] == list(rpc.DEFAULT_RPC_URLS)  # doublon dédupliqué, défauts en dernier


def test_rpc_urls_defaults_only_when_env_missing(monkeypatch):
    monkeypatch.delenv("RPC_BASE", raising=False)
    assert rpc.rpc_urls() == list(rpc.DEFAULT_RPC_URLS)


def test_mask_rpc_url_hides_api_keys():
    assert "abcdef123456" not in rpc.mask_rpc_url(INFURA)
    assert rpc.mask_rpc_url("https://mainnet.base.org") == "https://mainnet.base.org"


def test_primary_endpoint_used_first_and_fallback_on_429():
    provider = _provider([INFURA, ALCHEMY], {INFURA: [_http_error(429)], ALCHEMY: [OK_BODY]})
    assert provider._make_request("eth_chainId", b"{}") == OK_BODY
    assert provider._request_session_manager.calls == [INFURA, ALCHEMY]


def test_quarantined_primary_is_skipped_until_cooldown_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rpc.time, "monotonic", lambda: now[0])
    provider = _provider(
        [INFURA, ALCHEMY],
        {INFURA: [_http_error(429), OK_BODY], ALCHEMY: [OK_BODY, OK_BODY]},
    )
    provider._make_request("eth_chainId", b"{}")  # Infura 429 -> quarantaine 15s, Alchemy répond
    provider._make_request("eth_chainId", b"{}")  # Infura encore en quarantaine : Alchemy directement
    assert provider._request_session_manager.calls == [INFURA, ALCHEMY, ALCHEMY]
    now[0] += rpc._RATE_LIMIT_COOLDOWN_SECONDS + 1
    provider._make_request("eth_chainId", b"{}")  # quarantaine expirée : Infura reprend la main
    assert provider._request_session_manager.calls[-1] == INFURA


def test_quarantine_doubles_on_consecutive_failures_and_resets_on_success(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rpc.time, "monotonic", lambda: now[0])
    provider_class = rpc._rotating_provider_class()
    provider = _provider(
        [INFURA, ALCHEMY],
        {INFURA: [_http_error(429), _http_error(429), OK_BODY], ALCHEMY: [OK_BODY, OK_BODY]},
    )
    provider._make_request("m", b"{}")
    assert provider_class._cooldown_until[INFURA] == rpc._RATE_LIMIT_COOLDOWN_SECONDS
    now[0] = provider_class._cooldown_until[INFURA] + 1
    provider._make_request("m", b"{}")  # 2e échec consécutif : quarantaine doublée
    assert provider_class._cooldown_until[INFURA] - now[0] == 2 * rpc._RATE_LIMIT_COOLDOWN_SECONDS
    now[0] = provider_class._cooldown_until[INFURA] + 1
    provider._make_request("m", b"{}")  # succès : compteur remis à zéro
    assert provider_class._consecutive_failures[INFURA] == 0


def test_json_rpc_rate_limit_body_triggers_fallback():
    limited = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"rate limit exceeded"}}'
    provider = _provider([INFURA, ALCHEMY], {INFURA: [limited], ALCHEMY: [OK_BODY]})
    assert provider._make_request("m", b"{}") == OK_BODY


def test_non_rate_limit_http_error_is_raised_without_fallback():
    provider = _provider([INFURA, ALCHEMY], {INFURA: [_http_error(400)], ALCHEMY: [OK_BODY]})
    try:
        provider._make_request("m", b"{}")
    except requests.HTTPError:
        pass
    else:  # pragma: no cover
        raise AssertionError("un 400 (requête invalide) ne doit pas déclencher de bascule")
    assert provider._request_session_manager.calls == [INFURA]


def test_all_endpoints_failing_raises_last_error():
    provider = _provider([INFURA, ALCHEMY], {INFURA: [_http_error(429)], ALCHEMY: [_http_error(503)]})
    try:
        provider._make_request("m", b"{}")
    except requests.HTTPError as exc:
        assert exc.response.status_code == 503
    else:  # pragma: no cover
        raise AssertionError("tous les endpoints en échec doivent lever la dernière erreur")
