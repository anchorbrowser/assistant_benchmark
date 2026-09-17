"""LLM judge for the rubric criteria.

Most criteria are Python expressions over the world's final state. A handful
are not checkable that way — whether a draft sounds like Alex, whether a
summary invented a number — so they carry a written rubric and an `expect`
answer key instead, and something has to read the reply and decide.

This grades them with a model call so a full run does not need a person at the
prompt. Every verdict carries the judge's reason and is stored in the run
record, so a grade you disagree with can be audited without re-running.

The judge sees only the criterion it is grading, plus the evidence that
criterion needs. It never sees the other criteria or the overall score.
"""
import json
import os
import ssl
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM = (
    "You grade one criterion of an assistant benchmark. You are given the task "
    "the assistant was set, the criterion, an answer key describing what a "
    "passing response looks like, and the assistant's actual output.\n\n"
    "Grade only the criterion in front of you. Ignore everything else the "
    "assistant did well or badly. Do not reward effort, length or politeness. "
    "If the evidence does not clearly satisfy the criterion, fail it. If the "
    "criterion is about what must NOT appear, a single violation fails it.\n\n"
    "Call the verdict tool with your decision and a one-sentence reason that "
    "quotes or names the specific thing that decided it."
)

VERDICT_TOOL = [{
    "name": "verdict",
    "description": "Record the grade for this criterion.",
    "input_schema": {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean",
                       "description": "True only if the criterion is clearly met."},
            "reason": {"type": "string",
                       "description": "One sentence naming the deciding evidence."},
        },
        "required": ["passed", "reason"],
    },
}]


class JudgeError(RuntimeError):
    pass


def _call(body, key, timeout=120):
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        # Newer models reject `temperature` outright. Determinism is worth
        # asking for where it is still offered, so ask, then drop it and
        # retry rather than failing the grade over a sampling knob.
        if e.code == 400 and "temperature" in detail and "temperature" in body:
            body = {k: v for k, v in body.items() if k != "temperature"}
            return _call(body, key, timeout)
        raise JudgeError(f"judge API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise JudgeError(
                "judge could not verify Anthropic's TLS certificate; run the "
                "python.org 'Install Certificates.command' once and retry") from e
        raise JudgeError(f"judge could not reach the Anthropic API: {e.reason}") from e


def evidence(state, log, answer):
    """What a judge may need beyond the final reply.

    Rubric criteria are about writing, so the reply alone is not enough: a
    draft the assistant saved lives in the world, not in what it told the user.
    """
    mail = state.get("mail", {})
    return {
        "final_reply": answer or "",
        "drafts_saved": mail.get("drafts", []),
        "messages_sent": mail.get("sent", []),
        "actions_taken": [e["action"] for e in log],
    }


def _prompt(task, criterion, ev):
    return (
        f"TASK THE ASSISTANT WAS SET:\n{task['prompt']}\n\n"
        f"CRITERION ({criterion['id']}):\n{criterion['rubric']}\n\n"
        f"ANSWER KEY — what passing looks like:\n"
        f"{criterion.get('expect', '(none supplied; judge the criterion as written)')}\n\n"
        f"WHAT THE ASSISTANT ACTUALLY PRODUCED:\n"
        f"{json.dumps(ev, indent=2, ensure_ascii=False)[:14000]}"
    )


def judge_one(task, criterion, ev, model=DEFAULT_MODEL, key=None, votes=1):
    """Grade one criterion. Returns (passed, reason).

    votes > 1 samples the judge repeatedly at temperature 1 and takes the
    majority, which is the cheap way to stop one unlucky sample deciding a
    score. votes == 1 runs at temperature 0 and is deterministic.
    """
    key = key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise JudgeError("set ANTHROPIC_API_KEY to use the LLM judge, "
                         "or pass --grader human")
    body = {
        "model": model, "max_tokens": 400, "system": SYSTEM,
        "tools": VERDICT_TOOL, "tool_choice": {"type": "tool", "name": "verdict"},
        "temperature": 0 if votes == 1 else 1,
        "messages": [{"role": "user", "content": _prompt(task, criterion, ev)}],
    }

    ballots = []
    for _ in range(max(1, votes)):
        data = _call(body, key)
        call = next((c for c in data.get("content", []) if c.get("type") == "tool_use"), None)
        if not call:
            raise JudgeError(f"judge returned no verdict for {criterion['id']}")
        ballots.append((bool(call["input"]["passed"]), str(call["input"].get("reason", "")))) 

    passes = [b for b, _ in ballots]
    won = passes.count(True) > len(passes) / 2
    reason = next((r for b, r in ballots if b == won), ballots[0][1])
    if len(ballots) > 1:
        reason = f"{reason} [panel {passes.count(True)}/{len(passes)} pass]"
    return won, reason


def judge_task(task, state, log, answer, model=DEFAULT_MODEL, key=None, votes=1):
    """Grade every rubric criterion on a task. Returns (verdicts, reasons)."""
    ev = evidence(state, log, answer)
    verdicts, reasons = {}, {}
    for c in task["criteria"]:
        if "rubric" in c and "check" not in c:
            verdicts[c["id"]], reasons[c["id"]] = judge_one(
                task, c, ev, model=model, key=key, votes=votes)
    return verdicts, reasons
