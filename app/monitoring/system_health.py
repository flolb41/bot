"""Surveillance santé du Raspberry Pi (température + alimentation + mémoire).

Lecture via `vcgencmd` (disponible sur Raspberry Pi OS) ou fallback sur
`/sys/class/thermal/thermal_zone0/temp` pour la température. La mémoire est
lue via `/proc/meminfo`.

But : détecter un Pi qui chauffe, un voltage instable ou une mémoire saturée
AVANT qu'il ne gèle, et activer le killswitch pour arrêter proprement le bot
(plutôt que de laisser le processus planter en plein scan / exécution on-chain).

Non bloquant : si `vcgencmd` n'est pas disponible (non-Pi, environnement
dev), le module retourne des valeurs "inconnues" et ne bloque rien.

Seuils (configurables dans `config/config.yaml`, section `system_health`) :
- `max_temperature_celsius` : arrêt si temp > seuil (défaut 80°C)
- l'indicateur de sous-tension courant de `vcgencmd get_throttled`
- `max_memory_percent` : arrêt si RAM > seuil (défaut 90%)
- `check_interval_seconds` : période entre deux vérifications (défaut 60s)
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Seuils par défaut (valeurs sûres pour un Pi3) — overridables via config.
DEFAULT_MAX_TEMP_C = 80.0
DEFAULT_MAX_MEMORY_PERCENT = 90.0
DEFAULT_CHECK_INTERVAL_SECONDS = 60.0


def _run_cmd(cmd: list[str]) -> Optional[str]:
    """Exécute une commande et retourne sa sortie stdout, ou None en cas d'échec."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5.0,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def read_temperature_celsius() -> Optional[float]:
    """Lit la température CPU du Pi en °C.

    Ordre de préférence :
    1. `vcgencmd measure_temp` (exact, disponible sur Pi OS)
    2. `/sys/class/thermal/thermal_zone0/temp` (milli-degrés, Linux générique)

    Retourne None si indisponible."""
    out = _run_cmd(["vcgencmd", "measure_temp"])
    if out:
        # Format attendu : "temp=45.0'C"
        try:
            value = out.split("=")[1].split("'")[0]
            return float(value)
        except (IndexError, ValueError):
            pass
    # Fallback sysfs
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            raw = f.read().strip()
            return float(raw) / 1000.0
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def read_undervoltage_active() -> Optional[bool]:
    """Lit l'indicateur de sous-tension courant via `vcgencmd get_throttled`.

    Le bit 0 signale une sous-tension présente maintenant ; les bits historiques
    ne déclenchent pas le killswitch une fois le problème résolu. `measure_volts`
    mesure le rail du cœur du SoC, pas l'alimentation 5V du Pi.
    """
    out = _run_cmd(["vcgencmd", "get_throttled"])
    if not out:
        return None
    try:
        flags = int(out.split("=", 1)[1], 16)
    except (IndexError, ValueError):
        return None
    return bool(flags & 0x1)


def read_memory_percent() -> Optional[float]:
    """Lit le pourcentage de mémoire RAM utilisée via /proc/meminfo.

    Retourne None si indisponible."""
    try:
        with open("/proc/meminfo", "r") as f:
            lines = f.readlines()
        mem_total = None
        mem_available = None
        for line in lines:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])  # en kB
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])  # en kB
        if mem_total and mem_total > 0 and mem_available is not None:
            used = mem_total - mem_available
            return (used / mem_total) * 100.0
    except (FileNotFoundError, ValueError, OSError, IndexError):
        pass
    return None


class SystemHealthMonitor:
    """Vérifie périodiquement température + voltage + mémoire du Pi.

    Si un seuil est franchi, active le killswitch (base `system_state`) et
    journalise une alerte. Le scheduler respecte le killswitch et arrête tous
    les scans/exécutions.

    Conçu pour être appelé depuis une tâche APScheduler (interval) ou manuellement.
    """

    def __init__(
        self,
        db,
        *,
        max_temp_celsius: float = DEFAULT_MAX_TEMP_C,
        max_memory_percent: float = DEFAULT_MAX_MEMORY_PERCENT,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        telegram=None,
    ):
        self._db = db
        self._max_temp = max_temp_celsius
        self._max_memory = max_memory_percent
        self._interval = check_interval_seconds
        self._telegram = telegram
        self._last_check_at = 0.0

    def should_check(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self._last_check_at) >= self._interval

    def check(self, now: float | None = None) -> dict:
        """Effectue une vérification température/voltage/mémoire.

        Retourne un dict avec les valeurs lues et les alertes déclenchées.
        Active le killswitch si un seuil critique est dépassé."""
        now = now if now is not None else time.time()
        self._last_check_at = now

        temp = read_temperature_celsius()
        undervoltage_active = read_undervoltage_active()
        memory = read_memory_percent()

        alerts: list[str] = []
        stop = False

        if temp is not None and temp > self._max_temp:
            alerts.append(f"température CPU {temp:.1f}°C > seuil {self._max_temp:.1f}°C")
            stop = True
        if undervoltage_active:
            alerts.append("sous-tension détectée par vcgencmd get_throttled")
            stop = True
        if memory is not None and memory > self._max_memory:
            alerts.append(f"mémoire {memory:.1f}% > seuil {self._max_memory:.1f}% (risque OOM)")
            stop = True

        result: dict = {
            "checked_at": now,
            "temperature_celsius": temp,
            "undervoltage_active": undervoltage_active,
            "memory_percent": memory,
            "alerts": alerts,
            "killswitch_activated": stop,
        }

        if alerts:
            msg = "🌡️ <b>Pi santé dégradée</b> — " + "; ".join(alerts)
            if stop:
                msg += "\nKillswitch activé : scans et exécutions arrêtés jusqu'à refroidissement/réparation."
                self._db.set_state("killswitch", "1")
                logger.error("Killswitch activé par system_health : %s", "; ".join(alerts))
                result["message"] = msg
            else:
                result["message"] = msg
            logger.warning("System health alert : %s", "; ".join(alerts))
            if self._telegram is not None:
                try:
                    self._telegram.send(msg, level="warning")
                except Exception:  # noqa: BLE001
                    logger.exception("Échec envoi alerte système Telegram.")

        return result
