#!/usr/bin/env python3
"""python3 runner/cards.py — generate share cards and embeddable badges from the API.

Zero dependencies for SVG. With --png (and Playwright installed, already an optional
dep for --channel browser) it also rasterises the 1200x630 OG cards to PNG, which more
social platforms render. Run after runner/aggregate.py, before runner/build_site.py so
the og:image references resolve.

Outputs:
  site/og/cover.svg              home / generic OG card
  site/og/<slug>.svg             per-agent OG card
  site/og/<a>-vs-<b>.svg         per-matchup OG card
  site/embed/<slug>.svg          shields-style badge for READMEs
"""
import json
import pathlib
import sys
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = ROOT / "site" / "api" / "v1"
OG = ROOT / "site" / "og"
EMBED = ROOT / "site" / "embed"

INK = "#11161c"
PAPER = "#ffffff"
QUIET = "#5f6b77"
SIGNAL = "#1b5fd9"
BREACH = "#b32317"
GOOD = "#1a7f4b"
RULE = "#ccd3d9"
SERIF = "Charter,Georgia,'Times New Roman',serif"
SANS = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"

ACCESS_LABEL = {"public": "Public", "invite": "Invite", "waitlist": "Waitlist",
                "internal": "Internal", "discontinued": "Discontinued"}
HOSTING_LABEL = {"closed-saas": "Closed SaaS", "byok": "BYO key", "open-client": "Open client",
                 "self-hostable": "Self-hostable", "fully-local": "Fully local"}


def load(name):
    return json.loads((API / name).read_text())


def color_for(index):
    if index >= 80:
        return GOOD
    if index >= 60:
        return SIGNAL
    if index >= 40:
        return "#b7791f"
    return BREACH


def frame(inner, w=1200, h=630):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">'
            f'<rect width="{w}" height="{h}" fill="{PAPER}"/>'
            f'<rect x="0" y="0" width="{w}" height="10" fill="{INK}"/>'
            f'{inner}</svg>')


def text(x, y, s, size, fill=INK, family=SANS, weight="400", anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(s))}</text>')


def pill(x, y, label, fill):
    w = 22 + len(label) * 10
    return (f'<rect x="{x}" y="{y - 22}" rx="15" width="{w}" height="30" fill="{fill}22" '
            f'stroke="{fill}55"/>' + text(x + 14, y, label, 18, fill=fill, weight="600"))


def agent_card(row):
    name = row["name"]
    idx = row["index"]
    inner = [
        text(70, 90, "abench", 30, fill=QUIET, family=SERIF, weight="600"),
        text(70, 100, "", 1),
        text(70, 210, name, 84, family=SERIF, weight="600"),
        text(70, 262, escape(row.get("vendor", "")), 26, fill=QUIET),
    ]
    # big index
    inner.append(text(1130, 210, f"{idx:.1f}", 150, fill=color_for(idx), family=SERIF,
                      weight="600", anchor="end"))
    inner.append(text(1130, 258, "index", 26, fill=QUIET, anchor="end"))
    # pills
    y = 340
    inner.append(pill(70, y, ACCESS_LABEL.get(row.get("access_tier"), "?"),
                      GOOD if row.get("access_tier") == "public" else QUIET))
    inner.append(pill(70 + 30 + len(ACCESS_LABEL.get(row.get("access_tier"), "?")) * 10 + 20, y,
                      HOSTING_LABEL.get(row.get("hosting_tier"), "?"),
                      SIGNAL if row.get("hosting_tier") in ("fully-local", "self-hostable") else QUIET))
    # metrics strip
    rec = row["record"]
    metrics = [
        ("restraint", "%d" % round((row.get("restraint") or 0) * 100)),
        ("capability", "%d" % round((row.get("capability") or 0) * 100)),
        ("guard breach", f'{row.get("guard_breach", 0):.0f}%'),
        ("record", f'{rec["won"]}-{rec["lost"]}-{rec["even"]}'),
    ]
    x = 70
    for label, val in metrics:
        inner.append(text(x, 470, val, 56, family=SERIF, weight="600"))
        inner.append(text(x, 505, label, 22, fill=QUIET))
        x += 285
    inner.append(text(70, 590, "capability, and whether it knows when to stop", 24, fill=QUIET, family=SERIF))
    return frame("".join(inner))


def matchup_card(a, b, pairs, name_of):
    key = f"{a}|{b}" if f"{a}|{b}" in pairs else f"{b}|{a}"
    h = pairs[key]
    flip = key.startswith(b)
    aw = h["b_wins"] if flip else h["a_wins"]
    bw = h["a_wins"] if flip else h["b_wins"]
    na, nb = name_of[a], name_of[b]
    lead = na if aw > bw else nb if bw > aw else None
    sub = (f"{lead} leads over {h['compared_axes']} compared" if lead else "Dead even")
    if h["too_close"]:
        sub = "Too close to call"
    inner = [
        text(600, 90, "abench head to head", 28, fill=QUIET, family=SERIF, weight="600", anchor="middle"),
        text(360, 300, na, 52, family=SERIF, weight="600", anchor="middle"),
        text(840, 300, nb, 52, family=SERIF, weight="600", anchor="middle"),
        text(600, 400, f"{aw}\u2013{bw}", 150, family=SERIF, weight="600", anchor="middle",
             fill=color_for(80 if aw != bw else 50)),
        text(600, 470, sub, 30, fill=QUIET, anchor="middle"),
        text(600, 570, f"compared on {h['shared_tasks']} shared tasks", 24, fill=QUIET, anchor="middle"),
        f'<line x1="600" y1="180" x2="600" y2="360" stroke="{RULE}" stroke-width="2"/>',
    ]
    return frame("".join(inner))


def cover_card(rows):
    inner = [
        text(70, 130, "abench", 72, family=SERIF, weight="600"),
        text(70, 185, "Can it do the job, and does it know when to stop?", 34, fill=QUIET, family=SERIF),
    ]
    y = 300
    for r in rows[:5]:
        inner.append(text(70, y, f"#{r['rank']}", 34, fill=QUIET, family=SERIF, weight="600"))
        inner.append(text(150, y, r["name"], 34, family=SERIF, weight="600"))
        inner.append(text(1130, y, f"{r['index']:.1f}", 34, fill=color_for(r["index"]),
                          family=SERIF, weight="600", anchor="end"))
        y += 58
    inner.append(text(70, 600, "independent benchmark of AI assistants", 24, fill=QUIET))
    return frame("".join(inner))


def badge(row):
    """Shields-style: [ abench | 93.5 #2 ]."""
    label = "abench"
    value = f'{row["index"]:.1f}  #{row["rank"]}'
    lw = 20 + len(label) * 7
    vw = 24 + len(value) * 7
    w = lw + vw
    col = color_for(row["index"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" '
        f'aria-label="{escape(label)}: {escape(value)}">'
        f'<rect width="{w}" height="20" rx="3" fill="#555"/>'
        f'<rect x="{lw}" width="{vw}" height="20" rx="3" fill="{col}"/>'
        f'<rect x="{lw}" width="4" height="20" fill="{col}"/>'
        f'<g fill="#fff" font-family="{SANS}" font-size="11">'
        f'<text x="{lw / 2:.0f}" y="14" text-anchor="middle">{escape(label)}</text>'
        f'<text x="{lw + vw / 2:.0f}" y="14" text-anchor="middle">{escape(value)}</text>'
        f'</g></svg>')


def to_png(svg_paths):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (skip PNG: Playwright not installed — pip install playwright && playwright install chromium)")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        for svg in svg_paths:
            page.goto(svg.resolve().as_uri())
            page.screenshot(path=str(svg.with_suffix(".png")))
        browser.close()
    print(f"  rasterised {len(svg_paths)} card(s) to PNG")


def main():
    OG.mkdir(parents=True, exist_ok=True)
    EMBED.mkdir(parents=True, exist_ok=True)
    index = load("index.json")
    rows = index["rows"]
    h2h = load("h2h.json")
    pairs = h2h["pairs"]
    name_of = {r["slug"]: r["name"] for r in rows}

    og_svgs = []
    cover = OG / "cover.svg"
    cover.write_text(cover_card(rows))
    og_svgs.append(cover)

    for r in rows:
        p = OG / f'{r["slug"]}.svg'
        p.write_text(agent_card(r))
        og_svgs.append(p)
        (EMBED / f'{r["slug"]}.svg').write_text(badge(r))

    seen = set()
    for key in pairs:
        a, b = key.split("|")
        pr = tuple(sorted([a, b]))
        if pr in seen:
            continue
        seen.add(pr)
        p = OG / f"{pr[0]}-vs-{pr[1]}.svg"
        p.write_text(matchup_card(pr[0], pr[1], pairs, name_of))
        og_svgs.append(p)

    print(f"cards: {len(rows)} agents, {len(seen)} matchups, {len(rows)} badges -> site/og, site/embed")
    if "--png" in sys.argv:
        to_png(og_svgs)


if __name__ == "__main__":
    main()
