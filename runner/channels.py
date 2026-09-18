"""Channel adapters — drive a texting assistant without a human in the loop.

Products like Poke, Muse or a Grok bot have no agent API. What they do have is
a channel you can already write to programmatically. If the runner can post a
message and read the reply on the same channel the assistant listens to, the
paste loop disappears and a full suite can run unattended.

The contract is two methods:

    send(text)                 -> put the prompt on the channel
    wait_for_reply(...)        -> block until the assistant has finished talking

The second one is the interesting half. These assistants rarely answer in one
message: they send "on it", then three progress notes, then the actual answer.
So a reply is not "the next message", it is "everything inbound since we asked,
once it stops arriving". That is what `settle` measures.

Unlike --mode api, the assistant browses the world itself here, so the world
has to be on a public URL (ngrok, cloudflared) and ABENCH_BASE must point at it.

Everything is stdlib. No new dependencies.
"""
import json
import os
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds


class ChannelError(RuntimeError):
    pass


class Channel:
    name = "channel"
    # False only when the assistant runs on this machine and can reach localhost.
    needs_public_world = True

    def send(self, text):
        raise NotImplementedError

    def close(self):
        pass

    def poll(self, since):
        """Everything the assistant has said since `since`, oldest first.

        A full snapshot each call, not a delta. Messaging channels grow the
        list; a streaming web UI also rewrites its last entry as tokens land.
        """
        raise NotImplementedError

    def wait_for_reply(self, since, timeout=420, settle=25, poll_every=5):
        """Wait until the assistant's output stops changing.

        Handles both shapes of "still talking": another message arriving, and
        the current message still streaming. Any change to the snapshot resets
        the quiet timer, so the answer is only taken once it holds still.

        Returns the joined text, or "" if nothing arrived before `timeout`.
        """
        deadline = time.time() + timeout
        snapshot, last_change = [], None
        while time.time() < deadline:
            current = [m for m in self.poll(since) if m and m.strip()]
            if current != snapshot:
                grew = len(current) - len(snapshot)
                snapshot, last_change = current, time.time()
                preview = current[-1][:70].replace("\n", " ")
                note = f"{len(current)} message(s)" if grew else "still writing"
                print(f"       … {note}, latest: {preview!r}")
            if last_change and time.time() - last_change >= settle:
                break
            time.sleep(poll_every)
        return "\n\n".join(snapshot).strip()


# ------------------------------------------------------------------ iMessage
class IMessage(Channel):
    """macOS Messages. Sends with AppleScript, reads the local chat.db.

    Reading chat.db needs Full Disk Access for whichever app runs python
    (Terminal, iTerm, your editor): System Settings -> Privacy & Security ->
    Full Disk Access. Without it sqlite raises "authorization denied".
    """
    name = "imessage"
    DB = os.path.expanduser("~/Library/Messages/chat.db")

    def __init__(self, to):
        if not to:
            raise ChannelError("--to is required for imessage, e.g. --to '+15551234567'")
        self.to = to
        self._check_db()

    def _check_db(self):
        try:
            self._query("SELECT 1")
        except sqlite3.OperationalError as e:
            raise ChannelError(
                f"cannot read {self.DB}: {e}. Grant Full Disk Access to the app "
                f"running python (System Settings -> Privacy & Security)."
            ) from e

    def _query(self, sql, args=()):
        # immutable avoids fighting Messages for the write lock.
        uri = f"file:{self.DB}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=5) as con:
            return con.execute(sql, args).fetchall()

    def send(self, text):
        script = (
            'tell application "Messages"\n'
            '  set svc to 1st account whose service type = iMessage\n'
            f'  set bud to participant {json.dumps(self.to)} of svc\n'
            f'  send {json.dumps(text)} to bud\n'
            'end tell'
        )
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise ChannelError(
                f"osascript failed: {p.stderr.strip()}. Messages.app must be signed "
                f"in, and the terminal needs Automation permission for Messages."
            )

    def poll(self, since):
        rows = self._query(
            """
            SELECT m.text, m.attributedBody
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
              AND h.id = ?
              AND (m.date / 1000000000.0 + ?) > ?
            ORDER BY m.date
            """,
            (self.to, APPLE_EPOCH, since),
        )
        out = []
        for text, blob in rows:
            body = text or _from_attributed_body(blob)
            if body and body.strip():
                out.append(body.strip())
        return out


def _from_attributed_body(blob):
    """Newer macOS sometimes leaves `text` NULL and puts it in a binary plist."""
    if not blob:
        return ""
    raw = bytes(blob).decode("utf-8", "replace")
    m = re.search(r"NSString\x01\x94\x84\x01\+(.*?)\x86", raw, re.S)
    if m:
        return m.group(1)
    printable = re.findall(r"[ -~\n]{6,}", raw)
    return max(printable, key=len) if printable else ""


# --------------------------------------------------------------------- email
class Email(Channel):
    """Any assistant with an email address.

    The prompt body is never touched; the correlation token rides in the
    Subject, and replies keep it via "Re: ...".

    Env: ABENCH_SMTP_HOST, ABENCH_SMTP_PORT (587), ABENCH_IMAP_HOST,
         ABENCH_EMAIL_USER, ABENCH_EMAIL_PASS
    """
    name = "email"

    def __init__(self, to):
        if not to:
            raise ChannelError("--to is required for email, e.g. --to assistant@example.com")
        self.to = to
        self.user = _env("ABENCH_EMAIL_USER")
        self.password = _env("ABENCH_EMAIL_PASS")
        self.smtp_host = _env("ABENCH_SMTP_HOST")
        self.smtp_port = int(os.environ.get("ABENCH_SMTP_PORT", "587"))
        self.imap_host = _env("ABENCH_IMAP_HOST")
        self.token = None

    def send(self, text):
        import smtplib
        from email.message import EmailMessage
        self.token = f"abench-{int(time.time())}"
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = self.to
        msg["Subject"] = f"[{self.token}]"
        msg.set_content(text)
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(self.user, self.password)
            s.send_message(msg)

    def poll(self, since):
        import email as emaillib
        import imaplib
        out = []
        with imaplib.IMAP4_SSL(self.imap_host) as im:
            im.login(self.user, self.password)
            im.select("INBOX")
            _, data = im.search(None, "SUBJECT", f'"{self.token}"')
            for num in (data[0] or b"").split():
                _, raw = im.fetch(num, "(RFC822)")
                msg = emaillib.message_from_bytes(raw[0][1])
                if self.user in (msg.get("From") or ""):
                    continue  # our own outbound
                out.append(_email_text(msg))
        return [t for t in out if t.strip()]


def _email_text(msg):
    if not msg.is_multipart():
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode("utf-8", "replace")
    return ""


# ------------------------------------------------------------------- webhook
class Webhook(Channel):
    """For a bot you built yourself.

    POST {"text": prompt} to ABENCH_WEBHOOK_URL. If that returns the answer
    synchronously (JSON "reply"/"text", or a plain body) it is used directly.
    Otherwise set ABENCH_WEBHOOK_POLL to a URL returning {"messages": [...]}.
    """
    name = "webhook"

    def __init__(self, to=None):
        self.url = to or _env("ABENCH_WEBHOOK_URL")
        self.poll_url = os.environ.get("ABENCH_WEBHOOK_POLL")
        self.sync_reply = None
        # A bot on this machine can reach a world on this machine, no tunnel.
        self.needs_public_world = not re.search(r"localhost|127\.0\.0\.1", self.url)

    def send(self, text):
        self.sync_reply = None
        req = urllib.request.Request(
            self.url, data=json.dumps({"text": text}).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            got = json.loads(body)
            self.sync_reply = got.get("reply") or got.get("text") or ""
        except json.JSONDecodeError:
            self.sync_reply = body

    def poll(self, since):
        if self.sync_reply:
            return [self.sync_reply]
        if not self.poll_url:
            return []
        with urllib.request.urlopen(self.poll_url, timeout=30) as r:
            got = json.loads(r.read())
        return [m for m in got.get("messages", []) if m.strip()]


# ------------------------------------------------------------------- browser
class Browser(Channel):
    """A consumer chat product with no API — Grok Bot, grok.com, anything.

    Drives the real web UI: types the prompt into the composer and reads the
    reply out of the DOM. This is the only programmatic handle on a product
    that ships no API, and it is the most fragile adapter here: a redesign of
    their frontend breaks the selectors, not the benchmark.

    Login happens once, by hand, into a persistent browser profile:

        python3 runner/channels.py login https://grok.com

    Then runs reuse that session. If the selectors miss, find better ones with:

        python3 runner/channels.py probe https://grok.com

    and set ABENCH_INPUT_SEL / ABENCH_MSG_SEL.

    Needs playwright (the one optional dependency in the project):
        pip install playwright && playwright install chromium
    """
    name = "browser"
    PROFILE = os.path.expanduser("~/.abench/browser-profile")

    # Deliberately broad. Chat UIs converge on a contenteditable composer and
    # a list of role-tagged bubbles, so these catch most of them.
    INPUT_SEL = ('textarea:not([readonly]), div[contenteditable="true"], '
                 '[role="textbox"]')
    MSG_SEL = ('[data-message-author-role="assistant"], [data-testid*="assistant"], '
               '[class*="assistant"], [data-testid*="message"]')

    def __init__(self, to):
        self.url = to or "https://grok.com"
        self.input_sel = os.environ.get("ABENCH_INPUT_SEL", self.INPUT_SEL)
        self.msg_sel = os.environ.get("ABENCH_MSG_SEL", self.MSG_SEL)
        self.headless = os.environ.get("ABENCH_HEADLESS") == "1"
        self._ctx = self._pw = self.page = None
        self._baseline = 0

    def _start(self):
        if self.page:
            return
        self._pw, self._ctx, self.page = _open_profile(self.url, self.headless)

    def send(self, text):
        self._start()
        box = self.page.locator(self.input_sel).first
        try:
            box.wait_for(state="visible", timeout=30000)
        except Exception as e:  # noqa: BLE001
            raise ChannelError(
                f"no composer matched {self.input_sel!r} on {self.url}. If you are "
                f"not logged in, run: python3 runner/channels.py login {self.url}. "
                f"Otherwise find the right selector with `probe` and set "
                f"ABENCH_INPUT_SEL."
            ) from e
        # Everything already on screen is history, not this task's answer.
        self._baseline = self.page.locator(self.msg_sel).count()
        box.click()
        # A contenteditable composer ignores fill(); insert_text also avoids
        # per-newline keystrokes, which would submit a multi-line prompt early.
        if box.get_attribute("contenteditable") == "true":
            self.page.keyboard.insert_text(text)
        else:
            box.fill(text)
        self.page.keyboard.press("Enter")

    def poll(self, since):
        if not self.page:
            return []
        items = self.page.locator(self.msg_sel)
        out = []
        for i in range(self._baseline, items.count()):
            try:
                out.append((items.nth(i).inner_text() or "").strip())
            except Exception:  # noqa: BLE001, S112  (bubble re-rendered mid-read)
                continue
        return out

    def new_chat(self):
        """Fresh thread per task, so memory does not leak between tasks."""
        if self.page:
            self.page.goto(self.url)
            self._baseline = 0

    def close(self):
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()
        self._ctx = self._pw = self.page = None


def _open_profile(url, headless=False):
    """Launch Chromium on the persistent abench profile. Returns (pw, ctx, page)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ChannelError(
            "the browser channel needs playwright: "
            "pip install playwright && playwright install chromium"
        ) from e
    os.makedirs(Browser.PROFILE, exist_ok=True)
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            Browser.PROFILE, headless=headless, viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
    except Exception as e:  # noqa: BLE001
        pw.stop()
        hint = ("run: python3 -m playwright install chromium"
                if "Executable doesn't exist" in str(e)
                else "another abench browser may already hold this profile")
        raise ChannelError(f"could not launch chromium — {hint}") from e
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    return pw, ctx, page


# ------------------------------------------------------- fire-and-forget hook
class CursorAutomation(Channel):
    """A trigger-only webhook, e.g. a Cursor automation.

    Firing it starts an agent that answers into its own chat, which the runner
    cannot see. So the payload names a return address — the world's /abench/reply
    mailbox — and asks the agent to post its final answer there. One ngrok tunnel
    covers both, because the mailbox rides on the world server.

    If the agent ignores the return address the reply stays empty. That is not
    fatal: state criteria still grade normally and only the written-quality ones
    go ungraded, which the score already accounts for.

    Env: ABENCH_CURSOR_TOKEN (bearer), ABENCH_CURSOR_WEBHOOK (or pass --to).
    """
    name = "cursor"

    def __init__(self, to=None, base=None):
        self.url = to or _env("ABENCH_CURSOR_WEBHOOK")
        self.token = _env("ABENCH_CURSOR_TOKEN")
        if not base:
            raise ChannelError("the cursor channel needs --base (the public world URL)")
        self.mailbox = base.rstrip("/") + "/abench/reply"
        # The agent acts first and writes its summary afterwards, so the world
        # going quiet is not the end. Measured gap was ~38s; allow well over it.
        self.grace = int(os.environ.get("ABENCH_REPLY_GRACE", "150"))

    def _payload(self, text):
        # The return instruction goes inside `task`, not beside it. In a sibling
        # field the agent read it and still answered only in chat; as part of the
        # instruction it is meant to carry out, it complies.
        #
        # It is also told never to message third parties unless the payload
        # explicitly asks and names the recipient, so this does both.
        ask = (
            f"{text}\n\n"
            f"--- HOW TO DELIVER YOUR ANSWER (required) ---\n"
            f"This request came from an automated benchmark that CANNOT read your "
            f"chat. Answering only in chat is recorded as no answer at all.\n"
            f"So when the work above is done, deliver your final answer by running "
            f"exactly this, once:\n"
            f"curl -X POST '{self.mailbox}' --data-urlencode "
            f"'text=<your full final answer here>'\n"
            f"Put everything you would have told the user in that text: what you "
            f"did, and any values you were asked for."
        )
        return {"task": ask, "report_result_to": self.mailbox}

    def send(self, text):
        try:
            urllib.request.urlopen(self.mailbox + "?clear=1", timeout=15).read()
        except OSError as e:
            raise ChannelError(
                f"cannot reach the mailbox at {self.mailbox}: {e}. Is the world "
                f"running and is --base the public tunnel URL?"
            ) from e
        req = urllib.request.Request(
            self.url, data=json.dumps(self._payload(text)).encode(),
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                print(f"       webhook accepted ({r.status})")
        except urllib.error.HTTPError as e:
            raise ChannelError(
                f"webhook returned {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            ) from e

    def poll(self, since):
        return self._state()[0]

    def _state(self):
        """(messages, hits) — hits counts the assistant's requests on the world."""
        try:
            with urllib.request.urlopen(self.mailbox, timeout=15) as r:
                got = json.loads(r.read())
            return got.get("messages", []), got.get("hits", 0)
        except OSError:
            return [], 0

    def wait_for_reply(self, since, timeout=420, settle=25, poll_every=5):
        """Wait for the assistant to stop touching the world.

        A trigger-only webhook returns nothing and reports elsewhere, so there
        is no message to wait on. What the world can see is its own traffic:
        once the hit count stops climbing, the assistant has finished. Quiet
        before it has touched anything at all is just latency, not completion.
        """
        deadline = time.time() + timeout
        msgs, hits, last_change, started = [], 0, None, False
        while time.time() < deadline:
            m, h = self._state()
            if m != msgs or h != hits:
                if h > hits:
                    started = True
                    print(f"       … {h} request(s) on the world"
                          + (f", {len(m)} message(s) back" if m else ""))
                elif m != msgs:
                    print(f"       … {len(m)} message(s) back")
                msgs, hits, last_change = m, h, time.time()
            if started and time.time() - last_change >= settle:
                break
            time.sleep(poll_every)

        if not started:
            print("       !! the assistant never touched the world")
            return ""

        # Work finished, summary not in yet. Keep listening on the mailbox only.
        if not msgs:
            print(f"       world quiet after {hits} request(s); waiting up to "
                  f"{self.grace}s for the written answer")
            until = time.time() + self.grace
            while time.time() < until:
                msgs = self._state()[0]
                if msgs:
                    print(f"       … answer received after "
                          f"{self.grace - int(until - time.time())}s")
                    break
                time.sleep(poll_every)

        if not msgs:
            print(f"       note: acted on the world ({hits} requests) but sent no "
                  f"answer back, so written criteria stay ungraded")
        return "\n\n".join(msgs).strip()


# ---------------------------------------------------------------------- hermes
class Hermes(Channel):
    """Local Hermes agent (CLI oneshot or its OpenAI-compatible API).

    Same machine as the world, so localhost is fine — no tunnel.

    Default: `hermes --yolo -z <prompt>`. Each task is a new process, so
    memory does not leak across tasks. --yolo is on because a hung approval
    prompt would freeze the suite; set ABENCH_HERMES_YOLO=0 to keep prompts.

    If the gateway API server is up:

        API_SERVER_ENABLED=true
        API_SERVER_KEY=...
        hermes gateway restart

    set ABENCH_HERMES_URL=http://127.0.0.1:8642 (and ABENCH_HERMES_KEY) and
    we POST /v1/chat/completions with a fresh X-Hermes-Session-Id per task.
    """
    name = "hermes"
    needs_public_world = False

    def __init__(self, to=None):
        self.url = (to or os.environ.get("ABENCH_HERMES_URL") or "").rstrip("/")
        self.key = os.environ.get("ABENCH_HERMES_KEY", "")
        self.bin = (os.environ.get("ABENCH_HERMES_BIN")
                    or shutil.which("hermes")
                    or os.path.expanduser("~/.local/bin/hermes"))
        self.timeout = int(os.environ.get("ABENCH_HERMES_TIMEOUT", "420"))
        self.yolo = os.environ.get("ABENCH_HERMES_YOLO", "1") != "0"
        self._reply = ""
        if self.url:
            return
        if not (os.path.isfile(self.bin) or shutil.which(self.bin)):
            raise ChannelError(
                f"hermes CLI not found at {self.bin!r}. Put it on PATH, set "
                f"ABENCH_HERMES_BIN, or enable the API server and set "
                f"ABENCH_HERMES_URL=http://127.0.0.1:8642"
            )

    def send(self, text):
        print(f"       hermes {'API' if self.url else 'CLI'} (timeout {self.timeout}s)")
        self._reply = self._via_api(text) if self.url else self._via_cli(text)

    def poll(self, since):
        return [self._reply] if self._reply.strip() else []

    def wait_for_reply(self, since, timeout=420, settle=25, poll_every=5):
        return self._reply.strip()

    def _via_cli(self, text):
        cmd = [self.bin]
        if self.yolo:
            cmd.append("--yolo")
        cmd.extend(["-z", text])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise ChannelError(f"hermes -z timed out after {self.timeout}s") from e
        out = (p.stdout or "").strip()
        if p.returncode != 0 and not out:
            err = (p.stderr or "").strip()[:400]
            raise ChannelError(f"hermes exited {p.returncode}: {err or 'no output'}")
        if p.returncode != 0:
            print(f"       hermes exit {p.returncode}, using stdout anyway")
        return out

    def _via_api(self, text):
        endpoint = self.url
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
        session = f"abench-{int(time.time())}"
        body = json.dumps({
            "model": os.environ.get("ABENCH_HERMES_MODEL", "hermes-agent"),
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }).encode()
        headers = {"content-type": "application/json",
                   "x-hermes-session-id": session}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(endpoint, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                got = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ChannelError(
                f"hermes API {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            ) from e
        except OSError as e:
            raise ChannelError(f"hermes API unreachable at {endpoint}: {e}") from e
        choices = got.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return (msg.get("content") or "").strip()
        return (got.get("output_text") or got.get("text") or json.dumps(got))[:20000]


# ------------------------------------------------------------------- openclaw
class OpenClaw(Channel):
    """Local OpenClaw via `openclaw agent --local`.

    OpenClaw's fetch guard blocks private IPs (127.0.0.1, localhost), so the
    world still needs a public --base (ngrok), even though the agent is local.
    The process also tends to leave Chrome attached and never exit; we run it
    in its own process group, take stdout once it goes quiet or JSON lands,
    then SIGTERM the group.

    Needs ANTHROPIC_API_KEY (falls back to ~/.hermes/.env). Prefers `openclaw`
    on PATH, else npx openclaw@2026.3.2 under Homebrew Node 24.
    """
    name = "openclaw"
    needs_public_world = True
    NPX_PKG = "openclaw@2026.3.2"
    NODE24 = "/opt/homebrew/opt/node@24/bin"

    def __init__(self, to=None):
        self.timeout = int(os.environ.get("ABENCH_OPENCLAW_TIMEOUT", "300"))
        self.bin = os.environ.get("ABENCH_OPENCLAW_BIN") or shutil.which("openclaw")
        self._reply = ""
        self._ensure_keys()
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            raise ChannelError(
                "openclaw --local needs ANTHROPIC_API_KEY or OPENAI_API_KEY in "
                "the environment (or ~/.hermes/.env)"
            )

    def _ensure_keys(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return
        path = os.path.expanduser("~/.hermes/.env")
        if not os.path.isfile(path):
            return
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and "ANTHROPIC_API_KEY" not in os.environ:
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("'\"")
                return

    def send(self, text):
        env = os.environ.copy()
        if os.path.isdir(self.NODE24):
            env["PATH"] = self.NODE24 + os.pathsep + env.get("PATH", "")
        sid = f"abench-{int(time.time())}"
        if self.bin:
            cmd = [self.bin, "agent", "--local",
                   "--timeout", str(self.timeout), "--session-id", sid,
                   "--message", text]
        else:
            cmd = ["npx", "--yes", "--package", self.NPX_PKG, "openclaw",
                   "agent", "--local",
                   "--timeout", str(self.timeout), "--session-id", sid,
                   "--message", text]
        print(f"       openclaw {'CLI' if self.bin else 'npx '+self.NPX_PKG} "
              f"(timeout {self.timeout}s, kills hung chrome)")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True, env=env)
        chunks, last, deadline = [], time.time(), time.time() + self.timeout
        try:
            while time.time() < deadline:
                if p.stdout is None:
                    break
                ready, _, _ = select.select([p.stdout], [], [], 1.0)
                if ready:
                    line = p.stdout.readline()
                    if line == "":
                        break
                    chunks.append(line)
                    last = time.time()
                elif p.poll() is not None:
                    rest = p.stdout.read() if p.stdout else ""
                    if rest:
                        chunks.append(rest)
                    break
                elif chunks and time.time() - last >= 40:
                    print("       … stdout quiet, stopping openclaw")
                    break
        finally:
            _killpg(p)
        out = "".join(chunks).strip()
        err = ""
        try:
            if p.stderr:
                err = p.stderr.read()[:400]
        except OSError:
            pass
        self._reply = self._parse(out) or self._reply
        if not self._reply:
            raise ChannelError(f"openclaw produced no answer. stderr: {err or 'empty'}")
        if "Blocked hostname" in err:
            print("       note: openclaw SSRF blocked a private URL; use a public --base")

    def _parse(self, out):
        if not out:
            return ""
        try:
            got = json.loads(out)
        except json.JSONDecodeError:
            # skip service lines like [browser/service]
            lines = [ln for ln in out.splitlines()
                     if ln.strip() and not ln.strip().startswith("[")]
            return "\n".join(lines).strip()[:20000]
        payloads = got.get("payloads") or []
        texts = [p.get("text") for p in payloads if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n\n".join(texts)
        return (got.get("text") or got.get("reply") or "")[:20000]

    def poll(self, since):
        return [self._reply] if self._reply.strip() else []

    def wait_for_reply(self, since, timeout=420, settle=25, poll_every=5):
        return self._reply.strip()


def _killpg(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _env(name):
    """Local OpenClaw agent via `openclaw agent --local`.

    Same machine as the world, so localhost is fine — no tunnel and no
    Gateway daemon. Each task gets a fresh --session-id.

    Needs a model key in the environment (ANTHROPIC_API_KEY, etc.). If the
    shell has none, we reuse ~/.hermes/.env when that file exists.

    Prefers `openclaw` on PATH. Otherwise npx openclaw@2026.3.2 (Node 24),
    because current latest wants Node ≥24.16 and this machine is on 24.3.
    """
    name = "openclaw"
    needs_public_world = False
    NPX_PKG = "openclaw@2026.3.2"
    NODE24 = "/opt/homebrew/opt/node@24/bin"

    def __init__(self, to=None):
        self.timeout = int(os.environ.get("ABENCH_OPENCLAW_TIMEOUT", "420"))
        self.bin = os.environ.get("ABENCH_OPENCLAW_BIN") or shutil.which("openclaw")
        self._reply = ""
        self._ensure_keys()
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            raise ChannelError(
                "openclaw --local needs ANTHROPIC_API_KEY or OPENAI_API_KEY in "
                "the environment (or ~/.hermes/.env)"
            )

    def _ensure_keys(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return
        path = os.path.expanduser("~/.hermes/.env")
        if not os.path.isfile(path):
            return
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and "ANTHROPIC_API_KEY" not in os.environ:
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("'\"")
                return

    def send(self, text):
        env = os.environ.copy()
        if os.path.isdir(self.NODE24):
            env["PATH"] = self.NODE24 + os.pathsep + env.get("PATH", "")
        sid = f"abench-{int(time.time())}"
        if self.bin:
            cmd = [self.bin, "agent", "--local", "--json",
                   "--timeout", str(self.timeout), "--session-id", sid,
                   "--message", text]
        else:
            cmd = ["npx", "--yes", "--package", self.NPX_PKG, "openclaw",
                   "agent", "--local", "--json",
                   "--timeout", str(self.timeout), "--session-id", sid,
                   "--message", text]
        print(f"       openclaw {'CLI' if self.bin else 'npx '+self.NPX_PKG} "
              f"(timeout {self.timeout}s)")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout + 30, env=env)
        except subprocess.TimeoutExpired as e:
            raise ChannelError(f"openclaw timed out after {self.timeout}s") from e
        out = (p.stdout or "").strip()
        if p.returncode != 0 and not out:
            err = (p.stderr or "").strip()[:500]
            raise ChannelError(f"openclaw exited {p.returncode}: {err or 'no output'}")
        self._reply = self._parse(out)

    def _parse(self, out):
        try:
            got = json.loads(out)
        except json.JSONDecodeError:
            return out
        payloads = got.get("payloads") or []
        texts = [p.get("text") for p in payloads if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n\n".join(texts)
        return (got.get("text") or got.get("reply") or out)[:20000]

    def poll(self, since):
        return [self._reply] if self._reply.strip() else []

    def wait_for_reply(self, since, timeout=420, settle=25, poll_every=5):
        return self._reply.strip()


def _env(name):
    val = os.environ.get(name)
    if not val:
        raise ChannelError(f"set {name}")
    return val


BUILDERS = {"imessage": IMessage, "email": Email, "webhook": Webhook,
            "browser": Browser, "cursor": CursorAutomation, "hermes": Hermes,
            "openclaw": OpenClaw}

# Channels that need to know the world's public URL to build a return address.
WANTS_BASE = {"cursor"}


def build(kind, to, base=None):
    if kind not in BUILDERS:
        raise ChannelError(f"unknown channel {kind!r}; pick one of {', '.join(BUILDERS)}")
    if kind in WANTS_BASE:
        return BUILDERS[kind](to, base)
    return BUILDERS[kind](to)


def run_channel(channel, prompt, timeout, settle):
    """Post the prompt, wait for the assistant to stop talking, return the reply."""
    if hasattr(channel, "new_chat"):
        channel.new_chat()
    since = time.time()
    channel.send(prompt)
    print(f"       sent on {channel.name}, waiting up to {timeout}s "
          f"(settles after {settle}s of quiet)")
    # An empty reply is returned as-is. The caller must not read silence as a
    # claim of success, so it stays empty rather than becoming prose.
    reply = channel.wait_for_reply(since, timeout=timeout, settle=settle)
    return reply, None, time.time() - since


# ------------------------------------------------- browser setup, as a script
def _cmd_login(url):
    print(f"opening {url} on the abench profile.\n"
          f"log in, get to a chat screen, then press Enter here.")
    pw, ctx, _ = _open_profile(url)
    try:
        input()
    finally:
        ctx.close()
        pw.stop()
    print(f"session saved to {Browser.PROFILE} — runs will reuse it.")


def _cmd_probe(url):
    """Print what the default selectors actually match, plus candidates."""
    pw, ctx, page = _open_profile(url)
    try:
        page.wait_for_timeout(4000)
        for label, sel in (("composer", Browser.INPUT_SEL), ("messages", Browser.MSG_SEL)):
            n = page.locator(sel).count()
            print(f"\n{label}: default selector matches {n}")
            print(f"  {sel}")
        print("\ncandidate composers:")
        for sel in ('textarea', 'div[contenteditable="true"]', '[role="textbox"]',
                    'input[type="text"]'):
            print(f"  {page.locator(sel).count():>3}  {sel}")
        print("\ncandidate message containers:")
        for sel in ('[data-message-author-role="assistant"]', '[data-testid*="message"]',
                    '[class*="assistant"]', '[class*="bubble"]', '[class*="markdown"]',
                    '[role="listitem"]', 'article'):
            print(f"  {page.locator(sel).count():>3}  {sel}")
        print("\nset ABENCH_INPUT_SEL / ABENCH_MSG_SEL to whichever fit.")
        print("a composer count of 1 is what you want. zero messages is normal on "
              "an empty chat —\nto check the message selector, send one message in "
              "this window by hand first, then probe again.")
    finally:
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    import sys
    import pathlib
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    cmds = {"login": _cmd_login, "probe": _cmd_probe}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        raise SystemExit("usage: python3 runner/channels.py {login|probe} [url]")
    try:
        cmds[sys.argv[1]](sys.argv[2] if len(sys.argv) > 2 else "https://grok.com")
    except ChannelError as e:
        raise SystemExit(f"error: {e}") from e
