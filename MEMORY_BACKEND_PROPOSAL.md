# Memory Backend Integration — Proposal

**Date:** 2026-05-12
**Status:** Draft. Nothing in `advisor_backend.py` has been changed.

This is the concrete diff against `advisor_backend.py` for Phase 1 of the
per-user memory work outlined in `MEMORY_SUBSYSTEM.md`. Read it end-to-end,
push back on anything that feels wrong, then we apply.

The three pieces — cookie middleware, `load_user_context()`, and
`save_session_digest()` — are independent and small. Each is shown as a
**before / after** hunk with the rationale below it.

---

## Piece 1 — User identity via cookie

A user_id is minted on first visit (UUID4), set as a long-lived cookie,
and read back on every request. The LiveAvatar SDK's `session_id` stays
unchanged — it identifies a browser session; the cookie identifies the
person.

### Add near the top of the file, after the existing imports

```python
import uuid
from pathlib import Path
from fastapi import Request, Response

USER_COOKIE_NAME = "liveavatar_user"
USER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
USERS_DIR = Path(_HERE) / "users"          # users/<user_id>/...
```

### Replace the `@app.get("/")` handler

**Before:**

```python
@app.get("/")
async def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "index.html"))
```

**After:**

```python
@app.get("/")
async def index(request: Request):
    response = FileResponse(os.path.join(_HERE, "index.html"))
    if not request.cookies.get(USER_COOKIE_NAME):
        response.set_cookie(
            USER_COOKIE_NAME,
            str(uuid.uuid4()),
            max_age=USER_COOKIE_MAX_AGE,
            httponly=False,   # frontend may want to read for "claim conversation"
            samesite="lax",
        )
    return response
```

### Add a small helper

```python
def _user_id(request: Request) -> str:
    """Return the visitor's stable id. Falls back to a one-shot UUID if the
    cookie hasn't been set yet — e.g. the user lands directly on an API
    endpoint instead of through /. That conversation will be orphaned from
    long-term memory, which is correct behavior for a stranger calling the
    API."""
    return request.cookies.get(USER_COOKIE_NAME) or f"anon-{uuid.uuid4()}"
```

### Rationale

Cookie middleware is overkill for one cookie. A simple `set_cookie` on the
index handler covers every real first visit (the browser loads `/` before
anything else). The `_user_id()` helper takes a `Request` and is called
explicitly from each endpoint that needs it, which keeps the dependency
visible in function signatures rather than buried in middleware state.

`httponly=False` so the frontend JS can read the cookie if we later add a
"this is me" UI element (claim conversation, view your profile, delete
your data). If you'd rather keep it server-only for now, flip to `True`
and we can revisit when we build the user-facing UI.

---

## Piece 2 — Load user context on each LLM call

Two endpoints need it: `/api/llm` and `/api/converse-stream`. Both build
a `system` prompt today. We prepend the user's profile + compiled memory
before the existing system prompt content.

### Add the loader function

```python
def load_user_context(user_id: str) -> str:
    """Read users/<user_id>/USER.md + MEMORY.md, return as a delimited
    block to prepend to the system prompt. Returns '' for first-time
    visitors (no folder yet)."""
    user_dir = USERS_DIR / user_id
    if not user_dir.exists():
        return ""

    parts: list[str] = []
    user_md = user_dir / "USER.md"
    memory_md = user_dir / "MEMORY.md"

    if user_md.exists():
        parts.append(
            "[About this person — durable facts]\n"
            + user_md.read_text(encoding="utf-8").strip()
        )
    if memory_md.exists():
        parts.append(
            "[Compiled memory from past conversations]\n"
            + memory_md.read_text(encoding="utf-8").strip()
        )

    if not parts:
        return ""

    return (
        "\n\n--- CLIENT CONTEXT ---\n"
        "The following is what you already know about the person you are "
        "speaking with. Use it naturally — don't quote it back verbatim, "
        "and don't say 'according to my notes.' Speak as someone who "
        "remembers them.\n\n"
        + "\n\n".join(parts)
        + "\n--- END CLIENT CONTEXT ---\n"
    )
```

### Patch the two endpoints

**`/api/llm` — current signature and lines 350–352:**

```python
async def llm(req: LlmReq):
    history = _history.setdefault(req.session_id, [])
    context = await retrieve_context(req.user_text)
    system = SYSTEM_PROMPT
```

**After:**

```python
async def llm(req: LlmReq, request: Request):
    user_id = _user_id(request)
    history = _history.setdefault(req.session_id, [])
    context = await retrieve_context(req.user_text)
    system = SYSTEM_PROMPT + load_user_context(user_id)
```

**`/api/converse-stream` — same pattern. Current lines 575, 595–597:**

```python
async def converse_stream(req: LlmReq):
    ...
    async def event_stream():
        try:
            history = _history.setdefault(req.session_id, [])
            context = await retrieve_context(req.user_text)
            system = SYSTEM_PROMPT
```

**After:**

```python
async def converse_stream(req: LlmReq, request: Request):
    user_id = _user_id(request)
    ...
    async def event_stream():
        try:
            history = _history.setdefault(req.session_id, [])
            context = await retrieve_context(req.user_text)
            system = SYSTEM_PROMPT + load_user_context(user_id)
```

### Rationale

Concatenating onto `SYSTEM_PROMPT` rather than adding a second `system`
message keeps the existing message-array shape unchanged — fewer moving
parts in the streaming path. The CLIENT CONTEXT block is delimited so we
can later strip / replace / version it without regex-hunting.

The instruction "Use it naturally — don't quote it back verbatim, and
don't say 'according to my notes.' Speak as someone who remembers them"
is doing real work. Without it, models often start the next reply with
"As I noted previously, you mentioned you're 62..." which feels robotic
and breaks the human-advisor illusion.

Cost: USER.md will be small (<1 KB). MEMORY.md is capped at ~25 KB by
the consolidate-memory pattern. Worst case: 30 KB of context per call —
about 7,500 tokens. At gpt-5.4 input rates this is negligible compared
to the value of continuity, and we already inject 4 RAG chunks per call
(typically larger).

---

## Piece 3 — Save a session digest when the conversation ends

The `/api/forget` endpoint already runs when the frontend signals
session end. We piggyback on it: before clearing `_history`, hand the
transcript to a small async function that writes a digest.

### Add the digest writer

```python
import datetime as _dt


_DIGEST_SYSTEM = """You are summarizing a single conversation between a
financial advisor (Tom Olds) and a client. Output a markdown file with
exactly these sections:

# Session <YYYY-MM-DD HH:MM>

## Context
2-3 sentences. What did this conversation focus on?

## What the client shared
Bullet list. Facts about the client we learned: age, accounts, family,
goals, concerns, preferences. Only what they actually said. Do not
invent.

## Decisions and answers
Bullet list. Specific recommendations made or questions answered.

## Open questions
Bullet list. Things to follow up on next time, or facts we still need.

Constraints:
- Plain markdown. No emojis. No headings beyond ##.
- Concise. The whole file should be under 400 words.
- Voice: declarative third-person. NEVER write 'the client mentioned'
  or 'the advisor explained.' State the fact directly.
- If a section has nothing to capture, write the heading and 'None this session.'
"""


async def save_session_digest(user_id: str, turns: list[dict]) -> None:
    """Write users/<user_id>/memory/<timestamp>.md by asking the LLM to
    summarize the turns. Idempotent in the sense that re-running on the
    same turns simply produces another (near-identical) timestamped file."""
    if not turns or user_id.startswith("anon-"):
        return  # nothing to save, or stranger calling API directly

    user_dir = USERS_DIR / user_id / "memory"
    user_dir.mkdir(parents=True, exist_ok=True)

    transcript = "\n".join(
        f"{t['role'].upper()}: {t['content']}"
        for t in turns
        if t.get("content")
    )
    now = _dt.datetime.now()
    when = now.strftime("%Y-%m-%d %H:%M")

    messages = [
        {"role": "system", "content": _DIGEST_SYSTEM.replace("<YYYY-MM-DD HH:MM>", when)},
        {"role": "user", "content": f"Transcript:\n\n{transcript}"},
    ]

    try:
        completion = await aclient.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.2,
        )
        digest = (completion.choices[0].message.content or "").strip()
    except Exception:
        log.exception("Failed to generate session digest for %s", user_id)
        return

    if not digest:
        return

    filename = now.strftime("%Y-%m-%d_%H-%M.md")
    path = user_dir / filename
    path.write_text(digest + "\n", encoding="utf-8")
    log.info("Saved session digest for %s → %s", user_id, path)
```

### Patch `/api/forget`

**Before:**

```python
@app.post("/api/forget")
async def forget(payload: dict):
    """Clear the in-memory transcript when a session ends."""
    sid = payload.get("session_id")
    if sid:
        _history.pop(sid, None)
    return {"ok": True}
```

**After:**

```python
@app.post("/api/forget")
async def forget(payload: dict, request: Request):
    """Save a digest of the conversation, then clear the in-memory transcript."""
    sid = payload.get("session_id")
    if sid:
        turns = _history.get(sid, [])
        user_id = _user_id(request)
        # Fire-and-forget: don't block the client's tab-close on the digest call.
        asyncio.create_task(save_session_digest(user_id, turns))
        _history.pop(sid, None)
    return {"ok": True}
```

### Rationale

`asyncio.create_task` means the LLM call happens in the background.
The client tab closes immediately; the digest writes a few seconds
later. If uvicorn shuts down mid-write the file is just missing — no
half-written digest because `path.write_text` is atomic at the OS level.

`anon-` prefixed user_ids (the fallback when no cookie was set, e.g.
direct API calls) are explicitly skipped. They have no folder; we don't
want orphaned `users/anon-xxx/memory/...` files.

Cost: one extra LLM call per session. With Flash-tier models that's
~$0.001–0.003. Acceptable. If we move to AWS later, this is the natural
place to drop in a SQS message that triggers a Lambda summarizer
instead of running it inline.

---

## What's NOT in this proposal

Deliberately deferred to keep this diff small:

- **`memory_compiler.py`** — the synthesizer that regenerates
  `users/<id>/MEMORY.md` from accumulated `memory/*.md` files. Separate
  task (#59). Runs on a cron / event, not inline.
- **First-visit greeting flow** — the avatar asking "what should I call
  you?" and writing the initial `USER.md`. Separate task (#60). This
  proposal assumes `USER.md` either exists already or starts empty; both
  cases are handled by `load_user_context()` returning '' gracefully.
- **Supervisor view** — the page that lets you (Tom-the-human) review
  what your digital twin has been telling clients. Phase 2+.
- **Magic-link / proper auth** — Phase 1.5, before any external demo.
- **Consent capture** — Phase 1.5, same trigger.

---

## Open decisions to make before applying

1. **Cookie scope.** I default to `httponly=False` so the frontend can
   eventually use the cookie for a "this is me" UX. If you want
   server-only for now, change to `True`. We can change it later — no
   data migration needed.

2. **`session_id` from frontend.** The frontend currently mints
   `session_id` via `crypto.randomUUID()` and passes it on every request.
   That's fine for now. If we later want a user to be able to resume the
   exact same conversation after closing the tab, we'd need to make
   `session_id` derived from the cookie too. Not blocking.

3. **Digest model.** Defaulting to `LLM_MODEL` (gpt-5.4) for now. Could
   drop the digest to a cheaper Flash-tier model — separate decision,
   easy to change.

4. **Skipped on empty.** `save_session_digest` returns early if the
   conversation had no turns. Tom said "this is the start of a useful
   project" — should we instead write a placeholder file noting the user
   visited but didn't say anything? My default is no, but it's a
   one-line change.

5. **Folder location.** `users/` at the project root sits next to
   `chroma_db/`, `annuity_docs/`, `natebjones/`, etc. Should it be
   gitignored from day one? My recommendation: **yes** — client data
   should never enter a public repo. I'll add `users/` to `.gitignore`
   when we apply.

---

## Test plan after applying

1. **First visit cold.** Open `localhost:8000/` in an incognito window,
   talk to the avatar, end the session. Confirm:
   - A new cookie appears for `localhost`.
   - `users/<uuid>/memory/<timestamp>.md` exists and reads sensibly.
   - The system prompt during the conversation did NOT include client
     context (no folder yet).

2. **Second visit warm.** Reopen the same window (cookie persists),
   start a new conversation. Confirm:
   - The avatar references things from the prior session naturally,
     without saying "I remember from my notes."
   - A second digest file appears.
   - `users/<uuid>/memory/` now has two files.

3. **Forget endpoint robustness.** Trigger `/api/forget` with no
   session, with a session that has no turns, and with a normal
   session. Confirm no exceptions; confirm digest is written only for
   the normal case.

4. **Anonymous API call.** `curl POST /api/llm` directly without
   cookies. Confirm:
   - Response is normal (no client context loaded).
   - No `users/anon-*/` folder is created.

---

## Apply order (when ready)

1. Add `users/` to `.gitignore`.
2. Apply Piece 1 (cookie + `_user_id`).
3. Apply Piece 2 (`load_user_context` + the two endpoint patches).
4. Apply Piece 3 (`save_session_digest` + `/api/forget` patch).
5. Restart the server. Walk the test plan in order.

Total lines added: ~110. Lines changed in existing functions: ~6.
