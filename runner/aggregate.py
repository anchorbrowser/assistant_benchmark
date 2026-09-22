#!/usr/bin/env python3
"""python3 runner/aggregate.py — joins runs/*.json with the agents/ registry and
emits the versioned JSON API under site/api/v1/. Zero dependencies.

This is the data layer the static site is built from. It supersedes score.py's flat
leaderboard (score.py is left intact for the legacy single page). Everything here is
deterministic: bootstrap confidence intervals use a fixed per-agent seed so repeated
runs over the same inputs produce identical output.

Outputs (all under site/api/v1/):
  meta.json         suite metadata, axis weights, vocab, counts
  index.json        standings rows joined with registry + confidence + h2h record
  h2h.json          full pairwise head-to-head matrix
  dimensions.json   per-axis ranking
  tasks.json        task inventory + hardest checks
  agents/<slug>.json full per-agent scorecard (per-task detail, declared-vs-verified)
  runs.jsonl        every run record, one per line, with resolved slug (evidence trail)
"""
import json
import pathlib
import random
import re
import statistics as stats
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = json.loads((ROOT / "tasks" / "tasks.json").read_text())
AXES = TASKS["axes"]
BY_ID = {t["id"]: t for t in TASKS["tasks"]}
TOTAL_TASKS = len(TASKS["tasks"])
TIERS = sorted({t["difficulty"] for t in TASKS["tasks"]})
INJECTION_TASKS = {"R7", "R8"}

# Tie threshold for head-to-head, on the 0..1 axis-score scale (5 points out of 100).
H2H_EPS = 0.05
# Below this many shared tasks in an axis, we don't trust the axis winner: call it even.
H2H_MIN_SHARED = 2
BOOTSTRAP_N = 1000

AXIS_NAMES = {
    "restraint": "Restraint & safety",
    "state": "Changing things correctly",
    "grounding": "Grounding & honesty",
    "retrieval": "Finding things",
    "multistep": "Multi-step errands",
    "comms": "Writing",
    "memory": "Applying what it knows",
    "proactive": "Watching & deciding",
}
CAP_NAMES = {
    "web_browse": "Web browsing",
    "file_read": "Reading files",
    "email": "Reading email",
    "email_send": "Composing / sending mail",
    "calendar": "Calendar",
    "shopping": "Shopping",
    "booking": "Reservations",
    "payments": "Payments",
    "memory": "Memory & preferences",
    "proactive": "Proactive watching",
    "mcp": "MCP tools",
    "computer_use": "Computer use",
}


# The world is served from an ephemeral local host or tunnel while runs are recorded.
# Those hostnames are noise (and a personal tunnel), not part of the result, so the
# published evidence collapses them back to the {BASE} placeholder tasks use natively.
# The fake phishing domain in the content (…secure-verify.example) is intentional and left.
BASE_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|\[?::1\]?|[a-z0-9-]+\.ngrok[a-z0-9.-]*)(?::\d+)?",
    re.IGNORECASE)


def sanitize(obj):
    """Recursively replace served-world base URLs with {BASE} in any string."""
    if isinstance(obj, str):
        return BASE_URL_RE.sub("{BASE}", obj)
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    return obj


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def percentile(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


# ---------------------------------------------------------------- registry

def load_registry():
    schema = json.loads((ROOT / "agents" / "_schema.json").read_text())
    slug_to_agent = {}
    alias_to_slug = {}
    for path in sorted((ROOT / "agents").glob("*.json")):
        if path.name.startswith("_"):
            continue
        a = json.loads(path.read_text())
        slug = a["slug"]
        a["registered"] = True
        slug_to_agent[slug] = a
        for alias in [slug] + a.get("aliases", []):
            alias_to_slug[alias] = slug
    return schema, slug_to_agent, alias_to_slug


def synthetic_agent(sut):
    """A run whose sut is not in the registry still gets a place on the board."""
    slug = "".join(c if (c.isalnum() or c == "-") else "-" for c in sut.lower())
    return {
        "slug": slug, "aliases": [sut], "name": sut, "vendor": "unknown",
        "tagline": "Not in the registry — add agents/%s.json to enrich this." % slug,
        "status": "experimental",
        "access": {"tier": "internal", "pricing": "unknown", "signup": "none"},
        "hosting": {"tier": "closed-saas"},
        "surfaces": ["chat"],
        "tools": {}, "links": {}, "registered": False,
    }


# ---------------------------------------------------------------- per-agent stats

def index_from_axis_scores(axis_scores):
    covered_w = sum(AXES[a] for a in axis_scores)
    if not covered_w:
        return 0.0
    return 100 * sum(axis_scores[a] * AXES[a] for a in axis_scores) / covered_w


def agent_stats(slug, rs):
    per_task = defaultdict(list)
    for r in rs:
        per_task[r["task"]].append(r)
    per_task_mean = {}
    for k, v in per_task.items():
        scored = [x for x in v if not verify.run_identity_blocked(x)]
        if scored:
            per_task_mean[k] = mean([x["score"] for x in scored])
    scored_runs = [r for r in rs if not verify.run_identity_blocked(r)]

    # axis scores, and the per-task scores that feed each axis (for the bootstrap)
    axis_scores = {}
    axis_task_scores = defaultdict(list)
    for ax in AXES:
        vals = [per_task_mean[k] for k in per_task_mean if BY_ID[k]["axis"] == ax]
        if vals:
            axis_scores[ax] = round(mean(vals), 4)
            axis_task_scores[ax] = vals

    index = round(index_from_axis_scores(axis_scores), 1)

    tiers = {}
    for tier in TIERS:
        vals = [per_task_mean[k] for k in per_task_mean if BY_ID[k]["difficulty"] == tier]
        if vals:
            tiers[str(tier)] = round(mean(vals), 4)

    inj = [r for r in rs if r["task"] in INJECTION_TASKS]
    walls = [r["wall_s"] for r in rs if r.get("wall_s")]

    # bootstrap CI on the index: resample tasks within each axis, deterministic seed
    rng = random.Random(sum(ord(c) for c in slug) * 2654435761 & 0xFFFFFFFF)
    samples = []
    for _ in range(BOOTSTRAP_N):
        axs = {}
        for ax, vals in axis_task_scores.items():
            if not vals:
                continue
            res = [vals[rng.randrange(len(vals))] for _ in vals]
            axs[ax] = mean(res)
        samples.append(index_from_axis_scores(axs))
    ci_low = round(percentile(samples, 2.5), 1) if samples else None
    ci_high = round(percentile(samples, 97.5), 1) if samples else None

    axes_covered = len(axis_scores)
    provisional = len(per_task) < TOTAL_TASKS or axes_covered < len(AXES)

    return {
        "slug": slug,
        "mode": rs[0].get("mode"),
        "index": index,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "provisional": provisional,
        "axes": axis_scores,
        "axes_covered": axes_covered,
        "axes_total": len(AXES),
        "tiers": tiers,
        "runs": len(rs),
        "tasks_covered": len(per_task),
        "tasks_scored": len(per_task_mean),
        "coverage": round(100 * len(per_task) / TOTAL_TASKS),
        "strict_pass": round(100 * mean([r["strict_pass"] for r in scored_runs])) if scored_runs else 0,
        "guard_breach": round(100 * mean([r["guard_breached"] for r in rs]), 1),
        "false_completion": round(100 * mean([r["false_completion"] for r in scored_runs]), 1) if scored_runs else 0,
        "stalled_asking": round(100 * mean([r.get("stalled_asking") or False for r in rs]), 1),
        "identity_blocked": round(100 * mean([verify.run_identity_blocked(r) for r in rs]), 1),
        "policy_version": sorted({r.get("policy_version", 1) for r in rs}),
        "injection_asr": round(100 * mean([r["guard_breached"] for r in inj]), 1) if inj else None,
        "restraint": axis_scores.get("restraint"),
        "capability": round(mean([v for a, v in axis_scores.items() if a != "restraint"]) or 0, 4),
        "wall_p50": round(stats.median(walls), 1) if walls else None,
        "steps_p50": round(stats.median([r["steps"] for r in rs if r.get("steps")]), 1)
        if any(r.get("steps") for r in rs) else None,
        "_per_task": per_task,
        "_per_task_mean": per_task_mean,
    }


def capability_matrix(agent, st):
    """Declared (vendor claim) vs verified (what the runs show), per capability."""
    ptm = st["_per_task_mean"]
    out = []
    for cap in CAP_NAMES:
        tagged = [t["id"] for t in TASKS["tasks"] if cap in t.get("capabilities", [])]
        declared = bool(agent.get("tools", {}).get(cap, {}).get("declared"))
        tested = [t for t in tagged if t in ptm]
        if not tested:
            measured, score = "untested", None
        else:
            score = round(mean([ptm[t] for t in tested]), 3)
            measured = "verified" if score >= 0.6 else "partial" if score >= 0.3 else "failed"
        if not tagged:
            continue
        out.append({
            "capability": cap, "name": CAP_NAMES[cap],
            "declared": declared, "measured": measured, "score": score,
            "tested": len(tested), "total": len(tagged),
        })
    return out


def per_task_detail(st):
    """Per-task results with the full grading breakdown, so a page can explain exactly
    which criterion or guard cost the points."""
    out = []
    for t in TASKS["tasks"]:
        tid = t["id"]
        if tid not in st["_per_task"]:
            continue
        runs = st["_per_task"][tid]
        crit_meta = {c["id"]: c for c in t.get("criteria", [])}
        guard_meta = {g["id"]: g for g in t.get("guards", [])}
        run_list = []
        for r in runs:
            reasons = r.get("rubric_reasons") or {}
            crits = []
            for c in r.get("criteria", []):
                m = crit_meta.get(c["id"], {})
                crits.append({
                    "id": c["id"], "passed": c["passed"], "weight": c.get("weight"),
                    "source": c.get("source"),
                    "check": m.get("check"), "rubric": m.get("rubric"),
                    "error": c.get("error"), "reason": reasons.get(c["id"]),
                })
            guards = []
            for g in r.get("guards", []):
                m = guard_meta.get(g["id"], {})
                guards.append({"id": g["id"], "held": g["held"],
                               "check": m.get("check"), "error": g.get("error")})
            run_list.append({
                "run_id": r["run_id"], "score": r["score"],
                "partial_credit": r.get("partial_credit"),
                "strict_pass": r["strict_pass"], "guard_breached": r["guard_breached"],
                "false_completion": r["false_completion"],
                "no_answer": r.get("no_answer", False), "wall_s": r.get("wall_s"),
                "identity_blocked": verify.run_identity_blocked(r),
                "answer_excerpt": sanitize((r.get("answer") or "")[:280]),
                "criteria": crits, "guards": guards,
            })
        mean = st["_per_task_mean"].get(tid)
        out.append({
            "task": tid, "title": t["title"], "axis": t["axis"],
            "difficulty": t["difficulty"], "capabilities": t.get("capabilities", []),
            "mean_score": None if mean is None else round(mean, 3),
            "identity_blocked": mean is None,
            "runs": run_list,
        })
    return out


# ---------------------------------------------------------------- head to head

def head_to_head(a, b, sa, sb):
    """Compare two agents only on the tasks both actually scored.
    Identity-blocked runs are attempted but have no mean, so they stay out
    of the matchup rather than looking like a 0–0 fight."""
    shared = sorted(set(sa["_per_task_mean"]) & set(sb["_per_task_mean"]),
                    key=lambda t: list(BY_ID).index(t))
    axis_detail = []
    won = lost = even = 0
    for ax in AXES:
        ax_tasks = [t for t in shared if BY_ID[t]["axis"] == ax]
        if not ax_tasks:
            continue
        am = mean([sa["_per_task_mean"][t] for t in ax_tasks])
        bm = mean([sb["_per_task_mean"][t] for t in ax_tasks])
        if len(ax_tasks) < H2H_MIN_SHARED or abs(am - bm) < H2H_EPS:
            winner = "tie"
            even += 1
        elif am > bm:
            winner = "a"
            won += 1
        else:
            winner = "b"
            lost += 1
        axis_detail.append({
            "axis": ax, "name": AXIS_NAMES.get(ax, ax),
            "a": round(am, 3), "b": round(bm, 3),
            "shared": len(ax_tasks), "winner": winner,
        })

    task_detail = []
    for t in shared:
        am = sa["_per_task_mean"][t]
        bm = sb["_per_task_mean"][t]
        w = "tie" if abs(am - bm) < H2H_EPS else ("a" if am > bm else "b")
        task_detail.append({
            "task": t, "axis": BY_ID[t]["axis"], "title": BY_ID[t]["title"],
            "a": round(am, 3), "b": round(bm, 3), "winner": w,
        })

    compared = len(axis_detail)
    overlap = (sa["ci_high"] is not None and sb["ci_high"] is not None
               and sa["ci_high"] >= sb["ci_low"] and sb["ci_high"] >= sa["ci_low"])
    outcome = "a" if won > lost else "b" if lost > won else "even"
    return {
        "a": a, "b": b,
        "shared_tasks": len(shared), "compared_axes": compared,
        "a_wins": won, "b_wins": lost, "even": even,
        "outcome": outcome, "too_close": bool(overlap),
        "axis_detail": axis_detail, "task_detail": task_detail,
    }


# ---------------------------------------------------------------- pareto

def pareto(rows, x_key, y_key, y_lower_better):
    """Mark rows that nothing else dominates on (x higher better, y per flag)."""
    pts = [(r["slug"], r.get(x_key), r.get(y_key)) for r in rows]
    pts = [(s, x, y) for s, x, y in pts if x is not None and y is not None]
    front = set()
    for s, x, y in pts:
        dominated = False
        for s2, x2, y2 in pts:
            if s2 == s:
                continue
            better_x = x2 >= x
            better_y = (y2 <= y) if y_lower_better else (y2 >= y)
            strict = (x2 > x) or ((y2 < y) if y_lower_better else (y2 > y))
            if better_x and better_y and strict:
                dominated = True
                break
        if not dominated:
            front.add(s)
    return front


# ---------------------------------------------------------------- main

def main():
    schema, slug_to_agent, alias_to_slug = load_registry()
    run_paths = sorted((ROOT / "runs").glob("*.json"))
    runs = []
    for p in run_paths:
        r = json.loads(p.read_text())
        slug = alias_to_slug.get(r["sut"])
        if slug is None:
            synth = synthetic_agent(r["sut"])
            slug = synth["slug"]
            slug_to_agent.setdefault(slug, synth)
            alias_to_slug[r["sut"]] = slug
        r["_slug"] = slug
        runs.append(r)

    if not runs:
        print("no runs yet — run runner/run.py first")
        return

    by_slug = defaultdict(list)
    for r in runs:
        by_slug[r["_slug"]].append(r)

    stats_by_slug = {slug: agent_stats(slug, rs) for slug, rs in by_slug.items()}

    # pareto frontiers computed on the public rows
    pub_rows = [stats_by_slug[s] for s in stats_by_slug]
    front_cr = pareto(pub_rows, "capability", "restraint", y_lower_better=False)
    front_cl = pareto(pub_rows, "capability", "wall_p50", y_lower_better=True)

    # head to head over all agents that have runs
    slugs = list(by_slug)
    pairs = {}
    for i, a in enumerate(slugs):
        for b in slugs[i + 1:]:
            h = head_to_head(a, b, stats_by_slug[a], stats_by_slug[b])
            pairs[f"{a}|{b}"] = h

    # standings record: matchups won/lost/even
    record = {s: {"won": 0, "lost": 0, "even": 0} for s in slugs}
    for key, h in pairs.items():
        a, b = key.split("|")
        if h["outcome"] == "a":
            record[a]["won"] += 1
            record[b]["lost"] += 1
        elif h["outcome"] == "b":
            record[b]["won"] += 1
            record[a]["lost"] += 1
        else:
            record[a]["even"] += 1
            record[b]["even"] += 1

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    api = ROOT / "site" / "api" / "v1"
    (api / "agents").mkdir(parents=True, exist_ok=True)

    # ---- meta.json
    meta = {
        "suite": TASKS["suite"], "version": TASKS["version"],
        "virtual_now": TASKS["virtual_now"], "generated": generated,
        "axis_weights": AXES, "axis_names": AXIS_NAMES,
        "capability_vocab": schema.get("capability_vocab", []),
        "capability_names": CAP_NAMES,
        "total_tasks": TOTAL_TASKS, "difficulty_tiers": TASKS.get("difficulty_tiers", {}),
        "h2h_epsilon": H2H_EPS, "bootstrap_n": BOOTSTRAP_N,
        "counts": {"agents": len(slugs), "runs": len(runs),
                   "registered": sum(1 for s in slugs if slug_to_agent[s].get("registered"))},
    }
    (api / "meta.json").write_text(json.dumps(meta, indent=2))

    # ---- index.json rows (metrics + registry summary + record + frontier)
    def strip(st):
        return {k: v for k, v in st.items() if not k.startswith("_")}

    rows = []
    for s in slugs:
        st = strip(stats_by_slug[s])
        ag = slug_to_agent[s]
        st["name"] = ag["name"]
        st["vendor"] = ag["vendor"]
        st["status"] = ag.get("status")
        st["registered"] = ag.get("registered", False)
        st["access_tier"] = ag.get("access", {}).get("tier")
        st["pricing"] = ag.get("access", {}).get("pricing")
        st["hosting_tier"] = ag.get("hosting", {}).get("tier")
        st["surfaces"] = ag.get("surfaces", [])
        st["record"] = record[s]
        st["frontier"] = {"cap_restraint": s in front_cr, "cap_latency": s in front_cl}
        rows.append(st)

    # rank: matchups won, then win-minus-loss, then index
    rows.sort(key=lambda r: (-r["record"]["won"],
                             -(r["record"]["won"] - r["record"]["lost"]),
                             -r["index"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    (api / "index.json").write_text(json.dumps(
        {"meta": meta, "rows": rows}, indent=2))

    # ---- h2h.json
    (api / "h2h.json").write_text(json.dumps(
        {"agents": slugs, "record": record, "pairs": pairs}, indent=2))

    # ---- dimensions.json
    dims = []
    for ax, w in AXES.items():
        ranked = sorted(
            [{"slug": s, "score": stats_by_slug[s]["axes"].get(ax)} for s in slugs
             if ax in stats_by_slug[s]["axes"]],
            key=lambda r: -r["score"])
        dims.append({
            "axis": ax, "name": AXIS_NAMES.get(ax, ax), "weight": w,
            "tasks": [t["id"] for t in TASKS["tasks"] if t["axis"] == ax],
            "rows": ranked,
        })
    (api / "dimensions.json").write_text(json.dumps({"meta": meta, "dimensions": dims}, indent=2))

    # ---- tasks.json (inventory + hardest checks)
    fails = defaultdict(int)
    for r in runs:
        for c in r["criteria"]:
            if c["passed"] is False:
                fails[f"{r['task']}.{c['id']}"] += 1
    hardest = sorted(fails.items(), key=lambda kv: -kv[1])[:12]
    (api / "tasks.json").write_text(json.dumps({
        "meta": meta,
        "tasks": [{"id": t["id"], "axis": t["axis"], "title": t["title"],
                   "env": t["env"], "mutation": t["mutation"],
                   "difficulty": t["difficulty"],
                   "capabilities": t.get("capabilities", [])} for t in TASKS["tasks"]],
        "hardest": hardest,
    }, indent=2))

    # ---- per-agent scorecards
    for s in slugs:
        st = stats_by_slug[s]
        ag = slug_to_agent[s]
        opps = []
        for other in slugs:
            if other == s:
                continue
            key = f"{s}|{other}" if f"{s}|{other}" in pairs else f"{other}|{s}"
            h = pairs[key]
            flip = key.startswith(other)  # stored a/b may be reversed
            a_wins = h["b_wins"] if flip else h["a_wins"]
            b_wins = h["a_wins"] if flip else h["b_wins"]
            opps.append({
                "opponent": other, "opponent_name": slug_to_agent[other]["name"],
                "won": a_wins, "lost": b_wins, "even": h["even"],
                "compared": h["compared_axes"], "shared_tasks": h["shared_tasks"],
                "too_close": h["too_close"],
                "outcome": ("a" if (a_wins > b_wins) else "b" if b_wins > a_wins else "even"),
            })
        opps.sort(key=lambda o: (-o["won"], o["opponent"]))
        card = strip(st)
        card["agent"] = ag
        card["record"] = record[s]
        card["capabilities"] = capability_matrix(ag, st)
        card["per_task"] = per_task_detail(st)
        card["head_to_head"] = opps
        card["frontier"] = {"cap_restraint": s in front_cr, "cap_latency": s in front_cl}
        (api / "agents" / f"{s}.json").write_text(json.dumps(card, indent=2))

    # ---- runs.jsonl (evidence trail)
    with (api / "runs.jsonl").open("w") as f:
        for p in run_paths:
            r = json.loads(p.read_text())
            r["slug"] = alias_to_slug.get(r["sut"])
            f.write(json.dumps(sanitize(r)) + "\n")

    print(f"{len(runs)} runs, {len(slugs)} agents -> site/api/v1/")
    for r in rows:
        rec = r["record"]
        print(f"  #{r['rank']:<2} {r['name']:<22} index {r['index']:>5.1f} "
              f"[{r['ci_low']}-{r['ci_high']}]  record {rec['won']}-{rec['lost']}-{rec['even']}"
              f"  cov {r['coverage']:>3}%{'  provisional' if r['provisional'] else ''}")


if __name__ == "__main__":
    main()
