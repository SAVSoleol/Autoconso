"""Rapport PDF SOLEOL : deux pages de synthèse, annexes si nécessaire.

Génération locale en mémoire, à partir des résultats du calculateur. Aucune
valeur de la maquette n'est codée en dur. Aucun navigateur ni réseau requis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
import unicodedata
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from energy_metrics import EnergyMetrics

INK = colors.HexColor("#182a39")
MUTED = colors.HexColor("#667583")
LINE = colors.HexColor("#dce4e8")
SOFT = colors.HexColor("#f3f6f7")
GREEN = colors.HexColor("#087d5c")
GREEN_SOFT = colors.HexColor("#edf8f3")
BLUE = colors.HexColor("#447bd4")
GRID = colors.HexColor("#dce3eb")
ORANGE = colors.HexColor("#c87b16")
WARNING = colors.HexColor("#fff7e9")
WARNING_INK = colors.HexColor("#855716")
PAGE_W, PAGE_H = A4
MARGIN = 40.
WIDTH = PAGE_W - 2 * MARGIN


def _today():
    return datetime.now(ZoneInfo("Europe/Zurich")).date()


@dataclass(frozen=True)
class BatteryReport:
    capacity_kwh: float
    power_kw: float
    efficiency: float
    cycles: float
    charge_kwh: float
    discharge_kwh: float
    losses_kwh: float
    final_stock_kwh: float
    untransferred_stock_kwh: float = 0.

    @classmethod
    def from_simulation(cls, simulation):
        return cls(simulation.capacity_kWh, simulation.power_kW, simulation.roundtrip_eff,
                   simulation.cycles_period, simulation.charge_total_kWh,
                   simulation.discharge_total_kWh, simulation.conversion_losses_kWh,
                   simulation.final_stock_kWh, simulation.untransferred_stock_kWh)


@dataclass(frozen=True)
class ReportData:
    base: EnergyMetrics
    after: EnergyMetrics
    period_start: str
    period_end: str  # Fin exclusive, comme dans les métadonnées du compteur.
    client_name: str = ""
    site_address: str = ""
    reference: str = ""
    source_files: tuple[str, ...] = ()
    battery: BatteryReport | None = None
    valid_intervals: int = 0
    expected_intervals: int = 0
    dt_minutes: float = 15.
    completeness: float = 1.
    monthly_entered_kwh: float = 0.
    production_estimated: bool = False
    prorata: bool = False
    existing_battery: bool = False
    is_demo: bool = False
    warnings: tuple[str, ...] = ()
    generated_on: date = field(default_factory=_today)

    def __post_init__(self):
        if self.existing_battery and self.battery is not None:
            raise ValueError("Une batterie existante ne peut pas être simulée une seconde fois dans le rapport.")
        if not 0 <= self.completeness <= 1:
            raise ValueError("Couverture des mesures invalide.")

    @property
    def nature(self):
        if self.is_demo:
            return "Exemple synthétique"
        if self.existing_battery:
            return "Bilan apparent"
        if self.prorata or self.production_estimated or self.completeness < 1 - 1e-8:
            return "Bilan estimatif"
        return "Mesures et simulation" if self.battery else "Bilan énergétique mesuré"

    @property
    def period_label(self):
        start = datetime.fromisoformat(self.period_start)
        end = datetime.fromisoformat(self.period_end) - timedelta(microseconds=1)
        return f"{start:%d.%m.%Y} au {end:%d.%m.%Y}"


def _clean(value):
    return " ".join("".join(c for c in str(value) if c >= " " or c in "\n\t").split())


def _safe(value):
    return escape(_clean(value))


def fmt(value, decimals=0):
    return f"{float(value):,.{decimals}f}".replace(",", "’").replace(".", ",")


def pct(value):
    return fmt(value, 1) + " %"


def report_filename(data):
    name = unicodedata.normalize("NFKD", _clean(data.client_name)).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")[:70] or "installation"
    return f"Rapport_SOLEOL_{name}_{data.generated_on:%Y%m%d}.pdf"


def _fonts():
    if "Soleol" in pdfmetrics.getRegisteredFontNames():
        return
    directory = Path(__file__).resolve().parent / "assets" / "fonts"
    for name, file in [("Soleol", "DejaVuSans.ttf"), ("Soleol-Bold", "DejaVuSans-Bold.ttf")]:
        if not (directory / file).is_file():
            raise FileNotFoundError("Polices PDF absentes : ajouter le dossier assets/fonts du programme sur GitHub.")
        pdfmetrics.registerFont(TTFont(name, str(directory / file)))
    pdfmetrics.registerFontFamily("Soleol", normal="Soleol", bold="Soleol-Bold", italic="Soleol", boldItalic="Soleol-Bold")


def _styles():
    base = dict(fontName="Soleol", textColor=INK, fontSize=8.3, leading=12.2,
                alignment=TA_LEFT, splitLongWords=True)
    styles = {"body": ParagraphStyle("body", **base)}
    for key, overrides in {
        "title": dict(fontName="Soleol-Bold", fontSize=22., leading=26.5),
        "title_small": dict(fontName="Soleol-Bold", fontSize=21., leading=26.),
        "intro": dict(fontSize=9., leading=13., textColor=MUTED),
        "kicker": dict(fontName="Soleol-Bold", fontSize=7.3, leading=10., textColor=GREEN),
        "section": dict(fontName="Soleol-Bold", fontSize=10.2, leading=14.),
        "small": dict(fontSize=7.3, leading=10.5, textColor=MUTED),
        "label": dict(fontSize=7., leading=10., textColor=MUTED),
        "cell": dict(fontSize=8., leading=11.5),
        "number": dict(fontSize=8., leading=11.5, alignment=TA_RIGHT),
        "head": dict(fontName="Soleol-Bold", fontSize=7.5, leading=11., textColor=MUTED),
        "number_head": dict(fontName="Soleol-Bold", fontSize=7.5, leading=11., textColor=MUTED, alignment=TA_RIGHT),
        "warning": dict(fontSize=7.9, leading=11.3, textColor=WARNING_INK),
    }.items():
        styles[key] = ParagraphStyle(key, parent=styles["body"], **overrides)
    return styles


def _para(text, styles, key="body"):
    return Paragraph(text, styles[key])


def _box(text, styles, *, warning=False):
    paragraph = _para(text, styles, "warning" if warning else "body")
    box = Table([[paragraph]], colWidths=[WIDTH])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WARNING if warning else GREEN_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return box


def _section(number, title, styles):
    return [_para(f'<font color="#087d5c">{number:02d}</font>  {title}', styles, "section"), Spacer(1, 8)]


def _fit_font(text, size, width, font="Soleol-Bold"):
    measured = pdfmetrics.stringWidth(text, font, size)
    return min(size, size * width / measured) if measured else size


class _Stats(Flowable):
    def __init__(self, items, styles, *, large=False):
        super().__init__()
        self.width, self.height = WIDTH, 47 if large else 42
        self.items, self.styles, self.large = items, styles, large

    def draw(self):
        c, gap = self.canv, 13
        width = (self.width - gap * (len(self.items) - 1)) / len(self.items)
        for i, (label, value, unit) in enumerate(self.items):
            x = i * (width + gap)
            if self.large:
                c.setStrokeColor(LINE)
                c.setLineWidth(1)
                c.line(x, 5, x, self.height)
                x += 10
            label_para = _para(label, self.styles, "label")
            _, h = label_para.wrap(width - 10, 26)
            label_para.drawOn(c, x, self.height - h)
            value = str(value)
            size = _fit_font(value, 18 if self.large else 13., width - 12)
            c.setFont("Soleol-Bold", size)
            c.setFillColor(INK)
            c.drawString(x, 10, value)
            if unit:
                c.setFont("Soleol", 7)
                c.setFillColor(MUTED)
                c.drawString(x, 0, unit)


class _ComparisonCards(Flowable):
    def __init__(self, data, styles):
        super().__init__()
        self.width, self.height = WIDTH, 137
        self.data, self.styles = data, styles

    def draw(self):
        c, d, gap = self.canv, self.data, 12
        width = (self.width - gap) / 2
        rows = [("Autoconsommation", "Part de la production solaire conservée sur site.", d.base.self_consumption_pct, d.after.self_consumption_pct, GREEN),
                ("Autonomie énergétique", "Part de la consommation couverte par le solaire.", d.base.self_sufficiency_pct, d.after.self_sufficiency_pct, BLUE)]
        for i, (title, description, before, after, color) in enumerate(rows):
            x = i * (width + gap)
            c.setStrokeColor(LINE)
            c.setLineWidth(.7)
            c.roundRect(x, 0, width, self.height, 7, stroke=1, fill=0)
            title += " apparente" if d.existing_battery else ""
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", _fit_font(title, 9.5, width - 28))
            c.drawString(x + 14, self.height - 21, title)
            desc = _para(description, self.styles, "small")
            _, h = desc.wrap(width - 28, 30)
            desc.drawOn(c, x + 14, self.height - 29 - h)
            if d.battery:
                c.setFont("Soleol", 13.)
                c.setFillColor(MUTED)
                c.drawString(x + 14, 68, pct(before) + "  →")
                offset = pdfmetrics.stringWidth(pct(before) + "  →", "Soleol", 13.) + 10
                size = _fit_font(pct(after), 23., width - offset - 28)
                c.setFont("Soleol-Bold", size)
                c.setFillColor(INK)
                c.drawString(x + 14 + offset, 68, pct(after))
                c.setFont("Soleol-Bold", 7.6)
                c.setFillColor(GREEN)
                c.drawString(x + 14, 51, f"+{fmt(after-before, 1)} points avec batterie")
            else:
                c.setFont("Soleol-Bold", 25)
                c.drawString(x + 14, 68, pct(after))
                c.setFont("Soleol", 7.5)
                c.setFillColor(MUTED)
                c.drawString(x + 14, 51, "Bilan apparent du compteur" if d.existing_battery else "Situation mesurée sans batterie")
            for label, value, y, fill in ([('Sans batterie', before, 34, GRID), ('Avec batterie', after, 14, color)] if d.battery else [('Situation mesurée', after, 14, color)]):
                c.setFillColor(MUTED)
                c.setFont("Soleol", 6.8)
                c.drawString(x + 14, y + 7, label)
                c.drawRightString(x + width - 14, y + 7, pct(value))
                c.setFillColor(SOFT)
                c.roundRect(x + 14, y, width - 28, 4.2, 2, stroke=0, fill=1)
                c.setFillColor(fill)
                c.rect(x + 14, y, (width - 28) * max(0., min(100., value)) / 100, 4.2, stroke=0, fill=1)


class _EnergyBar(Flowable):
    def __init__(self, data):
        super().__init__()
        self.width, self.height, self.data = WIDTH, 52, data

    def draw(self):
        c, d = self.canv, self.data
        total = d.base.consumption_kwh
        if not total:
            c.setFont("Soleol", 9)
            c.setFillColor(MUTED)
            c.drawString(0, 30, "Aucune consommation du bâtiment sur cette période.")
            return
        values = [("Solaire retenu" if d.existing_battery else "Solaire direct", d.base.solar_used_kwh / total * 100, GREEN)]
        if d.battery:
            values.append(("Via la batterie", d.battery.discharge_kwh / total * 100, BLUE))
        values.append(("Réseau électrique", d.after.import_kwh / total * 100, GRID))
        x = 0
        for _, value, color in values:
            width = self.width * max(0., value) / 100
            c.setFillColor(color)
            c.rect(x, 36, width, 13, stroke=0, fill=1)
            x += width
        col_width = self.width / len(values)
        for i, (label, value, color) in enumerate(values):
            x = i * col_width
            c.setFillColor(color)
            c.rect(x, 21, 5, 5, stroke=0, fill=1)
            c.setFillColor(MUTED)
            c.setFont("Soleol", 7.3)
            c.drawString(x + 10, 21, label)
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", 10.5)
            c.drawString(x, 5, pct(value))


class _NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, report_data, **kwargs):
        super().__init__(*args, **kwargs)
        self._report_data = report_data
        self._saved_pages = []

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        count = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self.setStrokeColor(LINE)
            self.setLineWidth(.6)
            self.line(MARGIN, 37, PAGE_W - MARGIN, 37)
            self.setFont("Soleol", 6.8)
            self.setFillColor(MUTED)
            self.drawString(MARGIN, 23, "SOLEOL · Autoconsommation & autonomie · " + self._report_data.nature)
            self.drawRightString(PAGE_W - MARGIN, 23, f"{self._pageNumber:02d} / {count:02d}")
            super().showPage()
        super().save()


def _header(c, doc, data):
    c.saveState()
    c.setFillColor(INK)
    c.setFont("Soleol-Bold", 20)
    text = c.beginText(MARGIN, PAGE_H - 45)
    text.setCharSpace(2)
    text.textOut("SOLE")
    text.setFillColor(ORANGE)
    text.textOut("O")
    text.setFillColor(INK)
    text.textOut("L")
    text.setCharSpace(0)
    c.drawText(text)
    c.setFillColor(MUTED)
    c.setFont("Soleol", 6.8)
    c.drawString(MARGIN, PAGE_H - 60, "L'ÉNERGIE SOLAIRE, AU QUOTIDIEN.")
    for i, text in enumerate(["ÉTUDE ÉNERGÉTIQUE", f"Rapport du {data.generated_on:%d.%m.%Y}", data.nature]):
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - 38 - i * 12, text)
    c.setStrokeColor(LINE)
    c.setLineWidth(.6)
    c.line(MARGIN, PAGE_H - 77, PAGE_W - MARGIN, PAGE_H - 77)
    c.restoreState()


def _status(data):
    parts = [f"<b>{_safe(data.nature)}.</b>"]
    if data.is_demo:
        parts.append("Données synthétiques de démonstration ; ce rapport ne représente pas des mesures client.")
    if data.existing_battery:
        parts.append("Batterie déjà incluse dans les mesures : ses pertes, son origine de charge et la variation du stock ne sont pas isolées.")
    if data.prorata:
        parts.append("Courbes incomplètes : production PV des mois incomplets ajustée au prorata du temps mesuré.")
    if data.production_estimated:
        parts.append("Production solaire prévisionnelle : les taux calculés sont estimatifs.")
    if not any((data.is_demo, data.existing_battery, data.prorata, data.production_estimated)):
        parts.append("Bilan issu des flux réseau et de la production AC renseignée." + (" L'ajout de batterie est une simulation." if data.battery else ""))
    return " ".join(parts)


def _conclusion(data):
    if data.existing_battery:
        return "<b>Une lecture du bilan existant</b><br/>Les taux présentés sont apparents. Les seules courbes du compteur ne permettent pas d'isoler le fonctionnement de la batterie déjà en service. Aucune seconde batterie n'a été simulée."
    if data.battery:
        return ("<b>Ce que la batterie apporterait</b><br/>Sur la période étudiée, environ "
                f"<b>{fmt(data.battery.discharge_kwh)} kWh de solaire supplémentaire</b> seraient livrés au bâtiment. "
                f"L'autonomie passerait de <b>{pct(data.base.self_sufficiency_pct)}</b> à <b>{pct(data.after.self_sufficiency_pct)}</b>. "
                "Les pertes et le stock final ne sont pas crédités à la consommation du bâtiment.")
    return ("<b>Votre bilan sans batterie</b><br/>Votre installation utilise directement "
            f"<b>{fmt(data.base.solar_used_kwh)} kWh</b> de solaire sur la période. "
            f"Le solaire couvre <b>{pct(data.base.self_sufficiency_pct)}</b> de la consommation du bâtiment. "
            "Aucun stockage supplémentaire n'est simulé dans ce rapport.")


def build_report(data: ReportData) -> bytes:
    """Retourne le PDF complet ; les résultats proviennent du même bilan que l'UI."""
    _fonts()
    styles = _styles()
    buffer = BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, title="SOLEOL - Autoconsommation et autonomie",
                          author="SOLEOL", leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=99, bottomMargin=47, pageCompression=1)
    doc.addPageTemplates(PageTemplate(id="soleol", frames=[Frame(MARGIN, 47, WIDTH, PAGE_H - 146,
                                                               leftPadding=0, rightPadding=0,
                                                               topPadding=0, bottomPadding=0)],
                                     onPage=lambda c, d: _header(c, d, data)))
    story = []
    story += [_para("AUTOCONSOMMATION &amp; AUTONOMIE", styles, "kicker"), Spacer(1, 6)]
    title = "Votre bilan solaire.<br/>Le potentiel d'une batterie." if data.battery else "Votre bilan solaire.<br/>Autoconsommation et autonomie."
    story += [_para(title, styles, "title"), Spacer(1, 7),
              _para("Analyse des flux du compteur et de la production photovoltaïque.", styles, "intro"), Spacer(1, 13)]
    client = _safe(data.client_name or "Installation photovoltaïque")
    if data.site_address:
        client += "<br/>" + _safe(data.site_address)
    client += "<br/><font color='#667583'>Période : " + _safe(data.period_label) + "</font>"
    if data.reference:
        client += "<br/><font color='#667583'>Référence : " + _safe(data.reference) + "</font>"
    if data.battery:
        b = data.battery
        scenario = f"<b>Batterie de {fmt(b.capacity_kwh)} kWh utiles</b><br/>Puissance : {fmt(b.power_kw,1)} kW<br/>Rendement aller-retour : {fmt(b.efficiency*100)} %"
    else:
        scenario = "<b>Batterie existante dans les mesures</b><br/>Aucune seconde simulation" if data.existing_battery else "<b>Situation mesurée sans batterie</b><br/>Aucun stockage simulé"
    identity = Table([[_para("CLIENT / INSTALLATION", styles, "label"), _para("SCÉNARIO ÉTUDIÉ", styles, "label")],
                      [_para(client, styles), _para(scenario, styles)]], colWidths=[WIDTH*.53, WIDTH*.47])
    identity.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING",(0,0),(-1,-1),0),
                                  ("RIGHTPADDING",(0,0),(-1,-1),14), ("TOPPADDING",(0,0),(-1,-1),0),
                                  ("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story += [identity, Spacer(1, 9), _box(_status(data), styles, warning=data.prorata or data.production_estimated or data.existing_battery), Spacer(1, 15)]
    story += _section(1, "Votre installation aujourd'hui", styles)
    story += [_Stats([("Production solaire AC", fmt(data.base.production_kwh), "kWh sur la période"),
                       ("Consommation + pertes / stock" if data.existing_battery else "Consommation calculée", fmt(data.base.consumption_kwh), "kWh sur la période"),
                       ("Solaire retenu sur site" if data.existing_battery else "Solaire utilisé directement", fmt(data.base.solar_used_kwh), "kWh sur la période")], styles, large=True), Spacer(1, 15)]
    story += _section(2, "L'effet estimé de la batterie" if data.battery else "Vos indicateurs énergétiques", styles)
    story += [_ComparisonCards(data, styles), Spacer(1, 17)]
    story += _section(3, "Votre alimentation avec batterie" if data.battery else "Votre alimentation - bilan apparent" if data.existing_battery else "Votre alimentation sans batterie", styles)
    story += [_EnergyBar(data), Spacer(1, 12), _box(_conclusion(data), styles), Spacer(1, 8),
              _para("Valeurs arrondies sur la période étudiée. Aucun résultat n'est extrapolé aux heures manquantes ni automatiquement annualisé.", styles, "small"), PageBreak()]

    story += [_para("DÉTAILS DE L'ÉTUDE", styles, "kicker"), Spacer(1, 6),
              _para("Les chiffres et la méthode.", styles, "title_small"), Spacer(1, 6),
              _para("Flux d'énergie, paramètres de stockage et qualité des mesures.", styles, "intro"), Spacer(1, 16)]
    story += _section(4, "Comparatif des flux d'énergie" if data.battery else "Flux d'énergie mesurés", styles)
    flow_rows = [("Production photovoltaïque AC", data.base.production_kwh, data.after.production_kwh),
                 ("Consommation + pertes / stock" if data.existing_battery else "Consommation du bâtiment", data.base.consumption_kwh, data.after.consumption_kwh),
                 ("Injection au réseau", data.base.export_kwh, data.after.export_kwh),
                 ("Soutirage du réseau", data.base.import_kwh, data.after.import_kwh)]
    if data.battery:
        b = data.battery
        flow_rows += [("Charge de la batterie", None, b.charge_kwh), ("Restitution de la batterie", None, b.discharge_kwh),
                      ("Pertes de conversion", None, b.losses_kwh), ("Stock final disponible (DC)", None, b.final_stock_kwh)]
        if b.untransferred_stock_kwh > 1e-6:
            flow_rows.append(("Stock non transféré après les trous (DC)", None, b.untransferred_stock_kwh))
    table_data = [[_para("Énergie sur la période", styles, "head"), _para("Sans batterie" if data.battery else "Situation mesurée", styles, "number_head")]]
    if data.battery:
        table_data[0].append(_para("Avec batterie", styles, "number_head"))
    for label, before, after in flow_rows:
        row = [_para(label, styles, "cell"), _para("-" if before is None else fmt(before) + " kWh", styles, "number")]
        if data.battery:
            row.append(_para(fmt(after) + " kWh", styles, "number"))
        table_data.append(row)
    widths = [WIDTH*.50, WIDTH*.25, WIDTH*.25] if data.battery else [WIDTH*.65, WIDTH*.35]
    table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),SOFT), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                               ("LINEBELOW",(0,0),(-1,-1),.5,LINE), ("TOPPADDING",(0,0),(-1,-1),5),
                               ("BOTTOMPADDING",(0,0),(-1,-1),5), ("LEFTPADDING",(0,0),(-1,-1),7),
                               ("RIGHTPADDING",(0,0),(-1,-1),7)]))
    story += [table, Spacer(1, 5), _para("Énergies arrondies au kWh. Les écarts d'arrondi peuvent atteindre 1 kWh.", styles, "small"), Spacer(1, 14)]
    story += _section(5, "Paramètres de la batterie simulée" if data.battery else "Situation de stockage", styles)
    if data.battery:
        b = data.battery
        story += [_Stats([("Capacité utile", fmt(b.capacity_kwh) + " kWh", ""),
                          ("Charge / décharge", fmt(b.power_kw,1) + " kW", ""),
                          ("Rendement aller-retour", fmt(b.efficiency*100) + " %", "")], styles),
                  _para(f"Cycles équivalents calculés sur la période : <b>{fmt(b.cycles,1)}</b>. Charge sur le surplus solaire uniquement ; stock utilisable initial nul. Capacité et puissance limitées à chaque intervalle.", styles, "small")]
    else:
        story += [_para("Batterie existante : fonctionnement déjà inclus dans les courbes. Aucune seconde simulation." if data.existing_battery else "Installation analysée sans ajout de batterie. Aucun cycle ni flux de stockage simulé.", styles)]
    story += [Spacer(1, 14)]
    story += _section(6, "Sources et qualité des données", styles)
    story += [_Stats([("Pas de mesure", fmt(data.dt_minutes, 0) + " min", "Europe/Zurich"),
                      ("Intervalles exploités", fmt(data.valid_intervals), "sur " + fmt(data.expected_intervals)),
                      ("Intervalles valides", pct(data.completeness*100), "")], styles), Spacer(1, 5)]
    sources = " · ".join(_clean(s) for s in data.source_files) or "Courbes du compteur"
    annex = []
    if len(sources) > 240:
        annex.append(("Fichiers source", sources))
        sources = sources[:200] + "… (liste complète en annexe)"
    production = "prévisionnelle" if data.production_estimated else "saisie mensuellement aux onduleurs"
    story += [_para("<b>Réseau :</b> import et export des courbes du compteur. " + _safe(sources), styles, "small"),
              _para(f"<b>Production PV :</b> {production} ; {fmt(data.monthly_entered_kwh)} kWh saisis et {fmt(data.base.production_kwh)} kWh retenus sur les intervalles exploités.", styles, "small"), Spacer(1, 6)]
    if data.prorata:
        story += [_box("<b>Données manquantes.</b> Les heures absentes ne sont pas reconstituées. La production des mois incomplets est estimée au prorata du temps mesuré. La batterie repart sans stock utilisable après chaque trou.", styles, warning=True), Spacer(1, 6)]
    for warning in data.warnings:
        annex.append(("Contrôle des données", _clean(warning)))
    if annex:
        story += [_para("Les avertissements de lecture et la traçabilité détaillée sont conservés en annexe.", styles, "small")]
    story += [Spacer(1, 11)] + _section(7, "Comment lire ces résultats", styles)
    if data.existing_battery:
        methods = "<b>Bilan apparent.</b> Les flux du compteur seuls ne distinguent ni les pertes du stockage, ni sa variation de stock, ni une recharge réseau éventuelle. Ils ne permettent pas de quantifier précisément l'autonomie utile."
        story += [_para(methods, styles, "small")]
    else:
        definitions = [[_para("<b>Autoconsommation</b><br/>Production PV conservée sur site / production totale. Inclut les pertes du stockage et une éventuelle variation du stock.", styles, "small"),
                        _para("<b>Autonomie énergétique</b><br/>Solaire direct + restitution AC de la batterie / consommation du bâtiment. Les pertes et le stock final ne sont pas crédités au bâtiment.", styles, "small")]]
        definitions_table = Table(definitions, colWidths=[WIDTH/2,WIDTH/2])
        definitions_table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),
                                               ("RIGHTPADDING",(0,0),(-1,-1),14),("TOPPADDING",(0,0),(-1,-1),0),
                                               ("BOTTOMPADDING",(0,0),(-1,-1),0)]))
        story += [definitions_table]
    if data.battery:
        limitations = "Simulation indicative, sans vieillissement ni auxiliaires, sans recharge réseau ni revente depuis la batterie. Si import et export coexistent dans un pas, la décharge précède la charge ; l'ordre réel est inconnu à l'intérieur du pas. L'analyse économique nécessite des coûts et tarifs distincts."
        if annex:
            annex.append(("Limites de la simulation", limitations))
            story += [Spacer(1, 6), _para("Les limites détaillées de la simulation figurent en annexe.", styles, "small")]
        else:
            story += [Spacer(1, 6), _para(limitations, styles, "small")]
    if annex:
        story += [PageBreak(), _para("ANNEXE - TRAÇABILITÉ", styles, "kicker"), Spacer(1, 6),
                  _para("Sources et avertissements.", styles, "title_small"), Spacer(1, 14)]
        for title, text in annex:
            story += [KeepTogether([_para(_safe(title), styles, "section"), Spacer(1, 5)]),
                      _para(_safe(text), styles), Spacer(1, 12)]
    doc.build(story, canvasmaker=lambda *args, **kwargs: _NumberedCanvas(*args, report_data=data, **kwargs))
    return buffer.getvalue()
