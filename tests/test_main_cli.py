"""Tests du verrou mono-instance (`app.runtime_lock`), utilisé par `main.py run`
et `perps_main.py run`."""
from __future__ import annotations

import importlib.util

import pytest

from app import runtime_lock


@pytest.fixture
def _release_lock():
    yield
    runtime_lock.release_single_instance_locks()


def test_single_instance_lock_is_noop_without_fcntl(tmp_path, _release_lock):
    if importlib.util.find_spec("fcntl") is not None:
        pytest.skip("fcntl disponible : voir test_single_instance_lock_refuses_second_instance")
    assert runtime_lock.acquire_single_instance_lock(str(tmp_path / "bot.db")) is True
    assert runtime_lock._HANDLES == {}


def test_single_instance_lock_refuses_second_instance(tmp_path, _release_lock):
    pytest.importorskip("fcntl")
    db_path = str(tmp_path / "bot.db")

    assert runtime_lock.acquire_single_instance_lock(db_path) is True
    assert (tmp_path / "bot.db.lock").read_text(encoding="utf-8").strip().isdigit()
    # Second `open()` + flock sur le même fichier : nouvelle description de
    # fichier, donc conflit même depuis le même process.
    assert runtime_lock.acquire_single_instance_lock(db_path) is False


def test_main_cli_reexports_the_lock_helper():
    import main as cli

    assert cli.acquire_single_instance_lock is runtime_lock.acquire_single_instance_lock
