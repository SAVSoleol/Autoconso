"""Rapport SOLEOL : autoconsommation, autonomie et flux mensuels.

Génération locale en mémoire, à partir des résultats du calculateur. Aucune
valeur de la maquette n'est codée en dur. Aucun navigateur ni réseau requis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from energy_metrics import EnergyMetrics, MONTHS_FR

INK = colors.HexColor("#15344d")
MUTED = colors.HexColor("#667583")
LINE = colors.HexColor("#dce4e8")
SOFT = colors.HexColor("#f3f6f7")
GREEN = colors.HexColor("#e45638")
GREEN_SOFT = colors.HexColor("#fff3ef")
BLUE = colors.HexColor("#447bd4")
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


def _section(number, title, styles):
    return [_para(f'<font color="#e45638">{number}.</font>  {title}', styles, "section"), Spacer(1, 8)]


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
            self.drawString(MARGIN, MARGIN-21, "SOLEOL · Autoconsommation & autonomie · " + self._report_data.nature)
            self.drawRightString(PAGE_W - MARGIN, MARGIN-21, f"{self._pageNumber:02d} / {count:02d}")
            super().showPage()
        super().save()


def _header(c, doc, data):
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
        self.width, self.height = WIDTH, 100 if data.battery else 87
        self.data, self.styles = data, styles

    def draw(self):
        c, d = self.canv, self.data
        width = (WIDTH - 12)/2
        for i, (title, definition, current, before) in enumerate([
            ("Autoconsommation", "Part de votre production solaire utilisée sur place.", d.after.self_consumption_pct, d.base.self_consumption_pct),
            ("Autonomie énergétique", "Part de votre consommation couverte par votre solaire.", d.after.self_sufficiency_pct, d.base.self_sufficiency_pct),
        ]):
            x = i*(width+12)
            c.setStrokeColor(LINE)
            c.setLineWidth(.7)
            c.roundRect(x, 0, width, self.height, 5, stroke=1, fill=0)
            c.setFillColor(INK)
            c.setFont("Soleol-Bold", 12)
            c.drawString(x+12, self.height-22, title)
            p = _para(definition, self.styles, "small")
            _, h = p.wrap(width-24, 35)
            p.drawOn(c, x+12, self.height-30-h)
            c.setFillColor(ORANGE)
            c.setFont("Soleol-Bold", 26)
            c.drawString(x+12, 12 if not d.battery else 28, pct(current))
            if d.battery:
                c.setFont("Soleol", 9.5)
                c.setFillColor(MUTED)
                c.drawString(x+12, 11, "Situation mesurée : " + pct(before))


class _MonthlyHistogram(Flowable):
    """Histogramme vectoriel : 12 mois, 3 barres par mois, échelle en kWh."""
    def __init__(self, months):
        super().__init__()
        self.width, self.height, self.months = WIDTH, 166, months

    def draw(self):
        c = self.canv
        series = [("Production solaire", "production_kwh", colors.HexColor("#2e9f57")),
                  ("Soutirage réseau", "import_kwh", BLUE),
                  ("Export réseau", "export_kwh", colors.HexColor("#e7b91b"))]
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


def build_report(data: ReportData) -> bytes:
    """Bilan général de l'installation, comparaison de batterie facultative."""
    _fonts()
    styles, buffer = _styles(), BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, title="SOLEOL - Autoconsommation et autonomie",
                          author="SOLEOL", pageCompression=1)
    doc.subject = "Sources : " + " ; ".join(_clean(s) for s in data.source_files)
    frame_top = PAGE_H-MARGIN-70
    frame = Frame(MARGIN,MARGIN,WIDTH,frame_top-MARGIN,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="soleol",frames=[frame],onPage=lambda c,d:_header(c,d,data)))
    story = [_para("Autoconsommation<br/>et autonomie énergétique.",styles,"title"),Spacer(1,15)]
    identity = f"<b>{_safe(data.client_name or 'Installation photovoltaïque')}</b>"
    if data.site_address:
        identity += "<br/>"+_safe(data.site_address)
    if data.reference:
        identity += "<br/>Référence : "+_safe(data.reference)
    identity += "<br/>Période étudiée : <b>"+_safe(data.period_label)+"</b>"
    story += [_para(identity,styles),Spacer(1,17)]
    story += _section(1,"Vos deux indicateurs",styles)
    story += [_Indicators(data,styles),Spacer(1,17)]
    if data.existing_battery:
        story += [_para("Indicateurs apparents : le fonctionnement du stockage existant est inclus dans les mesures.",styles,"small"),Spacer(1,8)]
    story += _section(2,"Vos énergies sur les 12 mois",styles)
    story += [_MonthlyHistogram(data.months),Spacer(1,8)]
    story += _section(3,"Ce que cela signifie pour vous",styles)
    if data.existing_battery:
        conclusion = "Ces taux décrivent le bilan apparent de votre installation. Les seuls échanges du compteur ne permettent pas d'isoler les pertes ni l'origine de charge d'une batterie déjà en service."
    else:
        used = data.after.self_consumption_pct
        autonomy = data.after.self_sufficiency_pct
        conclusion = (f"<b>Vous conservez sur place {pct(used)} de votre production solaire.</b><br/>"
                      f"Votre solaire couvre {pct(autonomy)} de vos besoins. Le réseau fournit les {pct(100-autonomy)} restants.")
    story += [_para(conclusion,styles)]
    if data.battery:
        b = data.battery
        story += [Spacer(1,9),_para(f"Comparaison facultative : batterie de {fmt(b.capacity_kwh)} kWh utiles, {fmt(b.power_kw,1)} kW, rendement {fmt(b.efficiency*100)} %. Situation mesurée : {pct(data.base.self_consumption_pct)} d'autoconsommation et {pct(data.base.self_sufficiency_pct)} d'autonomie. Pertes de stockage simulées : {fmt(b.losses_kwh)} kWh.",styles,"small")]
    story += [PageBreak(),_para("Votre bilan énergétique.",styles,"title_small"),Spacer(1,15)]
    story += _section(4,"Les énergies de la période",styles)
    header = ["Énergie (kWh)","Situation<br/>mesurée"] + (["Avec batterie<br/>simulée"] if data.battery else [])
    rows = [header]
    for label,before,after in [
        ("Production solaire",data.base.production_kwh,data.after.production_kwh),
        ("Soutirage réseau",data.base.import_kwh,data.after.import_kwh),
        ("Export réseau",data.base.export_kwh,data.after.export_kwh),
        ("Solaire utilisé sur place",data.base.solar_used_kwh,data.after.solar_used_kwh),
        ("Consommation du bâtiment",data.base.consumption_kwh,data.after.consumption_kwh)]:
        rows.append([label,fmt(before)]+([fmt(after)] if data.battery else []))
    widths = [WIDTH*.50,WIDTH*.25,WIDTH*.25] if data.battery else [WIDTH*.64,WIDTH*.36]
    story += [_table(rows,widths,styles),Spacer(1,13)]
    story += _section(5,"Comprendre les deux calculs",styles)
    retained = data.after.solar_retained_kwh
    useful = data.after.solar_used_kwh
    suffix = " apparente" if data.existing_battery else ""
    defs = (f"<b>Autoconsommation{suffix}</b><br/>{fmt(retained)} kWh conservés sur place ÷ {fmt(data.after.production_kwh)} kWh produits = <b>{pct(data.after.self_consumption_pct)}</b>.<br/><br/>"
            f"<b>Autonomie énergétique{suffix}</b><br/>{fmt(useful)} kWh de solaire ÷ {fmt(data.after.consumption_kwh)} kWh consommés = <b>{pct(data.after.self_sufficiency_pct)}</b>.")
    story += [_para(defs,styles,"calculation"),Spacer(1,14)]
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
    doc.build(story,canvasmaker=lambda *a,**kw:_NumberedCanvas(*a,report_data=data,**kw))
    return buffer.getvalue()


