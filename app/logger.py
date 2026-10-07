"""Configuration du logging : fichier tournant + console, léger pour Raspberry Pi."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(name: str = "app", level: str = "INFO", log_file: str = "logs/app.log") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # déjà configuré

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 2 Mo x 5 fichiers max : reste léger sur la carte SD du Pi
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
