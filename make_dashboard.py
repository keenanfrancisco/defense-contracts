"""Render four SQL-backed charts and computed takeaways as a static dashboard."""

from datetime import date
from contextlib import closing
from html import escape
import json
import sqlite3

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs_version

from pull import ENDPOINT, ROOT, load_config


GROUP_LABELS = {"fixed": "Fixed-price", "cost_plus": "Cost-plus",
                "time_materials": "Time & materials", "other": "Other"}
# Plotly's canvas renderer accepts sRGB strings. The page theme uses OKLCH.
COLORS = {"new_entrant": "rgb(94, 109, 35)", "prime": "rgb(68, 89, 119)",
          "fixed": "rgb(94, 109, 35)", "cost_plus": "rgb(68, 89, 119)",
          "time_materials": "rgb(184, 98, 49)", "other": "rgb(125, 128, 133)"}
COMPANY_COLORS = ["rgb(94,109,35)", "rgb(18,125,117)", "rgb(180,113,29)",
                  "rgb(68,89,119)", "rgb(151,62,71)", "rgb(101,83,147)",
                  "rgb(80,118,151)", "rgb(128,96,66)"]
CAVEATS = [
    "Obligations, not outlays; modifications can be negative.",
    "Recipient matching is by name; subsidiaries outside the explicit list may be missed or mis-assigned.",
    "FY2026 data may still be updating (fiscal year ended 2026-09-30).",
    "Pricing type is recorded per transaction; large primes have mixed portfolios.",
    "These are eight companies across twelve configured searches, not all defense spending; RTX searches Raytheon, RTX, Pratt & Whitney, Collins Aerospace, and Rockwell Collins.",
    "Configured subsidiaries count in every FY regardless of acquisition date; joint ventures and uncertain names remain excluded.",
    "This is not a complete corporate-family analysis; historical ownership and divestitures are not reconstructed.",
    "Fixed-price share uses max(fixed-group net, 0) divided by the sum of positive net amounts across the 16 configured pricing codes; a zero denominator is undefined.",
]


def latest_full_fy(as_of=None):
    today = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or date.today()
    return today.year if today.month >= 10 else today.year - 1


def money(amount):
    magnitude = abs(amount)
    if magnitude >= 1_000_000_000:
        return f"${amount / 1_000_000_000:,.2f}B"
    if magnitude >= 1_000_000:
        return f"${amount / 1_000_000:,.1f}M"
    return f"${amount:,.2f}"


def growth_index(totals, years, group):
    """Return (base_year, [amount / base amount per year]) using the first positive year as 1x."""
    base_year = next((fy for fy in years if totals.get((fy, group), 0) > 0), None)
    if base_year is None:
        return None, []
    base = totals[(base_year, group)]
    return base_year, [None if fy < base_year else totals.get((fy, group), 0) / base for fy in years]


def headline_stats(data):
    fy = data["latest_fy"]
    years = sorted({row["fy"] for row in data["cohort_totals"] if row["fy"] <= fy})
    totals = {(row["fy"], row["grp"]): row["amount"] for row in data["cohort_totals"]}
    stats = []
    base_year, multiples = growth_index(totals, years, "new_entrant")
    if base_year is not None and base_year < fy:
        stats.append((f"{multiples[-1]:.1f}x", f"Growth in new-entrant DoD obligations, FY{base_year} to FY{fy}"))
    entrants, primes = totals.get((fy, "new_entrant"), 0), totals.get((fy, "prime"), 0)
    if entrants + primes:
        stats.append((f"{entrants / (entrants + primes):.1%}", f"New entrants' share of the two groups' combined FY{fy} obligations"))
    anduril = next((row for row in data["latest_shares"] if row["company"] == "Anduril" and row["share_fixed"] is not None), None)
    if anduril:
        stats.append((f"{anduril['share_fixed']:.1%}", f"Share of Anduril's FY{fy} DoD dollars on fixed-price terms"))
    return stats


def compute_takeaways(data):
    fy = data["latest_fy"]
    totals = {row["grp"]: row["amount"] for row in data["cohort_totals"] if row["fy"] == fy}
    entrants, primes = totals.get("new_entrant", 0), totals.get("prime", 0)
    combined = entrants + primes
    first = (f"In FY{fy}, new entrants receive {entrants / combined:.1%} of these cohorts’ combined DoD obligations "
             f"({money(entrants)} versus {money(primes)} for the primes)." if combined else
             f"FY{fy} has no net obligations for the configured cohorts, so their relative share is undefined.")
    growth_years = sorted({row["fy"] for row in data["cohort_totals"] if row["fy"] <= fy})
    growth_totals = {(row["fy"], row["grp"]): row["amount"] for row in data["cohort_totals"]}
    pieces = []
    for group, label in [("new_entrant", "new entrants"), ("prime", "primes")]:
        base_year, multiples = growth_index(growth_totals, growth_years, group)
        if base_year is not None and base_year < fy:
            pieces.append(f"{label} {multiples[-1]:.1f}x since FY{base_year}")
    prime_years = [growth_totals[(year, "prime")] for year in growth_years if (year, "prime") in growth_totals]
    if pieces:
        first += " Growth: " + pieces[0] + "."
    if prime_years:
        first += f" The primes moved between {money(min(prime_years))} and {money(max(prime_years))} a year with no clear trend."
    by_company = {}
    for row in data.get("company_totals", []):
        if row["fy"] == fy and row["grp"] == "new_entrant":
            by_company[row["company"]] = row["amount"]
    if entrants and by_company:
        top = max(by_company, key=by_company.get)
        first += f" {top} accounts for {by_company[top] / entrants:.0%} of the new entrants' FY{fy} total."
    valid = [row for row in data["latest_shares"] if row["share_fixed"] is not None]
    if valid:
        leader = max(valid, key=lambda row: row["share_fixed"])
        leaders = [row["company"] for row in valid if row["share_fixed"] == leader["share_fixed"]]
        if len(leaders) > 1:
            names = ", ".join(leaders[:-1]) + " and " + leaders[-1]
            second = f"{names} tie for the highest fixed-price share in FY{fy}, at {leader['share_fixed']:.1%} of their positive pricing-group obligations."
        else:
            second = f"{leader['company']} has the highest fixed-price share in FY{fy}, at {leader['share_fixed']:.1%} of its positive pricing-group obligations."
    else:
        second = f"No company has a defined fixed-price share in FY{fy}."
    histories = {}
    for row in data["share_history"]:
        if row["share_fixed"] is not None and row["fy"] <= fy:
            histories.setdefault(row["company"], []).append(row)
    changes = []
    for company, rows in histories.items():
        rows.sort(key=lambda row: row["fy"])
        if len(rows) >= 2 and rows[-1]["fy"] == fy and rows[0]["fy"] < fy:
            changes.append((company, rows[-1]["share_fixed"] - rows[0]["share_fixed"], rows[0]["fy"]))
    if changes:
        company, change, start = max(changes, key=lambda item: abs(item[1]))
        direction = "increased" if change >= 0 else "decreased"
        third = (f"{company}’s fixed-price share {direction} {abs(change) * 100:.1f} percentage points from FY{start} "
                 f"to FY{fy}, the largest absolute change among companies with comparable endpoints.")
    else:
        third = "No company has two comparable fiscal-year endpoints for a fixed-price share change."
    mix = [row for row in data["anduril_mix"] if row["fy"] == fy]
    total = sum(row["amount"] for row in mix)
    if mix and total:
        largest = max(mix, key=lambda row: row["amount"])
        fourth = (f"In FY{fy}, {GROUP_LABELS[largest['pricing_group']].lower()} accounts for {largest['amount'] / total:.1%} "
                  f"of Anduril’s {money(total)} in net DoD obligations.")
    else:
        fourth = f"Anduril has no net obligations with a defined pricing mix in FY{fy}."
    return [first, second, third, fourth]


def load_analysis(database):
    if not database.exists():
        raise ValueError("Missing data/contracts.db; run python build_db.py first")
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        max_year = conn.execute("SELECT MAX(fy) FROM coverage").fetchone()[0]
        if max_year is None:
            raise ValueError("Database has no fiscal-year coverage")
        latest = min(latest_full_fy(), max_year)
        data = {"latest_fy": latest}
        for key, filename in [("cohort_totals", "cohort_totals.sql"),
                              ("latest_shares", "latest_fixed_share.sql"),
                              ("share_history", "fixed_share_history.sql"),
                              ("anduril_mix", "anduril_pricing_mix.sql")]:
            data[key] = [dict(row) for row in conn.execute((ROOT / "queries" / filename).read_text(), {"latest_fy": latest})]
        data["company_totals"] = [dict(row) for row in conn.execute(
            "SELECT company, grp, fy, SUM(amount) AS amount FROM obligations GROUP BY company, grp, fy")]
        data["row_count"] = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
        data["query_count"] = conn.execute("SELECT COUNT(*) FROM pull_metadata").fetchone()[0]
        data["pull_start"], data["pull_end"] = conn.execute("SELECT MIN(pulled_at), MAX(pulled_at) FROM pull_metadata").fetchone()
        data["exclusions"] = [dict(row) for row in conn.execute((ROOT / "queries/recipient_matching.sql").read_text())]
    return data


def make_figures(data, companies):
    years = sorted({row["fy"] for row in data["cohort_totals"]})
    totals = {(row["fy"], row["grp"]): row["amount"] for row in data["cohort_totals"]}
    cohort = go.Figure()
    for group, label in [("prime", "Primes"), ("new_entrant", "New entrants")]:
        base_year, multiples = growth_index(totals, years, group)
        if base_year is None:
            continue
        cohort.add_trace(go.Scatter(name=label, x=years, y=multiples, mode="lines+markers+text",
                                    line=dict(color=COLORS[group], width=3), marker=dict(size=9),
                                    text=[f"{value:.1f}x" if fy == years[-1] and value is not None else "" for fy, value in zip(years, multiples)],
                                    textposition="middle right", textfont=dict(color="rgb(35,40,43)"), cliponaxis=False,
                                    customdata=[totals.get((fy, group), 0) / 1e9 for fy in years],
                                    hovertemplate="FY%{x}: %{y:.1f}x FY" + str(base_year) + " · $%{customdata:,.2f}B<extra>%{fullData.name}</extra>"))
    cohort.update_layout(yaxis_title=f"Obligations vs. FY{years[0]} (1x = FY{years[0]} level)")
    cohort.update_yaxes(ticksuffix="x", rangemode="tozero")
    shares = sorted((row for row in data["latest_shares"] if row["share_fixed"] is not None),
                    key=lambda row: row["share_fixed"])
    latest = go.Figure(go.Bar(x=[row["share_fixed"] * 100 for row in shares],
                             y=[row["company"] for row in shares], orientation="h",
                             marker_color=[COLORS[row["grp"]] for row in shares],
                             text=[f"{row['share_fixed']:.1%}" for row in shares], textposition="outside",
                             cliponaxis=False, hovertemplate="%{y}: %{x:.2f}%<extra></extra>"))
    latest.update_layout(xaxis_title="Fixed-price share of positive group obligations (%)", showlegend=False,
                         margin=dict(l=140, r=65, t=30, b=60))
    history = go.Figure()
    for index, company in enumerate(companies):
        rows = [row for row in data["share_history"] if row["company"] == company["name"]]
        history.add_trace(go.Scatter(name=company["name"], x=[row["fy"] for row in rows],
                                     y=[None if row["share_fixed"] is None else row["share_fixed"] * 100 for row in rows],
                                     mode="lines+markers", connectgaps=False,
                                     line=dict(color=COMPANY_COLORS[index % len(COMPANY_COLORS)],
                                               dash="solid" if company["group"] == "new_entrant" else "dash"),
                                     hovertemplate="FY%{x}: %{y:.2f}%<extra>%{fullData.name}</extra>"))
    history.update_layout(yaxis_title="Fixed-price share (%)")
    mix = go.Figure()
    lookup = {(row["fy"], row["pricing_group"]): row["amount"] for row in data["anduril_mix"]}
    for group, label in GROUP_LABELS.items():
        mix.add_trace(go.Bar(name=label, x=years, y=[lookup.get((fy, group), 0) / 1e6 for fy in years],
                             marker_color=COLORS[group], hovertemplate="FY%{x}: $%{y:,.2f}M<extra>%{fullData.name}</extra>"))
    mix.update_layout(barmode="relative", yaxis_title="Net obligations ($M)")
    figures = [cohort, latest, history, mix]
    for index, figure in enumerate(figures):
        figure.update_layout(template="plotly_white", height=420, autosize=True,
                             font=dict(family="Arial, sans-serif", size=13, color="rgb(35,40,43)"),
                             paper_bgcolor="white", plot_bgcolor="white",
                             legend=dict(orientation="h", yanchor="bottom", y=1.04, x=0),
                             hoverlabel=dict(font_size=13))
        if index != 1:
            figure.update_layout(margin=dict(l=65, r=30, t=70, b=55))
            figure.update_xaxes(tickmode="array", tickvals=years, ticktext=[f"FY{str(fy)[2:]}" for fy in years])
        figure.update_yaxes(gridcolor="rgb(229,231,232)", zerolinecolor="rgb(155,160,163)")
    return figures


def data_table(rows, columns):
    header = "".join(f"<th scope='col'>{escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if key == "share_fixed":
                text = "Undefined (zero positive total)" if value is None else f"{value:.2%}"
            elif key in {"amount", "fixed_amount", "fixed_group_net", "total_amount"}:
                text = f"${value:,.2f}"
            else:
                text = str(value)
            cells.append(f"<td>{escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<details><summary>View data table</summary><div class='table-scroll'><table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table></div></details>"


def render_dashboard(data, figures, takeaways, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    titles = ["Is the money shifting to new entrants?", f"Who gets paid on fixed-price terms in FY{data['latest_fy']}?",
              "How has each company's pricing mix changed?", "How is Anduril paid?"]
    subtitles = ["Each group's DoD obligations as a multiple of its first-year level, so both fit on one scale. Hover for dollar amounts; exact totals are in the table.",
                 "Share of each company's DoD contract dollars on fixed-price contracts. Green bars are new entrants, blue bars are primes.",
                 "Fixed-price share by company per fiscal year. Solid lines are new entrants, dashed lines are primes. Click a name in the legend to hide it.",
                 "Anduril's DoD obligations by contract pricing type. Bars below zero are net downward modifications."]
    slugs = ["cohort-totals", "latest-fixed-share", "fixed-share-history", "anduril-pricing-mix"]
    sources = ["cohort_totals", "latest_shares", "share_history", "anduril_mix"]
    columns = [[("fy", "Fiscal year"), ("grp", "Cohort"), ("amount", "Net obligations")],
               [("company", "Company"), ("fy", "Fiscal year"), ("share_fixed", "Fixed share"), ("positive_group_total", "Positive pricing-group total"), ("fixed_amount", "Fixed numerator (floored)"), ("fixed_group_net", "Fixed-group net"), ("total_amount", "Total net obligations")],
               [("company", "Company"), ("fy", "Fiscal year"), ("share_fixed", "Fixed share")],
               [("fy", "Fiscal year"), ("pricing_group", "Pricing group"), ("amount", "Net obligations")]]
    sections = []
    exports, export_error = [], None
    for figure, slug in zip(figures, slugs):
        try:
            image_dir = output_dir / "img"
            image_dir.mkdir(exist_ok=True)
            figure.write_image(image_dir / f"{slug}.png", width=1600, height=900, scale=1.5)
            exports.append(slug)
        except Exception as error:
            export_error = f"{type(error).__name__}: {error}"
            break
    for index, figure in enumerate(figures):
        slug = slugs[index]
        chart = figure.to_html(full_html=False, include_plotlyjs=False, div_id=f"chart-{slug}",
                               config={"responsive": True, "displaylogo": False, "scrollZoom": False,
                                       "toImageButtonOptions": {"format": "png", "filename": slug, "width": 1600, "height": 900}})
        download = f"<a class='download' href='img/{slug}.png' download>Download PNG</a>" if slug in exports else ""
        sections.append(f"<section id='{slug}' aria-labelledby='heading-{slug}'><div class='section-top'><h2 id='heading-{slug}'>{escape(titles[index])}</h2>{download}</div>"
                        f"<p class='subtitle'>{escape(subtitles[index])}</p><p class='takeaway'>{escape(takeaways[index])}</p>"
                        f"<div role='img' aria-label='{escape(titles[index])}. {escape(takeaways[index])}'>{chart}</div>"
                        f"{data_table(data[sources[index]], columns[index])}</section>")
    caveats = "".join(f"<li>{escape(caveat)}</li>" for caveat in CAVEATS)
    export_note = ("All four chart PNGs are available for slides." if len(exports) == 4 else
                   "PNG export was unavailable or incomplete in this environment; the interactive charts remain available.")
    stats = "".join(f"<div class='stat'><p class='stat-value'>{escape(value)}</p><p class='stat-label'>{escape(label)}</p></div>"
                    for value, label in headline_stats(data))
    min_fy = min(row["fy"] for row in data["cohort_totals"])
    max_fy = max(row["fy"] for row in data["cohort_totals"])
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="A repeatable comparison of DoD contract pricing for eight defense-tech companies and traditional primes, FY2020–FY2026.">
<title>How Defense Gets Paid · DoD Contract Pricing</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js" charset="utf-8"></script>
<style>
:root {{ --bg:oklch(1 0 0); --ink:oklch(.25 .008 110); --muted:oklch(.44 .012 110); --primary:oklch(.49 .09 110); --accent:oklch(.43 .06 250); --line:oklch(.88 .008 110); --surface:oklch(.97 0 0); }}
* {{box-sizing:border-box}} html {{scroll-behavior:smooth}} body {{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a {{color:var(--accent);text-underline-offset:4px}} a:focus-visible,summary:focus-visible {{outline:3px solid var(--primary);outline-offset:4px}}
.skip {{position:absolute;left:16px;top:-80px;background:white;padding:10px}} .skip:focus {{top:10px}}
header,main,footer {{width:min(1160px,calc(100% - 64px));margin:auto}} header {{padding:52px 0 30px;border-bottom:2px solid var(--ink)}}
.eyebrow {{font-size:.8rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--primary);margin:0 0 12px}}
h1 {{font-size:3.2rem;line-height:1.12;letter-spacing:-.035em;font-weight:650;margin:0 0 20px;max-width:800px;text-wrap:balance}}
.intro {{font-size:1.12rem;max-width:70ch;color:var(--muted);margin:0 0 24px}} .meta {{display:flex;gap:12px 28px;flex-wrap:wrap;font-size:.88rem;color:var(--muted)}}
nav {{display:flex;gap:10px 28px;flex-wrap:wrap;padding:20px 0;font-size:.9rem}} nav a {{text-decoration:none}} nav a:hover {{text-decoration:underline}}
section {{padding:36px 0 38px;border-bottom:1px solid var(--line);scroll-margin-top:20px}} .section-top {{display:flex;align-items:baseline;justify-content:space-between;gap:16px}}
h2 {{font-size:1.55rem;line-height:1.3;font-weight:650;margin:0;text-wrap:balance}} .download {{font-size:.85rem;white-space:nowrap}}
.subtitle {{color:var(--muted);font-size:.9rem;margin:8px 0 18px}} .takeaway {{border-left:3px solid var(--primary);padding:2px 0 2px 16px;margin:0 0 12px;font-size:1.04rem;max-width:85ch}}
details {{font-size:.88rem;margin-top:12px}} summary {{cursor:pointer;color:var(--accent);width:fit-content;padding:6px 0}} .table-scroll {{overflow:auto;margin-top:10px}}
table {{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}} th,td {{padding:9px 14px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}} th {{background:var(--surface)}}
footer {{padding:34px 0 50px;font-size:.9rem;color:var(--muted)}} footer p,footer li {{max-width:85ch}} footer h2 {{font-size:1.25rem;color:var(--ink)}} footer ul {{padding-left:20px}} .method {{display:flex;gap:12px 26px;flex-wrap:wrap}}
.cohorts {{display:flex;gap:14px 30px;flex-wrap:wrap;font-size:.86rem;padding-top:4px;color:var(--muted)}} .cohorts p {{margin:0;max-width:60ch}}
.stats {{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:0 0 24px}} .stat {{border:1px solid var(--line);border-radius:8px;padding:16px 18px}}
.stat-value {{font-size:2.3rem;font-weight:650;line-height:1.1;margin:0 0 6px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}} .stat-label {{margin:0;color:var(--muted);font-size:.9rem;line-height:1.4}}
.explainer {{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px 28px;background:var(--surface);border-radius:8px;padding:16px 18px;margin:0 0 22px;font-size:.92rem}} .explainer p {{margin:0}} .explainer strong {{display:block;color:var(--ink)}}
noscript p {{padding:18px;background:var(--surface)}}
@media(max-width:680px) {{header,main,footer {{width:calc(100% - 32px)}} header {{padding-top:32px}} h1 {{font-size:2.25rem}} h2 {{font-size:1.25rem}} section {{padding-top:28px}} .section-top {{align-items:flex-start;flex-direction:column;gap:6px}} nav {{gap:10px 20px}} .meta {{gap:6px 20px}} .takeaway {{font-size:1rem}} }}
@media(prefers-reduced-motion:reduce) {{html {{scroll-behavior:auto}}}}
</style></head><body><a class="skip" href="#analysis">Skip to analysis</a>
<header><p class="eyebrow">DoD contract pricing · FY{min_fy}–FY{max_fy}</p><h1>How defense gets paid.</h1>
<p class="intro">New defense-tech companies and traditional primes, compared through the pricing terms on their Department of Defense contracts, using public USAspending.gov data.</p>
<div class="stats">{stats}</div>
<div class="explainer"><p><strong>Fixed-price</strong> The contractor agrees to a price up front and keeps the profit or eats the overrun. This is how commercial products are usually bought.</p><p><strong>Cost-plus</strong> The government reimburses the contractor's costs and adds a fee on top. This is the traditional model for large development programs.</p></div>
<div class="meta"><span>Analysis by Keenan Francisco</span><span>Latest full year: FY{data['latest_fy']}</span><span>Source: USAspending.gov</span><span>Pulled: {escape(data['pull_end'][:10])} UTC</span></div></header>
<main id="analysis"><nav aria-label="Analysis"><a href="#cohort-totals">Obligations</a><a href="#latest-fixed-share">Fixed-price ranking</a><a href="#fixed-share-history">Trends</a><a href="#anduril-pricing-mix">Anduril</a><a href="#method">Method</a></nav>
<div class="cohorts"><p><strong>New entrants</strong> Anduril · Palantir · Shield AI</p><p><strong>Primes</strong> Lockheed Martin · RTX (Raytheon) · Northrop Grumman · General Dynamics · Boeing</p></div>
<noscript><p>Enable JavaScript for interactive charts, or use each chart’s data table below.</p></noscript>{''.join(sections)}</main>
<footer id="method"><h2>Source &amp; method</h2><p>Net obligations on DoD-awarded contracts (award types A–D), grouped by transaction action date and fiscal year. Each of the 16 pricing codes is queried separately, all recipient pages are collected, and case-insensitive full-name regexes plus exact subsidiary lists select named legal entities. Fixed-price share is max(fixed-group net obligations, 0) divided by the sum of positive net obligations across configured pricing groups; a zero denominator is undefined.</p>
<p>{data['row_count']:,} recipient/code/year rows · {data['query_count']:,} completed queries · pull window {escape(data['pull_start'])} to {escape(data['pull_end'])}.</p>
<p>{len(data['exclusions'])} company/name pairs were excluded by the matching rules; subsidiaries and joint ventures in this list remain attribution uncertainties.</p>
<div class="method"><a href="{ENDPOINT}">USAspending API endpoint</a><a href="https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/search/spending_by_category/recipient.md">API documentation</a><a href="recipient_matching.csv" download>All excluded recipient names (CSV)</a></div>
<p>{escape(export_note)} This page includes its data and styles; the pinned Plotly CDN requires an internet connection.</p><h2>Caveats</h2><ul>{caveats}</ul></footer></body></html>
"""
    (output_dir / "index.html").write_text(html)
    report = {"row_count": data["row_count"], "latest_full_fy": data["latest_fy"],
              "pull_start": data["pull_start"], "pull_end": data["pull_end"],
              "takeaways": takeaways, "excluded_recipients": data["exclusions"],
              "png_exports": [f"img/{slug}.png" for slug in exports], "png_export_error": export_error}
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    companies, _ = load_config()
    data = load_analysis(ROOT / "data/contracts.db")
    takeaways = compute_takeaways(data)
    report = render_dashboard(data, make_figures(data, companies), takeaways, ROOT / "docs")
    print(f"Wrote docs/index.html; {data['row_count']:,} obligations rows.")
    for index, takeaway in enumerate(takeaways, 1):
        print(f"{index}. {takeaway}")
    if report["png_export_error"]:
        print(f"PNG export skipped/incomplete: {report['png_export_error']}")
    else:
        print("Exported all 4 chart PNGs to docs/img/.")


if __name__ == "__main__":
    main()
