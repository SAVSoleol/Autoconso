"""Lecture des compteurs, contrôles de qualité et normalisation en kWh par intervalle.

Contrat : timestamp = début d'intervalle UTC avec fuseau ; pas constant ;
une valeur inconnue reste NaN. Les traitements des trous sont explicites.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
import re
import unicodedata

import numpy as np
import pandas as pd

TIMEZONE = "Europe/Zurich"
STD_COLS = ["timestamp", "import_kWh", "export_kWh"]
UNIT_OPTIONS = ("auto", "kWh", "kW", "Wh", "W")


class UnsupportedFormatError(ValueError):
    """Format, unité ou chronologie insuffisamment déterminés."""


@dataclass
class Meta:
    vendor: str
    dt_hours: float
    n_rows: int
    coverage_days: float
    source: str
    data_unit: str = "kWh"
    source_rows: int = 0
    invalid_rows: int = 0
    absent_rows: int = 0
    valid_rows: int = 0
    estimated_rows: int = 0
    completeness: float = 0.0
    start: str = ""
    end: str = ""
    timestamp_position: str = "end"
    warnings: list[str] = field(default_factory=list)
    missing_periods: list[dict] = field(default_factory=list)
    annualization_allowed: bool = False
    complete_year: bool = False
    missing_policy: str = "block"
    fingerprint: str = ""
    blank_zero_cells: int = 0


def _norm(value) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(value).lower().strip())
                   if not unicodedata.combining(c))


def _find_col(cols, *tokens):
    return next((c for c in cols if all(t in _norm(c) for t in tokens)), None)


def _numeric(series: pd.Series) -> pd.Series:
    """Décimales point/virgule et séparateurs de milliers suisses/français.

Une virgule seule est décimale. Si point et virgule coexistent, le dernier
séparateur est décimal. Les valeurs illisibles et infinies restent inconnues.
"""
    def clean(x):
        if not isinstance(x, str):
            return x
        x = re.sub(r"[\s'’]", "", x)
        if "," in x and "." in x:
            if x.rfind(",") > x.rfind("."):
                x = x.replace(".", "").replace(",", ".")
            else:
                x = x.replace(",", "")
        else:
            x = x.replace(",", ".")
        return x
    return pd.to_numeric(series.map(clean), errors="coerce").replace([np.inf, -np.inf], np.nan)


def _empty_text(series: pd.Series) -> pd.Series:
    """True empty source text only; NA/error markers are never empty cells."""
    return series.map(lambda value: isinstance(value, str) and not value.strip())


def _parse_datetime(series: pd.Series, dayfirst=True, ambiguous_policy="auto") -> pd.Series:
    """Localise les heures suisses ; conserve les offsets déjà fournis.

L'ordre source permet de distinguer les deux occurrences de l'heure d'automne.
En mode auto, une occurrence isolée est affectée à l'heure d'été et comptée
comme hypothèse. Les modes raise/daylight/standard restent disponibles.
"""
    if ambiguous_policy not in {"auto", "raise", "daylight", "standard"}:
        raise UnsupportedFormatError("Choix d'heure d'automne invalide.")
    s = series.astype(str).str.strip()
    for label, offset in (("CEST", "+02:00"), ("DST", "+02:00"),
                          ("CET", "+01:00"), ("ST", "+01:00"), ("UTC", "+00:00")):
        s = s.str.replace(rf"\s+{label}$", offset, regex=True)
    explicit = s.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns, UTC]")
    assumed = 0
    if explicit.any():
        out.loc[explicit] = pd.to_datetime(s[explicit], errors="coerce", format="mixed",
                                          dayfirst=dayfirst, utc=True)
    if (~explicit).any():
        local = pd.to_datetime(s[~explicit], errors="coerce", format="mixed", dayfirst=dayfirst)
        idx = pd.DatetimeIndex(local)
        try:
            localized = idx.tz_localize(TIMEZONE, ambiguous="infer", nonexistent="raise")
        except Exception as error:
            # First check nonexistent spring labels: shifting them would merge energy.
            try:
                probe = idx.tz_localize(TIMEZONE, ambiguous="NaT", nonexistent="raise")
            except Exception as spring_error:
                raise UnsupportedFormatError(
                    "Heure locale inexistante au passage à l'heure d'été. "
                    "Vérifier le fuseau ou la convention de l'export ; aucun décalage automatique."
                ) from spring_error
            ambiguous = probe.isna() & ~idx.isna()
            flags = np.zeros(len(idx), dtype=bool)
            for label in idx[ambiguous].unique():
                positions = np.flatnonzero(idx == label)
                if len(positions) == 2:
                    flags[positions] = [True, False]
                elif len(positions) == 1 and ambiguous_policy != "raise":
                    flags[positions] = ambiguous_policy in {"auto", "daylight"}
                    assumed += 1
                elif len(positions) > 2:
                    raise UnsupportedFormatError(
                        "Plus de deux occurrences du même horaire d'automne : "
                        "vérifier les doublons de l'export."
                    ) from error
                else:
                    raise UnsupportedFormatError(
                        "Heure d'automne ambiguë : l'export ne distingue pas les deux occurrences "
                        "de 02h. Choisir explicitement la première (été) ou la seconde (hiver), "
                        "ou fournir un export avec offsets UTC."
                    ) from error
            localized = idx.tz_localize(TIMEZONE, ambiguous=flags, nonexistent="raise")
        out.loc[~explicit] = pd.Series(localized.tz_convert("UTC"), index=local.index)
    out.attrs["ambiguous_assumed"] = assumed
    return out


def _infer_dt_hours(ts: pd.Series, interval_minutes=None) -> float:
    idx = pd.DatetimeIndex(ts).sort_values()
    if idx.has_duplicates:
        raise UnsupportedFormatError("Intervalles qui se chevauchent : mêmes instants UTC.")
    diffs = np.diff(idx.asi8) / 3.6e12
    if interval_minutes is not None:
        dt = float(interval_minutes) / 60
    elif len(diffs):
        values, counts = np.unique(np.round(diffs, 9), return_counts=True)
        dt = float(values[np.argmax(counts)])
        if counts.max() / len(diffs) < .60:
            raise UnsupportedFormatError("Pas de temps indéterminé ou variable ; préciser le pas du compteur.")
    else:
        raise UnsupportedFormatError("Au moins deux horodatages ou un pas explicite sont nécessaires.")
    if not np.isfinite(dt) or not 0 < dt <= 24:
        raise UnsupportedFormatError("Pas de temps invalide (attendu : plus de 0 à 1 440 minutes).")
    if len(diffs) and not np.allclose(diffs / dt, np.round(diffs / dt), atol=1e-6, rtol=0):
        raise UnsupportedFormatError("Pas de temps incompatibles ou non alignés. Fournir un export à pas constant.")
    return dt


def _unit(cols, fallback=None):
    units = []
    for col in cols:
        match = re.search(r"(?<![a-z])(kwh|wh|kw|w)(?![a-z])", _norm(col))
        if match:
            units.append({"kwh": "kWh", "wh": "Wh", "kw": "kW", "w": "W"}[match[1]])
    if len(set(units)) > 1:
        raise UnsupportedFormatError("Les colonnes import et export n'ont pas la même unité.")
    return units[0] if units else fallback


def _generic_cols(cols):
    def choose(groups):
        return next((c for group in groups if (c := _find_col(cols, *group)) is not None), None)
    date = choose([("date",), ("time",), ("horodat",), ("heure",)])
    imp = choose([("- import -",), ("soutirage",), ("provenant", "reseau"),
                  ("import",), ("prelev",), ("achat",)])
    exp = choose([("- export -",), ("surplus",), ("excedent",), ("inject", "reseau"),
                  ("export",), ("refoul",), ("injection",), ("revente",)])
    # A consumption column alone is not proof of grid import. RE is handled explicitly.
    return date, imp, exp


def _columns(cols):
    date, imp, exp = _generic_cols(cols)
    cumulative = _find_col(cols, "negativ") is not None and _find_col(cols, "positiv") is not None
    if cumulative:
        date = _find_col(cols, "heure", "debut") or date
        return date, _find_col(cols, "negativ"), _find_col(cols, "positiv"), "huawei"
    if _find_col(cols, "consommation") is not None and _find_col(cols, "excedent") is not None:
        return date, _find_col(cols, "consommation"), _find_col(cols, "excedent"), "romande_energie"
    blob = " | ".join(_norm(c) for c in cols)
    vendor = "generic"
    if "soutirage" in blob and "surplus" in blob:
        vendor = "groupe_e"
    elif "provenant du reseau" in blob and "injectee dans le reseau" in blob:
        vendor = "fronius"
    elif "- import -" in blob and "- export -" in blob:
        vendor = "solaredge"
    return date, imp, exp, vendor


def _read_table(path: Path):
    if path.suffix.lower() in {".xlsx", ".xls"}:
        matches = []
        with pd.ExcelFile(path) as book:
            for sheet in book.sheet_names:
                preview = pd.read_excel(book, sheet_name=sheet, header=None, nrows=30)
                for row in range(len(preview)):
                    cols = preview.iloc[row].dropna().tolist()
                    date, imp, exp, vendor = _columns(cols)
                    if date is not None and imp is not None and exp is not None and imp != exp:
                        labels = {_norm(v) for v in preview.iloc[:row, 0].dropna()}
                        meter_template = {
                            "numero de compteur actuel", "adresse du lieu de consommation",
                            "designation de l'objet",
                        }.issubset(labels)
                        matches.append((sheet, row, vendor, meter_template))
                        break
            if len(matches) != 1:
                raise UnsupportedFormatError(
                    f"{path.name} : {len(matches)} feuille(s) de mesures identifiée(s). "
                    "Fournir une seule feuille Date + Import réseau + Export réseau."
                )
            sheet, row, _, meter_template = matches[0]
            frame = pd.read_excel(book, sheet_name=sheet, header=row, dtype=object, keep_default_na=False)
            frame.attrs["groupe_e_meter_template"] = meter_template
            return frame
    if path.suffix.lower() == ".csv":
        try:
            return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        except UnicodeDecodeError:
            return pd.read_csv(path, sep=None, engine="python", encoding="cp1252", dtype=str, keep_default_na=False)
    raise UnsupportedFormatError(f"Extension non prise en charge : {path.suffix}")


def _read_raw(path: Path, data_unit, ambiguous_policy, blank_policy="auto"):
    raw = _read_table(path)
    meter_template = raw.attrs.get("groupe_e_meter_template", False)
    date, imp, exp, vendor = _columns(raw.columns)
    if any(c is None for c in (date, imp, exp)) or imp == exp:
        raise UnsupportedFormatError("Colonnes import/export réseau absentes ou ambiguës (consommation totale ≠ import).")
    # Unit-only rows and entirely empty footers are metadata; other bad dates are rejected.
    labels = raw[date].astype(str).str.strip()
    unit_row = labels.str.fullmatch(r"\[?(?:Wh|kWh|kW|W)\]?", case=False, na=False)
    empty_row = pd.DataFrame({c: raw[c].isna() | _empty_text(raw[c]) for c in (date, imp, exp)}).all(axis=1)
    empty_date = raw[date].isna() | _empty_text(raw[date])
    units_only = raw[imp].astype(str).str.fullmatch(r"\[?(?:Wh|kWh|kW|W)\]?", case=False, na=False)
    raw = raw.loc[~(empty_row | unit_row | (empty_date & units_only))].reset_index(drop=True)
    dev_col = _find_col(raw.columns, "appareil")
    devices = raw[dev_col].fillna("inconnu").astype(str) if dev_col else pd.Series("single", index=raw.index)
    ts = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns, UTC]")
    assumed = 0
    for _, indices in devices.groupby(devices).groups.items():
        parsed = _parse_datetime(raw.loc[indices, date], ambiguous_policy=ambiguous_policy)
        assumed += parsed.attrs.get("ambiguous_assumed", 0)
        ts.loc[indices] = parsed
    if ts.isna().any():
        raise UnsupportedFormatError(f"{path.name} : {int(ts.isna().sum())} date(s) illisible(s), à corriger.")
    fallback = {"groupe_e": "kW" if path.suffix.lower() != ".csv" else "Wh",
                "huawei": "kWh", "fronius": "Wh", "solaredge": "Wh", "romande_energie": "kWh"}.get(vendor)
    if data_unit not in UNIT_OPTIONS:
        raise UnsupportedFormatError("Unité attendue : auto, kWh, Wh, kW ou W.")
    unit = _unit([imp, exp], fallback) if data_unit == "auto" else data_unit
    if unit is None:
        raise UnsupportedFormatError("Unité absente des en-têtes : choisir kWh, Wh, kW ou W dans les paramètres.")
    if vendor == "huawei" and unit not in {"kWh", "Wh"}:
        raise UnsupportedFormatError("Les index cumulés Huawei sont des énergies, pas des puissances.")
    if blank_policy not in {"auto", "unknown"}:
        raise UnsupportedFormatError("Convention des cellules vides inconnue.")
    import_values, export_values = _numeric(raw[imp]), _numeric(raw[exp])
    zero_cells = 0
    # Native Groupe E template: one-sided empty fields can encode a zero flow.
    # Apply this documented assumption only to true blanks with a known opposite
    # flow. Both-empty rows, error strings, infinities and negative values stay unknown.
    if blank_policy == "auto" and vendor == "groupe_e" and meter_template:
        import_zero = _empty_text(raw[imp]) & export_values.ge(0)
        export_zero = _empty_text(raw[exp]) & import_values.ge(0)
        zero_cells = int(import_zero.sum() + export_zero.sum())
        import_values = import_values.mask(import_zero, 0.)
        export_values = export_values.mask(export_zero, 0.)
    work = pd.DataFrame({"timestamp": ts, "import_kWh": import_values,
                         "export_kWh": export_values, "device": devices})
    work.attrs.update(vendor=vendor, data_unit=unit, source=path.name, ambiguous_assumed=assumed,
                      blank_zero_cells=zero_cells)
    return work


def missing_periods(frame, dt_hours):
    bad = ~frame["valid"].to_numpy(bool)
    starts = np.flatnonzero(bad & ~np.r_[False, bad[:-1]])
    ends = np.flatnonzero(bad & ~np.r_[bad[1:], False])
    step = pd.Timedelta(hours=dt_hours)
    return [{"debut": frame.timestamp.iloc[a].tz_convert(TIMEZONE).isoformat(),
             "fin_exclue": (frame.timestamp.iloc[b] + step).tz_convert(TIMEZONE).isoformat(),
             "intervalles": int(b - a + 1), "heures": float((b - a + 1) * dt_hours),
             "valeurs_invalides": int(frame.source_present.iloc[a:b+1].sum()),
             "lignes_absentes": int((~frame.source_present.iloc[a:b+1]).sum())}
            for a, b in zip(starts, ends)]


def _finalize(df, vendor, source, data_unit="kWh", default_unit=None, *,
              timestamp_position="start", interval_minutes=None, notes=None, fingerprint=""):
    if timestamp_position not in {"start", "end"}:
        raise UnsupportedFormatError("Préciser si l'heure représente le début ou la fin de l'intervalle.")
    work = df.copy().sort_values("timestamp", kind="stable").reset_index(drop=True)
    if work.empty:
        raise UnsupportedFormatError("Fichier sans mesures.")
    if pd.DatetimeIndex(work.timestamp).tz is None:
        raise UnsupportedFormatError("Fuseau horaire requis avant normalisation.")
    work["timestamp"] = pd.to_datetime(work.timestamp, utc=True)
    dt = _infer_dt_hours(work.timestamp, interval_minutes)
    step = pd.Timedelta(hours=dt)
    if timestamp_position == "end":
        work["timestamp"] -= step
    effective = default_unit if data_unit == "auto" else data_unit
    if effective not in UNIT_OPTIONS[1:]:
        raise UnsupportedFormatError("Unité non déterminée.")
    factor = {"kWh": 1, "Wh": .001, "kW": dt, "W": dt / 1000}[effective]
    for col in STD_COLS[1:]:
        work[col] = _numeric(work[col]) * factor
        work.loc[work[col] < 0, col] = np.nan
    invalid = int(work[STD_COLS[1:]].isna().any(axis=1).sum())
    work["source_present"] = True
    expected = int(round((work.timestamp.iloc[-1] - work.timestamp.iloc[0]) / step)) + 1
    if expected > 2_000_000:
        raise UnsupportedFormatError("Période trop longue pour ce pas de temps (limite : 2 millions d'intervalles).")
    timeline = pd.date_range(work.timestamp.iloc[0], periods=expected, freq=step)
    work = work.set_index("timestamp").reindex(timeline).rename_axis("timestamp").reset_index()
    work["source_present"] = work["source_present"].eq(True)
    work["valid"] = work[STD_COLS[1:]].notna().all(axis=1)
    work["estimated"] = False
    valid = int(work.valid.sum())
    absent = int((~work.source_present).sum())
    local = work.timestamp.dt.tz_convert(TIMEZONE)
    months = local.dt.tz_localize(None).dt.to_period("M")
    monthly_quality = work.valid.groupby(months).mean()
    days = expected * dt / 24
    start, end = local.iloc[0], (work.timestamp.iloc[-1] + step).tz_convert(TIMEZONE)
    whole_year = (start.month, start.day, start.hour, start.minute, start.second) == (1, 1, 0, 0, 0) and (
        end.month, end.day, end.hour, end.minute, end.second) == (1, 1, 0, 0, 0) and end.year > start.year
    messages = list(notes or [])
    if invalid or absent:
        messages.append(f"{invalid} ligne(s) avec valeurs inconnues et {absent} intervalle(s) absent(s) ; les valeurs invalides et les intervalles absents restent inconnus.")
    both = int(((work.import_kWh > 0) & (work.export_kWh > 0)).sum())
    if both:
        messages.append(f"{both} intervalle(s) avec import et export : leur ordre à l'intérieur de l'intervalle est inconnu.")
    meta = Meta(vendor, dt, expected, days, source, effective, len(df), invalid, absent, valid,
                completeness=valid / expected, start=start.isoformat(), end=end.isoformat(),
                timestamp_position=timestamp_position, warnings=messages,
                missing_periods=missing_periods(work, dt), fingerprint=fingerprint,
                annualization_allowed=days >= 330 and valid / expected >= .98 and
                    len(set(months.dt.month)) == 12 and monthly_quality.min() >= .90,
                complete_year=bool(whole_year and valid == expected))
    return work, meta


def load_meter_file(path, data_unit="auto", *, timestamp_position="end",
                    ambiguous_policy="auto", interval_minutes=None, aggregate_devices=False, blank_policy="auto"):
    return load_meter_files([path], data_unit=data_unit, timestamp_position=timestamp_position,
                            ambiguous_policy=ambiguous_policy, interval_minutes=interval_minutes,
                            aggregate_devices=aggregate_devices, blank_policy=blank_policy)


def load_meter_files(paths, data_unit="auto", *, timestamp_position="end", ambiguous_policy="auto",
                     interval_minutes=None, same_meter=False, aggregate_devices=False, blank_policy="auto"):
    paths = [Path(p) for p in paths]
    if not paths:
        raise UnsupportedFormatError("Aucun fichier.")
    if len(paths) > 1 and not same_meter:
        raise UnsupportedFormatError("La combinaison exige des périodes du même point de mesure, à confirmer.")
    hashes = [sha256(p.read_bytes()).hexdigest() for p in paths]
    if len(set(hashes)) != len(hashes):
        raise UnsupportedFormatError("Le même contenu a été fourni plusieurs fois, même sous un autre nom.")
    raws = [_read_raw(p, data_unit, ambiguous_policy, blank_policy) for p in paths]
    vendors = {r.attrs["vendor"] for r in raws}
    notes = []
    zero_cells = sum(r.attrs.get("blank_zero_cells", 0) for r in raws)
    if zero_cells:
        notes.append(
            f"Export Groupe E : {zero_cells} cellule(s) vide(s) interprétée(s) comme un flux nul "
            "lorsque l'autre flux est mesuré. Convention de lecture supposée, désactivable dans "
            "les réglages avancés. Les valeurs Erroné/Manquant et les lignes avec deux flux vides restent inconnues."
        )
    assumed = sum(r.attrs.get("ambiguous_assumed", 0) for r in raws)
    if assumed:
        if ambiguous_policy == "auto":
            notes.append(
                f"{assumed} horodatage(s) d'automne ambigu(s) : convention automatique, "
                "première occurrence (été). L'export ne permet pas de confirmer ce choix ; "
                "les mesures absentes restent inconnues."
            )
        else:
            notes.append(f"{assumed} horodatage(s) d'automne ambigu(s) : hypothèse explicite " +
                         ("première occurrence (été)." if ambiguous_policy == "daylight" else "seconde occurrence (hiver)."))
    source = "; ".join(p.name for p in paths)
    fingerprint = sha256("|".join(hashes).encode()).hexdigest()
    if "huawei" in vendors:
        if vendors != {"huawei"}:
            raise UnsupportedFormatError("Ne pas combiner index cumulés et énergies par intervalle.")
        for raw in raws:
            if raw.attrs["data_unit"] == "Wh":
                raw[STD_COLS[1:]] = raw[STD_COLS[1:]] / 1000
        # Join index readings first: monthly boundaries must not lose their delta.
        joined = pd.concat(raws, ignore_index=True).sort_values(["device", "timestamp"], kind="stable")
        device_count = joined.device.nunique()
        if device_count > 1 and not aggregate_devices:
            raise UnsupportedFormatError("Plusieurs appareils Huawei détectés : fournir un seul compteur ou choisir explicitement l'addition de compteurs distincts dans les paramètres.")
        if device_count > 1:
            notes.append(f"Addition explicite de {device_count} compteurs Huawei distincts, alignés en UTC. Un intervalle exige les mesures de chaque compteur.")
        duplicated = joined.duplicated(["device", "timestamp"], keep=False)
        if duplicated.any():
            counts = joined[duplicated].groupby(["device", "timestamp"])[STD_COLS[1:]].nunique(dropna=False)
            if (counts > 1).any().any():
                raise UnsupportedFormatError("Index Huawei contradictoires au même instant.")
            joined = joined.drop_duplicates(["device", "timestamp"])
            notes.append("Index Huawei identiques aux frontières de fichiers fusionnés avant calcul des différences.")
        times = joined.timestamp.drop_duplicates().sort_values()
        dt = _infer_dt_hours(times, interval_minutes)
        frames = []
        for _, group in joined.groupby("device"):
            g = group.copy()
            elapsed = g.timestamp.diff().dt.total_seconds() / 3600
            g[STD_COLS[1:]] = g[STD_COLS[1:]].diff()
            reset = (g[STD_COLS[1:]] < 0).any(axis=1)
            g.loc[~np.isclose(elapsed, dt) | reset, STD_COLS[1:]] = np.nan
            if reset.any():
                notes.append(f"Huawei : {int(reset.sum())} remise(s) à zéro d'index ; intervalle(s) laissé(s) inconnu(s).")
            frames.append(g.set_index("timestamp")[STD_COLS[1:]].reindex(times))
        # All devices must contribute at each timestamp; NaN never becomes zero.
        values = np.stack([f.to_numpy(float) for f in frames]).sum(axis=0)
        raw = pd.DataFrame(values, columns=STD_COLS[1:])
        raw.insert(0, "timestamp", times.to_numpy())
        notes.append("Index cumulés : différences entre lectures, affectées à l'intervalle se terminant à la lecture ; première différence inconnue.")
        return _finalize(raw, "huawei", source, timestamp_position="end", interval_minutes=dt * 60,
                         notes=notes, fingerprint=fingerprint)
    normalized = []
    steps = []
    units = []
    for raw in raws:
        dt = _infer_dt_hours(raw.timestamp, interval_minutes)
        steps.append(dt)
        unit = raw.attrs["data_unit"]
        units.append(unit)
        factor = {"kWh": 1, "Wh": .001, "kW": dt, "W": dt / 1000}[unit]
        raw = raw[STD_COLS].copy()
        raw[STD_COLS[1:]] *= factor
        normalized.append(raw)
    if not np.allclose(steps, steps[0]):
        raise UnsupportedFormatError("Pas de temps différents entre fichiers. Réexporter avec un pas commun.")
    joined = pd.concat(normalized, ignore_index=True)
    if joined.timestamp.duplicated().any():
        raise UnsupportedFormatError("Périodes qui se chevauchent : choisir un seul fichier ou retirer le recouvrement.")
    frame, meta = _finalize(joined, ", ".join(sorted(vendors)), source,
                            timestamp_position=timestamp_position, interval_minutes=steps[0] * 60,
                            notes=notes, fingerprint=fingerprint)
    meta.data_unit = "/".join(sorted(set(units)))
    meta.blank_zero_cells = zero_cells
    return frame, meta


def prepare_simulation_data(frame: pd.DataFrame, meta: Meta, policy="block"):
    """block / segments / estimate. La qualité mesurée originale reste inchangée."""
    if policy not in {"block", "segments", "estimate"}:
        raise ValueError("Politique de données manquantes inconnue.")
    work = frame.copy()
    notes = list(meta.warnings)
    bad = ~work.valid
    if bad.any() and policy == "block":
        raise UnsupportedFormatError("Données incomplètes : compléter l'export ou choisir un traitement explicite des trous.")
    if not work.valid.any():
        raise UnsupportedFormatError("Aucun intervalle entièrement mesuré.")
    if bad.any() and policy == "estimate":
        if meta.completeness < .95:
            raise UnsupportedFormatError("Estimation refusée : moins de 95 % des intervalles sont mesurés.")
        local = work.timestamp.dt.tz_convert(TIMEZONE)
        minute = local.dt.hour * 60 + local.dt.minute
        weekday = local.dt.weekday
        measured_indices = work.index[work.valid]
        groups = {}
        for index in measured_indices:
            groups.setdefault((int(weekday.iloc[index]), int(minute.iloc[index])), []).append(index)
        # Measured donors only, within +/- 28 days, same weekday and clock time.
        for i in work.index[bad]:
            indices = groups.get((int(weekday.iloc[i]), int(minute.iloc[i])), [])
            candidates = work.loc[indices].copy()
            distances = (candidates.timestamp - work.timestamp.iloc[i]).abs()
            candidates = candidates[(distances <= pd.Timedelta(days=28)) & (distances >= pd.Timedelta(hours=20))]
            candidates["day"] = local.loc[candidates.index].dt.date
            # A repeated autumn hour is a single donor day, not two independent days.
            donors = candidates.groupby("day")[STD_COLS[1:]].mean()
            if len(donors) < 3:
                raise UnsupportedFormatError("Estimation impossible : au moins trois jours comparables mesurés sont nécessaires par trou.")
            for col in STD_COLS[1:]:
                if pd.isna(work.loc[i, col]):
                    work.loc[i, col] = donors[col].median()
            work.loc[i, "estimated"] = True
        notes.append(f"{int(bad.sum())} intervalle(s) estimé(s) par médiane du même jour de semaine et horaire sur +/- 28 jours. Ce ne sont pas des mesures.")
    usable = work[STD_COLS[1:]].notna().all(axis=1)
    work["simulation_valid"] = usable
    work["reset_before"] = usable & ~usable.shift(1, fill_value=False)
    if bad.any() and policy == "segments":
        notes.append("Calcul limité aux segments mesurés : redémarrage au SOC minimum après chaque trou ; stock final du segment précédent non transféré.")
    return work, replace(meta, warnings=notes, estimated_rows=int(work.estimated.sum()), missing_policy=policy)
