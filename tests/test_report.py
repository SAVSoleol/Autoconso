from dataclasses import replace
from datetime import date
from io import BytesIO

import pdfplumber
from pypdf import PdfReader
import pytest

from demo import make_demo
from energy_metrics import analyse
from report import BatteryReport, ReportData, build_report, fmt, pct, report_filename


@pytest.fixture(scope="module")
def report_data():
    f, m, pv = make_demo()
    base, after, sim, _, _ = analyse(f, m, pv, capacity=30, power=10)
    return ReportData(base=base, after=after, period_start=m.start, period_end=m.end,
                      client_name="Exemple de démonstration", source_files=(m.source,),
                      battery=BatteryReport.from_simulation(sim),
                      valid_intervals=len(f), expected_intervals=len(f),
                      monthly_entered_kwh=sum(pv.values()), is_demo=True,
                      generated_on=date(2026, 10, 28))


def read_pdf(data):
    content = build_report(data)
    assert content.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(content))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


def test_two_a4_pages_and_results_are_from_simulation(report_data):
    reader, text = read_pdf(report_data)
    assert len(reader.pages) == 2
    assert reader.pages[0].mediabox.width == pytest.approx(595.2756, abs=.01)
    assert reader.pages[0].mediabox.height == pytest.approx(841.8898, abs=.01)
    assert "01 / 02" in text and "02 / 02" in text
    for value in [report_data.base.production_kwh, report_data.after.import_kwh,
                  report_data.after.export_kwh, report_data.battery.losses_kwh]:
        assert fmt(value) in text
    assert pct(report_data.after.self_sufficiency_pct) in text
    assert "Exemple synthétique" in text
    assert "Pertes de conversion" in text
    assert "01.01.2025 au 31.12.2025" in text


def test_no_glyph_outside_page_margins(report_data):
    with pdfplumber.open(BytesIO(build_report(report_data))) as pdf:
        for page in pdf.pages:
            assert all(c["x0"] >= 38 and c["x1"] <= page.width - 38 for c in page.chars)
            assert all(c["top"] > 18 and c["bottom"] < page.height - 15 for c in page.chars)


def test_accents_markup_and_filename_are_safe(report_data):
    data = replace(report_data, client_name="Müller & Fils <PV> / Genève", site_address="Rue de l'Énergie 12, 1700 Fribourg", reference="Étude n° 42")
    _, text = read_pdf(data)
    assert "Müller & Fils <PV> / Genève" in text
    assert "Rue de l'Énergie" in text
    assert "Étude n° 42" in text
    assert report_filename(data) == "Rapport_SOLEOL_Muller_Fils_PV_Geneve_20261028.pdf"


def test_without_battery_does_not_invent_comparison(report_data):
    data = replace(report_data, battery=None, after=report_data.base, is_demo=False)
    reader, text = read_pdf(data)
    assert len(reader.pages) == 2
    assert "Aucun stockage simulé" in text
    assert "Charge de la batterie" not in text
    assert "Pertes de conversion" not in text
    assert "Avec batterie" not in text


def test_existing_battery_is_labelled_apparent(report_data):
    data = replace(report_data, battery=None, after=report_data.base, is_demo=False, existing_battery=True)
    reader, text = read_pdf(data)
    assert len(reader.pages) == 2
    assert "Bilan apparent" in text
    assert "Autonomie énergétique apparente" in text
    assert "Aucune seconde" in text
    assert "Pertes de conversion" not in text
    with pytest.raises(ValueError):
        replace(data, battery=report_data.battery)


@pytest.mark.parametrize("discarded_stock", [0., 1.])
def test_forecast_and_incomplete_data_keep_all_warnings(report_data, discarded_stock):
    warning = "13 horodatages d'automne ambigus : convention supposée. Les mesures absentes restent inconnues."
    data = replace(report_data, is_demo=False, prorata=True, production_estimated=True,
                   completeness=.998, warnings=(warning,),
                   battery=replace(report_data.battery, untransferred_stock_kwh=discarded_stock))
    reader, text = read_pdf(data)
    assert "Bilan estimatif" in text
    assert "prévisionnelle" in text
    assert "prorata" in text
    assert warning in text
    assert "ANNEXE" in text
    assert len(reader.pages) == 3
    assert "03 / 03" in text


def test_multiple_long_sources_and_names_never_disappear(report_data):
    source = "mesures_" + "client_long_" * 25 + ".xlsx"
    client = "Client " + "référence très longue " * 7
    data = replace(report_data, client_name=client, site_address="Adresse " * 25,
                   source_files=(source,), warnings=("Contrôle : valeur inconnue. " * 80,))
    reader, text = read_pdf(data)
    assert len(reader.pages) >= 3
    # Les paragraphes sont repliés mais le contenu n'est pas supprimé.
    normalized = "".join(text.split())
    assert "".join(source.split()) in normalized
    assert "".join(client.split()) in normalized


def test_changed_parameters_change_reported_values(report_data):
    without = replace(report_data, battery=None, after=report_data.base)
    _, before_text = read_pdf(without)
    _, after_text = read_pdf(report_data)
    assert before_text != after_text
    assert pct(report_data.after.self_sufficiency_pct) in after_text
    assert pct(report_data.after.self_sufficiency_pct) not in before_text
