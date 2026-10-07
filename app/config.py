"""Chargement de la configuration YAML avec substitution des variables d'environnement."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


class Config:
    """Wrapper d'accès à la configuration, style attribut ou dict."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def raw(self) -> dict:
        return self._data


def load_config(config_path: str | Path = "config/config.yaml", env_path: str | Path = ".env") -> "Config":
    """Charge le .env puis le fichier YAML en substituant les ${VARIABLES}."""
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Fichier de configuration introuvable: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return Config(_substitute_env(raw))
