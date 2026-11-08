"""Tests de scripts/ci_status.py (gate CI de la mise à jour automatique du Pi)."""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_status", Path(__file__).resolve().parents[1] / "scripts" / "ci_status.py",
)
ci_status = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_status)


def _run(status: str, conclusion: str | None, started_at: str, run_id: int = 1) -> dict:
    return {"id": run_id, "name": "tests", "status": status, "conclusion": conclusion, "started_at": started_at}


def test_evaluate_without_check_runs_is_pending():
    assert ci_status.evaluate([]) == ci_status.EXIT_PENDING


def test_evaluate_in_progress_is_pending():
    assert ci_status.evaluate([_run("in_progress", None, "2026-11-08T16:00:00Z")]) == ci_status.EXIT_PENDING


def test_evaluate_success_and_failure():
    assert ci_status.evaluate([_run("completed", "success", "2026-11-08T16:00:00Z")]) == ci_status.EXIT_SUCCESS
    assert ci_status.evaluate([_run("completed", "failure", "2026-11-08T16:00:00Z")]) == ci_status.EXIT_FAILED
    assert ci_status.evaluate([_run("completed", "cancelled", "2026-11-08T16:00:00Z")]) == ci_status.EXIT_FAILED


def test_evaluate_only_considers_the_most_recent_run():
    runs = [
        _run("completed", "failure", "2026-11-08T16:00:00Z", run_id=1),
        _run("completed", "success", "2026-11-08T16:10:00Z", run_id=2),  # re-run manuel réussi
    ]
    assert ci_status.evaluate(runs) == ci_status.EXIT_SUCCESS
    assert ci_status.evaluate(list(reversed(runs))) == ci_status.EXIT_SUCCESS


def test_fetch_check_runs_targets_commit_and_check_name(monkeypatch):
    seen = {}

    @contextmanager
    def fake_opener(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["timeout"] = timeout
        yield io.BytesIO(json.dumps({"check_runs": [_run("completed", "success", "x")]}).encode("utf-8"))

    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    runs = ci_status.fetch_check_runs("flolb41/bot", "abc123", "tests", opener=fake_opener)

    assert len(runs) == 1
    assert seen["url"] == "https://api.github.com/repos/flolb41/bot/commits/abc123/check-runs?check_name=tests&per_page=50"
    assert seen["headers"]["authorization"] == "Bearer secret-token"
    assert seen["headers"]["accept"] == "application/vnd.github+json"
    assert seen["timeout"] == 20


def test_main_returns_unknown_when_github_is_unreachable(monkeypatch, capsys):
    def failing_fetch(*_args, **_kwargs):
        raise urllib.error.URLError("réseau coupé")

    monkeypatch.setattr(ci_status, "fetch_check_runs", failing_fetch)

    assert ci_status.main(["ci_status.py", "flolb41/bot", "abc123def", "tests"]) == ci_status.EXIT_UNKNOWN
    assert "indisponible" in capsys.readouterr().err


def test_main_reports_verdict(monkeypatch, capsys):
    monkeypatch.setattr(
        ci_status, "fetch_check_runs",
        lambda *_args, **_kwargs: [_run("completed", "success", "2026-11-08T16:00:00Z")],
    )

    assert ci_status.main(["ci_status.py", "flolb41/bot", "abc123def"]) == ci_status.EXIT_SUCCESS
    assert "succès" in capsys.readouterr().out


def test_main_without_arguments_is_unknown():
    assert ci_status.main(["ci_status.py"]) == ci_status.EXIT_UNKNOWN
