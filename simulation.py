"""Moteur unique : autoconsommation immédiate, sans vieillissement ni auxiliaires.

Les flux sont des kWh AC par intervalle. Le SOC interne est le stock DC au-dessus
du SOC minimum. Charge et décharge partagent le temps du même convertisseur.
L'énergie initiale utilisable est nulle. Aucun service Peak Shaving n'est simulé.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import numpy as np
import pandas as pd

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False
    def njit(*args, **kwargs):
        return lambda function: function


def _finite(value, label, minimum=None, maximum=None):
    value = float(value)
    if not np.isfinite(value) or (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum):
        raise ValueError(f"{label} invalide : {value}.")
    return value


def _periods(periods):
    result = tuple(tuple(float(v) for v in p) for p in periods)
    for p in result:
        if len(p) != 2 or not all(np.isfinite(v) and 0 <= v <= 24 and np.isclose(v * 60, round(v * 60)) for v in p) or p[0] == p[1]:
            raise ValueError("Plage HT invalide : heures entre 0 et 24 à la minute entière, début différent de fin.")
    return result


def _weekdays(value):
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or any(
        isinstance(day, (bool, np.bool_)) or not isinstance(day, (int, np.integer)) or not 0 <= day <= 6
        for day in value
    ):
        raise ValueError("Jours HT invalides : liste d'entiers de 0 (lundi) à 6 (dimanche).")
    return tuple(sorted(set(value)))


def _high_mask(local, periods, weekend_low, high_tariff_weekdays=None):
    hour = local.hour + local.minute / 60 + local.second / 3600
    mask = np.zeros(len(local), dtype=bool)
    for start, end in periods:
        mask |= ((hour >= start) & (hour < end)) if start < end else ((hour >= start) | (hour < end))
    if weekend_low:
        mask &= local.weekday < 5
    if high_tariff_weekdays is not None:
        mask &= np.isin(local.weekday, high_tariff_weekdays)
    return mask


def _tariff_vectors(n, timestamps=None, tariff_import=.32, tariff_import_ht=None,
                    tariff_import_bt=None, high_tariff_periods=((7., 22.),), weekend_low_tariff=False,
                    *, dt_hours=.25, tariff_export=.06, tariff_schedule=None):
    """Prix pondérés et fraction HT. Les plages s'appliquent en heure suisse.

Intégration à la minute (données et limites tarifaires alignées à la minute).
Un calendrier optionnel couvre chaque date avec start inclus / end exclu.
"""
    ht = _finite(tariff_import if tariff_import_ht is None else tariff_import_ht, "Tarif HT")
    bt = _finite(tariff_import if tariff_import_bt is None else tariff_import_bt, "Tarif BT")
    sell = _finite(tariff_export, "Tarif de reprise")
    periods = _periods(high_tariff_periods)
    if timestamps is None:
        if tariff_schedule or ht != bt:
            raise ValueError("Horodatages avec fuseau requis pour les tarifs variables.")
        return np.full(n, ht), np.ones(n), np.full(n, sell), np.full(n, ht)
    idx = pd.DatetimeIndex(timestamps)
    if len(idx) != n or idx.tz is None or idx.hasnans or idx.has_duplicates or not idx.is_monotonic_increasing:
        raise ValueError("Chronologie attendue : instants uniques, croissants, avec fuseau horaire.")
    if (idx.second != 0).any() or (idx.microsecond != 0).any() or (idx.nanosecond != 0).any():
        raise ValueError("Horodatages attendus à la minute entière.")
    minutes = int(round(dt_hours * 60))
    if minutes < 1 or not np.isclose(minutes, dt_hours * 60):
        raise ValueError("Le pas doit être un nombre entier de minutes.")
    schedule = []
    for entry in tariff_schedule or []:
        start, end = pd.Timestamp(entry["start"]), pd.Timestamp(entry["end"])
        start = start.tz_localize("Europe/Zurich") if start.tzinfo is None else start.tz_convert("Europe/Zurich")
        end = end.tz_localize("Europe/Zurich") if end.tzinfo is None else end.tz_convert("Europe/Zurich")
        if end <= start:
            raise ValueError("Fin de période tarifaire antérieure ou égale au début.")
        if any(t.second or t.microsecond or t.nanosecond for t in (start, end)):
            raise ValueError("Les limites du calendrier tarifaire doivent être alignées à la minute.")
        schedule.append((start, end, _finite(entry["ht"], "HT calendrier"),
                         _finite(entry["bt"], "BT calendrier"), _finite(entry["export"], "Reprise calendrier"),
                         _periods(entry.get("periods", periods)), bool(entry.get("weekend_low", weekend_low_tariff)),
                         _weekdays(entry.get("high_tariff_weekdays"))))
    buy = np.zeros(n)
    buy_ht = np.zeros(n)
    high_fraction = np.zeros(n)
    export = np.zeros(n)
    for minute in range(minutes):
        local = (idx + pd.Timedelta(minutes=minute, seconds=30)).tz_convert("Europe/Zurich")
        if not schedule:
            mask = _high_mask(local, periods, weekend_low_tariff)
            buy += np.where(mask, ht, bt)
            buy_ht += mask * ht
            high_fraction += mask
            export += sell
        else:
            covered = np.zeros(n, dtype=int)
            for start, end, h, b, e, windows, weekend, weekdays in schedule:
                selected = (local >= start) & (local < end)
                mask = _high_mask(local, windows, weekend, weekdays)
                covered += selected
                buy += np.where(selected, np.where(mask, h, b), 0.)
                buy_ht += (selected & mask) * h
                high_fraction += selected & mask
                export += selected * e
            if (covered != 1).any():
                raise ValueError("Le calendrier tarifaire comporte un trou ou un chevauchement sur la période de mesures.")
    return buy / minutes, high_fraction / minutes, export / minutes, buy_ht / minutes


@njit(cache=True)
def _dispatch(imp, exp, valid, resets, capacity, power_per_step, eta, charge_first):
    n = len(imp)
    after_i = np.full(n, np.nan)
    after_e = np.full(n, np.nan)
    soc = np.full(n, np.nan)
    soc_start = np.full(n, np.nan)
    stock = 0.0
    discarded = 0.0
    for i in range(n):
        if not valid[i]:
            continue
        if resets[i]:
            discarded += stock
            stock = 0.0
        soc_start[i] = stock
        if charge_first:
            charge = max(0., min(exp[i], power_per_step, (capacity - stock) / eta))
            stock += charge * eta
            discharge = max(0., min(imp[i], power_per_step - charge, stock * eta))
            stock -= discharge / eta
        else:
            # Default: no use of surplus that has not yet arrived in this interval.
            discharge = max(0., min(imp[i], power_per_step, stock * eta))
            stock -= discharge / eta
            charge = max(0., min(exp[i], power_per_step - discharge, (capacity - stock) / eta))
            stock += charge * eta
        after_i[i] = imp[i] - discharge
        after_e[i] = exp[i] - charge
        soc[i] = stock
    return after_i, after_e, soc, soc_start, discarded, stock


@dataclass
class SimResult:
    capacity_kWh: float
    power_kW: float
    soc: np.ndarray
    soc_start: np.ndarray
    import_after: np.ndarray
    export_after: np.ndarray
    import_before: float
    export_before: float
    import_avoided: float
    import_avoided_ht: float
    import_avoided_bt: float
    export_stored: float
    gain_chf: float
    gain_ht_chf: float
    gain_bt_chf: float
    export_value_lost_chf: float
    cycles_period: float
    cycles_per_year: float
    cycles_nominal_period: float
    usable_capacity_kWh: float
    soc_min_pct: float
    charge_total_kWh: float
    discharge_total_kWh: float
    surplus_captured: float
    import_reduction: float
    conversion_losses_kWh: float
    untransferred_stock_kWh: float
    final_stock_kWh: float
    peak_before_kW: float
    peak_after_kW: float
    dt_hours: float
    roundtrip_eff: float
    coverage_days: float
    annual_factor: float | None
    gain_annual_chf: float | None
    dispatch_order: str
    valid: np.ndarray
    reset_before: np.ndarray
    gain_by_interval: np.ndarray
    charge_by_interval: np.ndarray
    discharge_by_interval: np.ndarray

    @property
    def import_after_total(self):
        return float(np.nansum(self.import_after))

    @property
    def export_after_total(self):
        return float(np.nansum(self.export_after))

    @property
    def soc_pct(self):
        return self.soc_min_pct + self.soc / self.capacity_kWh * 100


def _prepare(import_kWh, export_kWh, dt_hours, roundtrip_eff, tariff_import, tariff_export,
             coverage_days=None, soc_min_pct=5., timestamps=None, tariff_import_ht=None,
             tariff_import_bt=None, high_tariff_periods=((7., 22.),), weekend_low_tariff=False,
             *, valid_mask=None, reset_before=None, annualize=False, dispatch_order="discharge_first",
             tariff_schedule=None):
    imp, exp = np.asarray(import_kWh, dtype=float), np.asarray(export_kWh, dtype=float)
    if imp.ndim != 1 or exp.ndim != 1 or imp.shape != exp.shape or len(imp) == 0:
        raise ValueError("Import et export doivent être deux vecteurs non vides de même longueur.")
    dt = _finite(dt_hours, "Pas de temps", 1 / 60, 24)
    eff = _finite(roundtrip_eff, "Rendement aller-retour", 1e-9, 1.)
    soc = _finite(soc_min_pct, "SOC minimum", 0., 99.999)
    valid = np.ones(len(imp), dtype=bool) if valid_mask is None else np.asarray(valid_mask, dtype=bool)
    if valid.shape != imp.shape or not valid.any():
        raise ValueError("Masque de validité incorrect ou aucune mesure utilisable.")
    if not np.isfinite(imp[valid]).all() or not np.isfinite(exp[valid]).all() or (
            imp[valid] < 0).any() or (exp[valid] < 0).any():
        raise ValueError("Les intervalles utilisables exigent des énergies finies et positives ou nulles.")
    auto_reset = valid & ~np.r_[False, valid[:-1]]
    resets = auto_reset.copy() if reset_before is None else np.asarray(reset_before, dtype=bool)
    if resets.shape != valid.shape or not np.all(resets[auto_reset]) or (resets & ~valid).any():
        raise ValueError("Chaque segment mesuré doit redémarrer au SOC minimum.")
    if timestamps is not None:
        idx = pd.DatetimeIndex(timestamps)
        if len(idx) != len(imp) or (len(idx) > 1 and not np.allclose(np.diff(idx.asi8) / 3.6e12, dt)):
            raise ValueError("La chronologie doit comporter chaque intervalle, y compris les trous masqués.")
    days = len(imp) * dt / 24
    if coverage_days is not None and not np.isclose(float(coverage_days), days, rtol=0, atol=1e-5):
        raise ValueError("La couverture ne correspond pas au nombre d'intervalles et au pas de temps.")
    if annualize and (days < 330 or valid.mean() < .98):
        raise ValueError("Annualisation insuffisamment représentative : au moins 330 jours et 98 % d'intervalles requis.")
    if dispatch_order not in {"discharge_first", "charge_first"}:
        raise ValueError("Ordre des flux inconnu.")
    buy, high, sell, buy_ht = _tariff_vectors(len(imp), timestamps, tariff_import, tariff_import_ht, tariff_import_bt,
                                    high_tariff_periods, weekend_low_tariff, dt_hours=dt,
                                    tariff_export=tariff_export, tariff_schedule=tariff_schedule)
    return dict(imp=np.ascontiguousarray(imp), exp=np.ascontiguousarray(exp), valid=valid, resets=resets,
                dt=dt, eff=eff, eta=np.sqrt(eff), soc_min=soc, days=days, buy=buy, high=high, sell=sell,
                buy_ht=buy_ht, buy_bt=buy-buy_ht,
                annual_factor=365 / days if annualize else None, order=dispatch_order)


def _run(context, capacity_kWh, power_kW):
    c = context
    cap = _finite(capacity_kWh, "Capacité", 1e-9)
    power = _finite(power_kW, "Puissance", 1e-9)
    usable = cap * (1 - c["soc_min"] / 100)
    ia, ea, soc, soc_start, discarded, final = _dispatch(
        c["imp"], c["exp"], c["valid"], c["resets"], usable, power * c["dt"], c["eta"], c["order"] == "charge_first")
    discharge, charge = c["imp"] - ia, c["exp"] - ea
    charge_total, discharge_total = float(np.nansum(charge)), float(np.nansum(discharge))
    import_before = float(c["imp"][c["valid"]].sum())
    export_before = float(c["exp"][c["valid"]].sum())
    # Price-weighted HT/BT shares, including intervals crossing a tariff boundary.
    gain_import = discharge * c["buy"]
    gain_by_interval = gain_import - charge * c["sell"]
    gain = float(np.nansum(gain_by_interval))
    ht_value = float(np.nansum(discharge * c["buy_ht"]))
    bt_value = float(np.nansum(discharge * c["buy_bt"]))
    cycles = discharge_total / c["eta"] / usable
    losses = charge_total * (1 - c["eta"]) + discharge_total * (1 / c["eta"] - 1)
    af = c["annual_factor"]
    return SimResult(cap, power, soc, soc_start, ia, ea, import_before, export_before, discharge_total,
        float(np.nansum(discharge * c["high"])), float(np.nansum(discharge * (1 - c["high"]))),
        charge_total, gain, ht_value, bt_value, float(np.nansum(charge * c["sell"])), cycles,
        cycles * af if af is not None else np.nan, discharge_total / c["eta"] / cap,
        usable, c["soc_min"], charge_total, discharge_total,
        charge_total / export_before if export_before else 0.,
        discharge_total / import_before if import_before else 0., losses, float(discarded), float(final),
        float(np.max(c["imp"][c["valid"]]) / c["dt"]), float(np.nanmax(ia) / c["dt"]),
        c["dt"], c["eff"], c["days"], af, gain * af if af is not None else None, c["order"],
        c["valid"], c["resets"], gain_by_interval, charge, discharge)


def simulate(import_kWh, export_kWh, capacity_kWh, power_kW, dt_hours, roundtrip_eff,
             tariff_import, tariff_export, coverage_days=None, soc_min_pct=5., timestamps=None,
             tariff_import_ht=None, tariff_import_bt=None, high_tariff_periods=((7., 22.),),
             weekend_low_tariff=False, *, valid_mask=None, reset_before=None, annualize=False,
             dispatch_order="discharge_first", tariff_schedule=None):
    return _run(_prepare(import_kWh, export_kWh, dt_hours, roundtrip_eff, tariff_import, tariff_export,
        coverage_days, soc_min_pct, timestamps, tariff_import_ht, tariff_import_bt, high_tariff_periods,
        weekend_low_tariff, valid_mask=valid_mask, reset_before=reset_before, annualize=annualize,
        dispatch_order=dispatch_order, tariff_schedule=tariff_schedule), capacity_kWh, power_kW)


def result_row(sim):
    return {"Cap_kWh": sim.capacity_kWh, "Power_kW": sim.power_kW, "Gain_CHF": sim.gain_chf,
        "Gain_annual_CHF": sim.gain_annual_chf, "Gain_import_HT_CHF": sim.gain_ht_chf,
        "Gain_import_BT_CHF": sim.gain_bt_chf, "Export_value_lost_CHF": sim.export_value_lost_chf,
        "Import_avoided_kWh": sim.import_avoided, "Export_stored_kWh": sim.export_stored,
        "Cycles_period": sim.cycles_period, "Cycles_per_year": sim.cycles_per_year,
        "Cycles_nominal_period": sim.cycles_nominal_period, "Usable_capacity_kWh": sim.usable_capacity_kWh,
        "SOC_min_pct": sim.soc_min_pct, "Losses_kWh": sim.conversion_losses_kWh,
        "Final_stock_kWh": sim.final_stock_kWh, "Untransferred_stock_kWh": sim.untransferred_stock_kWh,
        "Peak_before_kW": sim.peak_before_kW, "Peak_after_kW": sim.peak_after_kW}


def grid_search(import_kWh, export_kWh, caps: Iterable, powers: Iterable, dt_hours, roundtrip_eff,
                tariff_import, tariff_export, coverage_days=None, soc_min_pct=5., timestamps=None,
                tariff_import_ht=None, tariff_import_bt=None, high_tariff_periods=((7., 22.),),
                weekend_low_tariff=False, max_c_rate=None, *, valid_mask=None, reset_before=None,
                annualize=False, dispatch_order="discharge_first", tariff_schedule=None):
    caps = sorted(set(_finite(v, "Capacité", 1e-9) for v in caps))
    powers = sorted(set(_finite(v, "Puissance", 1e-9) for v in powers))
    if not caps or not powers:
        raise ValueError("Plage de capacité ou puissance vide.")
    rate = None if max_c_rate is None else _finite(max_c_rate, "C-rate", 1e-9)
    pairs = []
    for cap in caps:
        limit = min(powers[-1], cap * rate) if rate else powers[-1]
        if limit < powers[0] - 1e-9:
            continue
        allowed = [p for p in powers if p <= limit + 1e-9]
        # Exact C-rate endpoint avoids artificial alternating plateaus in the gain curve.
        allowed = sorted(set(allowed + [limit]))
        pairs.extend((cap, p) for p in allowed)
    if not pairs:
        raise ValueError("Aucune combinaison admissible avec ces limites de puissance et de C-rate.")
    if len(pairs) > 30_000:
        raise ValueError("Grille trop grande (maximum 30 000 combinaisons). Réduire les limites techniques.")
    c = _prepare(import_kWh, export_kWh, dt_hours, roundtrip_eff, tariff_import, tariff_export, coverage_days,
        soc_min_pct, timestamps, tariff_import_ht, tariff_import_bt, high_tariff_periods, weekend_low_tariff,
        valid_mask=valid_mask, reset_before=reset_before, annualize=annualize, dispatch_order=dispatch_order,
        tariff_schedule=tariff_schedule)
    return pd.DataFrame([result_row(_run(c, cap, p)) for cap, p in pairs])


def simple_payback(capex_chf, annual_gain_chf):
    """Retour simple sans actualisation ; absent si coût ou gain annuel inexploitable."""
    if capex_chf is None or annual_gain_chf is None:
        return None
    cost = _finite(capex_chf, "Coût installé", 0.)
    gain = _finite(annual_gain_chf, "Économie annuelle")
    return cost / gain if cost > 0 and gain > 0 else None
