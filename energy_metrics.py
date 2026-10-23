"""Bilan autoconsommation et autonomie à partir du compteur réseau."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


MONTHS_FR = ("Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
             "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre")


@dataclass(frozen=True)
class EnergyMetrics:
    production_kwh: float
    import_kwh: float
    export_kwh: float
    solar_used_kwh: float
    consumption_kwh: float
    self_consumption_pct: float
    self_sufficiency_pct: float


def production_for_measurements(timestamps, valid_mask, dt_hours: float,
                                monthly_kwh: dict[int, float]) -> float:
    """Répartit les productions mensuelles sur la période réellement mesurée.

    Les valeurs saisies sont celles d'une année-type. Un mois partiellement
    mesuré est pondéré à la durée disponible, y compris les changements d'heure.
    """
    idx = pd.DatetimeIndex(timestamps)
    valid = np.asarray(valid_mask, dtype=bool)
    dt = float(dt_hours)
    values = {int(k): float(v) for k, v in monthly_kwh.items()}
    if idx.tz is None or len(idx) != len(valid) or dt <= 0:
        raise ValueError("Chronologie de mesure invalide.")
    if set(values) != set(range(1, 13)) or any(not np.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("Saisir une production positive ou nulle pour chacun des 12 mois.")
    local = idx.tz_convert("Europe/Zurich")
    periods = pd.PeriodIndex(local.tz_localize(None), freq="M")
    result = 0.0
    for period in periods.unique():
        measured_hours = float(((periods == period) & valid).sum()) * dt
        start = pd.Timestamp(period.start_time, tz="Europe/Zurich")
        end = start + pd.offsets.MonthBegin(1)
        month_hours = (end.tz_convert("UTC") - start.tz_convert("UTC")).total_seconds() / 3600
        result += values[period.month] * min(1.0, measured_hours / month_hours)
    return float(result)


def calculate_metrics(production_kwh: float, import_kwh: float, export_kwh: float) -> EnergyMetrics:
    production = float(production_kwh)
    imported = float(import_kwh)
    exported = float(export_kwh)
    if not all(np.isfinite(v) and v >= 0 for v in (production, imported, exported)):
        raise ValueError("Les énergies doivent être positives ou nulles.")
    if production <= 0:
        raise ValueError("La production PV sur la période doit être supérieure à zéro.")
    if exported > production + 1e-6:
        raise ValueError(
            "L'injection réseau est supérieure à la production PV calculée. "
            "Vérifier les productions mensuelles et la période de mesures."
        )
    solar_used = max(0.0, production - exported)
    consumption = solar_used + imported
    return EnergyMetrics(
        production_kwh=production,
        import_kwh=imported,
        export_kwh=exported,
        solar_used_kwh=solar_used,
        consumption_kwh=consumption,
        self_consumption_pct=100 * solar_used / production,
        self_sufficiency_pct=100 * solar_used / consumption if consumption else 0.0,
    )
