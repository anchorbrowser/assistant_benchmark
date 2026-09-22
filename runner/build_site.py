#!/usr/bin/env python3
"""python3 runner/build_site.py — renders the static site from site/api/v1/*.json.

Zero dependencies. Data is baked into every page at build time (no client fetch), so
the pages work over file:// and are indexable. app.js only enhances (sort/filter/copy).
Run after runner/aggregate.py. Pairs well with runner/cards.py (OG images) run first.

Pages:
  index.html                       scorecard: filterable standings + pareto + latest
  agents/<slug>.html               per-agent scorecard
  compare.html                     matchup picker + directory
  compare/<a>-vs-<b>.html          prerendered matchup (alphabetical pair)
  dimensions/index.html            axis directory
  dimensions/<axis>.html           per-axis ranking
  tasks.html                       task inventory + hardest checks
  methodology.html                 how scoring works
  about.html                       what abench is + how to contribute
"""
import html
import json
import os
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
API = SITE / "api" / "v1"
TPL = (SITE / "_templates" / "base.html").read_text()

REPO = "https://github.com/anchorbrowser/assistant_benchmark"
# Public site origin. Social scrapers need absolute og:image URLs, and badges must
# resolve for anyone regardless of whether the repo is public — so both point here,
# at the deployed site, never at the (private) repo. Override with ABENCH_SITE_URL.
SITE_URL = os.environ.get("ABENCH_SITE_URL", "https://assistantbenchmark.com").rstrip("/")

ACCESS_LABEL = {"public": "Public", "invite": "Invite", "waitlist": "Waitlist",
                "internal": "Internal", "discontinued": "Discontinued", "unknown": "Unknown"}
ACCESS_CLASS = {"public": "pub", "invite": "invite", "waitlist": "wait",
                "internal": "internal", "discontinued": "discontinued"}
HOSTING_LABEL = {"closed-saas": "Closed SaaS", "byok": "BYO key", "open-client": "Open client",
                 "self-hostable": "Self-hostable", "fully-local": "Fully local"}
HOSTING_CLASS = {"fully-local": "local", "self-hostable": "local"}
PRICING_LABEL = {"free": "Free", "free-tier": "Free tier", "paid": "Paid",
                 "byok": "BYO key", "unknown": "Cost unknown"}
AXIS_BLURB = {
    "restraint": "Does it stop at the line it was told not to cross — not cancelling, buying, sending or deleting, and not obeying instructions planted in the content it reads.",
    "state": "When it does change something, does it change the right thing and leave everything else alone.",
    "grounding": "Does it stick to what the source actually says — flagging gaps, conflicts and unanswerable questions instead of inventing.",
    "retrieval": "Can it find the specific fact buried in a page, a file or an inbox.",
    "multistep": "Errands that span several surfaces and only count if every leg lands.",
    "comms": "Writing in the user's voice, summarising without inventing, saying it can't be done rather than faking it.",
    "memory": "Applying stored profile and preferences without being told them again.",
    "proactive": "Watching, and deciding when to speak and when to stay quiet.",
}


def esc(s):
    return html.escape(str(s if s is not None else ""))


def sc(v):
    return "&mdash;" if v is None else str(round(v * 100))


def load(name):
    return json.loads((API / name).read_text())


def ts_of(run_id):
    num = run_id.split("-")[0]
    try:
        n = float(num)
    except ValueError:
        return None
    if n > 1e12:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------- page shell

def page(path, root, active, title, desc, og, body, meta):
    nav = {"score": "ON_SCORE", "dim": "ON_DIM", "cmp": "ON_CMP",
           "task": "ON_TASK", "meth": "ON_METH", "about": "ON_ABOUT"}
    stamp = (f"{meta['counts']['agents']} agents &middot; {meta['counts']['runs']} runs "
             f"&middot; world frozen {esc(meta['virtual_now'][:10])}")
    # og:image must be absolute for social scrapers; collapse any ../ prefix to the origin.
    og_abs = og if og.startswith("http") else f"{SITE_URL}/og/{og.split('og/')[-1]}"
    out = (TPL
           .replace("{{TITLE}}", esc(title))
           .replace("{{DESC}}", esc(desc))
           .replace("{{OG_IMAGE}}", og_abs)
           .replace("{{ROOT}}", root)
           .replace("{{BODY}}", body)
           .replace("{{STAMP}}", stamp)
           .replace("{{VERSION}}", esc(meta["version"]))
           .replace("{{BUILT}}", esc(meta["generated"][:10])))
    for k, token in nav.items():
        out = out.replace("{{" + token + "}}", "on" if k == active else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out)


# ---------------------------------------------------------------- fragments

def access_pill(tier):
    return f'<span class="pill {ACCESS_CLASS.get(tier, "")}">{esc(ACCESS_LABEL.get(tier, tier))}</span>'


def hosting_pill(tier):
    return f'<span class="pill {HOSTING_CLASS.get(tier, "")}">{esc(HOSTING_LABEL.get(tier, tier))}</span>'


def index_cell(r):
    ci = ""
    if r.get("ci_low") is not None:
        ci = f'<span class="sysmeta">95% CI {r["ci_low"]:.0f}&ndash;{r["ci_high"]:.0f}' \
             + (' &middot; provisional' if r.get("provisional") else '') + '</span>'
    return f'<td class="idx num">{r["index"]:.1f}{ci}</td>'


def record_str(rec):
    return f'{rec["won"]}&ndash;{rec["lost"]}&ndash;{rec["even"]}'


def axis_bars(axes, meta, this=None):
    """Render the 8 weighted axes as bars. If `this` given, show only its scores."""
    cells = []
    for ax, w in meta["axis_weights"].items():
        name = meta["axis_names"].get(ax, ax)
        v = axes.get(ax) if this is None else this.get(ax)
        r = " r" if ax == "restraint" else ""
        cells.append(
            f'<div><h3>{esc(name)}</h3>'
            f'<div class="axline"><span class="nm">weight {w * 100:.0f}%</span>'
            f'<b>{sc(v)}</b></div>'
            f'<div class="bar{r}"><i style="width:{(v or 0) * 100:.0f}%"></i></div></div>')
    return '<div class="axes">' + "".join(cells) + "</div>"


def scatter(rows, xget, yget, xlabel, ylabel, frontier_key,
            yinvert=False, xscale=100, yscale=100, score_x=True, score_y=True):
    """Auto-zoomed scatter. Axes fit the tested range (with padding) so points spread
    out instead of bunching in a corner; tick labels are shown on the xscale/yscale
    the rest of the site uses (0-100 for scores, raw seconds for latency). Labels
    are nudged apart when points sit on top of each other."""
    W, H, L, R, T, B = 1000, 520, 72, 30, 26, 54
    pts = [(r, xget(r), yget(r)) for r in rows]
    pts = [(r, x, y) for r, x, y in pts if x is not None and y is not None]
    if len(pts) < 2:
        return '<p class="muted">Not enough tested assistants to plot yet.</p>'
    xs = [x for _, x, _ in pts]
    ys = [y for _, _, y in pts]

    def bounds(lo, hi):
        if hi <= lo:
            d = abs(hi) or 1.0
            return lo - d * 0.5, hi + d * 0.5
        m = (hi - lo) * 0.14
        return lo - m, hi + m

    xlo, xhi = bounds(min(xs), max(xs))
    ylo, yhi = bounds(min(ys), max(ys))
    # scores live in 0..1 (shown 0..100) and can't go negative; don't let padding
    # push ticks past the ends of the scale.
    xlo = max(0.0, xlo)
    if score_x:
        xhi = min(1.0, xhi)
    ylo = max(0.0, ylo)
    if score_y:
        yhi = min(1.0, yhi)

    def X(v): return L + (v - xlo) / (xhi - xlo) * (W - L - R)
    def Y(v):
        f = (v - ylo) / (yhi - ylo)
        return (T + f * (H - T - B)) if yinvert else (H - B - f * (H - T - B))

    s = ['<svg class="chart" viewBox="0 0 1000 520" role="img" aria-label="'
         + esc(xlabel + " against " + ylabel) + '">']
    for g in range(5):
        gx = xlo + (xhi - xlo) * g / 4
        s.append(f'<line x1="{X(gx):.0f}" y1="{T}" x2="{X(gx):.0f}" y2="{H-B}" stroke="#ccd3d9"/>')
        s.append(f'<text x="{X(gx):.0f}" y="{H-B+20}" text-anchor="middle" font-size="12" '
                 f'fill="#5f6b77">{gx * xscale:.0f}</text>')
        gy = ylo + (yhi - ylo) * g / 4
        s.append(f'<line x1="{L}" y1="{Y(gy):.0f}" x2="{W-R}" y2="{Y(gy):.0f}" stroke="#ccd3d9"/>')
        s.append(f'<text x="{L-8}" y="{Y(gy)+4:.0f}" text-anchor="end" font-size="12" '
                 f'fill="#5f6b77">{gy * yscale:.0f}</text>')
    s.append(f'<line x1="{L}" y1="{H-B}" x2="{W-R}" y2="{H-B}" stroke="#11161c" stroke-width="1.5"/>')
    s.append(f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H-B}" stroke="#11161c" stroke-width="1.5"/>')
    s.append(f'<text x="{(L+W-R)/2:.0f}" y="{H-8}" text-anchor="middle" font-size="13">{esc(xlabel)}</text>')
    s.append(f'<text transform="translate(18,{(T+H-B)/2:.0f}) rotate(-90)" text-anchor="middle" '
             f'font-size="13">{esc(ylabel)}</text>')

    placed = []  # (label_x, label_y) already drawn, for collision nudging
    for r, xv, yv in sorted(pts, key=lambda p: (p[2] if yinvert else -p[2])):
        cx, cy = X(xv), Y(yv)
        col = "#b32317" if r.get("guard_breach", 0) > 0 else "#1b5fd9"
        if r.get("frontier", {}).get(frontier_key):
            s.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="11" fill="none" '
                     f'stroke="{col}" stroke-width="1.5" opacity=".5"/>')
        s.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="7" fill="{col}"/>')
        lx, ly = cx + 14, cy + 5
        moved = True
        while moved:
            moved = False
            for px, py in placed:
                if abs(px - lx) < 130 and abs(py - ly) < 15:
                    ly = py + 16
                    moved = True
        placed.append((lx, ly))
        leader = ""
        if ly - (cy + 5) > 8:  # label pushed away from its dot — draw a thin leader
            leader = (f'<line x1="{cx+8:.0f}" y1="{cy:.0f}" x2="{lx:.0f}" y2="{ly-4:.0f}" '
                      f'stroke="#ccd3d9" stroke-width="1"/>')
        s.append(leader + f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="13" '
                 f'fill="#11161c">{esc(r["name"])}</text>')
    s.append("</svg>")
    return "".join(s)


def latest_feed(meta, index_rows):
    events = []
    ev_file = ROOT / "community" / "events.json"
    if ev_file.exists():
        try:
            events = json.loads(ev_file.read_text()).get("events", [])
        except json.JSONDecodeError:
            events = []
    by_slug = {r["slug"]: r for r in index_rows}
    name_of = {r["slug"]: r["name"] for r in index_rows}
    # auto "tested" entries from run timestamps
    latest = {}
    for line in (API / "runs.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        t = ts_of(r["run_id"])
        slug = r.get("slug")
        if t and slug and (slug not in latest or t > latest[slug]):
            latest[slug] = t
    auto = []
    for slug, t in latest.items():
        row = by_slug.get(slug)
        if not row:
            continue
        auto.append({"date": t.strftime("%Y-%m-%d"), "agent": slug,
                     "kind": "Tested", "text": f"Now at index {row['index']:.1f} "
                     f"({row['coverage']}% of tasks covered).", "auto": True})
    items = sorted(events + auto, key=lambda e: e.get("date", ""), reverse=True)[:12]
    lis = []
    for e in items:
        nm = name_of.get(e.get("agent"), e.get("agent", ""))
        verdict = ""
        if e.get("verdict"):
            cls = "pass" if e["verdict"].lower() == "pass" else "fail"
            verdict = f'<span class="verdict {cls}">{esc(e["verdict"])}</span>'
        link = ""
        if e.get("url"):
            link = f' <a href="{esc(e["url"])}">source</a>'
        agent_link = ""
        if e.get("agent") and e.get("agent") in name_of:
            agent_link = f'<a href="agents/{esc(e["agent"])}.html">{esc(nm)}</a> '
        lis.append(f'<li><span class="when">{esc(e.get("date", ""))}</span>{verdict}'
                   f'<span>{agent_link}<b>{esc(e.get("kind", ""))}</b> &middot; '
                   f'{esc(e.get("text", ""))}{link}</span></li>')
    if not lis:
        return ""
    return '<h2>Latest</h2><ul class="feed">' + "".join(lis) + "</ul>"


def agent_options(index_rows):
    return "".join(f'<option value="{esc(r["slug"])}">{esc(r["name"])}</option>'
                   for r in sorted(index_rows, key=lambda r: r["name"]))


# ---------------------------------------------------------------- pages

def build_index(meta, index):
    rows = index["rows"]
    body = ['<h1>Can it do the job, and does it know when to stop?</h1>',
            f'<p class="lede">{meta["total_tasks"]} tasks against a frozen, self-hosted '
            'world, graded on the world\'s own final state, not on what the assistant said '
            'it did. Standings are ranked by head-to-head matchups won.</p>']

    # head-to-head quick picker
    body.append('<h2>Head to head</h2>')
    body.append('<p>Pick two assistants. Each of the 8 dimensions goes to the higher tested '
                'score on the tasks both actually ran.</p>')
    body.append(f'<div class="filters" data-compare="compare/">'
                f'<label>Left<select data-cmp-left>{agent_options(rows)}</select></label>'
                f'<label>Right<select data-cmp-right>{agent_options(rows)}</select></label>'
                f'<button class="reset" data-cmp-go>Open scorecard</button></div>')

    # filters + standings
    body.append('<h2>Standings</h2>')
    def opts(vals, labels=None):
        o = ['<option value="">any</option>']
        for v in vals:
            lab = labels.get(v, v) if labels else v
            o.append(f'<option value="{esc(v)}">{esc(lab)}</option>')
        return "".join(o)
    access_vals = sorted({r["access_tier"] for r in rows if r.get("access_tier")})
    host_vals = sorted({r["hosting_tier"] for r in rows if r.get("hosting_tier")})
    price_vals = sorted({r["pricing"] for r in rows if r.get("pricing")})
    surf_vals = sorted({s for r in rows for s in r.get("surfaces", [])})
    body.append(
        '<div class="filters" data-filters="#board">'
        f'<label>Availability<select data-filter="access">{opts(access_vals, ACCESS_LABEL)}</select></label>'
        f'<label>Hosting<select data-filter="hosting">{opts(host_vals, HOSTING_LABEL)}</select></label>'
        f'<label>Cost<select data-filter="pricing">{opts(price_vals, PRICING_LABEL)}</select></label>'
        f'<label>Surface<select data-filter="surface">{opts(surf_vals)}</select></label>'
        '<label>Search<input data-filter="name" placeholder="name"></label>'
        '<button class="reset">Reset</button></div>')

    head = ('<table id="board" data-sortable><thead><tr>'
            '<th class="nosort">#</th><th class="nosort">System</th><th>Index</th>'
            '<th>Record</th><th>Restraint</th><th>Capability</th><th>Strict<br>pass</th>'
            '<th>Guard<br>breaches</th><th>Injection<br>ASR</th><th>Median<br>reply</th>'
            '<th>Coverage</th></tr></thead><tbody>')
    trs = []
    for r in rows:
        surfaces = " ".join(r.get("surfaces", []))
        meta_pills = (access_pill(r.get("access_tier")) + " " + hosting_pill(r.get("hosting_tier")))
        breach = f'{r["guard_breach"]:.1f}%'
        asr = "&mdash;" if r.get("injection_asr") is None else f'{r["injection_asr"]:.0f}%'
        reply = "&mdash;" if r.get("wall_p50") is None else f'{r["wall_p50"]:.0f}s'
        trs.append(
            f'<tr class="{"breached" if r["guard_breach"] > 0 else ""}" '
            f'data-access="{esc(r.get("access_tier"))}" data-hosting="{esc(r.get("hosting_tier"))}" '
            f'data-pricing="{esc(r.get("pricing"))}" data-surface="{esc(surfaces)}" '
            f'data-name="{esc(r["name"])}">'
            f'<td class="rank num">{r["rank"]}</td>'
            f'<td><a class="sysname" href="agents/{esc(r["slug"])}.html">{esc(r["name"])}</a>'
            f'<span class="sysmeta">{meta_pills}</span></td>'
            f'{index_cell(r)}'
            f'<td class="num" data-sort="{r["record"]["won"]}">{record_str(r["record"])}</td>'
            f'<td class="num">{sc(r.get("restraint"))}</td>'
            f'<td class="num">{sc(r.get("capability"))}</td>'
            f'<td class="num">{r["strict_pass"]:.0f}%</td>'
            f'<td class="num {"bad" if r["guard_breach"] > 0 else ""}" data-sort="{r["guard_breach"]}">{breach}</td>'
            f'<td class="num {"bad" if (r.get("injection_asr") or 0) > 0 else ""}" data-sort="{r.get("injection_asr") or 0}">{asr}</td>'
            f'<td class="num" data-sort="{r.get("wall_p50") or 0}">{reply}</td>'
            f'<td class="num" data-sort="{r["coverage"]}">{r["tasks_covered"]}/{meta["total_tasks"]}</td>'
            f'</tr>')
    body.append(head + "".join(trs) + "</tbody></table>")
    body.append('<p class="note">Ranked by matchups won. <b>Index</b> is the weighted '
                'mean of the dimensions covered so far — a running mean until an assistant '
                'is fully tested. <b>Guard breaches</b> are actions the task ruled out; any '
                'breach scores that task zero. <b>Injection ASR</b> is how often a planted '
                'instruction got obeyed. See <a href="methodology.html">methodology</a>.</p>')

    # pareto — fully-tested real assistants only. The scripted reference floor/ceiling and
    # barely-covered provisional entries would otherwise pin the axes and repack everyone.
    real = [r for r in rows if r.get("status") != "reference" and not r.get("provisional")]
    body.append('<h2>Frontier</h2>')
    body.append('<p>The useful trade-offs, across the fully-tested assistants (scripted '
                'reference rows and still-provisional entries are left out so the axes fit the '
                'real range). Ringed points are on the Pareto frontier: nothing beats them on '
                'both axes at once.</p>')
    body.append('<figure>'
                + scatter(real, lambda r: r.get("capability"), lambda r: r.get("restraint"),
                          "capability", "restraint", "cap_restraint")
                + '<figcaption>Capability (everything except restraint) against restraint, '
                'zoomed to the tested range. Top-right is capable and restrained; bottom-right '
                'is capable but commits things it wasn\'t asked to.</figcaption></figure>')
    lat_rows = [r for r in real if r.get("wall_p50") is not None]
    if len(lat_rows) >= 2:
        body.append('<figure>'
                    + scatter(lat_rows, lambda r: r.get("capability"), lambda r: r.get("wall_p50"),
                              "capability", "median reply (s)", "cap_latency",
                              yinvert=True, yscale=1, score_y=False)
                    + '<figcaption>Capability against speed. Fast and capable is top-right; '
                    'ringed points are the frontier.</figcaption></figure>')

    body.append(latest_feed(meta, rows))
    og = "og/cover.svg"
    page(SITE / "index.html", "", "score",
         "abench — assistant capability and restraint",
         "Independent benchmark of AI assistants: capability, restraint, and head-to-head, "
         "graded on a frozen world's final state.", og, "".join(body), meta)


def score_breakdown(card):
    """Expandable per-task grading detail: which criteria failed and which guards
    breached, with the exact check behind each, so a score drop is traceable."""
    lost = [t for t in card["per_task"]
            if t.get("mean_score") is not None and (
                t["mean_score"] < 0.999
                or any(r["guard_breached"] for r in t["runs"])
                or any(r["false_completion"] for r in t["runs"]))]
    if not lost:
        return '<p class="muted">Nothing lost points — every task scored full marks.</p>'

    def what_col(c):
        if c.get("rubric"):
            return esc(c["rubric"])
        if c.get("check"):
            return f'<code>{esc(c["check"])}</code>'
        return "&mdash;"

    blocks = []
    for t in lost:
        badges = []
        if any(r["guard_breached"] for r in t["runs"]):
            badges.append('<span class="pill internal">guard breached</span>')
        if any(r["false_completion"] for r in t["runs"]):
            badges.append('<span class="pill wait">false completion</span>')
        if t.get("identity_blocked") or any(r.get("identity_blocked") for r in t["runs"]):
            badges.append('<span class="pill">identity blocked</span>')
        runs_html = []
        for r in t["runs"]:
            rows = []
            g_total = sum((c["weight"] or 0) for c in r["criteria"] if c["passed"] is not None)
            g_pass = sum((c["weight"] or 0) for c in r["criteria"] if c["passed"] is True)
            for c in r["criteria"]:
                if c["passed"] is True:
                    res, cls = "passed", "res-pass"
                elif c["passed"] is False:
                    res, cls = "failed", "res-fail"
                else:
                    res, cls = "not graded", "muted"
                rows.append(
                    f'<tr class="{"cfail" if c["passed"] is False else ""}">'
                    f'<td>{esc(c["id"])}</td>'
                    f'<td class="num">{(c["weight"] or 0):.0f}</td>'
                    f'<td class="{cls}">{res}</td><td>{what_col(c)}</td></tr>')
                if c.get("reason") and c["passed"] is not True:
                    rows.append(f'<tr><td colspan="3"></td>'
                                f'<td class="reason">judge: {esc(c["reason"])}</td></tr>')
            crit_tbl = ('<table class="brk"><thead><tr><th class="nosort">Criterion</th>'
                        '<th class="nosort">Weight</th><th class="nosort">Result</th>'
                        '<th class="nosort">What it checks</th></tr></thead><tbody>'
                        + "".join(rows) + '</tbody></table>')
            breached = [g for g in r["guards"] if not g["held"]]
            if breached:
                gl = "; ".join(f'<code>{esc(g["id"])}</code> ({esc(g.get("check") or "")})'
                               for g in breached)
                guard_html = (f'<p class="zero">Guard breached &rarr; whole task scored 0: {gl}</p>')
            elif r["guards"]:
                guard_html = f'<p class="muted small">All {len(r["guards"])} guard(s) held.</p>'
            else:
                guard_html = ""
            if r["guard_breached"]:
                math = (f'<p class="small">Score <b>0</b> — a guard breach overrides the '
                        f'{g_pass:.0f} of {g_total:.0f} criterion points it earned.</p>')
            else:
                math = (f'<p class="small">Score <b>{r["score"] * 100:.0f}</b> = {g_pass:.0f} of '
                        f'{g_total:.0f} weighted criterion points'
                        + ('. Un-graded rows are rubric checks no judge ran, left out of the total.'
                           if any(c["passed"] is None for c in r["criteria"]) else '.') + '</p>')
            ans = ""
            if r.get("no_answer"):
                ans = '<p class="muted small">No reply captured.</p>'
            elif r.get("answer_excerpt"):
                ans = f'<p class="muted small">Reply: &ldquo;{esc(r["answer_excerpt"])}&hellip;&rdquo;</p>'
            runs_html.append(crit_tbl + guard_html + math + ans)
        blocks.append(
            f'<details class="brk"><summary><b>{esc(t["task"])}</b> &middot; {esc(t["title"])} '
            f'&mdash; <span class="num">{t["mean_score"] * 100:.0f}</span> &middot; '
            f'tier {t["difficulty"]} {" ".join(badges)}</summary>'
            f'<div class="body">{"".join(runs_html)}</div></details>')
    return "".join(blocks)


def build_agent(meta, index, slug):
    card = load(f"agents/{slug}.json")
    ag = card["agent"]
    name = ag["name"]
    body = [f'<div class="crumb"><a href="../index.html">Scorecard</a> &rsaquo; {esc(name)}</div>',
            f'<h1>{esc(name)}</h1>']
    if ag.get("tagline"):
        body.append(f'<p class="lede">{esc(ag["tagline"])}</p>')

    # share + index headline
    prov = ' &middot; <span class="muted">provisional, still being tested</span>' if card.get("provisional") else ""
    ci = ""
    if card.get("ci_low") is not None:
        ci = f' <span class="muted small">95% CI {card["ci_low"]:.0f}&ndash;{card["ci_high"]:.0f}</span>'
    scored = card.get("tasks_scored", card["tasks_covered"])
    blocked = card.get("identity_blocked") or 0
    blocked_note = (f' &middot; {blocked:.0f}% identity-blocked, scored on {scored} tasks'
                    if blocked else "")
    body.append(f'<p><span class="idx" style="font-size:34px;font-weight:700">{card["index"]:.1f}</span>'
                f' index{ci} &middot; record {record_str(card["record"])} '
                f'&middot; {card["tasks_covered"]}/{meta["total_tasks"]} tasks{blocked_note}{prov}</p>')

    tweet = ("https://twitter.com/intent/tweet?text="
             + esc(f"{name} scores {card['index']:.1f} on abench").replace(" ", "%20"))
    body.append(f'<div class="share"><a href="{tweet}">Share on X</a>'
                f'<a href="../compare.html">Compare</a>'
                f'<a href="../api/v1/agents/{esc(slug)}.json">JSON</a></div>')

    # parameter matrix
    acc = ag.get("access", {})
    host = ag.get("hosting", {})
    eng = ag.get("engine", {})
    perm = ag.get("permissions", {})
    def cell(k, v):
        return f'<div class="cell"><div class="k">{esc(k)}</div><div class="v">{v}</div></div>'
    links = ag.get("links", {})
    link_html = " ".join(f'<a href="{esc(u)}">{esc(k)}</a>' for k, u in links.items() if u) or "&mdash;"
    cells = [
        cell("Availability", access_pill(acc.get("tier")) + f' <span class="muted small">{esc(acc.get("signup", ""))}</span>'),
        cell("Cost", esc(PRICING_LABEL.get(acc.get("pricing"), acc.get("pricing")))),
        cell("Hosting", hosting_pill(host.get("tier")) + (f' <span class="muted small">{esc(host.get("license"))}</span>' if host.get("license") else "")),
        cell("Offline", "Yes" if host.get("offline_capable") else "No"),
        cell("Data", esc(host.get("data_residency", "&mdash;"))),
        cell("Model", esc(eng.get("default_model") or ("bring your own" if eng.get("byo_model") else "&mdash;"))),
        cell("Surfaces", " ".join(f'<span class="pill">{esc(s)}</span>' for s in ag.get("surfaces", [])) or "&mdash;"),
        cell("Confirmation", esc(perm.get("confirmation", "&mdash;"))),
        cell("Status", esc(ag.get("status"))),
        cell("Links", link_html),
    ]
    if host.get("install"):
        cells.append(cell("Install", " ".join(f'<code>{esc(i)}</code>' for i in host["install"])))
    body.append('<h2>Parameters</h2><div class="matrix">' + "".join(cells) + "</div>")

    # axes + tiers
    body.append('<h2>Where the score comes from</h2>')
    body.append(axis_bars(card["axes"], meta, this=card["axes"]))
    if card.get("tiers"):
        body.append('<h2>Difficulty spread</h2>')
        tcells = []
        for k, label in meta["difficulty_tiers"].items():
            v = card["tiers"].get(k)
            tcells.append(f'<div><h3>Tier {esc(k)}</h3>'
                          f'<div class="axline"><span class="nm">{esc(label)}</span><b>{sc(v)}</b></div>'
                          f'<div class="bar"><i style="width:{(v or 0) * 100:.0f}%"></i></div></div>')
        body.append('<div class="axes">' + "".join(tcells) + "</div>")

    # declared vs verified
    body.append('<h2>Claimed vs measured</h2>')
    body.append('<p>What the product says it can do, next to what the runs actually show. A '
                'green claim that comes back red is the interesting case.</p>')
    dv = ['<table class="dv"><thead><tr><th>Capability</th><th>Declared</th>'
          '<th>Measured</th><th>Score</th><th>Tested</th></tr></thead><tbody>']
    for c in card["capabilities"]:
        declared = "Yes" if c["declared"] else "No"
        gap = ""
        if c["declared"] and c["measured"] == "failed":
            gap = ' class="claimgap"'
        elif not c["declared"] and c["measured"] == "verified":
            gap = ' class="claimwin"'
        score = "&mdash;" if c["score"] is None else f'{c["score"] * 100:.0f}'
        dv.append(f'<tr><td>{esc(c["name"])}</td><td>{declared}</td>'
                  f'<td{gap}><span class="dot {c["measured"]}"></span>{esc(c["measured"])}</td>'
                  f'<td class="num">{score}</td><td class="num">{c["tested"]}/{c["total"]}</td></tr>')
    dv.append("</tbody></table>")
    body.append("".join(dv))

    # head to head
    if card["head_to_head"]:
        body.append('<h2>Head to head</h2>')
        h = ['<table data-sortable><thead><tr><th class="nosort">Opponent</th><th>Result</th>'
             '<th>Won</th><th>Lost</th><th>Even</th><th>Compared</th><th>Shared</th></tr></thead><tbody>']
        for o in card["head_to_head"]:
            pair = sorted([slug, o["opponent"]])
            href = f'compare/{pair[0]}-vs-{pair[1]}.html'
            if o["outcome"] == "a":
                res, cls = "leads", "win"
            elif o["outcome"] == "b":
                res, cls = "trails", "bad"
            else:
                res, cls = "even", ""
            if o["too_close"]:
                res = "too close"
            h.append(f'<tr><td><a href="../{href}">{esc(o["opponent_name"])}</a></td>'
                     f'<td class="{cls}">{res}</td>'
                     f'<td class="num">{o["won"]}</td><td class="num">{o["lost"]}</td>'
                     f'<td class="num">{o["even"]}</td><td class="num">{o["compared"]}</td>'
                     f'<td class="num">{o["shared_tasks"]}</td></tr>')
        h.append("</tbody></table>")
        body.append("".join(h))

    # per-task
    body.append('<h2>Every task</h2>')
    pt = ['<table data-sortable><thead><tr><th class="nosort">Task</th><th class="nosort">Title</th>'
          '<th>Axis</th><th>Tier</th><th>Score</th><th>Runs</th></tr></thead><tbody>']
    for t in card["per_task"]:
        breach = any(r["guard_breached"] for r in t["runs"])
        pt.append(f'<tr class="{"breached" if breach else ""}">'
                  f'<td>{esc(t["task"])}</td><td>{esc(t["title"])}</td>'
                  f'<td>{esc(t["axis"])}</td><td class="num">{t["difficulty"]}</td>'
                  f'<td class="num" data-sort="{t["mean_score"] if t.get("mean_score") is not None else -1}">'
                  + (f'{t["mean_score"] * 100:.0f}' if t.get("mean_score") is not None
                     else "n/a") + '</td>'
                  f'<td class="num">{len(t["runs"])}</td></tr>')
    pt.append("</tbody></table>")
    body.append("".join(pt))

    # where points were lost — the explainability layer
    body.append('<h2>Where points were lost</h2>')
    body.append('<p>Every task that did not score full marks, and exactly which criterion '
                'or guard cost the points. A breached guard zeros the task on its own.</p>')
    body.append(score_breakdown(card))

    # embed badge
    badge_url = f'{SITE_URL}/embed/{slug}.svg'
    md = f'[![abench: {name} {card["index"]:.1f}]({badge_url})]({SITE_URL}/agents/{slug})'
    body.append('<h2>Embed the badge</h2>')
    body.append(f'<p>Drop this into a README. <button class="copybtn" data-copy="#badgemd">copy</button></p>')
    body.append(f'<pre id="badgemd">{esc(md)}</pre>')

    og = f"../og/{slug}.svg"
    page(SITE / "agents" / f"{slug}.html", "../", "score", f"{name} — abench scorecard",
         f"{name}: index {card['index']:.1f} on abench. Availability, hosting, declared vs "
         f"measured capabilities, and head-to-head.", og, "".join(body), meta)


def build_matchup(meta, index, a, b, pairs):
    key = f"{a}|{b}" if f"{a}|{b}" in pairs else f"{b}|{a}"
    h = pairs[key]
    # orient so displayed a=a,b=b regardless of storage order
    flip = key.startswith(b)
    na = next(r["name"] for r in index["rows"] if r["slug"] == a)
    nb = next(r["name"] for r in index["rows"] if r["slug"] == b)
    a_wins = h["b_wins"] if flip else h["a_wins"]
    b_wins = h["a_wins"] if flip else h["b_wins"]

    def wof(w):  # translate stored winner to display side
        if w == "tie":
            return "tie"
        if flip:
            return "b" if w == "a" else "a"
        return w

    body = [f'<div class="crumb"><a href="../compare.html">Head to head</a> &rsaquo; '
            f'{esc(na)} vs {esc(nb)}</div>']
    lead = na if a_wins > b_wins else nb if b_wins > a_wins else None
    headline = (f"{esc(lead)} leads over {h['compared_axes']} compared" if lead
                else "Dead even") + (" &middot; too close to call" if h["too_close"] else "")
    body.append(
        '<div class="h2h">'
        f'<div class="side"><span class="aname"><a href="../agents/{esc(a)}.html">{esc(na)}</a></span></div>'
        f'<div class="score">{a_wins}&ndash;{b_wins}<small>{headline}</small></div>'
        f'<div class="side right"><span class="aname"><a href="../agents/{esc(b)}.html">{esc(nb)}</a></span></div>'
        '</div>')
    body.append(f'<p class="muted small">Compared only on the {h["shared_tasks"]} tasks both '
                'assistants actually ran. Each dimension goes to the higher mean; dimensions '
                'within 5 points, or backed by a single shared task, are called even.</p>')

    # axis detail
    body.append('<h2>By dimension</h2>')
    ax = ['<table><thead><tr><th class="nosort">Dimension</th><th>' + esc(na) + '</th><th>'
          + esc(nb) + '</th><th>Shared</th><th class="nosort">Winner</th></tr></thead><tbody>']
    for d in h["axis_detail"]:
        av = d["a"] if not flip else d["b"]
        bv = d["b"] if not flip else d["a"]
        w = wof(d["winner"])
        win_name = "even" if w == "tie" else (na if w == "a" else nb)
        ax.append(f'<tr><td>{esc(d["name"])}</td>'
                  f'<td class="num {"win" if w == "a" else ""}">{av * 100:.0f}</td>'
                  f'<td class="num {"win" if w == "b" else ""}">{bv * 100:.0f}</td>'
                  f'<td class="num">{d["shared"]}</td><td>{esc(win_name)}</td></tr>')
    ax.append("</tbody></table>")
    body.append("".join(ax))

    # task detail
    body.append('<h2>Task by task</h2>')
    td = ['<table data-sortable><thead><tr><th class="nosort">Task</th><th class="nosort">Title</th>'
          '<th>Axis</th><th>' + esc(na) + '</th><th>' + esc(nb) + '</th><th class="nosort">Winner</th></tr></thead><tbody>']
    for d in h["task_detail"]:
        av = d["a"] if not flip else d["b"]
        bv = d["b"] if not flip else d["a"]
        w = wof(d["winner"])
        win_name = "even" if w == "tie" else (na if w == "a" else nb)
        td.append(f'<tr><td>{esc(d["task"])}</td><td>{esc(d["title"])}</td>'
                  f'<td>{esc(d["axis"])}</td>'
                  f'<td class="num {"win" if w == "a" else ""}">{av * 100:.0f}</td>'
                  f'<td class="num {"win" if w == "b" else ""}">{bv * 100:.0f}</td>'
                  f'<td>{esc(win_name)}</td></tr>')
    td.append("</tbody></table>")
    body.append("".join(td))

    pair = sorted([a, b])
    og = f"../og/{pair[0]}-vs-{pair[1]}.svg"
    page(SITE / "compare" / f"{pair[0]}-vs-{pair[1]}.html", "../", "cmp",
         f"{na} vs {nb} — abench head to head",
         f"{na} vs {nb}: {a_wins}-{b_wins} across {h['compared_axes']} dimensions on abench.",
         og, "".join(body), meta)


def build_compare(meta, index, pairs):
    rows = index["rows"]
    body = ['<h1>Head to head</h1>',
            '<p class="lede">Pick two assistants. Each dimension goes to the higher tested '
            'score on the tasks both actually ran.</p>']
    body.append(f'<div class="filters" data-compare="compare/">'
                f'<label>Left<select data-cmp-left>{agent_options(rows)}</select></label>'
                f'<label>Right<select data-cmp-right>{agent_options(rows)}</select></label>'
                f'<button class="reset" data-cmp-go>Open scorecard</button></div>')
    body.append('<h2>All matchups</h2>')
    grid = ['<div class="cards-grid">']
    seen = set()
    for key, h in sorted(pairs.items()):
        a, b = key.split("|")
        pair = tuple(sorted([a, b]))
        if pair in seen:
            continue
        seen.add(pair)
        na = next(r["name"] for r in rows if r["slug"] == pair[0])
        nb = next(r["name"] for r in rows if r["slug"] == pair[1])
        aw = h["a_wins"] if key.startswith(pair[0]) else h["b_wins"]
        bw = h["b_wins"] if key.startswith(pair[0]) else h["a_wins"]
        grid.append(f'<a class="acard" href="compare/{pair[0]}-vs-{pair[1]}.html">'
                    f'<div class="an small">{esc(na)}<br>vs {esc(nb)}</div>'
                    f'<div class="ai">{aw}&ndash;{bw}</div>'
                    f'<div class="rec">{h["shared_tasks"]} shared tasks</div></a>')
    grid.append("</div>")
    body.append("".join(grid))
    page(SITE / "compare.html", "", "cmp", "Head to head — abench",
         "Compare any two AI assistants dimension by dimension on abench.",
         "og/cover.svg", "".join(body), meta)


def build_dimensions(meta, dims_data, index):
    dims = dims_data["dimensions"]
    # directory
    body = ['<h1>Dimensions</h1>',
            '<p class="lede">The eight things abench scores, and how much each weighs in the '
            'index. Restraint and multi-step carry the most.</p>']
    dl = ['<table data-sortable><thead><tr><th class="nosort">Dimension</th><th>Weight</th>'
          '<th>Tasks</th><th class="nosort">Leader</th></tr></thead><tbody>']
    for d in dims:
        leader = d["rows"][0] if d["rows"] else None
        lname = next((r["name"] for r in index["rows"] if r["slug"] == leader["slug"]), leader["slug"]) if leader else "&mdash;"
        lead_html = f'{esc(lname)} ({leader["score"] * 100:.0f})' if leader else "&mdash;"
        dl.append(f'<tr><td><a href="{esc(d["axis"])}.html">{esc(d["name"])}</a></td>'
                  f'<td class="num">{d["weight"] * 100:.0f}%</td>'
                  f'<td class="num">{len(d["tasks"])}</td><td>{lead_html}</td></tr>')
    dl.append("</tbody></table>")
    body.append("".join(dl))
    page(SITE / "dimensions" / "index.html", "../", "dim", "Dimensions — abench",
         "The eight dimensions abench scores and how they weigh into the index.",
         "../og/cover.svg", "".join(body), meta)

    name_of = {r["slug"]: r["name"] for r in index["rows"]}
    for d in dims:
        ax = d["axis"]
        b = [f'<div class="crumb"><a href="index.html">Dimensions</a> &rsaquo; {esc(d["name"])}</div>',
             f'<h1>{esc(d["name"])}</h1>',
             f'<p class="lede">{esc(AXIS_BLURB.get(ax, ""))}</p>',
             f'<p class="muted">Weight in the index: <b>{d["weight"] * 100:.0f}%</b> &middot; '
             f'{len(d["tasks"])} tasks: {", ".join(esc(t) for t in d["tasks"])}</p>']
        b.append('<h2>Ranking</h2>')
        tb = ['<table data-sortable><thead><tr><th class="nosort">#</th><th class="nosort">System</th>'
              '<th>Score</th></tr></thead><tbody>']
        for i, r in enumerate(d["rows"]):
            tb.append(f'<tr><td class="rank num">{i + 1}</td>'
                      f'<td><a href="../agents/{esc(r["slug"])}.html">{esc(name_of.get(r["slug"], r["slug"]))}</a></td>'
                      f'<td class="num" data-sort="{r["score"]}">{r["score"] * 100:.0f}</td></tr>')
        tb.append("</tbody></table>")
        b.append("".join(tb))
        page(SITE / "dimensions" / f"{ax}.html", "../", "dim", f"{d['name']} — abench dimension",
             f"How assistants rank on {d['name'].lower()} in abench.", "../og/cover.svg",
             "".join(b), meta)


def build_tasks(meta, tasks_data):
    body = ['<h1>The tasks</h1>',
            f'<p class="lede">{meta["total_tasks"]} tasks across eight dimensions and four '
            'difficulty tiers, each tagged with the capabilities it exercises.</p>']
    inv = ['<div class="tasks">']
    for t in tasks_data["tasks"]:
        caps = " ".join(t.get("capabilities", []))
        inv.append(f'<div><span class="tid">{esc(t["id"])}</span> {esc(t["title"])}'
                   f'<span class="ax"> &middot; {esc(t["axis"])} &middot; tier {t["difficulty"]} '
                   f'&middot; {esc(caps)}</span></div>')
    inv.append("</div>")
    body.append("".join(inv))
    if tasks_data.get("hardest"):
        body.append('<h2>Hardest checks so far</h2>')
        body.append('<p>Criteria that failed most often across every run.</p>')
        hb = ['<div class="tasks">']
        for k, n in tasks_data["hardest"]:
            hb.append(f'<div><span class="tid">{n}&times;</span> {esc(k)}</div>')
        hb.append("</div>")
        body.append("".join(hb))
    page(SITE / "tasks.html", "", "task", "The tasks — abench",
         "Every abench task, its dimension, difficulty tier and the capabilities it exercises.",
         "og/cover.svg", "".join(body), meta)


def build_methodology(meta):
    weights = ", ".join(f'{meta["axis_names"][a]} {w * 100:.0f}%'
                        for a, w in meta["axis_weights"].items())
    body = ['<h1>How scoring works</h1>',
            '<p class="lede">Every result is checked against the world\'s final state and '
            'action log, never against what the assistant claimed. Reference rows are scripted '
            'to set a calibrated floor and ceiling.</p>',
            '<h2>Per task</h2>',
            '<p>Each task has weighted criteria and a set of guards. Criteria are scored as a '
            'weighted pass rate. Guards are hard lines — cancelling a booking, placing an order, '
            'sending a message. Breaching any guard scores the whole task zero, regardless of '
            'what else it got right. A task is a <b>strict pass</b> only if every criterion '
            'passes and no guard is breached.</p>',
            '<h2>The index</h2>',
            f'<p>Scores roll up per dimension, then the index is the weighted mean of the '
            f'dimensions covered so far, renormalised to what has actually been tested: '
            f'{esc(weights)}. Because it renormalises, the index is a running mean and stays '
            f'<i>provisional</i> until every applicable dimension is scored.</p>',
            '<pre>index = 100 &times; &Sigma; (dimension_score &times; weight) / &Sigma; weight   '
            '(over covered dimensions)</pre>',
            '<h2>Confidence</h2>',
            f'<p>At roughly one run per task, raw index gaps are noisy. Each index carries a 95% '
            f'confidence interval from a {meta["bootstrap_n"]}-sample bootstrap that resamples '
            f'tasks within each dimension. When two assistants\' intervals overlap, their '
            f'matchup is marked <b>too close to call</b> rather than shown as a win.</p>',
            '<h2>Head to head</h2>',
            f'<p>Two assistants are compared only on the tasks both actually ran. Each dimension '
            f'goes to the higher mean on those shared tasks; a gap under {meta["h2h_epsilon"] * 100:.0f} '
            f'points, or a dimension backed by a single shared task, is called even. The matchup '
            f'record is wins&ndash;losses&ndash;evens over dimensions, and standings rank by '
            f'matchups won.</p>',
            '<h2>Difficulty tiers</h2>',
            '<p>Tasks span four tiers so systems spread out instead of bunching at the ceiling. '
            'A falling line from tier 1 to tier 4 means the suite is still discriminating.</p>',
            '<h2>Claimed vs measured</h2>',
            '<p>Each assistant declares which capabilities it supports; each task is tagged with '
            'the capabilities it exercises. A capability is <b>verified</b> when the tagged tasks '
            'pass, <b>failed</b> when they run and don\'t, and <b>untested</b> when nothing '
            'covering it has run. Declared-but-failed is the gap worth watching.</p>']
    page(SITE / "methodology.html", "", "meth", "Methodology — abench",
         "How abench scores capability and restraint: guards, the weighted index, bootstrap "
         "confidence intervals and head-to-head.", "og/cover.svg", "".join(body), meta)


def build_about(meta):
    body = ['<h1>About abench</h1>',
            '<p class="lede">An independent benchmark for AI assistants — the ones that book '
            'tables, check you in, clear your inbox — measuring whether they can do the job '
            'and whether they know when to stop.</p>',
            '<p>Most assistant demos show capability. abench weights capability against '
            '<b>restraint</b>: not cancelling the booking, not placing the order, not obeying '
            'the instruction planted in a product page or an email. Everything is graded on a '
            'frozen, self-hosted world\'s final state, so a confident wrong answer scores '
            'nothing.</p>',
            '<h2>Contribute</h2>',
            '<p>The registry and the suite are open. You can:</p>',
            f'<ul>'
            f'<li><a href="{REPO}/issues/new?template=request-a-test.yml">Request a test</a> '
            f'for an assistant that isn\'t here yet.</li>'
            f'<li><a href="{REPO}/issues/new?template=submit-an-agent.yml">Submit an agent</a> '
            f'— or open a PR adding <code>agents/&lt;slug&gt;.json</code>.</li>'
            f'<li><a href="{REPO}/issues/new?template=report-a-result.yml">Report a result</a> '
            f'you can reproduce.</li>'
            f'</ul>',
            '<p class="muted small">Every agent file is validated in CI against '
            '<code>agents/_schema.json</code>, and the suite self-test runs on every pull '
            'request.</p>',
            f'<p><a href="{esc(REPO)}">Source on GitHub</a> &middot; '
            f'<a href="api/v1/index.json">JSON API</a> &middot; '
            f'<a href="api/v1/runs.jsonl">every trajectory</a></p>']
    page(SITE / "about.html", "", "about", "About — abench",
         "What abench is, why restraint matters, and how to add an assistant.",
         "og/cover.svg", "".join(body), meta)


def main():
    meta = load("meta.json")
    index = load("index.json")
    dims = load("dimensions.json")
    tasks = load("tasks.json")
    h2h = load("h2h.json")
    pairs = h2h["pairs"]

    build_index(meta, index)
    build_compare(meta, index, pairs)
    build_dimensions(meta, dims, index)
    build_tasks(meta, tasks)
    build_methodology(meta)
    build_about(meta)
    for r in index["rows"]:
        build_agent(meta, index, r["slug"])
    seen = set()
    for key in pairs:
        a, b = key.split("|")
        pr = tuple(sorted([a, b]))
        if pr in seen:
            continue
        seen.add(pr)
        build_matchup(meta, index, pr[0], pr[1], pairs)

    print(f"built site: {len(index['rows'])} agents, {len(seen)} matchups, "
          f"{len(dims['dimensions'])} dimensions -> site/")


if __name__ == "__main__":
    main()
