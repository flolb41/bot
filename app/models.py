"""Modèles de données (dataclasses) pour le Crypto Reward Hunter."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProjectType(str, Enum):
    TESTNET = "testnet"
    AIRDROP = "airdrop"
    CAMPAIGNE = "campagne"
    QUEST_PLATFORM = "quest_platform"
    FAUCET = "faucet"
    A_PREPARER = "a_preparer"


class ProjectStatus(str, Enum):
    ACTIF = "actif"
    TESTNET = "testnet"
    CAMPAGNE_CLAIM = "campagne_claim"
    A_PREPARER = "a_preparer"
    TERMINE = "termine"
    SUSPECT = "suspect"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    EXPIRED = "expired"
    SKIPPED = "skipped"


class RewardStatus(str, Enum):
    PENDING = "pending"
    RECEIVED = "received"
    CONVERTED = "converted"
    LOST = "lost"


class RiskLevel(str, Enum):
    """Classification de score (section 7 du TODO)."""

    CRITIQUE = "PRIORITÉ CRITIQUE"
    HAUTE = "PRIORITÉ HAUTE"
    A_SURVEILLER = "À SURVEILLER"
    FAIBLE = "FAIBLE"
    IGNORER = "IGNORER"


@dataclass
class Project:
    id: str
    name: str
    token: str = ""
    network: str = ""
    url: str = ""
    official_url: str = ""
    type: str = ProjectType.TESTNET.value
    status: str = ProjectStatus.ACTIF.value
    score: int = 0
    start_date: str | None = None
    deadline: str | None = None
    requires_kyc: bool = False
    requires_capital: bool = False
    estimated_cost: float = 0.0
    notes: str = ""

    def to_row(self) -> dict:
        row = {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "network": self.network,
            "url": self.url,
            "official_url": self.official_url,
            "type": self.type,
            "status": self.status,
            "score": self.score,
            "start_date": self.start_date,
            "deadline": self.deadline,
            "requires_kyc": int(self.requires_kyc),
            "requires_capital": int(self.requires_capital),
            "estimated_cost": self.estimated_cost,
            "notes": self.notes,
        }
        return row


@dataclass
class Task:
    project_id: str
    title: str
    description: str = ""
    url: str = ""
    difficulty: str = "facile"
    estimated_time: str = ""
    reward_type: str = ""
    reward_estimate: str = ""
    status: str = TaskStatus.PENDING.value
    deadline: str | None = None

    def to_row(self) -> dict:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "difficulty": self.difficulty,
            "estimated_time": self.estimated_time,
            "reward_type": self.reward_type,
            "reward_estimate": self.reward_estimate,
            "status": self.status,
            "deadline": self.deadline,
        }


@dataclass
class Wallet:
    name: str
    address: str
    network: str
    purpose: str = ""


@dataclass
class Reward:
    project_id: str | None
    token: str
    amount: float
    wallet: str = ""
    status: str = RewardStatus.PENDING.value
    tx_hash: str = ""

    def to_row(self) -> dict:
        return {
            "project_id": self.project_id,
            "token": self.token,
            "amount": self.amount,
            "wallet": self.wallet,
            "status": self.status,
            "tx_hash": self.tx_hash,
        }


@dataclass
class Transaction:
    network: str
    tx_hash: str
    wallet: str = ""
    action: str = ""
    gas_cost: float = 0.0
    token: str = ""
    amount: float = 0.0

    def to_row(self) -> dict:
        return {
            "network": self.network,
            "tx_hash": self.tx_hash,
            "wallet": self.wallet,
            "action": self.action,
            "gas_cost": self.gas_cost,
            "token": self.token,
            "amount": self.amount,
        }


@dataclass
class ScoreBreakdown:
    """Détail du calcul de score pour un projet, utile pour la transparence /today."""

    total: int = 0
    bonuses: dict[str, int] = field(default_factory=dict)
    maluses: dict[str, int] = field(default_factory=dict)

    def classify(self) -> RiskLevel:
        if self.total >= 80:
            return RiskLevel.CRITIQUE
        if self.total >= 60:
            return RiskLevel.HAUTE
        if self.total >= 40:
            return RiskLevel.A_SURVEILLER
        if self.total >= 20:
            return RiskLevel.FAIBLE
        return RiskLevel.IGNORER
