"""Exemple synthétique reproductible ; aucune donnée client."""
import numpy as np
import pandas as pd

from loaders import _finalize, prepare_simulation_data


def make_demo():
    times = pd.date_range("2025-01-01", "2026-01-01", freq="15min", inclusive="left", tz="Europe/Zurich")
    hour = times.hour.to_numpy() + times.minute.to_numpy() / 60
    season = .7 + .3 * np.sin((times.dayofyear.to_numpy() - 80) * 2 * np.pi / 365)
    sun = np.maximum(0., np.sin((hour - 6) * np.pi / 12))
    sun[(hour <= 6) | (hour >= 18)] = 0.
    solar = sun * season
    solar *= 12000. / solar.sum()
    exported = solar * 7 / 12
    demand = np.where(sun == 0, .65 + .7 * ((hour >= 18) & (hour < 23)), 0.)
    imported = demand * (5000. / demand.sum())
    frame = pd.DataFrame({"timestamp": times.tz_convert("UTC"),
                          "import_kWh": imported, "export_kWh": exported})
    months = times.tz_localize(None).to_period("M").astype(str)
    pv = {key: float(solar[months == key].sum()) for key in sorted(set(months))}
    raw, meta = _finalize(frame, "Exemple synthétique", "Exemple SOLEOL 2025", timestamp_position="start")
    raw, meta = prepare_simulation_data(raw, meta, policy="block")
    return raw, meta, pv
