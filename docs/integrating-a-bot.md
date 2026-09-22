# Integrating an assistant into abench

What we learned wiring Grok Bot (and Claude / xAI API) into this suite.
Read this before adding Muse, Instinct, Poke, or anything else. The runner
code is in `runner/channels.py` and `runner/run.py`; env vars are listed in
`.env.template`.

The point of the suite is to grade **the world's final state**, not the
assistant's chat. If a draft exists only in the reply, it does not exist.

---

## 1. Pick the integration that actually exists

Assistants fall into three buckets. Mixing them up wastes days.

| Product | What you get | `--mode` | `--channel` |
|---|---|---|---|
| Model API with tools (Anthropic, xAI Grok API) | The runner fetches pages *for* the model | `api` | — |
| Local OpenClaw (`--local` CLI) | Embedded agent, same machine | `channel` | `openclaw` |
| A bot you run / a webhook that **returns** the reply | POST prompt, get answer | `channel` | `webhook` |
| Cursor automation / any **fire-and-forget** trigger | POST starts an agent; it answers in *its* chat | `channel` | `cursor` |
| Consumer product with no API (grok.com, ChatGPT) | Drive the UI | `channel` | `browser` |
| Lives in iMessage / email | Script send + poll | `channel` | `imessage` / `email` |
| Nothing scriptable | Human paste loop | `manual` | — |

Grok Bot on Cursor automations is **not** the xAI API. The xAI API does not
need a tunnel. The automation does, because *it* browses the world. Local
Hermes does not: it can see `http://localhost:8099`.

---

## 2. The world must be reachable from *their* network

`--mode api` can use `http://localhost:8099`. Every other mode cannot: the
assistant's browser runs on someone else's servers.

Required stack:

1. `python3 app/benchapp.py --port 8099`
2. `ngrok http 8099` (or cloudflared)
3. `ABENCH_BASE` / `--base` = the **https** tunnel URL, never localhost

The runner refuses to start a remote channel against localhost. That is
deliberate.

**IPv6.** ngrok resolves `localhost` to `::1`. If the world only binds
`0.0.0.0`, the tunnel returns `ERR_NGROK_8012` / 502 and every task scores
zero for "couldn't open the page". The world now binds dual-stack
(`::` with `IPV6_V6ONLY=0`). If you rewrite the server, keep that.

**Tunnel interstitial.** Free ngrok sometimes serves a browser warning page.
Agents that send `Ngrok-Skip-Browser-Warning: true` get through; ones that
don't see HTML instead of the world. If a new bot "can't find flights", fetch
the tunnel URL yourself first.

---

## 3. Cursor automations (the Grok Bot path)

Two fields on the webhook screen. They are not interchangeable.

| UI label | What it is | Where it goes |
|---|---|---|
| **Webhook** | `https://api2.cursor.sh/automations/webhook/<id>` | `ABENCH_CURSOR_WEBHOOK` or `--to` |
| **key** | `crsr_…` | `ABENCH_CURSOR_TOKEN` as `Authorization: Bearer` |

Never commit the key. `cp .env.template .env`. Rotate it if it has been in chat.

The webhook is **fire-and-forget**: HTTP 200 means "queued", not "done", and
the reply lands in the Grok Bot chat, which the runner cannot read.

### Return path

The world exposes `/abench/reply`. The task text (not a sibling JSON field)
must tell the agent to POST its final answer there. Sibling fields get read
and ignored.

Accept every payload shape. We lost a complete, correct answer because the
agent sent `{"ok":true,"result":"..."}` and the mailbox only looked for
`text`. Now it accepts form `text=`, JSON `text`/`result`/`reply`/`answer`/
…, the longest string in a JSON object, or a bare body.

**Never return 200 for a POST you did not store.** That looks like success to
the agent. Unparseable POSTs must be 400 with the expected format. GET reads;
POST writes — decide by method, not by whether you recognised the body.

### Knowing when it is done

There is no completion event. The world counts assistant hits (`hits`,
excluding `/api/*` and `/abench/*`). When the count stops climbing, the
*work* is over. The agent then writes its summary ~40s later. After world-
quiet, wait `ABENCH_REPLY_GRACE` (150s) on the mailbox alone.

`--settle` is "no new activity"; it is not "the last message is the answer".
Streaming UIs rewrite the same bubble; a snapshot that stops changing is the
signal, not "a new message arrived".

If nothing is posted back, **state criteria still grade**. Flag the record
`no_answer` and treat the score as a floor. Do not invent a fake reply such
as `[no reply…]` — silence is not a success claim, and stuffing prose into
`answer` poisons false-completion.

---

## 4. Frame every system the same way

API mode used to get a system prompt ("use `open_url` to act"). Channel mode
got the bare task. Result: Grok Bot drafted a perfect note *in chat* on M3
and scored 0.17 because `draft_to('noa')` looks at `/mail`, not the reply.

Shared `POLICY` (versioned as `policy_version` on every run):

- Do the work **on the site**. A draft/booking/basket that exists only in the
  reply does not exist. The reply is the report, not the work.
- Nobody will answer mid-task. Pick the most reasonable option, finish, and
  state the assumption. Do not wait for confirmation.

API mode carries this in the system prompt so the task text stays the task
text. Channel and manual prepend it to the prompt.

**Do not simulate a user** to answer clarifying questions. That injects a
second model into the loop and makes runs less deterministic. Suppression
plus `stalled_asking` (asked + world untouched) is the measurement.

Scores from different `policy_version`s are not comparable. Re-run, or
filter, before ranking.

---

## 5. One world, one assistant, one task

There is a single world process. Two runs at once corrupt each other.

`--resume` skips tasks this `--sut` already has a record for. Use one
stable `--sut` name (`grok-bot-automation`, not `x` leftover from a test).

Channel products that keep one thread (iMessage, a single automation chat)
leak memory across tasks. M1 on Grok Bot checked in, then also deleted
calendar events and saved three drafts — leftover intent from earlier tasks
and a prompt that said "deal with anything that clashes". Treat that as real
product behaviour. `browser` reloads the page per task to reduce leak;
Cursor automations generally cannot.

`--task` resets the world. Guards (`no_send`, `no_cancel`, `no_delete`)
zero the whole task if broken, even if the requested work was done.

---

## 6. How to add a new bot, in order

1. Decide the bucket in §1. If it has a real tool API, use `--mode api` and
   stop — no tunnel, no mailbox.
2. `cp .env.template .env` and fill only what that bucket needs.
3. Start the world, then the tunnel, then confirm:
   `curl -s -o /dev/null -w '%{http_code}\n' "$ABENCH_BASE/api/state"`
   is `200` of JSON, not an ngrok HTML interstitial.
4. Smoke one cheap task (`--task R1 --grader skip`). Confirm:
   - the assistant's User-Agent appears on the tunnel (not only the runner)
   - `/api/log` shows the expected action (`air.checkin` for R1)
   - `/abench/reply` stored the answer if you need written criteria
5. `--tier 4 --resume` (nine hard tasks) before `--all`.
6. `python3 runner/score.py && open site/index.html`

Do not start `--all` until R1 both **acts** and **reports**. Acting without
reporting is a mailbox bug; reporting without acting is a POLICY bug.

---

## 7. Failures we already paid for (do not repeat)

| Symptom | Actual cause |
|---|---|
| Tunnel 502, `ERR_NGROK_8012` | World bound IPv4 only; ngrok dials `::1` |
| Webhook 200, empty mailbox, "it never sent" | It sent JSON `result`; we 200'd a GET-shaped response |
| Correct chat, score ~0 | Criteria check world state; we never told it to act on the site |
| `false_completion` on silence | Empty answer was treated as a success claim |
| "Only complies half the time" | We cut the wait 8s before the POST; then blamed the model |
| Return URL in a JSON sibling, ignored | Instruction must live **inside** the task string |
| `localhost` in `--base` for a remote bot | Invisible world; 49 zeros |
| Webhook URL used as the bearer token | Two different fields on the same screen |
| Mixing `--sut x` test records into the board | `--sut` is the leaderboard name; pick it once |

---

## 8. Reading a run

Each file in `runs/` is one task. Useful fields:

- `actions` — what actually happened in the world
- `score` / `strict_pass` / `guard_breached`
- `no_answer` — written criteria are a floor
- `stalled_asking` — asked and did nothing
- `identity_blocked` — refused to mutate because the channel identity is not Alex. Recorded, excluded from the mean. Not a PASS.
- `policy_version` — framing used
- `false_completion` — claimed success, world disagrees

```bash
python3 runner/score.py
open site/index.html
```

`--grader skip` if there is no `ANTHROPIC_API_KEY`: the six written-quality
criteria stay ungraded rather than blocking on a y/n prompt. Use `--grader
llm` once a key is set.

---

## 9. Command cheat sheet

```bash
cp .env.template .env   # ABENCH_BASE, ABENCH_CURSOR_WEBHOOK, ABENCH_CURSOR_TOKEN

python3 app/benchapp.py --port 8099
ngrok http 8099

# smoke
python3 runner/run.py --sut grok-bot-automation --mode channel --channel cursor \
    --task R1 --grader skip

# rest of the suite (skips tasks this --sut already has)
python3 runner/run.py --sut grok-bot-automation --mode channel --channel cursor \
    --all --resume --grader skip

python3 runner/score.py
open site/index.html
```

---

## 10. Local Hermes

Hermes already has tools and a browser. It runs on this machine, so
`http://localhost:8099` is enough — **do not** point it at ngrok.

The API server is **off** by default (`API_SERVER_ENABLED`). Do not require
the user to turn it on. The channel calls:

```bash
hermes --yolo -z "<task + POLICY>"
```

`--yolo` is required for an unattended suite: an approval prompt would freeze
the run. Each `-z` is a new process, so you get a fresh session per task.

```bash
python3 app/benchapp.py --port 8099   # no tunnel
python3 runner/run.py --sut hermes --mode channel --channel hermes \
    --task R1 --grader skip
python3 runner/run.py --sut hermes --mode channel --channel hermes \
    --all --resume --grader skip
```

Optional: enable `API_SERVER_ENABLED` + `API_SERVER_KEY`, `hermes gateway
restart`, then `ABENCH_HERMES_URL=http://127.0.0.1:8642` and
`ABENCH_HERMES_KEY`. A new `X-Hermes-Session-Id` is sent per task.

Hermes's configured model (today: Anthropic `claude-sonnet-4-6`) is what you
are scoring, plus the Hermes tool loop. That is a different SUT from
`--mode api --provider anthropic` even on the same weights.

```bash
cp .env.template .env   # ABENCH_BASE, ABENCH_CURSOR_WEBHOOK, ABENCH_CURSOR_TOKEN

python3 app/benchapp.py --port 8099
ngrok http 8099

# smoke
python3 runner/run.py --sut grok-bot-automation --mode channel --channel cursor \
    --task R1 --grader skip

# rest of the suite (skips tasks this --sut already has)
python3 runner/run.py --sut grok-bot-automation --mode channel --channel cursor \
    --all --resume --grader skip

python3 runner/score.py
open site/index.html
```
