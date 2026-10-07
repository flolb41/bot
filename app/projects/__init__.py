"""Registre des projets suivis (section 1 du TODO)."""
from __future__ import annotations

from app.projects.analog import AnalogProject
from app.projects.asentum import AsentumProject
from app.projects.base import BaseProject, ScanResult
from app.projects.binance_learn import BinanceLearnProject
from app.projects.canopy import CanopyProject
from app.projects.flop import FlopProject
from app.projects.iris_credit import IrisCreditProject
from app.projects.kryvora import KryvoraProject
from app.projects.orbinum import OrbinumProject
from app.projects.polyester import PolyesterProject
from app.projects.push import PushProject
from app.projects.vibe import VibeProject

# Ordre = priorité décroissante telle que définie section 1 du TODO.
PROJECT_REGISTRY: list[type[BaseProject]] = [
    PushProject,       # A - PRIORITÉ 1
    CanopyProject,     # B - PRIORITÉ 1
    AnalogProject,     # C - PRIORITÉ 1
    KryvoraProject,    # D - PRIORITÉ 2
    VibeProject,       # E - PRIORITÉ 2
    PolyesterProject,  # F - PRIORITÉ 2
    FlopProject,       # G - à préparer
    OrbinumProject,    # H
    AsentumProject,    # I
    IrisCreditProject,  # J
    BinanceLearnProject,  # K - section 2 du TODO, surveillance site uniquement
]


def all_projects() -> list[BaseProject]:
    return [cls() for cls in PROJECT_REGISTRY]


def get_project(project_id: str) -> BaseProject | None:
    for cls in PROJECT_REGISTRY:
        instance = cls()
        if instance.id == project_id:
            return instance
    return None


__all__ = ["PROJECT_REGISTRY", "all_projects", "get_project", "BaseProject", "ScanResult"]
