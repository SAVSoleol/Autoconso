"""SOLEOL · Autoconsommation et autonomie. Lancer : streamlit run app.py"""
from __future__ import annotations

from html import escape
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import tempfile

import numpy as np
import pandas as pd
import streamlit as st

from demo import make_demo
from energy_metrics import MONTHS_FR, analyse, monthly_coverage
from loaders import UnsupportedFormatError, load_meter_files, prepare_simulation_data
from ui import dashboard_html, fmt, header_html

ROOT = Path(__file__).resolve().parent


@st.cache_data(show_spinner=False, max_entries=6)
def load_uploads(files, unit, position, minutes, blank_policy, same_meter):
    """Les noms fournis ne sont jamais utilisés comme chemins sur le serveur."""
    if sum(len(content) for _, content in files) > 120 * 1024**2:
        raise UnsupportedFormatError("Le total des fichiers dépasse 120 Mo.")
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for index, (name, content) in enumerate(files):
            suffix = Path(name).suffix.lower()
            if suffix not in {".csv", ".xls", ".xlsx"}:
                raise UnsupportedFormatError("Formats acceptés : CSV, XLS et XLSX.")
            if len(content) > 30 * 1024**2:
                raise UnsupportedFormatError("Un fichier dépasse 30 Mo.")
            path = Path(folder) / f"courbe_{index}{suffix}"
            path.write_bytes(content)
            if suffix == ".xlsx":
                with ZipFile(path) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 250 * 1024**2:
                        raise UnsupportedFormatError("Classeur trop volumineux après décompression.")
            paths.append(path)
        frame, meta = load_meter_files(paths, data_unit=unit, timestamp_position=position,
                                       interval_minutes=minutes, same_meter=same_meter,
                                       blank_policy=blank_policy)
    meta.source = " · ".join(name for name, _ in files)
    return frame, meta


@st.cache_data(show_spinner=False)
def demo_data():
    return make_demo()


def _switch_source(source):
    st.session_state["source"] = source
    st.session_state.pop("pv_source", None)
    for key in list(st.session_state):
        if key.startswith("pv_"):
            del st.session_state[key]


def _upload_changed():
    _switch_source("upload")


def _reset_battery():
    for key, value in {"capacity": 15, "power": 5., "efficiency": 92,
                       "simulate_battery": True}.items():
        st.session_state[key] = value


def _section(number, title):
    st.html(f'<div class="pv-section"><span class="pv-number">{number}</span>{title}</div>')


def _empty_dashboard():
    cards = ''.join(f'<article><p>{label}</p><div><strong>—</strong><small>kWh</small></div></article>'
                    for label in ["Production photovoltaïque", "Consommation du bâtiment", "Solaire utilisé directement"])
    rates = ''.join(f'<section class="pv-result"><h2>{label}</h2><p class="pv-subtitle">{description}</p><div class="pv-comparison"><span class="pv-after">— %</span></div><div class="pv-track"></div></section>'
                    for label, description in [("Autoconsommation", "Part de votre production solaire conservée sur site."),
                                                ("Autonomie énergétique", "Part de votre consommation couverte par le solaire.")])
    st.html(f'<div class="pv-dashboard"><div class="pv-summary">{cards}</div><div class="pv-cards">{rates}</div></div>')


def main():
    st.set_page_config(page_title="SOLEOL · Simulateur solaire", page_icon="☀️", layout="wide", initial_sidebar_state="expanded")
    st.html(f"<style>{(ROOT / 'styles.css').read_text(encoding='utf-8')}</style>")
    st.session_state.setdefault("source", "upload")
    raw = raw_meta = frame = meta = None
    load_error = None
    demo_pv = None

    with st.sidebar:
        st.html('<div class="pv-brand"><strong>SOLEOL</strong><span>/ Simulateur solaire</span></div>')
        _section(1, "Installation existante")
        uploads = st.file_uploader("Courbes import / export réseau", type=["csv", "xlsx", "xls"],
                                   accept_multiple_files=True, key="meter_uploads", on_change=_upload_changed,
                                   help="Export bidirectionnel du compteur, avec date, import et export. Plusieurs fichiers : même compteur et périodes sans chevauchement.")
        st.button("Voir un exemple", on_click=_switch_source, args=("demo",), width="stretch")
        if st.session_state.source == "demo":
            st.button("Mes données", on_click=_switch_source, args=("upload",), width="stretch")
        summary_placeholder = st.empty()

        with st.expander("Réglages des courbes"):
            unit = st.selectbox("Unité des valeurs", ["Automatique", "kWh", "kW", "Wh", "W"], key="unit")
            timestamp = st.selectbox("Horodatage", ["Fin d'intervalle", "Début d'intervalle"], key="timestamp",
                                     help="Pour une valeur à 00:15 couvrant 00:00–00:15, choisir fin d'intervalle.")
            interval = st.selectbox("Pas de mesure (minutes)", ["Automatique", 5, 10, 15, 30, 60], key="interval")
            segments = st.checkbox("Analyser les segments mesurés malgré les trous", value=False, key="segments",
                                   help="Les valeurs manquantes restent inconnues. La batterie redémarre vide après chaque trou.")
            prorata = st.checkbox("Estimer la production des mois incomplets au prorata", value=False, key="prorata",
                                  help="À activer uniquement si un bilan estimatif est acceptable. Le temps mesuré ne représente pas nécessairement le même pourcentage de production solaire.")
            blank_unknown = st.checkbox("Groupe E : cellules vides toujours inconnues", key="blank_unknown",
                                        help="Par défaut, la convention zéro d'un flux vide est appliquée uniquement au modèle Groupe E reconnu, si l'autre flux est renseigné.")
            existing = st.checkbox("Batterie déjà en service dans les courbes", key="existing",
                                   help="Aucune seconde batterie n'est simulée. Les taux affichés sont des bilans apparents ; les pertes et variations de stock ne sont pas isolées.")
            same_meter = False
            if len(uploads) > 1:
                same_meter = st.checkbox("Ces fichiers concernent le même compteur", key="same_meter")

        try:
            if st.session_state.source == "demo":
                frame, meta, demo_pv = demo_data()
                # La démo est déjà normalisée et complète, indépendamment des réglages du fichier.
            elif uploads:
                if len(uploads) > 1 and not same_meter:
                    raise UnsupportedFormatError("Confirmer que les fichiers concernent le même compteur dans les réglages des courbes.")
                raw, raw_meta = load_uploads(tuple((file.name, file.getvalue()) for file in uploads),
                                             "auto" if unit == "Automatique" else unit,
                                             "end" if timestamp == "Fin d'intervalle" else "start",
                                             None if interval == "Automatique" else interval,
                                             "unknown" if blank_unknown else "auto", same_meter)
                frame, meta = prepare_simulation_data(raw, raw_meta, policy="segments" if segments else "block")
        except (ValueError, OSError, KeyError, BadZipFile, ImportError) as error:
            load_error = str(error)

        st.divider()
        _section(2, "Production photovoltaïque")
        production_kind = st.selectbox("Origine de la production", ["Mesurée aux onduleurs", "Estimée (PVsyst / autre)"], key="production_kind")
        monthly_pv = {}
        if frame is not None and meta is not None:
            coverage = monthly_coverage(frame.timestamp, frame.simulation_valid, meta.dt_hours)
            active = set(coverage.loc[coverage.hours > 0, "period"])
            identity = "demo-2025" if demo_pv else meta.fingerprint
            if st.session_state.get("pv_source") != identity:
                for period in active:
                    st.session_state[f"pv_{period}"] = demo_pv.get(period) if demo_pv else None
                st.session_state.pv_source = identity
            for year in sorted({int(period[:4]) for period in active}):
                with st.expander(f"Production mensuelle · {year}", expanded=not all(st.session_state.get(f"pv_{p}") is not None for p in active)):
                    st.caption("Production totale AC de chaque mois, à la sortie des onduleurs. Saisir 0 pour un mois sans production.")
                    cols = st.columns(2)
                    for month, label in enumerate(MONTHS_FR, 1):
                        period = f"{year}-{month:02d}"
                        with cols[(month - 1) % 2]:
                            value = st.number_input(label + " (kWh)", min_value=0., max_value=20_000_000., value=None,
                                                    step=10., placeholder="À saisir", key=f"pv_{period}", disabled=period not in active)
                        if period in active:
                            monthly_pv[period] = value
            available_pv = sum(v for v in monthly_pv.values() if v is not None)
            st.caption(f"Production saisie : {fmt(available_pv)} kWh")
        else:
            st.caption("Importez la courbe pour renseigner les 12 mois de l'année concernée.")

        st.divider()
        _section(3, "Ajouter une batterie")
        simulate_enabled = st.checkbox("Simuler une batterie", value=True, key="simulate_battery", disabled=existing)
        battery_disabled = existing or not simulate_enabled
        capacity = st.slider("Capacité utile (kWh)", min_value=1, max_value=200, value=15, step=1, key="capacity", disabled=battery_disabled,
                             help="Énergie utilisable entre le SOC minimum et maximum. Le modèle ne retranche pas une seconde réserve.")
        power = st.number_input("Puissance charge / décharge (kW)", min_value=.1, max_value=2500., value=5., step=.5, key="power", disabled=battery_disabled)
        efficiency = st.slider("Rendement aller-retour (%)", min_value=50, max_value=100, value=92, step=1, key="efficiency", disabled=battery_disabled)
        st.button("Réinitialiser la batterie", on_click=_reset_battery, width="stretch")
        cycles_placeholder = st.empty()
        st.caption("Les cycles sont calculés à partir des courbes. Aucun nombre de cycles n'est imposé.")

    is_demo = st.session_state.source == "demo"
    scenario = f"Batterie {capacity} kWh · {fmt(power,1)} kW" if simulate_enabled and not existing else "Batterie existante" if existing else "Sans batterie"
    period_label = ""
    if meta:
        start = pd.Timestamp(meta.start)
        last = pd.Timestamp(meta.end) - pd.Timedelta(minutes=1)
        period_label = f"{start:%d.%m.%Y} au {last:%d.%m.%Y}"
    st.html(header_html(period_label, "Exemple synthétique · 2025" if is_demo else "Courbes du compteur", scenario))
    if load_error:
        st.error(load_error)
        _empty_dashboard()
        return
    if frame is None or meta is None:
        st.info("Importez vos courbes de charge dans la barre latérale, puis renseignez la production solaire mois par mois. « Voir un exemple » permet de tester l'interface.")
        _empty_dashboard()
        return

    valid = frame.simulation_valid.to_numpy(bool)
    imported = float(frame.loc[valid, "import_kWh"].sum())
    exported = float(frame.loc[valid, "export_kWh"].sum())
    summary_placeholder.html(f'<div class="pv-readonly"><div><span>Import réseau mesuré</span><strong>{fmt(imported)} kWh</strong></div><div><span>Export réseau mesuré</span><strong>{fmt(exported)} kWh</strong></div></div>')
    st.caption(f"{meta.dt_hours * 60:g} min par mesure · {int(valid.sum()):,} intervalles exploités · {meta.completeness:.1%} des intervalles présents et valides · {escape(meta.source)}")

    if any(value is None for value in monthly_pv.values()):
        missing = [f"{MONTHS_FR[int(period[-2:])-1]} {period[:4]}" for period, value in monthly_pv.items() if value is None]
        st.info("Production mensuelle à compléter : " + ", ".join(missing) + ".")
        _empty_dashboard()
        return
    try:
        with st.spinner("Calcul du bilan et de la simulation…"):
            base, after, sim, monthly, coverage = analyse(
                frame, meta, monthly_pv, capacity=capacity, power=power,
                efficiency=efficiency / 100, simulate_battery=simulate_enabled and not existing,
                allow_prorata=prorata)
    except ValueError as error:
        st.error(str(error))
        _empty_dashboard()
        return

    estimated_period = bool(((coverage.fraction < 1 - 1e-8) & (coverage.hours > 0)).any())
    if estimated_period:
        st.warning("Bilan estimatif : la production des mois incomplets est répartie au prorata du temps mesuré. Les heures manquantes ne sont pas reconstituées.")
    if production_kind.startswith("Estimée") and not is_demo:
        st.warning("La production PV est prévisionnelle : les taux calculés sont des estimations, même si les flux réseau sont mesurés.")
    if existing:
        st.warning("Batterie existante : aucune seconde simulation. Les résultats sont des bilans apparents incluant pertes et variation de stock. Une charge réseau éventuelle ne peut pas être distinguée.")
    st.html(dashboard_html(base, after, sim, existing=existing, estimated=estimated_period))
    if sim:
        cycles_placeholder.caption(f"Cycles équivalents sur la période : {fmt(sim.cycles_period,1)}")

    with st.expander("Détail mensuel et export"):
        if existing:
            st.caption("Les colonnes « avant / après » sont identiques : situation mesurée avec la batterie existante. Les taux sont apparents.")
        st.dataframe(monthly.round(2), hide_index=True, width="stretch")
        columns = st.columns(2)
        columns[0].download_button("Télécharger le détail mensuel", data=monthly.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                                   file_name="soleol_bilan_mensuel.csv", mime="text/csv", width="stretch")
        summary = pd.DataFrame({"Indicateur": ["Production PV (kWh)", "Consommation bâtiment (kWh)", "Import réseau (kWh)", "Export réseau (kWh)", "Autoconsommation (%)", "Autonomie (%)"],
                                "Avant": [base.production_kwh, base.consumption_kwh, base.import_kwh, base.export_kwh, base.self_consumption_pct, base.self_sufficiency_pct],
                                "Après": [after.production_kwh, after.consumption_kwh, after.import_kwh, after.export_kwh, after.self_consumption_pct, after.self_sufficiency_pct]})
        summary["Période"] = period_label
        summary["Nature du bilan"] = "Apparent (batterie existante)" if existing else "Exemple synthétique" if is_demo else "Estimatif" if estimated_period or production_kind.startswith("Estimée") else "Production et réseau mesurés ; batterie simulée" if sim else "Production et réseau mesurés"
        columns[1].download_button("Télécharger la synthèse", data=summary.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                                   file_name="soleol_synthese.csv", mime="text/csv", width="stretch")
        st.caption("Les valeurs concernent uniquement la période indiquée. Aucune extrapolation automatique à l'année.")

    with st.expander("Qualité des données et hypothèses"):
        st.write(f"Période : {period_label} · Fuseau : Europe/Zurich · Pas : {meta.dt_hours * 60:g} min.")
        st.write(f"Couverture du fichier : {meta.coverage_days:.1f} jours. Intervalles valides : {meta.completeness:.1%}.")
        for warning in meta.warnings:
            st.warning(warning)
        st.dataframe(coverage.rename(columns={"label": "Mois", "hours": "Heures exploitées", "month_hours": "Heures du mois", "fraction": "Part du mois"})[["Mois", "Heures exploitées", "Heures du mois", "Part du mois"]], hide_index=True)
        st.caption("Si un intervalle contient à la fois import et export, la décharge précède la charge. Leur ordre réel n'est pas connu à l'intérieur du pas de mesure. Ni vieillissement, ni auxiliaires, ni recharge réseau ne sont simulés.")


if __name__ == "__main__":
    main()
