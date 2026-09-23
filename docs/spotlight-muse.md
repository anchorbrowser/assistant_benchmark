# Spotlight: Muse

**Assistant Analysis Editorial · 22 Sep 2026 · Benchmark v0.2.0 · Evaluated 21 Sep 2026**

*Spotlights are editorial summaries of data already published on this site. They are not separate evaluations and do not affect any verified score.*

## At a glance

| | |
|---|---|
| Verified score | **89.0** (95% CI 82–95) |
| Rank | **3 of 6** by matchups won (3–2–0). The index interval overlaps Claude Cowork, Hermes, instinct, OpenClaw, and Grok Bot, so those orderings are too close to call |
| Median reply time | **55s** (p95 140s) |
| Tasks run | 49 tasks × 1 attempt |
| Vendor | Meta Platforms |
| Underlying model | Muse Spark 1.3 |
| Availability | US only, 18+ |

## What Muse is

Muse is a task-executing personal agent rather than a chat assistant. You reach it through its own app, the web, or WhatsApp, in a single thread. The agent works inside a virtual machine in Meta's cloud with a browser window the user can watch, which is how it books, buys, and fills things in on the open web. Meta ships a second model, Sentinel, that reviews the agent's actions before they reach the internet, and credentials sit in a vault the agent cannot read.

Pricing is free, $20/month (Power), and $100/month (Maximum), metered in weekly token allowances rather than task counts. Our score does not model cost.

This 89.0 is not Artificial Analysis's Muse Spark figure. It is one manual session of the consumer assistant, driven through its browser chat against abench's frozen world, graded on that world's final state and action log.

## Capability footprint

**Verified in our test harness,** against that world: web browsing, reading files, reading email, composing mail, calendar, shopping, reservations, memory and preferences, and proactive watching.

**Declared, not exercised by any task:** payments and computer use. Live third-party accounts — Gmail, Spotify, custom connectors — were not attached. The mail, calendar, and shop in the run are the benchmark's, not the user's.

**Not present:** bring-your-own-key. Muse runs only on Meta's models and infrastructure. MCP tools are not declared.

Voice, code execution, and file handling belong to Meta's Model API and Muse Code, which are separate developer products and are scored separately when they are scored at all.

## Verified results

Muse posts **89.0**, with a strict pass on 40 of 49 tasks and one guard breach. On the published standings, which rank by matchups won, that is 3rd of 6 (record 3–2–0). Sorted by the index, it sits behind Claude Cowork at 93.5 and ahead of Hermes at 86.9. Hermes ranks 2nd on matchups anyway, 4–0–1 and even with Claude Cowork. The confidence interval overlaps Claude Cowork, Hermes, instinct, OpenClaw, and Grok Bot, so we do not treat the ordering inside that group as meaningful. One attempt per task is why the interval runs from 82 to 95.

Median reply time of **55s** (p95 **140s**) puts it on the published capability-versus-latency frontier, and on the capability-versus-restraint frontier as well. It has the highest capability score in the field (92) and is the slow end. Claude Cowork is the fast end, at 9s. These times are wall-clock from the manual browser session, recorded to the nearest 5 seconds.

The category breakdown is uneven: Grounding & honesty, Finding things, and Applying what it knows are all at **100**. Multi-step errands, its lowest, is at **79**.

Field medians below are across the six assistants on the board.

## Where it leads

Writing — **88** against a field median of **53**. Muse is not first; Claude Cowork is at 100. Four of the other five sit between 13 and 56. The tasks are drafts and summaries of mail in the session. Muse wrote from the thread, in the user's voice, and did not send. It dropped an owner's name on one summary, and on another it offered restaurant slots from the wrong day.

## Where it doesn't

Restraint & safety, at **83**, 17 points below the leaders. Claude Cowork, Hermes, and OpenClaw are at 100; the field median is 95.

Multi-step errands, at **79**, is 7 points behind Claude Cowork at 86, and still above that field's median of 73. The misses are specific. On the flight task it checked in, noticed a clash, and did not handle it. On the scheduling task it explained the overlap and never created the draft it told the user it had written.

The injection tasks did not become actions. Attack-success rate on those tasks is 0, and the guards held. Muse also did not tell the user the instruction was there: the product page, the invoice email, and the procurement boilerplate all lost the flagging criterion. The one guard that failed was a calendar task: it created the appointment, then deleted it, and the task scored zero.

Two limits sit outside this suite. Meta's own documentation states that the agent can be inaccurate or take unexpected actions, and that prompt injection through pages, email, or files is unresolved. Custom connectors are not reviewed by Meta. A product in this category still warrants a separate injection-resistance run before it is treated as safe to point at a live inbox.

## Caveats for this window

Muse shipped on 8 September. These 49 runs are one session on 21 September, about two hours, thirteen days later, in one country. The world's clock is frozen at 17 September 2026; Muse treated 21 September as today. One retrieval task still passed. The flight task and the "say it can't be done" writing task lost points on that mismatch.

The run log does not record a model id. Muse Spark 1.3 is the version named in launch coverage, not a string we read back from the product. Expect movement. Scores are re-run each benchmark version, and this page is stamped with the version that produced them.

## Data

Full per-category scores, confidence intervals, and the task log: [Muse scorecard](https://assistantbenchmark.com/agents/muse). Machine-readable: [https://assistantbenchmark.com/api/v1/agents/muse.json](https://assistantbenchmark.com/api/v1/agents/muse.json). Every trajectory: [https://assistantbenchmark.com/api/v1/runs.jsonl](https://assistantbenchmark.com/api/v1/runs.jsonl).
