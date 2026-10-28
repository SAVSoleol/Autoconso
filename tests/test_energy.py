import numpy as np
import pandas as pd
import pytest

from demo import make_demo
from energy_metrics import (analyse, calculate_metrics, metrics_after_storage,
                            monthly_coverage, production_for_measurements)
from loaders import prepare_simulation_data
from simulation import simulate


@pytest.fixture(scope="module")
def demo():
    return make_demo()


def test_reference_balance_and_storage_losses():
    before = calculate_metrics(12000, 5000, 7000)
    after = metrics_after_storage(before, 2930, 4750, 2070)
    assert before.solar_used_kwh == 5000
    assert before.consumption_kwh == 10000
    assert before.self_consumption_pct == pytest.approx(41.6666666667)
    assert before.self_sufficiency_pct == 50
    assert after.consumption_kwh == 10000
    assert after.solar_used_kwh == 7070
    assert after.solar_retained_kwh == 7250
    assert after.self_consumption_pct == pytest.approx(60.4166666667)
    assert after.self_sufficiency_pct == pytest.approx(70.7)


@pytest.mark.parametrize("values", [(100, 0, 101), (0, 10, 0), (100, -1, 0), (np.nan, 10, 0)])
def test_inconsistent_balance_rejected(values):
    with pytest.raises(ValueError):
        calculate_metrics(*values)


def test_storage_delivered_must_match_import_reduction():
    with pytest.raises(ValueError, match="restitution"):
        metrics_after_storage(calculate_metrics(12000, 5000, 7000), 2930, 4750, 2250)


def test_dispatch_no_energy_before_first_charge():
    s = simulate([2., 0., 2.], [0., 2., 0.], capacity_kWh=1., power_kW=1.,
                 dt_hours=1., roundtrip_eff=1., tariff_import=0., tariff_export=0., soc_min_pct=0.)
    np.testing.assert_allclose(s.import_after, [2., 0., 1.])
    np.testing.assert_allclose(s.export_after, [0., 1., 0.])
    assert s.charge_total_kWh == s.discharge_total_kWh == 1


def test_annual_energy_conservation_and_monthly_totals(demo):
    f, m, pv = demo
    b, a, s, monthly, coverage = analyse(f, m, pv)
    assert b.production_kwh == pytest.approx(12000)
    assert b.consumption_kwh == pytest.approx(10000)
    assert a.consumption_kwh == b.consumption_kwh
    assert a.import_kwh + a.solar_used_kwh == pytest.approx(b.consumption_kwh)
    assert s.charge_total_kWh - s.discharge_total_kWh == pytest.approx(s.conversion_losses_kWh + s.final_stock_kWh + s.untransferred_stock_kWh)
    assert monthly["Import après (kWh)"].sum() == pytest.approx(a.import_kwh)
    assert monthly["Restitution batterie (kWh)"].sum() == pytest.approx(s.discharge_total_kWh)
    assert ((monthly["Autonomie après (%)"] >= -1e-8) & (monthly["Autonomie après (%)"] <= 100 + 1e-8)).all()
    assert len(monthly) == 12
    assert (coverage.fraction == 1.).all()
    assert np.nanmax(s.charge_by_interval + s.discharge_by_interval) <= 5 * .25 + 1e-8
    assert np.nanmax(s.soc) <= 15 + 1e-8


def test_swiss_dst_month_durations(demo):
    f, m, _ = demo
    coverage = monthly_coverage(f.timestamp, f.simulation_valid, m.dt_hours).set_index("period")
    assert coverage.loc["2025-03", "month_hours"] == 743
    assert coverage.loc["2025-10", "month_hours"] == 745
    assert coverage.hours.sum() == 8760


def test_incomplete_month_requires_explicit_estimate():
    ts = pd.date_range("2025-01-01", periods=48, freq="h", tz="Europe/Zurich")
    with pytest.raises(ValueError, match="incomplet"):
        production_for_measurements(ts, np.ones(48, bool), 1, {"2025-01": 744})
    assert production_for_measurements(ts, np.ones(48, bool), 1, {"2025-01": 744}, allow_prorata=True) == 48


def test_partial_and_missing_production_never_default_to_zero(demo):
    f, m, pv = demo
    values = dict(pv)
    values["2025-04"] = None
    with pytest.raises(ValueError, match="Avril"):
        analyse(f, m, values)


def test_monthly_inconsistency_cannot_hide_in_annual_balance(demo):
    f, m, pv = demo
    values = dict(pv)
    values["2025-01"] = 1
    values["2025-06"] += 10000
    with pytest.raises(ValueError, match="Janvier"):
        analyse(f, m, values)


def test_measured_gaps_reset_battery_and_production_is_explicit(demo):
    f, m, pv = demo
    raw = f.copy()
    raw.loc[1000:1009, ["import_kWh", "export_kWh"]] = np.nan
    raw.loc[1000:1009, "valid"] = False
    g, gm = prepare_simulation_data(raw, m, policy="segments")
    with pytest.raises(ValueError, match="incomplet"):
        analyse(g, gm, pv)
    _, _, s, _, _ = analyse(g, gm, pv, allow_prorata=True)
    assert np.isnan(s.import_after[1000:1010]).all()
    assert s.soc_start[1010] == 0
    assert g.loc[1010, "reset_before"]
    assert s.charge_total_kWh - s.discharge_total_kWh == pytest.approx(s.conversion_losses_kWh + s.final_stock_kWh + s.untransferred_stock_kWh)


def test_two_years_keep_different_month_values():
    ts = pd.date_range("2024-12-01", "2025-02-01", freq="h", inclusive="left", tz="Europe/Zurich")
    assert production_for_measurements(ts, np.ones(len(ts), bool), 1, {"2024-12": 100., "2025-01": 300.}) == 400


def test_no_battery_results_are_identical(demo):
    f, m, pv = demo
    b, a, s, monthly, _ = analyse(f, m, pv, simulate_battery=False)
    assert b == a
    assert s is None
    np.testing.assert_allclose(monthly["Import avant (kWh)"], monthly["Import après (kWh)"])


def test_zero_solar_month_is_accepted_without_export(demo):
    f, m, pv = demo
    f = f.copy()
    pv = dict(pv)
    january = f.timestamp.dt.tz_convert("Europe/Zurich").dt.month == 1
    f.loc[january, "export_kWh"] = 0.
    pv["2025-01"] = 0.
    _, _, _, monthly, _ = analyse(f, m, pv)
    assert np.isnan(monthly.iloc[0]["Autoconsommation après (%)"])
    assert monthly.iloc[0]["Production PV (kWh)"] == 0
