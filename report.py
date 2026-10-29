"""Rapport SOLEOL : autoconsommation, autonomie et flux mensuels.

Génération locale en mémoire, à partir des résultats du calculateur. Aucune
valeur de la maquette n'est codée en dur. Aucun navigateur ni réseau requis.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from io import BytesIO
from math import isfinite, ceil
from pathlib import Path
import re
import unicodedata
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageBreak,
    NextPageTemplate, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from energy_metrics import EnergyMetrics, MONTHS_FR

INK = colors.HexColor("#15344d")
MUTED = colors.HexColor("#667583")
LINE = colors.HexColor("#dce4e8")
SOFT = colors.HexColor("#f3f6f7")
GREEN = colors.HexColor("#e45638")
GREEN_SOFT = colors.HexColor("#fff3ef")
BLUE = colors.HexColor("#447bd4")
EXPORT_YELLOW = colors.HexColor("#e7b91b")
GRID = colors.HexColor("#dce3eb")
ORANGE = colors.HexColor("#e45638")
WARNING = colors.HexColor("#fff7e9")
WARNING_INK = colors.HexColor("#855716")
PAGE_W, PAGE_H = A4
MARGIN = 22 * mm
WIDTH = PAGE_W - 2 * MARGIN
_ACTIVE_FONT_PAIR = None


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
class MonthlyEnergy:
    month: int
    production_kwh: float | None
    import_kwh: float | None
    export_kwh: float | None

    @classmethod
    def from_record(cls, row):
        def value(key):
            number = float(row[key])
            return number if isfinite(number) else None
        return cls(int(row["Numéro du mois"]), value("Production PV (kWh)"),
                   value("Import avant (kWh)"), value("Export avant (kWh)"))


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
    months: tuple[MonthlyEnergy, ...] = ()

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
    global _ACTIVE_FONT_PAIR
    directory = Path(__file__).resolve().parent / "assets" / "fonts"
    arial = all((directory / file).is_file() for file in ("arial.ttf", "arialbd.ttf"))
    pair = ("arial.ttf", "arialbd.ttf") if arial else ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf")
    if "Soleol" in pdfmetrics.getRegisteredFontNames() and _ACTIVE_FONT_PAIR == pair:
        return
    for name, file in zip(("Soleol", "Soleol-Bold"), pair):
        if not (directory / file).is_file():
            raise FileNotFoundError("Polices PDF absentes : ajouter le dossier assets/fonts du programme sur GitHub.")
        pdfmetrics.registerFont(TTFont(name, str(directory / file)))
    pdfmetrics.registerFontFamily("Soleol", normal="Soleol", bold="Soleol-Bold", italic="Soleol", boldItalic="Soleol-Bold")
    _ACTIVE_FONT_PAIR = pair


def has_arial():
    directory = Path(__file__).resolve().parent / "assets" / "fonts"
    return all((directory / file).is_file() for file in ("arial.ttf", "arialbd.ttf"))


def _styles():
    base = dict(fontName="Soleol", textColor=INK, fontSize=11., leading=12.65,
                alignment=TA_LEFT, splitLongWords=True)
    styles = {"body": ParagraphStyle("body", **base)}
    for key, overrides in {
        "title": dict(fontName="Soleol-Bold", fontSize=23., leading=25.8),
        "title_small": dict(fontName="Soleol-Bold", fontSize=22., leading=25.),
        "intro": dict(fontSize=10.5, leading=12.1, textColor=MUTED),
        "kicker": dict(fontName="Soleol-Bold", fontSize=9.5, leading=11., textColor=GREEN),
        "section": dict(fontName="Soleol-Bold", fontSize=14., leading=16.1),
        "small": dict(fontSize=9.5, leading=10.95, textColor=MUTED),
        "label": dict(fontSize=9.5, leading=10.95, textColor=MUTED),
        "cell": dict(fontSize=9.4, leading=10.8),
        "number": dict(fontSize=9.4, leading=10.8, alignment=TA_RIGHT),
        "head": dict(fontName="Soleol-Bold", fontSize=9.3, leading=10.7, textColor=MUTED),
        "number_head": dict(fontName="Soleol-Bold", fontSize=9.3, leading=10.7, textColor=MUTED, alignment=TA_RIGHT),
        "warning": dict(fontSize=9.5, leading=10.95, textColor=WARNING_INK),
        "calculation": dict(fontSize=10.5, leading=12.075),
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


def _section(number, title, styles, gap=8):
    return [_para(f'<font color="#e45638">{number}.</font>  {title}', styles, "section"), Spacer(1, gap)]


def _identity_story(data, styles, *, details=True):
    identity = f"<b>{_safe(data.client_name or 'Installation photovoltaïque')}</b>"
    if details and data.site_address:
        identity += "<br/>" + _safe(data.site_address)
    if details and data.reference:
        identity += "<br/>Référence : " + _safe(data.reference)
    period = f"Période étudiée : <b>{_safe(data.period_label)}</b>"
    return [_para(identity, styles), Spacer(1, 6), _para(period, styles)]


def _fit_font(text, size, width, font="Soleol-Bold"):
    measured = pdfmetrics.stringWidth(text, font, size)
    return min(size, size * width / measured) if measured else size


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
            self.line(MARGIN, MARGIN-10, PAGE_W - MARGIN, MARGIN-10)
            self.setFont("Soleol", 9)
            self.setFillColor(MUTED)
            self.drawString(MARGIN, MARGIN-21, "SOLEOL · Autoconsommation & autonomie · " + self._soleol_page_nature)
            self.drawRightString(PAGE_W - MARGIN, MARGIN-21, f"{self._pageNumber:02d} / {count:02d}")
            super().showPage()
        super().save()


def _header(c, doc, data):
    c._soleol_page_nature = "Simulation batterie" if data.battery else data.nature
    c.saveState()
    logo = Path(__file__).resolve().parent / "assets" / "logo_soleol.png"
    if logo.is_file():
        image = ImageReader(str(logo))
        w, h = image.getSize()
        c.drawImage(image, MARGIN, PAGE_H - MARGIN - 34, width=145, height=145*h/w, mask="auto")
    else:
        c.setFont("Soleol-Bold", 22)
        c.setFillColor(INK)
        c.drawString(MARGIN, PAGE_H - MARGIN - 24, "SOLEOL")
    c.setFont("Soleol", 9.5)
    c.setFillColor(MUTED)
    for i, text in enumerate(["Étude énergétique", f"{data.generated_on:%d.%m.%Y}", data.nature]):
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 7 - i*12, text)
    c.restoreState()


class _Indicators(Flowable):
    def __init__(self, data, styles):
        super().__init__()
        self.width = WIDTH
        self.data, self.styles = data, styles
        used, autonomy = data.base.self_consumption_pct, data.base.self_sufficiency_pct
        explanation = (f"{pct(used)} de l'électricité produite par vos panneaux solaires "
                       "est utilisée directement chez vous.")
        if data.existing_battery:
            explanation = (f"{pct(used)} de votre production solaire n'est pas envoyée sur le réseau. "
                           "Ce taux inclut le fonctionnement de votre batterie.")
        self.indicators = [
            ("Autoconsommation", used, explanation),
            ("Autonomie énergétique", autonomy,
             f"{pct(autonomy)} de l'électricité que vous consommez provient de vos panneaux solaires. "
             "Le reste vient du réseau."),
        ]
        text_width = (WIDTH - 12) / 2 - 24
        heights = [_para(text, styles, "calculation").wrap(text_width, 100)[1]
                   for _, _, text in self.indicators]
        self.height = max(128, 92 + max(heights))

    def draw(self):
        c = self.canv
        width = (WIDTH - 12)/2
        for i, (title, current, explanation) in enumerate(self.indicators):
            x = i*(width+12)
            c.setStrokeColor(LINE)
            c.setLineWidth(.7)
            c.roundRect(x, 0, width, self.height, 5, stroke=1, fill=0)
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", 12)
            c.drawString(x+12, self.height-22, title)
            c.setFillColor(ORANGE)
            c.setFont("Soleol-Bold", 26)
            c.drawString(x+12, self.height-58, pct(current))
            p = _para(explanation, self.styles, "calculation")
            _, h = p.wrap(width-24, 100)
            p.drawOn(c, x+12, self.height-76-h)


class _MonthlyHistogram(Flowable):
    """Histogramme vectoriel : 12 mois, 3 barres par mois, échelle en kWh."""
    def __init__(self, months):
        super().__init__()
        self.width, self.height, self.months = WIDTH, 166, months

    def draw(self):
        c = self.canv
        series = [("Production solaire", "production_kwh", colors.HexColor("#2e9f57")),
                  ("Soutirage réseau", "import_kwh", BLUE),
                  ("Export réseau", "export_kwh", EXPORT_YELLOW)]
        for i, (label, _, color) in enumerate(series):
            x = i*WIDTH/3
            c.setFillColor(color)
            c.rect(x, 154, 6, 6, stroke=0, fill=1)
            c.setFillColor(MUTED)
            c.setFont("Soleol", 9.3)
            c.drawString(x+10, 153, label)
        values = [getattr(m, key) for m in self.months for _, key, _ in series]
        values = [v for v in values if v is not None]
        maximum = max(values, default=0.)
        unit = 10**max(0, len(str(int(maximum or 1)))-1)
        ceiling = max(unit, ceil(maximum*1.08/unit)*unit)
        left, right, bottom, top = 42, 3, 21, 134
        plot_width, plot_height = WIDTH-left-right, top-bottom
        c.setFillColor(MUTED)
        c.setFont("Soleol", 9)
        c.drawString(0, top+8, "kWh")
        tick_step = ceiling/3 if ceiling % 3 == 0 else ceiling/2
        for tick in range(int(round(ceiling/tick_step))+1):
            value = tick_step*tick
            y = bottom+plot_height*value/ceiling
            c.setStrokeColor(LINE)
            c.setLineWidth(.5)
            c.line(left, y, WIDTH-right, y)
            c.setFillColor(MUTED)
            c.drawRightString(left-5, y-3, fmt(value))
        months = {m.month:m for m in self.months}
        short = ("Jan.","Fév.","Mars","Avr.","Mai","Juin","Juil.","Août","Sept.","Oct.","Nov.","Déc.")
        step = plot_width/12
        bar_width = min(7.,step*.22)
        for index in range(12):
            center = left+step*(index+.5)
            c.setFillColor(MUTED)
            c.setFont("Soleol", 9)
            c.drawCentredString(center, 5, short[index])
            record = months.get(index+1)
            if record is None:
                continue
            for j, (_, key, color) in enumerate(series):
                value = getattr(record, key)
                if value is None:
                    continue
                x = center-1.5*bar_width-1+j*(bar_width+1)
                c.setFillColor(color)
                c.rect(x,bottom,bar_width,plot_height*value/ceiling,stroke=0,fill=1)


def _table(rows, widths, styles, *, total=False):
    cells = [[_para(str(text), styles, "head" if i==0 and j==0 else "number_head" if i==0 else "cell" if j==0 else "number")
              for j,text in enumerate(row)] for i,row in enumerate(rows)]
    table = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands = [("BACKGROUND",(0,0),(-1,0),SOFT), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("LINEBELOW",(0,0),(-1,-1),.5,LINE), ("TOPPADDING",(0,0),(-1,-1),3),
                ("BOTTOMPADDING",(0,0),(-1,-1),3), ("LEFTPADDING",(0,0),(-1,-1),6),
                ("RIGHTPADDING",(0,0),(-1,-1),6)]
    if total:
        commands.append(("BACKGROUND",(0,-1),(-1,-1),SOFT))
    table.setStyle(TableStyle(commands))
    return table


class _BatteryIndicators(Flowable):
    def __init__(self, data, styles):
        super().__init__()
        self.width, self.height = WIDTH, 100
        self.data, self.styles = data, styles

    def draw(self):
        c, d = self.canv, self.data
        width = (WIDTH - 12) / 2
        indicators = [
            ("Autoconsommation", "Part de votre production solaire conservée sur place.",
             d.base.self_consumption_pct, d.after.self_consumption_pct),
            ("Autonomie énergétique", "Part de votre consommation couverte par votre énergie solaire.",
             d.base.self_sufficiency_pct, d.after.self_sufficiency_pct),
        ]
        for index, (title, definition, before, after) in enumerate(indicators):
            x = index * (width + 12)
            c.setStrokeColor(LINE)
            c.setLineWidth(.7)
            c.roundRect(x, 0, width, self.height, 5, stroke=1, fill=0)
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", 12)
            c.drawString(x + 11, 80, title)
            paragraph = _para(definition, self.styles, "small")
            _, height = paragraph.wrap(width - 22, 30)
            paragraph.drawOn(c, x + 11, 73 - height)
            c.setFillColor(MUTED)
            c.setFont("Soleol", 9.5)
            c.drawString(x + 11, 43, "Sans batterie")
            c.drawRightString(x + width - 11, 43, "Avec batterie")
            c.setFillColor(INK)
            c.setFont("Soleol", _fit_font(pct(before), 20, width * .39, "Soleol"))
            c.drawString(x + 11, 22, pct(before))
            c.setFillColor(MUTED)
            c.setFont("Soleol", 12)
            c.drawCentredString(x + width * .49, 25, "→")
            c.setFillColor(ORANGE)
            c.setFont("Soleol-Bold", _fit_font(pct(after), 24, width * .43))
            c.drawRightString(x + width - 11, 21, pct(after))
            c.setFillColor(INK)
            c.setFont("Soleol", 9.5)
            gain = after - before
            sign = "+" if gain >= 0 else ""
            c.drawString(x + 11, 7, f"Gain : {sign}{fmt(gain, 1)} points")


class _NetworkComparison(Flowable):
    """Comparaison sur une même échelle, bleu pour l'import, jaune pour l'export."""
    def __init__(self, data):
        super().__init__()
        self.width, self.height, self.data = WIDTH, 108, data

    def draw(self):
        c, d = self.canv, self.data
        maximum = max(d.base.import_kwh, d.base.export_kwh, 1.)
        left, right = 91, WIDTH - 84
        plot_width = right - left
        for index, (title, before, after, color) in enumerate([
            ("Soutirage réseau", d.base.import_kwh, d.after.import_kwh, BLUE),
            ("Export réseau", d.base.export_kwh, d.after.export_kwh, EXPORT_YELLOW),
        ]):
            top = 96 - 47 * index
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", 10.5)
            c.drawString(0, top, title)
            reduction = 100 * (before - after) / before if before > 0 else 0.
            c.setFillColor(MUTED)
            c.setFont("Soleol", 9.5)
            c.drawRightString(WIDTH, top, "-" + pct(max(0., reduction)))
            for row, (label, value) in enumerate([("Sans batterie", before), ("Avec batterie", after)]):
                y = top - 16 - row * 14
                c.setFillColor(INK)
                c.setFont("Soleol", 9.5)
                c.drawString(0, y, label)
                c.drawRightString(WIDTH, y, fmt(value) + " kWh")
                fill = colors.Color(*(component * .38 + .62 for component in (color.red, color.green, color.blue))) if row == 0 else color
                c.setFillColor(fill)
                c.rect(left, y - 1, plot_width * value / maximum, 10, stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Soleol", 9.3)
        c.drawRightString(WIDTH, 0, f"Énergies de la période · échelle commune : 0 à {fmt(maximum)} kWh")


def _battery_page(data, styles):
    b = data.battery
    story = [_para("Impact d'une batterie<br/>sur votre installation", styles, "title_small"), Spacer(1, 8)]
    story += _identity_story(data, styles, details=False) + [Spacer(1, 8)]
    story += _section(7, "Batterie simulée", styles, gap=5)
    parameters = Table([[
        _para(f'<font color="#667583" size="9.5">Capacité utile</font><br/><b>{fmt(b.capacity_kwh, 1)} kWh</b>', styles),
        _para(f'<font color="#667583" size="9.5">Puissance charge / décharge</font><br/><b>{fmt(b.power_kw, 1)} kW</b>', styles),
        _para(f'<font color="#667583" size="9.5">Rendement aller-retour</font><br/><b>{pct(b.efficiency * 100)}</b>', styles),
    ]], colWidths=[WIDTH / 3] * 3)
    parameters.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [parameters, Spacer(1, 8)]
    story += _section(8, "Vos indicateurs avec une batterie", styles, gap=5)
    story += [_BatteryIndicators(data, styles), Spacer(1, 8)]
    story += _section(9, "Moins d'échanges avec le réseau", styles, gap=5)
    story += [_NetworkComparison(data), Spacer(1, 8)]
    story += _section(10, "Votre surplus solaire valorisé", styles, gap=5)
    balance = [("Surplus solaire envoyé dans la batterie", b.charge_kwh),
               ("<b>Énergie restituée pour vos besoins</b>", b.discharge_kwh),
               ("Pertes liées au stockage", b.losses_kwh)]
    if b.final_stock_kwh > 1e-6:
        balance.append(("Énergie encore stockée en fin de période", b.final_stock_kwh))
    if b.untransferred_stock_kwh > 1e-6:
        balance.append(("Stock non valorisé dans la simulation", b.untransferred_stock_kwh))
    rows = [[_para(label, styles, "cell"),
             _para(("<b>" if index == 1 else "") + fmt(value) + " kWh" + ("</b>" if index == 1 else ""), styles, "number")]
            for index, (label, value) in enumerate(balance)]
    table = Table(rows, colWidths=[WIDTH * .76, WIDTH * .24])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), .5, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [table, Spacer(1, 8)]
    conclusion = (f"La batterie vous permettrait d'utiliser <b>{fmt(b.discharge_kwh)} kWh de solaire supplémentaires</b> "
                  f"pour vos besoins et de couvrir <b>{pct(data.after.self_sufficiency_pct)} de votre consommation</b> avec votre installation.")
    if b.discharge_kwh <= 1e-6:
        conclusion = "Avec les paramètres choisis, la simulation ne montre pas de réduction du soutirage réseau sur cette période."
    note = (f"Simulation à partir des courbes de charge, au pas de {fmt(data.dt_minutes, 1)} minutes. "
            "L'autonomie compte l'énergie réellement restituée ; l'autoconsommation inclut les pertes et la variation de stock.")
    box = Table([[_para(conclusion, styles)]], colWidths=[WIDTH])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [KeepTogether(_section(11, "Ce que cela change pour vous", styles, gap=5) +
                          [box, Spacer(1, 5), _para(note, styles, "small")])]
    return story


def build_report(data: ReportData) -> bytes:
    """Deux pages mesurées, puis une page de simulation si une batterie est fournie."""
    simulation_data = data
    data = replace(data, after=data.base, battery=None)
    _fonts()
    styles, buffer = _styles(), BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, title="SOLEOL - Autoconsommation et autonomie",
                          author="SOLEOL", pageCompression=1)
    doc.subject = "Sources : " + " ; ".join(_clean(s) for s in data.source_files)
    frame_top = PAGE_H-MARGIN-70
    frame = Frame(MARGIN,MARGIN,WIDTH,frame_top-MARGIN,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="soleol", frames=[frame], onPage=lambda c,d:_header(c,d,data)),
        PageTemplate(id="battery", frames=[frame], onPage=lambda c,d:_header(c,d,simulation_data)),
    ])
    story = [_para("Autoconsommation<br/>et autonomie énergétique.",styles,"title"),Spacer(1,15)]
    story += _identity_story(data, styles) + [Spacer(1,17)]
    story += _section(1,"Vos deux indicateurs",styles)
    story += [_Indicators(data,styles),Spacer(1,17)]
    if data.existing_battery:
        story += [_para("Indicateurs apparents : le fonctionnement du stockage existant est inclus dans les mesures.",styles,"small"),Spacer(1,8)]
    story += _section(2,"Vos énergies sur les 12 mois",styles)
    story += [_MonthlyHistogram(data.months),Spacer(1,8)]
    story += _section(3,"Ce que cela signifie pour vous",styles)
    if data.existing_battery:
        conclusion = "Ces taux décrivent le bilan apparent de votre installation. Les seuls échanges du compteur ne permettent pas d'isoler les pertes ni l'origine de charge d'une batterie déjà en service."
        story += [_para(conclusion, styles)]
    else:
        used = data.after.self_consumption_pct
        autonomy = data.after.self_sufficiency_pct
        conclusion = (f"<b>Vous consommez directement chez vous {pct(used)} de l'électricité "
                      "produite par vos panneaux solaires.</b>")
        coverage = (f"Votre installation solaire fournit {pct(autonomy)} de toute l'électricité que vous consommez. "
                    f"Les {pct(100-autonomy)} restants sont prélevés sur le réseau électrique.")
        explanation = ("Lorsque vos panneaux produisent plus d'électricité que ce dont vous avez besoin, "
                       "le surplus est envoyé sur le réseau. Lorsque leur production est insuffisante, "
                       "notamment la nuit, le réseau complète vos besoins.")
        story += [_para(conclusion, styles), Spacer(1, 8),
                  _para(coverage, styles), Spacer(1, 8), _para(explanation, styles)]
    story += [PageBreak(),_para("Votre bilan énergétique.",styles,"title_small"),Spacer(1,15)]
    story += _section(4,"Les énergies de la période",styles)
    header = ["Énergie (kWh)","Situation<br/>mesurée"]
    rows = [header]
    for label,before,after in [
        ("Production solaire",data.base.production_kwh,data.after.production_kwh),
        ("Soutirage réseau",data.base.import_kwh,data.after.import_kwh),
        ("Export réseau",data.base.export_kwh,data.after.export_kwh),
        ("Solaire utilisé sur place",data.base.solar_used_kwh,data.after.solar_used_kwh),
        ("Consommation du bâtiment",data.base.consumption_kwh,data.after.consumption_kwh)]:
        rows.append([label,fmt(before) + " kWh"])
    widths = [WIDTH*.64,WIDTH*.36]
    story += [_table(rows,widths,styles),Spacer(1,13)]
    story += _section(5,"Comprendre les deux calculs",styles)
    retained = data.after.solar_retained_kwh
    useful = data.after.solar_used_kwh
    suffix = " apparente" if data.existing_battery else ""
    self_consumption = (f"{fmt(retained)} kWh consommés sur place ÷ {fmt(data.after.production_kwh)} kWh produits "
                        f"= <b>{pct(data.after.self_consumption_pct)}</b>.")
    self_sufficiency = (f"{fmt(useful)} kWh de solaire ÷ {fmt(data.after.consumption_kwh)} kWh consommés "
                        f"= <b>{pct(data.after.self_sufficiency_pct)}</b>.")
    story += [_para(f"<b>Autoconsommation{suffix}</b>", styles, "calculation"), Spacer(1, 6),
              _para(self_consumption, styles, "calculation"), Spacer(1, 13),
              _para(f"<b>Autonomie énergétique{suffix}</b>", styles, "calculation"), Spacer(1, 6),
              _para(self_sufficiency, styles, "calculation"), Spacer(1, 14)]
    story += _section(6,"Récapitulatif mensuel",styles)
    monthly = {m.month:m for m in data.months}
    rows = [["Mois","Production<br/>solaire (kWh)","Soutirage<br/>réseau (kWh)","Export<br/>réseau (kWh)"]]
    for number,label in enumerate(MONTHS_FR,1):
        m = monthly.get(number)
        values = [getattr(m,key) if m else None for key in ("production_kwh","import_kwh","export_kwh")]
        rows.append([label]+[fmt(v) if v is not None else "-" for v in values])
    rows.append(["Total",fmt(data.base.production_kwh),fmt(data.base.import_kwh),fmt(data.base.export_kwh)])
    story += [_table(rows,[WIDTH*.28,WIDTH*.24,WIDTH*.24,WIDTH*.24],styles,total=True),Spacer(1,7),
              _para("Les fragments d'un même mois sont regroupés sur la période affichée. Valeurs arrondies au kWh, sans extrapolation des flux réseau.",styles,"small")]
    if simulation_data.battery:
        story += [NextPageTemplate("battery"), PageBreak()]
        story += _battery_page(simulation_data, styles)
    doc.build(story,canvasmaker=lambda *a,**kw:_NumberedCanvas(*a,report_data=simulation_data,**kw))
    return buffer.getvalue()


