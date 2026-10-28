"""Bilans AC : production mensuelle et flux compteur sur la même période.

L'autoconsommation inclut le solaire chargé dans la batterie, donc ses pertes
et la variation de stock. L'autonomie ne compte que le solaire livré à la charge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

MONTHS_FR = ("Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
             "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre")
TIMEZONE = "Europe/Zurich"


@dataclass(frozen=True)
class EnergyMetrics:
    production_kwh: float
    import_kwh: float
    export_kwh: float
    solar_used_kwh: float
    consumption_kwh: float
    self_consumption_pct: float
    self_sufficiency_pct: float
    solar_retained_kwh: float


def calculate_metrics(production_kwh: float, import_kwh: float,
                      export_kwh: float) -> EnergyMetrics:
    """Bilan sans batterie ; bilan apparent si une batterie est déjà en service."""
    p, i, e = map(float, (production_kwh, import_kwh, export_kwh))
    if not all(np.isfinite(v) and v >= 0 for v in (p, i, e)):
        raise ValueError("Les énergies doivent être finies et positives ou nulles.")
    if p <= 0:
        raise ValueError("La production PV de la période doit être supérieure à zéro.")
    if e > p + 1e-6:
        raise ValueError("L'injection dépasse la production PV. Vérifier les kWh AC, les mois et l'unité du compteur.")
    solar = max(0., p - e)
    consumption = solar + i
    return EnergyMetrics(p, i, e, solar, consumption, solar / p * 100,
                         solar / consumption * 100 if consumption else 0., solar)


def metrics_after_storage(base: EnergyMetrics, import_after: float,
                          export_after: float, delivered: float) -> EnergyMetrics:
    """La consommation du bâtiment reste celle du bilan initial, hors pertes."""
    i, e, d = map(float, (import_after, export_after, delivered))
    if not all(np.isfinite(v) and v >= -1e-6 for v in (i, e, d)):
        raise ValueError("Bilan batterie invalide.")
    if i > base.import_kwh + 1e-6 or e > base.export_kwh + 1e-6:
        raise ValueError("La batterie ne peut ni charger sur le réseau ni augmenter l'injection dans ce modèle.")
    if not np.isclose(base.import_kwh - i, d, atol=1e-6, rtol=1e-8):
        raise ValueError("La restitution batterie ne correspond pas à la réduction d'import réseau.")
    useful = base.solar_used_kwh + d
    retained = base.production_kwh - e
    return EnergyMetrics(base.production_kwh, max(0., i), max(0., e), useful,
                         base.consumption_kwh, retained / base.production_kwh * 100,
                         useful / base.consumption_kwh * 100 if base.consumption_kwh else 0., retained)


def monthly_coverage(timestamps, valid_mask, dt_hours: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex(timestamps)
    valid = np.asarray(valid_mask, dtype=bool)
    dt = float(dt_hours)
    if (idx.tz is None or idx.hasnans or len(idx) != len(valid) or valid.ndim != 1
            or not np.isfinite(dt) or dt <= 0 or not valid.any()):
        raise ValueError("Chronologie ou masque de validité incorrect.")
    local = idx.tz_convert(TIMEZONE)
    periods = local.tz_localize(None).to_period("M")
    rows = []
    for period in periods.unique():
        hours = float(((periods == period) & valid).sum()) * dt
        start = pd.Timestamp(period.start_time, tz=TIMEZONE)
        end = start + pd.offsets.MonthBegin(1)
        total = (end.tz_convert("UTC") - start.tz_convert("UTC")).total_seconds() / 3600
        rows.append({"period": str(period), "label": f"{MONTHS_FR[period.month-1]} {period.year}",
                     "hours": hours, "month_hours": total, "fraction": min(1., hours / total)})
    return pd.DataFrame(rows)


def _monthly_production(coverage: pd.DataFrame, values: Mapping,
                        allow_prorata: bool) -> dict[str, float]:
    result = {}
    for row in coverage.itertuples():
        if row.hours == 0:
            result[row.period] = 0.
            continue
        # Clés AAAA-MM pour ne jamais confondre deux années.
        raw = values.get(row.period)
        if raw is None:
            raise ValueError(f"Production manquante : {row.label} (saisir 0 si aucune production).")
        value = float(raw)
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"Production invalide : {row.label}.")
        if row.fraction < 1 - 1e-8 and not allow_prorata:
            raise ValueError(
                f"{row.label} est incomplet ({row.fraction:.1%} du mois mesuré). "
                "Fournir le mois complet ou activer le prorata temporel dans les réglages avancés ; "
                "ce prorata sera indiqué comme une estimation."
            )
        result[row.period] = value * row.fraction
    return result


def production_for_measurements(timestamps, valid_mask, dt_hours: float,
                                monthly_kwh: Mapping, *, allow_prorata=False) -> float:
    coverage = monthly_coverage(timestamps, valid_mask, dt_hours)
    return float(sum(_monthly_production(coverage, monthly_kwh, allow_prorata).values()))


def analyse(frame, meta, monthly_kwh: Mapping, *, capacity=15., power=5.,
            efficiency=.92, simulate_battery=True, allow_prorata=False):
    """Point d'entrée commun aux résultats, au détail mensuel et aux exports."""
    from simulation import simulate
    valid = frame.simulation_valid.to_numpy(bool)
    imp = frame.import_kWh.to_numpy(float)
    exp = frame.export_kWh.to_numpy(float)
    coverage = monthly_coverage(frame.timestamp, valid, meta.dt_hours)
    monthly_pv = _monthly_production(coverage, monthly_kwh, allow_prorata)
    periods = pd.DatetimeIndex(frame.timestamp).tz_convert(TIMEZONE).tz_localize(None).to_period("M").astype(str)
    # Contrôle de cohérence mois par mois, avant toute agrégation annuelle.
    bases = {}
    for row in coverage.itertuples():
        mask = (periods == row.period) & valid
        if not mask.any():
            continue
        p, i, e = monthly_pv[row.period], float(imp[mask].sum()), float(exp[mask].sum())
        if p == 0:
            if e > 1e-6:
                raise ValueError(f"{row.label} : injection réseau avec une production PV nulle.")
            bases[row.period] = EnergyMetrics(0., i, 0., 0., i, 0., 0., 0.)
        else:
            try:
                bases[row.period] = calculate_metrics(p, i, e)
            except ValueError as error:
                raise ValueError(f"{row.label} : {error}") from error
    total_pv = sum(monthly_pv.values())
    base = calculate_metrics(total_pv, imp[valid].sum(), exp[valid].sum())
    sim = None
    after = base
    if simulate_battery:
        sim = simulate(imp, exp, capacity, power, dt_hours=meta.dt_hours,
                       roundtrip_eff=efficiency, tariff_import=0., tariff_export=0.,
                       coverage_days=meta.coverage_days, soc_min_pct=0.,
                       timestamps=frame.timestamp, valid_mask=valid,
                       reset_before=frame.reset_before.to_numpy(bool),
                       dispatch_order="discharge_first")
        after = metrics_after_storage(base, sim.import_after_total,
                                      sim.export_after_total, sim.discharge_total_kWh)
    records = []
    for row in coverage.itertuples():
        if row.period not in bases:
            continue
        b = bases[row.period]
        mask = (periods == row.period) & valid
        i = float(np.nansum(sim.import_after[mask])) if sim else b.import_kwh
        e = float(np.nansum(sim.export_after[mask])) if sim else b.export_kwh
        d = float(np.nansum(sim.discharge_by_interval[mask])) if sim else 0.
        a = metrics_after_storage(b, i, e, d) if b.production_kwh > 0 else None
        # Un mois sans PV peut utiliser un stock constitué le mois précédent.
        useful = b.solar_used_kwh + d
        auto_after = a.self_consumption_pct if a else np.nan
        autonomy_after = useful / b.consumption_kwh * 100 if b.consumption_kwh else 0.
        records.append({"Mois": row.period, "Couverture (%)": row.fraction * 100,
                        "Production PV (kWh)": b.production_kwh,
                        "Consommation bâtiment (kWh)": b.consumption_kwh,
                        "Import avant (kWh)": b.import_kwh, "Export avant (kWh)": b.export_kwh,
                        "Import après (kWh)": i, "Export après (kWh)": e,
                        "Charge batterie (kWh)": float(np.nansum(sim.charge_by_interval[mask])) if sim else 0.,
                        "Restitution batterie (kWh)": d,
                        "Autoconsommation avant (%)": b.self_consumption_pct if b.production_kwh else np.nan,
                        "Autoconsommation après (%)": auto_after if sim else (b.self_consumption_pct if b.production_kwh else np.nan),
                        "Autonomie avant (%)": b.self_sufficiency_pct,
                        "Autonomie après (%)": autonomy_after})
    return base, after, sim, pd.DataFrame(records), coverage
