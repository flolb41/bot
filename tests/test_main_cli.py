"""Tests du point d'entrée CLI (verrou mono-instance de `main.py run`)."""
from __future__ import annotations

import importlib.util

import pytest

import main as cli


@pytest.fixture
def _release_lock():
    yield
    handle = cli._SINGLE_INSTANCE_LOCK
    if handle is not None:
        handle.close()
        cli._SINGLE_INSTANCE_LOCK = None


def test_single_instance_lock_is_noop_without_fcntl(tmp_path, monkeypatch, _release_lock):
    if importlib.util.find_spec("fcntl") is not None:
        pytest.skip("fcntl disponible : voir test_single_instance_lock_refuses_second_instance")
    assert cli.acquire_single_instance_lock(str(tmp_path / "bot.db")) is True
    assert cli._SINGLE_INSTANCE_LOCK is None


def test_single_instance_lock_refuses_second_instance(tmp_path, _release_lock):
    pytest.importorskip("fcntl")
    db_path = str(tmp_path / "bot.db")

    assert cli.acquire_single_instance_lock(db_path) is True
    assert (tmp_path / "bot.db.lock").read_text(encoding="utf-8").strip().isdigit()
    # Second `open()` + flock sur le même fichier : nouvelle description de
    # fichier, donc conflit même depuis le même process.
    assert cli.acquire_single_instance_lock(db_path) is False
