"""Présentation du tableau de bord, fidèle à la maquette SOLEOL."""
from html import escape


def fmt(value, decimals=0):
    text = f"{float(value):,.{decimals}f}".replace(",", "’")
    return text.replace(".", ",")


def pct(value):
    return fmt(value, 1) + " %"


def header_html(period="", status="Données du compteur", scenario=""):
    return f"""<div class="pv-header">
    <div class="pv-eyebrow">Bilan énergétique · {escape(period or 'votre installation')}</div>
    <h1>Votre solaire, mieux utilisé.</h1>
    <p class="pv-description">Mesurez l'effet d'une batterie sur votre installation.</p>
    <div class="pv-status"><span>{escape(status)}</span><span>{escape(scenario)}</span></div>
    </div>"""


def _rate_card(title, description, before, after, color, *, simulated, existing):
    before_label = "Situation mesurée" if existing else "Sans batterie"
    after_label = "Avec batterie"
    comparison = f'<span class="pv-before">{pct(before)}</span><span class="pv-arrow">→</span>' if simulated else ""
    gain = f'<div class="pv-gain">+{fmt(after-before,1)} points avec batterie</div>' if simulated else '<div class="pv-measured">Bilan apparent du compteur</div>' if existing else '<div class="pv-measured">Situation mesurée sans batterie</div>'
    before_bar = f'<div class="pv-bar-label"><span>{before_label}</span><span>{pct(before)}</span></div><div class="pv-track"><div class="pv-fill pv-previous" style="width:{min(100,max(0,before)):.6f}%"></div></div>' if simulated else ""
    current_label = after_label if simulated else before_label
    return f"""<section class="pv-result"><h2>{title}</h2><p class="pv-subtitle">{description}</p>
    <div class="pv-comparison">{comparison}<span class="pv-after">{pct(after)}</span></div>{gain}
    {before_bar}<div class="pv-bar-label"><span>{current_label}</span><span>{pct(after)}</span></div>
    <div class="pv-track"><div class="pv-fill {color}" style="width:{min(100,max(0,after)):.6f}%"></div></div></section>"""


def dashboard_html(base, after, sim=None, *, existing=False, estimated=False):
    simulated = sim is not None
    consumption_label = "Consommation + pertes / stock" if existing else "Consommation du bâtiment"
    direct_label = "Solaire retenu sur site" if existing else "Solaire utilisé directement"
    summary = "".join(f'<article><p>{label}</p><div><strong>{fmt(value)}</strong><small>kWh</small></div></article>'
                      for label, value in [("Production photovoltaïque", base.production_kwh),
                                            (consumption_label, base.consumption_kwh),
                                            (direct_label, base.solar_used_kwh)])
    cards = _rate_card("Autoconsommation" + (" apparente" if existing else ""),
                       "Part de votre production solaire conservée sur site.",
                       base.self_consumption_pct, after.self_consumption_pct, "pv-solar",
                       simulated=simulated, existing=existing)
    cards += _rate_card("Autonomie énergétique" + (" apparente" if existing else ""),
                        "Part de votre consommation couverte par le solaire." if not existing else "Bilan incluant les pertes et la variation de stock non mesurées.",
                        base.self_sufficiency_pct, after.self_sufficiency_pct, "pv-battery",
                        simulated=simulated, existing=existing)
    delivered = sim.discharge_total_kWh if sim else 0.
    c = base.consumption_kwh
    direct = base.solar_used_kwh / c * 100 if c else 0.
    stored = delivered / c * 100 if c else 0.
    imported = after.import_kwh / c * 100 if c else 0.
    if c:
        segments = f'<div class="pv-solar" style="width:{direct:.6f}%"></div><div class="pv-battery" style="width:{stored:.6f}%"></div><div class="pv-grid" style="width:{imported:.6f}%"></div>'
        label = "Solaire retenu" if existing else "Solaire direct"
        legend = f'<span><i class="pv-key pv-solar"></i>{label} <strong>{pct(direct)}</strong></span>'
        if simulated:
            legend += f'<span><i class="pv-key pv-battery"></i>Via batterie <strong>{pct(stored)}</strong></span>'
        legend += f'<span><i class="pv-key pv-grid"></i>Réseau <strong>{pct(imported)}</strong></span>'
        energy_bar = f'<div class="pv-energy-bar" role="img" aria-label="{label} {pct(direct)}, batterie {pct(stored)}, réseau {pct(imported)}">{segments}</div><div class="pv-legend">{legend}</div>'
    else:
        energy_bar = '<p class="pv-description">Aucune consommation du bâtiment sur cette période.</p>'
    rows = [("Injection au réseau", base.export_kwh, after.export_kwh),
            ("Soutirage du réseau", base.import_kwh, after.import_kwh)]
    if sim:
        rows += [("Charge de la batterie", None, sim.charge_total_kWh),
                 ("Restitution de la batterie", None, sim.discharge_total_kWh),
                 ("Pertes de conversion", None, sim.conversion_losses_kWh),
                 ("Stock final disponible (DC)", None, sim.final_stock_kWh)]
        if sim.untransferred_stock_kWh > 1e-6:
            rows.append(("Stock non transféré après les trous (DC)", None, sim.untransferred_stock_kWh))
    first_label = "Sans batterie" if simulated else "Situation mesurée"
    thead = f'<th scope="col">Énergie</th><th scope="col">{first_label}</th>'
    if simulated:
        thead += '<th scope="col">Avec batterie</th>'
    tbody = ""
    for title, before, after_value in rows:
        tbody += f'<tr><td>{title}</td><td>{"—" if before is None else fmt(before)}</td>'
        if simulated:
            tbody += f'<td>{fmt(after_value)}</td>'
        tbody += '</tr>'
    note = "Autoconsommation : pertes de stockage et variation de stock incluses. Autonomie : solaire effectivement livré au bâtiment."
    if existing:
        note = "Batterie existante : bilan apparent. Les flux de batterie, ses pertes, son origine de charge et la variation de stock ne sont pas isolables avec le seul compteur."
    model = "Simulation au pas de mesure : charge sur le surplus solaire uniquement, capacité et puissance limitées, stock utilisable initial nul."
    if not simulated:
        model = "Bilan calculé à partir des importations et injections mesurées, et de la production AC renseignée."
    if estimated:
        model += " Production des mois incomplets estimée au prorata du temps mesuré."
    detail = "Avec batterie" if simulated else "Situation mesurée"
    if existing:
        detail = "Bilan apparent"
    return f"""<div class="pv-dashboard">
    <div class="pv-summary">{summary}</div><div class="pv-cards">{cards}</div>
    <section class="pv-balance"><div class="pv-heading-line"><h2>Comment votre bâtiment est alimenté</h2><span class="pv-small">{detail}</span></div>{energy_bar}</section>
    <section class="pv-detail"><div class="pv-heading-line"><h2>Les flux en détail</h2><span class="pv-small">kWh sur la période</span></div><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></section>
    <footer class="pv-foot"><p>{model}</p><p>{note}</p></footer></div>"""
