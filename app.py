"""Calculateur simple d'autoconsommation et d'autonomie PV.

Lancement : python -m streamlit run app.py
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile
import tempfile

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders import UnsupportedFormatError, load_meter_files, prepare_simulation_data
from simulation import simulate
from energy_metrics import MONTHS_FR, calculate_metrics, production_for_measurements


CSS = """
<style>
.block-container {max-width: 1300px; padding-top: 2rem; padding-bottom: 3rem;}
.metric-card {background:#101827;border:1px solid rgba(148,163,184,.25);border-radius:16px;padding:18px 20px;min-height:132px;}
.metric-label {color:#cbd5e1;font-size:.88rem;font-weight:700;margin-bottom:12px;}
.metric-value {font-size:2rem;font-weight:800;line-height:1;color:#f8fafc;}.orange{color:#fb923c}.green{color:#4ade80}.blue{color:#60a5fa}.purple{color:#c084fc}
.metric-sub {color:#94a3b8;font-size:.78rem;margin-top:10px;}
.metric-grid {display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:12px;margin:14px 0 22px;}
.notice {border-left:4px solid #fb923c;background:rgba(251,146,60,.10);padding:12px 15px;border-radius:0 10px 10px 0;color:#e2e8f0;margin:8px 0 18px;}
@media(max-width:900px){.metric-grid{grid-template-columns:repeat(2,minmax(150px,1fr));}}
</style>
"""


@st.cache_data(show_spinner=False, max_entries=8)
def _load_file(name: str, content: bytes, unit: str):
    """Charge un fichier sans utiliser le nom fourni comme chemin."""
    if len(content) > 30 * 1024**2:
        raise UnsupportedFormatError("Le fichier dépasse 30 Mo.")
    suffix = Path(name).suffix.lower()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"mesures{suffix}"
        path.write_bytes(content)
        if suffix == ".xlsx":
            with ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 250 * 1024**2:
                    raise UnsupportedFormatError("Le classeur est trop volumineux après décompression.")
        raw, meta = load_meter_files([path], unit, timestamp_position="end", ambiguous_policy="auto",
                                     interval_minutes=None, same_meter=False, aggregate_devices=False,
                                     blank_policy="auto")
    return raw, meta


def _fmt(value: float, decimals: int = 0) -> str:
    return f"{float(value):,.{decimals}f}".replace(",", " ")


def _metrics_html(metrics, title: str, battery_note: str = "") -> str:
    return f"""
    <h3 style="margin-top:1.5rem">{title}</h3>
    <div class="metric-grid">
      <div class="metric-card"><div class="metric-label">Production solaire</div><div class="metric-value orange">{_fmt(metrics.production_kwh)} kWh</div><div class="metric-sub">Production AC sur la période analysée</div></div>
      <div class="metric-card"><div class="metric-label">Électricité vendue</div><div class="metric-value orange">{_fmt(metrics.export_kwh)} kWh</div><div class="metric-sub">Injection réseau {battery_note}</div></div>
      <div class="metric-card"><div class="metric-label">Taux d'autoconsommation</div><div class="metric-value green">{metrics.self_consumption_pct:.1f} %</div><div class="metric-sub">{_fmt(metrics.solar_used_kwh)} kWh de solaire utilisés sur site</div></div>
      <div class="metric-card"><div class="metric-label">Autonomie énergétique</div><div class="metric-value blue">{metrics.self_sufficiency_pct:.1f} %</div><div class="metric-sub">{_fmt(metrics.solar_used_kwh)} kWh solaire / {_fmt(metrics.consumption_kwh)} kWh consommés</div></div>
    </div>
    """


def _monthly_results(frame: pd.DataFrame, valid: np.ndarray, dt_hours: float,
                     monthly_pv: dict[int, float], imp: np.ndarray, exp: np.ndarray) -> pd.DataFrame:
    local = pd.DatetimeIndex(frame.timestamp).tz_convert("Europe/Zurich")
    periods = pd.PeriodIndex(local.tz_localize(None), freq="M")
    records = []
    for period in periods.unique():
        mask = (periods == period) & valid
        if not mask.any():
            continue
        start = pd.Timestamp(period.start_time, tz="Europe/Zurich")
        end = start + pd.offsets.MonthBegin(1)
        total_hours = (end.tz_convert("UTC") - start.tz_convert("UTC")).total_seconds() / 3600
        pv = monthly_pv[period.month] * min(1.0, mask.sum() * dt_hours / total_hours)
        metric = calculate_metrics(pv, np.nansum(imp[mask]), np.nansum(exp[mask]))
        records.append({"Mois": period.strftime("%m.%Y"), "Production PV (kWh)": metric.production_kwh,
                        "Import réseau (kWh)": metric.import_kwh, "Injection (kWh)": metric.export_kwh,
                        "Autoconsommation (%)": metric.self_consumption_pct,
                        "Autonomie (%)": metric.self_sufficiency_pct})
    return pd.DataFrame(records)


def main() -> None:
    st.set_page_config(page_title="Autoconsommation & autonomie PV", page_icon="☀️", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("☀️ Autoconsommation & autonomie PV")
    st.caption("Analyse basée sur les flux réseau import/export et la production photovoltaïque mensuelle.")

    with st.sidebar:
        st.header("Données de mesure")
        unit = st.selectbox("Unité de la courbe", ["Automatique", "kWh", "kW", "Wh", "W"])
        st.caption("kW/W sont convertis automatiquement en kWh au pas de mesure détecté.")
        st.header("Batterie")
        battery_case = st.radio(
            "Situation de la courbe",
            ["Aucune batterie", "Batterie existante déjà incluse dans la courbe", "Batterie à simuler sur une courbe sans batterie"],
            help="Une courbe mesurée avec une batterie en service contient déjà son effet : ne pas la simuler une seconde fois."
        )
        capacity = 0.0
        power = 0.0
        efficiency = .90
        soc_min = 5
        if battery_case != "Aucune batterie":
            capacity = st.number_input("Capacité batterie (kWh)", min_value=.1, max_value=5_000., value=10., step=1.)
        if battery_case.startswith("Batterie à simuler"):
            power = st.number_input("Puissance charge/décharge (kW)", min_value=.1, max_value=2_500., value=5., step=.5)
            efficiency = st.slider("Rendement aller-retour", .50, 1.00, .90, .01)
            soc_min = st.slider("SOC minimum (%)", 0, 95, 5)

    uploaded = st.file_uploader("Courbe de charge import/export", type=["xlsx", "xls", "csv"])
    if uploaded is None:
        st.info("Dépose ici la courbe de charge du compteur. Ensuite, renseigne les 12 productions PV mensuelles.")
        return
    try:
        with st.spinner("Lecture de la courbe import/export…"):
            raw, raw_meta = _load_file(uploaded.name, uploaded.getvalue(), "auto" if unit == "Automatique" else unit)
            frame, meta = prepare_simulation_data(raw, raw_meta, policy="segments")
    except (ValueError, OSError, KeyError, UnsupportedFormatError) as error:
        st.error(str(error))
        return

    st.caption(f"Fichier : {uploaded.name} · {meta.coverage_days:.0f} jours · pas {meta.dt_hours * 60:g} min · données exploitables {meta.completeness:.1%}")
    st.markdown("### Production photovoltaïque mensuelle")
    st.markdown("<div class='notice'>Saisir la production <b>AC</b> à la sortie des onduleurs (production mesurée ou E_Grid PVsyst). Les valeurs sont celles d'une année-type ; le programme les pondère automatiquement si le premier ou le dernier mois est incomplet.</div>", unsafe_allow_html=True)
    columns = st.columns(3)
    monthly_pv = {}
    for month, label in enumerate(MONTHS_FR, start=1):
        with columns[(month - 1) % 3]:
            monthly_pv[month] = st.number_input(f"{label} (kWh)", min_value=0.0, max_value=2_000_000.0,
                                                value=0.0, step=100.0, key=f"pv_{month}")
    if sum(monthly_pv.values()) <= 0:
        st.info("Renseigne les productions solaires mensuelles pour obtenir les taux.")
        return

    valid = frame.simulation_valid.to_numpy(bool)
    imp_before = frame.import_kWh.to_numpy(float)
    exp_before = frame.export_kWh.to_numpy(float)
    try:
        pv_production = production_for_measurements(frame.timestamp, valid, meta.dt_hours, monthly_pv)
        base = calculate_metrics(pv_production, np.nansum(imp_before[valid]), np.nansum(exp_before[valid]))
    except ValueError as error:
        st.error(str(error))
        return

    if battery_case.startswith("Batterie à simuler"):
        try:
            simulation = simulate(
                imp_before, exp_before, capacity, power, dt_hours=meta.dt_hours, roundtrip_eff=efficiency,
                tariff_import=0., tariff_export=0., coverage_days=meta.coverage_days, soc_min_pct=float(soc_min),
                timestamps=frame.timestamp, valid_mask=valid, reset_before=frame.reset_before.to_numpy(bool),
                dispatch_order="discharge_first",
            )
            after = calculate_metrics(pv_production, simulation.import_after_total, simulation.export_after_total)
        except ValueError as error:
            st.error(f"Simulation batterie impossible : {error}")
            return
        st.markdown(_metrics_html(base, "Sans batterie"), unsafe_allow_html=True)
        st.markdown(_metrics_html(after, f"Avec batterie simulée : {capacity:g} kWh / {power:g} kW", "après batterie"), unsafe_allow_html=True)
        delta_auto = after.self_consumption_pct - base.self_consumption_pct
        delta_autonomy = after.self_sufficiency_pct - base.self_sufficiency_pct
        st.success(f"Gain estimé avec la batterie : +{delta_auto:.1f} points d'autoconsommation et +{delta_autonomy:.1f} points d'autonomie énergétique.")
        imp_display, exp_display = simulation.import_after, simulation.export_after
    else:
        title = "Situation mesurée sans batterie" if battery_case == "Aucune batterie" else f"Situation mesurée avec batterie existante : {capacity:g} kWh"
        st.markdown(_metrics_html(base, title), unsafe_allow_html=True)
        if battery_case != "Aucune batterie":
            st.info("La capacité est indiquée à titre de contexte. Comme la courbe contient déjà le fonctionnement réel de cette batterie, le calcul ne la simule pas une seconde fois.")
        imp_display, exp_display = imp_before, exp_before

    try:
        monthly = _monthly_results(frame, valid, meta.dt_hours, monthly_pv, imp_display, exp_display)
    except ValueError as error:
        st.warning(f"Le détail mensuel ne peut pas être calculé : {error}")
        return
    st.markdown("### Détail mensuel")
    st.dataframe(monthly.round(1), hide_index=True, width="stretch")
    fig = go.Figure()
    fig.add_bar(x=monthly["Mois"], y=monthly["Production PV (kWh)"], name="Production PV", marker_color="#f59e0b")
    fig.add_bar(x=monthly["Mois"], y=monthly["Injection (kWh)"], name="Injection réseau", marker_color="#fb7185")
    fig.add_bar(x=monthly["Mois"], y=monthly["Import réseau (kWh)"], name="Import réseau", marker_color="#60a5fa")
    fig.update_layout(barmode="group", height=400, yaxis_title="kWh / mois", legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
    st.caption("Autoconsommation = (production PV − injection réseau) / production PV. Autonomie énergétique = solaire utilisé sur site / (solaire utilisé sur site + import réseau). Les taux d'une batterie existante sont déduits du bilan compteur ; un journal charge/décharge batterie serait nécessaire pour isoler précisément ses pertes.")


if __name__ == "__main__":
    main()
