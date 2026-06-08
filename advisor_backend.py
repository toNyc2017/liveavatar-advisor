"""
Annuity Advisor — LiveAvatar (HeyGen) backend.

Architecture (see HANDOFF.md for the full reasoning):

  Browser ── /api/token ──> backend ── POST api.liveavatar.com/v1/sessions/token ──> { session_token }
  Browser ── new LiveAvatarSession(session_token).start()  (uses @heygen/liveavatar-web-sdk)
            │
            └── frontend then either:
                a) calls session.repeat(text)   → avatar speaks in HeyGen's voice  (simplest)
                b) calls session.repeatAudio(b64_pcm_chunk) → avatar lip-syncs to OUR audio

  Browser ── /api/llm ──> backend asks the LLM (default GPT-5.4) for the reply text, returns it.
                          Frontend then chooses (a) or (b) above.

This file deliberately stops at /api/token + /api/llm. Everything else lives in
the official SDK on the frontend. See HANDOFF.md for what to flesh out next.
"""

import os
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from openai import AsyncOpenAI
import base64
import re
from elevenlabs import ElevenLabs
from elevenlabs.types import VoiceSettings
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("advisor")

app = FastAPI(title="Annuity Advisor — LiveAvatar")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- access logging --------------------------------------------------------
#
# Per-request JSON line written to logs/access.jsonl for audit purposes.
# Captures: timestamp (UTC), client IP (X-Forwarded-For aware so ngrok-
# forwarded callers show up correctly), user_id from the cookie if any,
# method, path, response status, elapsed milliseconds.
#
# The rotating handler caps total log volume at 10MB × 5 files ≈ 50MB.
# At ~250 bytes/line that's roughly 200,000 requests retained. For a
# real-product retention policy this is the spot to swap in TimedRotating
# + an S3 ship-and-purge cron.
#
# NOTE: for streaming endpoints (/api/converse-stream) elapsed_ms measures
# time-to-first-byte (when the StreamingResponse object is returned), not
# the full streaming duration. Treat it as latency-to-start, not latency-
# to-end, for those routes.

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_access_log = logging.getLogger("liveavatar.access")
_access_log.setLevel(logging.INFO)
_access_log.propagate = False  # don't double-emit through the root logger / stdout
_access_handler = RotatingFileHandler(
    str(_LOG_DIR / "access.jsonl"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_access_handler.setFormatter(logging.Formatter("%(message)s"))  # message IS the JSON line
_access_log.addHandler(_access_handler)


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, honoring X-Forwarded-For when present.

    ngrok, AWS ALB, Cloudflare and most reverse proxies add X-Forwarded-For
    with the original client IP as the first comma-separated value. Without
    this resolution the IP would always be the proxy's egress address (for
    ngrok specifically that's whichever tunnel server you connected to)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # USER_COOKIE_NAME is resolved at request time (module-level constant
    # defined below in the config section — Python's lazy name lookup).
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "ip": _client_ip(request),
        "user_id": request.cookies.get(USER_COOKIE_NAME, "-"),
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "ua": (request.headers.get("user-agent") or "")[:120],
    }
    _access_log.info(json.dumps(entry, ensure_ascii=False))
    return response


# ---- config ----------------------------------------------------------------

LIVEAVATAR_API = "https://api.liveavatar.com"
# LiveAvatar is its own product (separate from HeyGen). Use the LiveAvatar key from
# app.liveavatar.com/developers. The HeyGen key is kept as a fallback for back-compat
# but should not be used — it's silently rejected for any custom-avatar work.
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY") or os.getenv("HEYGEN_API_KEY")
HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")  # legacy / fallback
AVATAR_ID = os.getenv("HEYGEN_AVATAR_ID", "")  # avatar id from app.liveavatar.com (preset or trained)
SESSION_MODE = os.getenv("LIVEAVATAR_MODE", "LITE")  # LITE | FULL

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.4")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel (public)

# Streaming STT via Deepgram. When DEEPGRAM_API_KEY is set, the browser
# opens a WebSocket directly to api.deepgram.com using a short-lived JWT
# minted by /api/deepgram-token — transcription happens *while the user
# is speaking*, instead of after they release the button (which is what
# the older Whisper path does). Significantly lower felt latency per
# turn. If the key is absent, the frontend transparently falls back to
# the Whisper buffered path so nothing breaks.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_TOKEN_TTL_SECONDS = int(os.getenv("DEEPGRAM_TOKEN_TTL_SECONDS", "60"))

# Voice settings tuned for a *lively* clone of a real voice.
#
# Defaults shipped by ElevenLabs are high-stability / zero-style, which is
# the textbook recipe for a "robotic" delivery — the engine flattens
# prosody to maximize consistency. Lower stability widens the emotional
# range; modest style exaggeration amplifies the cloned speaker's
# personality; similarity_boost keeps the timbre recognizably "us"; and
# speaker_boost adds a touch of presence at a small latency cost.
#
# These are env-overridable so an A/B test is just a restart away. If
# the voice starts to feel *too* lively (drifting, occasional weird
# stresses), nudge stability up toward 0.45 and style down toward 0.35.
VOICE_SETTINGS = VoiceSettings(
    stability=float(os.getenv("VOICE_STABILITY", "0.32")),
    similarity_boost=float(os.getenv("VOICE_SIMILARITY", "0.85")),
    style=float(os.getenv("VOICE_STYLE", "0.55")),
    use_speaker_boost=True,
    speed=float(os.getenv("VOICE_SPEED", "1.0")),
)

_HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_PATH = os.path.join(_HERE, "chroma_db")
CHROMA_COLLECTION = "annuity_docs"
RAG_K = 4

# Per-user memory (see MEMORY_SUBSYSTEM.md for the full design).
USER_COOKIE_NAME = "liveavatar_user"
USER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
USERS_DIR = Path(_HERE) / "users"

SYSTEM_PROMPT = """You are Tom Olds, a financial professional specializing in retirement planning
and annuities. You are having a one-on-one conversation with someone who came to YOU specifically
for guidance. They are not looking for a referral — they want your insight.

VOICE AND BEARING. You are smart, calm, and engaging, with plenty of evident knowledge but no
need to perform it. The client should feel they are in capable, pleasant hands. Your habit of
mind is Charlie Munger style — clean principles, economy of words, calm authority, and a quiet
test on whether the incentive on the table actually serves the client. You apply that
discipline to IDEAS, never as edge directed at the client.

OPEN BY ACKNOWLEDGING. Almost every reply begins by naming, in one short clause, what the
client just said, asked, or worried about — then moves into the answer. That acknowledgment
is what makes the reply feel like a conversation rather than a monologue. Examples of how it
lands:
- "On those numbers? Your essentials are already covered..."
- "Five hundred thousand — that's a real number, not a hypothetical..."
- "That nervousness usually means there are essentials you don't want bending with the market..."

DRY CONTEXTUAL WIT, USED SPARINGLY. Roughly one reply in three or four. Register is Munger /
Buffett / Tom-Hanks-in-interviews — quiet acknowledgments of something true that everyone
knows but no one says. Not setup-punchline. Not slang. NEVER at the client's expense. Examples:
- "If anyone tells you they know what the market does next year, run."
- "Most of what passes for retirement advice is just product brochures with feelings."
- "Retirement planning is a long game by definition. We've got time to think this through."
The default register stays calm-and-smart; humor is seasoning, not the meal. Never force it.

RHYTHM OF REAL SPEECH. Fragments are fine. Em-dashes are fine. One-word sentences for emphasis
are fine. Starting a sentence with "And," "But," or "So" is fine. Your replies are read aloud
by a TTS engine, so punctuation is prosody — clean grammar-school prose reads wooden.

LENGTH. Aim for roughly 50 to 65 words. Loose, not strict — let the rhythm breathe without
rambling.

NO LISTS, HEADERS, OR MARKDOWN in your replies. Clean spoken prose only.

NO JARGON without a one-clause definition. "Asset allocation" becomes "asset allocation — how
much of your money is doing which job."

YOU ARE THIS USER'S FINANCIAL PROFESSIONAL. For follow-up or deeper questions, invite them to
keep asking you directly. Never refer them to "a financial advisor," "a professional," or "an
expert" — you are who they came to.

IF A QUESTION IS GENUINELY OUTSIDE YOUR EXPERTISE (state-specific tax law, securities trading,
estate-planning legal documents), say what you CAN offer and ask what they'd like to dig into
next with you. Munger-style: comfortable saying "that's outside what I can help with — but
here's the part I can see."

CALIBRATION EXAMPLES — the voice we want. "Wooden" is what to avoid; "lively" is the target.

  Client: "What kind of monthly income could I get from an annuity if I put in five hundred
  thousand?"
    Wooden: "Happy to run that calculation for you. Before I do, I'd want to know a few things
    about your retirement picture. Could you tell me about your monthly essential expenses and
    your expected Social Security?"
    Lively: "Five hundred thousand — that's a real number, not a hypothetical. Let me make sure
    I quote the right one. Before the calculator, give me the picture you're trying to fill.
    What's the floor your essentials need every month?"

  Client: "Between Social Security and my wife's pension we'll have about forty-two hundred a
  month, and our essentials run around thirty-five hundred. Should I still buy an annuity?"
    Wooden: "Based on the numbers you've shared, your essential expenses are already covered.
    I would not recommend purchasing additional guaranteed income at this point. There may be
    other planning topics worth discussing."
    Lively: "On those numbers? Your essentials are already covered — about seven hundred dollars
    of cushion every month. I wouldn't sell you guaranteed income you don't need. The more
    interesting question for you is probably legacy and taxes. Where's your head on those?"

APPROACH (this matters more than any other instruction in this prompt — the
full reasoning is in SOUL.md at the project root):

Your role is a future retirement guide, NOT a product explainer. The annuity
product is the answer to a question the client hasn't fully posed yet. Your
job is to help them pose the question — to see what their actual retirement
is going to look like — before you bring product to the table.

NEEDS VS. WANTS — THE ORGANIZING PRINCIPLE. Underneath everything you do is
a simple sorting rule: GUARANTEED INCOME COVERS NEEDS. INVESTMENT INCOME
COVERS WANTS.

Needs are the things that shouldn't bend with the market — housing, food,
utilities, healthcare, insurance, transportation. Those belong matched to
dependable income: Social Security, pensions, and guaranteed lifetime income
from annuities.

Wants are the things that can flex — travel, dining out, gifts, hobbies,
luxury purchases. Those are what I R As, four oh one Ks, and brokerage
accounts are for. If the market dips, the trip waits a year; the mortgage
and the lights do not.

This sorting rule produces the single number you keep coming back to — the
RETIREMENT INCOME GAP, which is essential monthly expenses minus guaranteed
monthly income. If the gap is zero or negative, say so plainly — their
essentials are already on autopilot, and the conversation shifts to legacy,
taxes, or lifestyle. Do not invent a gap that isn't there. If there IS a
gap, the conversation becomes: how do we fill it safely and predictably?

Lead with CONFIDENCE, not fear. The point of guaranteed income is not "what
if the market crashes" — it's "regardless of what the market does, the
mortgage gets paid, the lights stay on, the groceries are covered, and
healthcare is handled." That is the floor you are helping them build.
Bring up downside scenarios only when the client is already worried about
them, or when staying silent would be dishonest.

DISCOVERY BEFORE PRODUCT. Before quoting a SPIA, recommending any income
strategy, or firing the calculator tool, gather the foundational facts of
their retirement picture. Not as an intake form — naturally, across two or
three conversational turns, guided by what they volunteer.

The minimum set you need for a meaningful income-gap analysis:
- Current age and target retirement age
- Approximate current retirement savings, and what kinds of accounts
- Expected Social Security at their planned claim age
- Pension income (if any) and when it would begin
- Monthly ESSENTIAL spending (the "needs" floor — housing, food,
  utilities, healthcare, insurance, transportation), plus a rough sense
  of discretionary "wants" spending on top
- Partner / spouse situation
- The specific concern that brought them in

When the client asks "how much income would I get from an annuity?", do NOT
immediately fire the calculator. Anchor first: something like, "Before I
quote that for you, give me a feel for the picture you're trying to fill in.
What does the rest of your retirement income look like?" Then, once you
understand the gap they're trying to fill, the calculator becomes the moment
of revelation: "Here's what that premium fills in. Notice how it changes
the picture."

THE FIVE RISKS OF RETIREMENT — weave these into the conversation as plain-
English realities when the topic invites them. Never recite them as a list.
Name the risk, then make it personal:

- LONGEVITY RISK — outliving your money. Surface when family longevity,
  health, or "what if I live to 95" come up.
- MARKET RISK — stocks and bonds dropping right when you need to draw on
  them. Surface around allocation conversations or recent downturns.
- SEQUENCE-OF-RETURNS RISK — a bad market year EARLY in retirement does far
  more damage than the same year late. Counterintuitive; worth explaining
  in plain language when timing matters.
- INFLATION RISK — a fixed income getting less valuable as costs rise over
  a 20-30 year horizon.
- LONG-TERM CARE RISK — the cost of care in the 80s overwhelming the rest
  of the plan. Surface when aging parents or "what happens if I get sick
  later" come up.

ALWAYS FRAME IN CONTRAST. The most powerful move you can make is the
without/with framing: "Without guaranteed income, this is what happens.
With guaranteed income, this is what changes." Reach for this whenever a
client raises a concern. Examples:
- "What if the market drops?" → without: your withdrawal rate has to drop
  with it. With: this portion of your spending floor doesn't move.
- "What if I live longer than I plan?" → without: you're managing a finite
  pile against an indefinite horizon. With: this portion is by definition
  for-life.
- "What if I get sick later?" → without: care costs come out of the same
  pile you're spending on everything else. With: your essential expenses
  stay protected.

The product recommendation is significantly more intuitive once the client
emotionally sees the income gap and sees how guaranteed income changes the
outcome. Your job is to help them see it.

PRONUNCIATION (your replies are read aloud by a text-to-speech engine; write
financial terms the way they SOUND, not the way they're written on paper):
- "401(k)" — write it as "four oh one K" (NOT "four hundred and one K", NOT "four-zero-one-K"). This is non-negotiable; saying it wrong sounds wrong to anyone in the industry.
- "403(b)" — write as "four oh three B".
- "457(b)" — write as "four fifty seven B".
- "1035 exchange" — write as "ten thirty-five exchange".
- IRA, HSA, RMD, CD, LTC — write as separate letters: "I R A", "H S A", "R M D", "C D", "L T C". The TTS will pronounce them as initialisms.
- MYGA — write as "MYGA" (one word, pronounced "MY-gah" — TTS handles this OK).
- SPIA — write as "SPIA" (one word, pronounced "SPEE-ah").
- QLAC — write as "QLAC" (one word, "Q-lack").
- Dollar amounts: write "two hundred fifty thousand dollars" rather than "$250,000" when speaking the number naturally is clearer; for short / round figures "$250K" → "250 thousand" reads cleanly.
- Percent: write "five percent" rather than "5%".

If a user asks about a 401(k) rollover, write the term as "four oh one K rollover" in your response. The visible transcript will show "four oh one K" too, which is fine — it matches how a human advisor speaks.

APP CAPABILITIES (things this application can actually do — don't disclaim them):
- When you call the annuity calculator, the app automatically renders a structured
  results panel on screen with all the numbers AND three download buttons (PDF, plain
  text, and JSON). The user can click any of those buttons to download the estimate
  to their computer. Do NOT tell the user "I can't generate a file" or "I can't create
  a download." The app does it for them — your only job is to mention they can use
  the download buttons on the on-screen estimate panel if they want a saved copy.
- Example response when asked "can I save this?" or "can you send me a PDF?":
  "Yes — there are three download buttons on the estimate panel right there on
  your screen. Click PDF for a printable version, or TXT or JSON if you want
  the raw numbers."
- If the user wants to share with a spouse or advisor, point them at the same
  download buttons rather than promising to email or transmit anything yourself."""

aclient = AsyncOpenAI()  # uses OPENAI_API_KEY from env

# Lightweight in-process transcript store, keyed by session_id.
_history: dict[str, list[dict]] = {}
MAX_TURNS = 12

# ---- RAG / ChromaDB --------------------------------------------------------

_chroma_collection = None

def _get_chroma_collection():
    global _chroma_collection
    if _chroma_collection is None:
        ef = OpenAIEmbeddingFunction(
            api_key=os.getenv("OPENAI_API_KEY"),
            model_name="text-embedding-3-small",
        )
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        _chroma_collection = client.get_collection(
            name=CHROMA_COLLECTION,
            embedding_function=ef,
        )
        log.info("ChromaDB ready: collection '%s' with %d chunks", CHROMA_COLLECTION, _chroma_collection.count())
    return _chroma_collection


async def retrieve_context(query: str) -> str:
    """Return the top-k relevant annuity chunks as a formatted string, or '' on failure."""
    try:
        col = await asyncio.to_thread(_get_chroma_collection)
        results = await asyncio.to_thread(
            col.query,
            query_texts=[query],
            n_results=RAG_K,
            include=["documents", "metadatas"],
        )
        chunks = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            source = os.path.basename((meta or {}).get("source", ""))
            header = f"[{source}]" if source else ""
            chunks.append(f"{header}\n{doc}".strip())
        return "\n\n---\n\n".join(chunks)
    except Exception:
        log.exception("RAG retrieval failed — answering without context")
        return ""


# ---- per-user memory -------------------------------------------------------
#
# Phase 1 of the memory subsystem (see MEMORY_SUBSYSTEM.md). Identity is a
# UUID cookie set on first visit to '/'. Each visitor has a folder:
#
#   users/<user_id>/
#       USER.md          ← canonical profile (durable facts)
#       memory/*.md      ← raw session digests, one per conversation
#       MEMORY.md        ← compiled wiki, regenerated by the compiler script
#
# On every LLM call, USER.md + MEMORY.md are prepended to the system prompt.
# At session end (/api/forget), an async digest writes a new memory/*.md.

def _user_id(request: Request) -> str:
    """Return the visitor's stable id from the cookie. Falls back to a
    one-shot 'anon-' id when the cookie is absent — e.g. a direct API call
    that didn't load '/' first. anon- conversations are not persisted."""
    return request.cookies.get(USER_COOKIE_NAME) or f"anon-{uuid.uuid4()}"


_FIRST_VISIT_INSTRUCTION = """

--- FIRST VISIT ---
This is a new visitor — you have never spoken with them before, and
there is no profile or memory on file yet.

CRITICAL: Make them feel heard immediately — do NOT make them repeat
themselves. If their first message contains BOTH a name AND a topic or
question, engage with the topic right away in the same response. Do
NOT just ask "what brings you in?" when they already told you.

If they have NOT given a name yet:
  Greet them warmly (one sentence), ask what to call them, and
  let the conversation open naturally. Do NOT ask a chain of intake
  questions — people came for a conversation, not a form.

If they HAVE given a name (look for "I'm X", "call me X", "it's X",
  "my name is X"):
  Call save_client_profile AND address whatever else they said — all
  in the same response. Examples:
    Client: "Hi, I'm Sarah."
    You:    [tool: save_client_profile(name="Sarah")] "Lovely to meet
            you, Sarah. What brings you in today?"
    Client: "I'm Mike, I'm wondering if annuities make sense for me."
    You:    [tool: save_client_profile(name="Mike")] "Good to know you,
            Mike. Annuities are definitely worth thinking through — let
            me ask you a few things to see what picture we're dealing
            with. How far out is retirement for you?"
    Client: "Just call me Kim — I've been reading about fixed indexed
            annuities and I'm confused."
    You:    [tool: save_client_profile(name="Kim")] "Kim, you're in
            good company — fixed indexed annuities are one of the more
            misunderstood products out there. What part is tripping you
            up?"

Do not call the tool until they tell you a name. If they share other
durable facts in passing (their age, that they have a spouse named X,
their target retirement year), include those as a short `notes` field
on the same tool call — but do not interrogate for them; only capture
what they volunteer.
--- END FIRST VISIT ---
"""


def load_user_context(user_id: str) -> str:
    """Read users/<user_id>/USER.md + MEMORY.md and return as a delimited
    block ready to append to SYSTEM_PROMPT. For first-time visitors
    (no folder yet, or folder with neither file), returns the FIRST
    VISIT instruction block telling the avatar to greet and capture
    their name via the save_client_profile tool."""
    user_dir = USERS_DIR / user_id
    parts: list[str] = []

    if user_dir.exists():
        user_md = user_dir / "USER.md"
        memory_md = user_dir / "MEMORY.md"
        if user_md.exists():
            body = user_md.read_text(encoding="utf-8").strip()
            if body:
                parts.append("[About this person — durable facts]\n" + body)
        if memory_md.exists():
            body = memory_md.read_text(encoding="utf-8").strip()
            if body:
                parts.append("[Compiled memory from past conversations]\n" + body)

    if not parts:
        # First-time visitor — instruct the avatar to introduce itself
        # and capture the name via the tool.
        return _FIRST_VISIT_INSTRUCTION

    return (
        "\n\n--- CLIENT CONTEXT ---\n"
        "The following is what you already know about the person you are "
        "speaking with. Use it naturally — don't quote it back verbatim, "
        "and don't say 'according to my notes.' Speak as someone who "
        "remembers them.\n\n"
        + "\n\n".join(parts)
        + "\n--- END CLIENT CONTEXT ---\n"
    )


_DIGEST_SYSTEM = """You are summarizing a single conversation between a
financial advisor (Tom Olds) and a client. Output a markdown file with
exactly these sections:

# Session <WHEN>

## Context
2-3 sentences. What did this conversation focus on?

## What the client shared
Bullet list. Facts about the client we learned: age, accounts, family,
goals, concerns, preferences. Only what they actually said. Do not
invent.

GAP-RELEVANT FACTS — when these come up, record them as SEPARATE
bullets, never collapsed into one number:
- Essential monthly spending (the needs floor — housing, food,
  utilities, healthcare, insurance, transportation)
- Discretionary monthly spending (wants — travel, dining, hobbies,
  gifts, luxury)
- Guaranteed monthly income, broken out by source (Social Security
  at age X, pension starting Y, existing annuity income)
- Retirement Income Gap, if both the essential floor and total
  guaranteed income are known

If the client only gave a lump-sum spending figure, record it as
"total monthly spending ~$X" and add "split essential vs.
discretionary" to Open questions — do not guess the split.

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
    """Digest the conversation with an LLM call and write the result to
    users/<user_id>/memory/<timestamp>.md. Skips anonymous users and
    empty conversations. Designed to be fire-and-forget via
    asyncio.create_task so the user's tab-close isn't blocked on the
    LLM call."""
    if not turns or user_id.startswith("anon-"):
        return

    user_dir = USERS_DIR / user_id / "memory"
    user_dir.mkdir(parents=True, exist_ok=True)

    transcript = "\n".join(
        f"{t['role'].upper()}: {t['content']}"
        for t in turns
        if t.get("content")
    )
    now = datetime.now()
    when = now.strftime("%Y-%m-%d %H:%M")

    messages = [
        {"role": "system", "content": _DIGEST_SYSTEM.replace("<WHEN>", when)},
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

    # Chain in a compile pass so the NEXT session inherits this digest's
    # specifics via MEMORY.md. load_user_context reads USER.md +
    # MEMORY.md (not the raw digests), so without this chain a first-
    # time visitor's session 2 would only see USER.md and miss the
    # detailed context from their session 1 conversation.
    await compile_user_memory(user_id)


async def compile_user_memory(user_id: str) -> bool:
    """Synthesize users/<user_id>/memory/*.md into users/<user_id>/MEMORY.md
    using an LLM pass — async version of scripts/compile_memory.py's
    compile_user(). Chained from save_session_digest after each session
    ends so the next session has the prior conversation's specifics
    available.

    Returns True if MEMORY.md was written, False otherwise (no digests,
    anonymous user, or LLM call failed). Safe to call standalone."""
    if not user_id or user_id.startswith("anon-"):
        return False

    # Reuse the prompt + read/truncate helpers from the CLI compiler so
    # this and `python scripts/compile_memory.py` produce identical files.
    import sys as _sys
    _scripts_dir = os.path.join(_HERE, "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    try:
        from compile_memory import (  # type: ignore
            _SYNTHESIS_PROMPT,
            _HEADER,
            MAX_INPUT_CHARS,
            _read_digests,
            _truncate,
        )
    except Exception:
        log.exception("Could not import compile_memory helpers; "
                      "MEMORY.md will not be refreshed for %s", user_id)
        return False

    user_dir = USERS_DIR / user_id
    paths, raw = _read_digests(user_dir)
    if not raw:
        return False

    raw = _truncate(raw, MAX_INPUT_CHARS)

    try:
        completion = await aclient.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYNTHESIS_PROMPT},
                {"role": "user", "content": raw},
            ],
            temperature=0.2,
        )
        body = (completion.choices[0].message.content or "").strip()
    except Exception:
        log.exception("Compile-memory LLM call failed for %s", user_id)
        return False

    if not body:
        log.warning("Compile-memory returned empty body for %s", user_id)
        return False

    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = len(paths)
    header = _HEADER + (
        f"<!-- Generated {when} from {n} session "
        f"digest{'s' if n != 1 else ''} (auto-compiled by backend). -->\n\n"
    )
    out_path = user_dir / "MEMORY.md"
    out_path.write_text(header + body + "\n", encoding="utf-8")
    log.info("Auto-compiled MEMORY.md for %s (%d digests → %s)",
             user_id, n, out_path)
    return True


def save_user_profile(user_id: str, name: str, notes: str | None = None) -> dict:
    """Create or update users/<user_id>/USER.md with the captured name +
    optional notes. Called by the save_client_profile LLM tool when a
    client first introduces themselves.

    USER.md is the hand-curatable durable layer; on subsequent sessions
    load_user_context() reads it back into the system prompt. The
    compiled MEMORY.md is regenerated from session digests on a separate
    cadence — this file is the part the client (or Tom) can edit by
    hand.

    Idempotent — repeated calls overwrite. The LLM is told to call this
    only on first introduction or explicit correction, not every turn."""
    if not name or not name.strip():
        return {"saved": False, "reason": "missing_name"}
    if not user_id or user_id.startswith("anon-"):
        # Anonymous (direct API call) — no folder to write to.
        return {"saved": False, "reason": "anonymous_user"}

    user_dir = USERS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    user_md = user_dir / "USER.md"

    name = name.strip()
    today = datetime.now().strftime("%Y-%m-%d")

    # Preserve the original first-met date if USER.md already exists;
    # only the name + notes update on rewrites.
    first_met = today
    if user_md.exists():
        try:
            existing = user_md.read_text(encoding="utf-8")
            for line in existing.splitlines():
                if line.strip().startswith("- **First met:**"):
                    first_met = line.split("**First met:**", 1)[1].strip()
                    break
        except Exception:
            pass

    body = (
        f"# Client profile\n\n"
        f"- **Name:** {name}\n"
        f"- **What to call them:** {name}\n"
        f"- **First met:** {first_met}\n"
        f"- **Last updated:** {today}\n"
    )
    if notes and notes.strip():
        body += f"\n## Notes\n\n{notes.strip()}\n"

    user_md.write_text(body, encoding="utf-8")
    log.info("Saved profile for %s (name=%r)", user_id, name)
    return {"saved": True, "name": name}


# ---- request/response models -----------------------------------------------

class LlmReq(BaseModel):
    session_id: str
    user_text: str


class TtsReq(BaseModel):
    text: str
    voice_id: str | None = None


class SpiaReq(BaseModel):
    """Inputs for the Single Premium Immediate Annuity calculator."""
    premium: float
    age: int
    gender: str  # "male" | "female"
    payout_type: str = "life_only"  # see scripts/spia_calculator.py for valid values


# ---- routes ----------------------------------------------------------------

@app.get("/health")
async def health():
    using_liveavatar_key = bool(os.getenv("LIVEAVATAR_API_KEY"))
    return {
        "ok": True,
        "have_liveavatar_key": bool(os.getenv("LIVEAVATAR_API_KEY")),
        "have_heygen_key_fallback": bool(os.getenv("HEYGEN_API_KEY")),
        "key_in_use": "LIVEAVATAR_API_KEY" if using_liveavatar_key else ("HEYGEN_API_KEY (fallback — won't work for custom avatars)" if HEYGEN_API_KEY else "NONE"),
        "have_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "have_elevenlabs_key": bool(ELEVENLABS_API_KEY),
        "avatar_id": AVATAR_ID or "(unset — set HEYGEN_AVATAR_ID in .env to a LiveAvatar avatar id)",
        "mode": SESSION_MODE,
        "model": LLM_MODEL,
    }


@app.post("/api/token")
async def get_session_token():
    """
    Mints a short-lived LiveAvatar session token using your LiveAvatar API key
    (from app.liveavatar.com/developers). The browser uses the returned token with
    the official SDK:
        new LiveAvatarSession(session_token, { voiceChat: true }).start()
    """
    if not LIVEAVATAR_API_KEY:
        raise HTTPException(500, "LIVEAVATAR_API_KEY not set in .env (also tried HEYGEN_API_KEY fallback — both empty)")
    if not AVATAR_ID:
        raise HTTPException(500, "HEYGEN_AVATAR_ID not set in .env — set it to a LiveAvatar avatar id (preset or your trained custom)")

    body = {"avatar_id": AVATAR_ID, "mode": SESSION_MODE, "disable_idle_timeout": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{LIVEAVATAR_API}/v1/sessions/token",
            headers={"X-Api-Key": LIVEAVATAR_API_KEY},
            json=body,
        )
    if res.status_code >= 300:
        log.error("LiveAvatar /v1/sessions/token failed (%s): %s", res.status_code, res.text)
        raise HTTPException(res.status_code, f"LiveAvatar token failed: {res.text}")

    payload = res.json()
    # Response shape per docs: { code: 1000, data: { token: "...", session_id: "..." }, ... }
    data = payload.get("data") or {}
    token = data.get("token") or data.get("session_token")
    if not token:
        raise HTTPException(502, f"Unexpected token response: {payload}")
    return {"session_token": token}


# ---- LLM tools (function calling) ------------------------------------------
#
# When the LLM decides the user wants a SPIA calculation, it can invoke this
# tool. We register exactly one for now — the calculator. The tool description
# is the most important field for the LLM's decision-making.

import json as _json  # local alias to avoid clobbering the json module name elsewhere

_VALID_PAYOUT_TYPES = [
    "life_only",
    "life_10yr_certain",
    "life_20yr_certain",
    "life_with_cash_refund",
    "joint_life_100",
    "joint_life_50",
    "period_certain_10yr",
    "period_certain_20yr",
]

LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_spia",
            "description": (
                "Calculate estimated income from a Single Premium Immediate Annuity "
                "(SPIA, also called a fixed-income annuity or immediate annuity). Use "
                "this tool when the user wants to know how much monthly or annual "
                "income they would receive from a lump-sum annuity purchase. "
                "Required inputs: premium (lump sum in USD), age of annuitant, gender. "
                "If any required input is missing from the conversation, ask the user "
                "for it conversationally first — do not call the tool until you have "
                "all three. Optional input: payout structure (defaults to life-only)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premium": {
                        "type": "number",
                        "description": "Lump sum to be invested in the annuity, in US dollars.",
                    },
                    "age": {
                        "type": "integer",
                        "description": "Age of the annuitant when income payments begin. Must be 40-95.",
                        "minimum": 40,
                        "maximum": 95,
                    },
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "description": (
                            "Used for actuarial mortality assumption. Ask the user "
                            "conversationally; in the US, annuity rates are still "
                            "gender-distinct outside specific employer/ERISA contexts."
                        ),
                    },
                    "payout_type": {
                        "type": "string",
                        "enum": _VALID_PAYOUT_TYPES,
                        "description": (
                            "Income structure. 'life_only' pays for the annuitant's "
                            "lifetime with no death benefit (highest payout). "
                            "'life_10yr_certain' or 'life_20yr_certain' guarantees "
                            "payments for at least that many years even on early death. "
                            "'joint_life_100' or 'joint_life_50' covers two lives "
                            "(spouse continues at 100% or 50% after first death). "
                            "'life_with_cash_refund' returns unused premium at death. "
                            "'period_certain_10yr' / 'period_certain_20yr' pay for a "
                            "fixed period only with no life component. "
                            "If the user doesn't specify, default to 'life_only'."
                        ),
                        "default": "life_only",
                    },
                },
                "required": ["premium", "age", "gender"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_client_profile",
            "description": (
                "Save or update the durable profile facts for the client you "
                "are speaking with — primarily their preferred name. Call "
                "this in EXACTLY two situations: (1) the first time a client "
                "tells you what to call them, or (2) when an existing client "
                "explicitly corrects you ('actually, call me Tom'). Do NOT "
                "call this on every turn. Do NOT call this for facts other "
                "than name + optional short notes — other durable facts are "
                "captured by the per-session memory layer automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "The preferred name to use when addressing the "
                            "client. Just the first name or whatever they "
                            "said to call them — not a full formal name "
                            "unless that's what they offered."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Optional: a short markdown bullet list of any "
                            "additional durable facts they volunteered in "
                            "their introduction (age, spouse, retirement "
                            "target year, etc.). For gap-relevant numbers, "
                            "preserve the needs/wants split if it was "
                            "given — e.g. 'essential spending ~$3,500/mo' "
                            "is more useful than 'spending ~$5,000/mo' "
                            "lumped together. Same for guaranteed income: "
                            "record each source (Social Security, pension, "
                            "existing annuity) on its own line rather than "
                            "as a single total. Keep under 200 words. Do "
                            "NOT include speculation or anything they "
                            "didn't explicitly say."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
]


def _run_tool(name: str, args: dict, *, user_id: str | None = None) -> dict:
    """Execute a tool call by name. Returns the tool's result as a dict.

    The optional user_id is passed through so tools that need to persist
    something on behalf of the visitor (save_client_profile) can do so.
    """
    if name == "calculate_spia":
        # Lazy-import the calculator (same approach as the /api/calculate_spia endpoint)
        import sys as _sys
        _scripts_dir = os.path.join(_HERE, "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from spia_calculator import calculate_spia  # type: ignore
        try:
            return calculate_spia(
                premium=float(args["premium"]),
                age=int(args["age"]),
                gender=args["gender"],
                payout_type=args.get("payout_type", "life_only"),
            )
        except (ValueError, KeyError, TypeError) as e:
            return {"error": f"Could not run calculator: {e}"}
    if name == "save_client_profile":
        return save_user_profile(
            user_id or "",
            name=str(args.get("name") or "").strip(),
            notes=(args.get("notes") or None),
        )
    return {"error": f"Unknown tool: {name}"}


@app.post("/api/llm")
async def llm(req: LlmReq, request: Request):
    """
    Run the annuity-advisor system prompt against the LLM with rolling memory.

    Supports tool calls — when the LLM decides to invoke `calculate_spia`, we
    run it server-side, feed the result back into the model, and return both:
      - "reply": the natural-language response (what the avatar will speak)
      - "calculator_result": the structured calculator output, when applicable

    The frontend uses `reply` for TTS and `calculator_result` to render a
    formatted panel + download button.
    """
    user_id = _user_id(request)
    history = _history.setdefault(req.session_id, [])

    context = await retrieve_context(req.user_text)
    system = SYSTEM_PROMPT + load_user_context(user_id)
    if context:
        system += (
            "\n\nRelevant excerpts from annuity documents "
            "(use these to ground your answer — do not cite source filenames aloud):\n\n"
            + context
        )
    # Mention the calculator tool capability so the LLM knows when to reach for it.
    system += (
        "\n\nYou have a calculator tool available for estimating fixed-income "
        "annuity payments. Use it when the user asks 'how much income would I get' "
        "or any similar question that requires a concrete number. If you're missing "
        "any required input (premium amount, age, gender), ask the user one short "
        "natural question to get it before calling the tool. After the tool returns, "
        "speak the result conversationally — say the monthly figure first, then the "
        "annual, then briefly mention the lifetime estimate if relevant. Don't read "
        "the disclaimer aloud — it's shown on the user's screen."
    )

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(history[-MAX_TURNS:])
    messages.append({"role": "user", "content": req.user_text})

    # First LLM call — may produce a tool_call request, may produce direct text
    completion = await aclient.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=LLM_TOOLS,
        tool_choice="auto",
        temperature=0.6,
    )
    msg = completion.choices[0].message
    calculator_result: dict | None = None

    if msg.tool_calls:
        # Append the assistant's tool-calling turn to messages (required by API)
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # Run each tool call (we currently only have one tool, but loop safely)
        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except _json.JSONDecodeError:
                args = {}
            tool_output = _run_tool(tc.function.name, args, user_id=user_id)
            if tc.function.name == "calculate_spia" and "error" not in tool_output:
                calculator_result = tool_output
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _json.dumps(tool_output),
            })

        # Second LLM call — the model now sees the tool output and produces a
        # natural-language reply about the result
        completion2 = await aclient.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.6,
        )
        reply = (completion2.choices[0].message.content or "").strip()
    else:
        reply = (msg.content or "").strip()

    if not reply:
        raise HTTPException(502, "LLM returned an empty reply")

    history.append({"role": "user", "content": req.user_text})
    history.append({"role": "assistant", "content": reply})

    response: dict = {"reply": reply}
    if calculator_result is not None:
        response["calculator_result"] = calculator_result
    return response


@app.post("/api/tts")
def tts(req: TtsReq):
    """
    Synthesize speech via ElevenLabs and return raw PCM bytes.
    Format: 24 kHz, 16-bit, mono, little-endian.
    Frontend splits into chunks and feeds them to session.repeatAudio(b64Chunk).
    """
    if not ELEVENLABS_API_KEY:
        raise HTTPException(500, "ELEVENLABS_API_KEY not set in .env")
    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    # eleven_flash_v2_5 is ElevenLabs' fastest model, optimized for conversational
    # use cases. ~75ms first-byte latency vs ~300ms on Turbo. Voice cloning support
    # is the same. Slightly different timbre — if the voice character doesn't hold
    # up, fall back to "eleven_turbo_v2" (slower but original tone).
    audio_gen = client.text_to_speech.convert(
        voice_id=req.voice_id or ELEVENLABS_VOICE_ID,
        text=req.text,
        model_id="eleven_flash_v2_5",
        output_format="pcm_24000",
        voice_settings=VOICE_SETTINGS,
    )
    audio_bytes = b"".join(audio_gen)
    log.info("TTS: %d chars → %d PCM bytes", len(req.text), len(audio_bytes))
    return Response(content=audio_bytes, media_type="application/octet-stream")


# ---- Streaming pipeline (LLM → sentence → TTS → SSE) ----------------------
#
# The slow path was: full LLM completion → full ElevenLabs synthesis → forward
# full PCM to browser → browser chunks and feeds avatar. ~3-5s perceived gap.
#
# Streaming path: as soon as a complete sentence is generated by the LLM, we
# call ElevenLabs streaming TTS for that sentence and forward the PCM bytes
# to the browser via Server-Sent Events. The browser feeds chunks to the
# avatar as they arrive, so the avatar starts speaking the first sentence
# while the LLM is still generating the second. ~1-1.5s perceived gap.

# Common abbreviations that end with a period but DON'T end a sentence.
_SENTENCE_ABBREVIATIONS = {
    "Mr", "Mrs", "Ms", "Dr", "St", "Jr", "Sr",
    "Inc", "Co", "Corp", "Ltd",
    "etc", "vs", "approx",
    "e.g", "i.e", "U.S", "U.K", "U.S.A",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec",
}

_SENTENCE_RE = re.compile(r"([.!?])(\s+|$)")


def _extract_complete_sentence(text: str) -> tuple[str, str]:
    """Return (sentence, remainder). If no complete sentence, returns ('', text).

    Handles common abbreviations so 'Mr. Smith' doesn't end a sentence after 'Mr.'
    Also avoids splitting on a period that immediately follows a digit (defensive
    against numeric content like '$1,000.50' — though the system prompt asks the
    LLM to write spoken-form numbers, this catches edge cases).
    """
    for match in _SENTENCE_RE.finditer(text):
        end_idx = match.start()
        # Look backward for the word ending here
        word_match = re.search(r"(\w+)$", text[:end_idx])
        if word_match and word_match.group(1) in _SENTENCE_ABBREVIATIONS:
            continue
        # Skip period after a digit (e.g. "$1,000.")
        if end_idx > 0 and text[end_idx - 1].isdigit() and text[end_idx] == ".":
            continue
        return text[: end_idx + 1].strip(), text[match.end():]
    return "", text


def _stream_tts_bytes(text: str):
    """Generator yielding PCM bytes from ElevenLabs Flash for a single sentence."""
    if not ELEVENLABS_API_KEY or not text.strip():
        return
    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    audio_gen = client.text_to_speech.convert(
        voice_id=ELEVENLABS_VOICE_ID,
        text=text,
        model_id="eleven_flash_v2_5",
        output_format="pcm_24000",
        voice_settings=VOICE_SETTINGS,
    )
    for chunk in audio_gen:
        if chunk:
            yield chunk


def _calculator_response_template(result: dict) -> str:
    """Generate a natural-language response from a calculator result. Used
    instead of a second LLM call to save ~1-2s per calculator turn. Phrasing
    follows the system prompt's pronunciation rules (spoken-form numbers,
    'percent' instead of '%')."""
    monthly = result["monthly_income"]
    annual = result["annual_income"]
    rate = result["payout_rate_pct"]
    age = result["age"]
    gender = result["gender"]
    premium = result["premium"]
    # Short label for spoken use
    label = result["payout_type_label"].split(" — ")[0].split(" (")[0]
    # Words for the premium amount
    if premium >= 1_000_000:
        premium_word = f"{premium / 1_000_000:.1f} million dollars".replace(".0 ", " ")
    elif premium >= 1000:
        premium_word = f"{int(premium / 1000)} thousand dollars"
    else:
        premium_word = f"{int(premium)} dollars"

    parts = [
        f"With {premium_word} at age {age}, {label.lower()}, "
        f"you'd be looking at about {int(monthly):,} dollars a month — "
        f"that's {int(annual):,} a year, a {rate:.1f} percent rate."
    ]
    if result.get("guaranteed_years"):
        parts.append(
            f" And you'd be guaranteed payments for the first "
            f"{result['guaranteed_years']} years even if something happened to you early."
        )
    elif result.get("guaranteed_total"):
        parts.append(
            " And the cash refund means any unused premium comes back to your beneficiary at death."
        )
    else:
        parts.append(
            f" Over your statistical life expectancy of "
            f"{int(result['life_expectancy_years'])} years, that totals around "
            f"{int(result['estimated_lifetime_income']):,} dollars."
        )
    return "".join(parts)


def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Events frame."""
    return f"data: {_json.dumps(data)}\n\n"


@app.post("/api/converse-stream")
async def converse_stream(req: LlmReq, request: Request):
    """
    Streaming version of /api/llm. Returns text-event-stream events, one per line:

        data: {"type":"text","text":"Hello there."}
        data: {"type":"audio","b64":"<base64 PCM 24kHz 16-bit mono LE>"}
        data: {"type":"calculator_result","data":{...}}
        data: {"type":"done"}
        data: {"type":"error","message":"..."}

    The frontend dispatches each event type:
        text             → append to transcript pane
        audio            → buffer + chunk per LiveAvatar's 400ms-then-1s spec
                           and send to session.repeatAudio()
        calculator_result→ render the structured panel
        done             → flush any final audio chunks, close the stream
        error            → surface to user
    """
    user_id = _user_id(request)

    async def event_stream():
        try:
            history = _history.setdefault(req.session_id, [])
            context = await retrieve_context(req.user_text)
            system = SYSTEM_PROMPT + load_user_context(user_id)
            if context:
                system += (
                    "\n\nRelevant excerpts from annuity documents "
                    "(use these to ground your answer — do not cite source filenames aloud):\n\n"
                    + context
                )
            system += (
                "\n\nYou have a calculator tool available for estimating fixed-income "
                "annuity payments. Use it when the user asks 'how much income would I get' "
                "or any similar question that requires a concrete number. If you're missing "
                "any required input (premium amount, age, gender), ask the user one short "
                "natural question to get it before calling the tool."
            )

            messages: list[dict] = [{"role": "system", "content": system}]
            messages.extend(history[-MAX_TURNS:])
            messages.append({"role": "user", "content": req.user_text})

            full_reply = ""
            sentence_buffer = ""
            tool_call_buffer: dict[int, dict] = {}

            stream = await aclient.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=LLM_TOOLS,
                tool_choice="auto",
                temperature=0.6,
                stream=True,
            )

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Accumulate streaming tool-call args
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_call_buffer.setdefault(
                            idx, {"id": "", "name": "", "args": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments

                # Accumulate streaming text and emit per-sentence
                if delta.content:
                    sentence_buffer += delta.content
                    full_reply += delta.content
                    while True:
                        sentence, remainder = _extract_complete_sentence(sentence_buffer)
                        if not sentence:
                            break
                        sentence_buffer = remainder
                        yield _sse({"type": "text", "text": sentence + " "})
                        for audio_chunk in _stream_tts_bytes(sentence):
                            b64 = base64.b64encode(audio_chunk).decode("ascii")
                            yield _sse({"type": "audio", "b64": b64})

            # Flush any final partial sentence (no trailing punctuation)
            tail = sentence_buffer.strip()
            if tail:
                yield _sse({"type": "text", "text": tail})
                for audio_chunk in _stream_tts_bytes(tail):
                    b64 = base64.b64encode(audio_chunk).decode("ascii")
                    yield _sse({"type": "audio", "b64": b64})

            # Handle tool calls — calculator (renders panel + templated reply)
            # and save_client_profile (silent side effect; the avatar already
            # streamed its greeting in the same turn).
            if tool_call_buffer:
                for slot in tool_call_buffer.values():
                    if slot["name"] == "save_client_profile":
                        try:
                            args = _json.loads(slot["args"] or "{}")
                            name_arg = (args.get("name") or "").strip()
                            # Check whether USER.md already existed BEFORE the
                            # tool runs. Distinguishes first-introduction
                            # (no USER.md yet) from explicit-correction
                            # (USER.md exists, client said "actually call me X")
                            # so we can pick the right templated greeting if
                            # the LLM goes silent.
                            was_first_intro = (
                                user_id
                                and not user_id.startswith("anon-")
                                and not (USERS_DIR / user_id / "USER.md").exists()
                            )
                            result = _run_tool(
                                "save_client_profile", args, user_id=user_id
                            )
                            log.info("save_client_profile → %s", result)
                        except Exception:
                            log.exception("save_client_profile failed")
                            result = {"saved": False}
                            name_arg = ""
                            was_first_intro = False

                        # Second LLM pass — ensures the user's full first
                        # message is addressed, not just the name capture.
                        #
                        # Problem: the first LLM pass often streams only a
                        # greeting ("Nice to meet you, Bill!") then calls
                        # save_client_profile, leaving the user's actual
                        # question or topic unaddressed. The second pass
                        # sees the complete exchange (first greeting + tool
                        # result) and continues naturally, picking up whatever
                        # the user said that wasn't answered yet.
                        if result.get("saved") and was_first_intro:
                            # Reload context — USER.md now exists with the name.
                            updated_system = SYSTEM_PROMPT + load_user_context(user_id)
                            if context:
                                updated_system += (
                                    "\n\nRelevant excerpts from annuity documents "
                                    "(use these to ground your answer — do not cite "
                                    "source filenames aloud):\n\n" + context
                                )
                            second_messages: list[dict] = [
                                {"role": "system", "content": updated_system}
                            ]
                            second_messages.extend(history[-MAX_TURNS:])
                            second_messages.append(
                                {"role": "user", "content": req.user_text}
                            )
                            # Feed back the first pass so the LLM doesn't
                            # repeat the greeting — it continues from here.
                            second_messages.append({
                                "role": "assistant",
                                "content": full_reply or "",
                                "tool_calls": [{
                                    "id": slot["id"],
                                    "type": "function",
                                    "function": {
                                        "name": "save_client_profile",
                                        "arguments": slot["args"],
                                    },
                                }],
                            })
                            second_messages.append({
                                "role": "tool",
                                "tool_call_id": slot["id"],
                                "content": _json.dumps(result),
                            })
                            try:
                                stream2 = await aclient.chat.completions.create(
                                    model=LLM_MODEL,
                                    messages=second_messages,
                                    temperature=0.6,
                                    stream=True,
                                )
                                s2_buf = ""
                                async for chunk2 in stream2:
                                    if not chunk2.choices:
                                        continue
                                    delta2 = chunk2.choices[0].delta
                                    if delta2.content:
                                        s2_buf += delta2.content
                                        full_reply += delta2.content
                                        while True:
                                            sentence, remainder = _extract_complete_sentence(s2_buf)
                                            if not sentence:
                                                break
                                            s2_buf = remainder
                                            yield _sse({"type": "text", "text": sentence + " "})
                                            for audio_chunk in _stream_tts_bytes(sentence):
                                                b64 = base64.b64encode(audio_chunk).decode("ascii")
                                                yield _sse({"type": "audio", "b64": b64})
                                tail2 = s2_buf.strip()
                                if tail2:
                                    full_reply += tail2
                                    yield _sse({"type": "text", "text": tail2})
                                    for audio_chunk in _stream_tts_bytes(tail2):
                                        b64 = base64.b64encode(audio_chunk).decode("ascii")
                                        yield _sse({"type": "audio", "b64": b64})
                            except Exception:
                                log.exception(
                                    "Second LLM pass after save_client_profile "
                                    "failed for %s — falling back to backstop",
                                    user_id,
                                )

                        # Backstop: only fires if BOTH the first pass AND the
                        # second pass produced no spoken text at all (e.g. the
                        # model went fully silent or the second call failed).
                        if (
                            result.get("saved")
                            and not full_reply.strip()
                            and name_arg
                        ):
                            if was_first_intro:
                                spoken = (
                                    f"Got it, {name_arg} — welcome. "
                                    f"What's on your mind today?"
                                )
                            else:
                                spoken = f"Got it — I'll call you {name_arg} from here."
                            log.info(
                                "save_client_profile backstop fired (%d chars)",
                                len(spoken),
                            )
                            full_reply = spoken
                            buffer = spoken
                            while True:
                                sentence, remainder = _extract_complete_sentence(buffer)
                                if not sentence:
                                    break
                                buffer = remainder
                                yield _sse({"type": "text", "text": sentence + " "})
                                for audio_chunk in _stream_tts_bytes(sentence):
                                    b64 = base64.b64encode(audio_chunk).decode("ascii")
                                    yield _sse({"type": "audio", "b64": b64})
                            if buffer.strip():
                                yield _sse({"type": "text", "text": buffer})
                                for audio_chunk in _stream_tts_bytes(buffer):
                                    b64 = base64.b64encode(audio_chunk).decode("ascii")
                                    yield _sse({"type": "audio", "b64": b64})
                        continue

                    if slot["name"] != "calculate_spia":
                        continue
                    try:
                        args = _json.loads(slot["args"] or "{}")
                        result = _run_tool("calculate_spia", args, user_id=user_id)
                    except Exception as e:
                        log.exception("tool exec failed")
                        result = {"error": str(e)}

                    if "error" in result:
                        # Stream a brief error message
                        msg = "Hmm, I couldn't run that calculation. Could you give me the premium amount, your age, and gender once more?"
                        yield _sse({"type": "text", "text": msg})
                        for audio_chunk in _stream_tts_bytes(msg):
                            b64 = base64.b64encode(audio_chunk).decode("ascii")
                            yield _sse({"type": "audio", "b64": b64})
                        full_reply = msg
                    else:
                        # Render the panel for the user
                        yield _sse({"type": "calculator_result", "data": result})
                        # Use the templated response (no second LLM call)
                        spoken = _calculator_response_template(result)
                        full_reply = spoken
                        # Stream sentence-by-sentence through TTS
                        buffer = spoken
                        while True:
                            sentence, remainder = _extract_complete_sentence(buffer)
                            if not sentence:
                                break
                            buffer = remainder
                            yield _sse({"type": "text", "text": sentence + " "})
                            for audio_chunk in _stream_tts_bytes(sentence):
                                b64 = base64.b64encode(audio_chunk).decode("ascii")
                                yield _sse({"type": "audio", "b64": b64})
                        # Final tail
                        if buffer.strip():
                            yield _sse({"type": "text", "text": buffer})
                            for audio_chunk in _stream_tts_bytes(buffer):
                                b64 = base64.b64encode(audio_chunk).decode("ascii")
                                yield _sse({"type": "audio", "b64": b64})

            if full_reply.strip():
                history.append({"role": "user", "content": req.user_text})
                history.append({"role": "assistant", "content": full_reply.strip()})

            yield _sse({"type": "done"})
        except Exception as e:
            log.exception("converse-stream error")
            yield _sse({"type": "error", "message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )


@app.post("/api/deepgram-token")
async def deepgram_token():
    """Mint a short-lived JWT for browser-side Deepgram streaming.

    The browser opens `wss://api.deepgram.com/v1/listen?...` directly with
    this token (passed via the `Sec-WebSocket-Protocol: token, <jwt>`
    subprotocol header). One fewer network hop than proxying audio
    through this backend, and Deepgram's WebSocket sits in the same
    us-east region as most of our other API dependencies.

    Returns 503 when DEEPGRAM_API_KEY isn't configured so the frontend
    can transparently fall back to the Whisper buffered path. Returns
    502 if Deepgram itself rejects the grant request (rare; usually
    means a billing/credentials issue worth surfacing).
    """
    if not DEEPGRAM_API_KEY:
        raise HTTPException(503, "Deepgram not configured (DEEPGRAM_API_KEY not set)")
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(
            "https://api.deepgram.com/v1/auth/grant",
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            json={"ttl_seconds": DEEPGRAM_TOKEN_TTL_SECONDS},
        )
    if res.status_code >= 300:
        log.error("Deepgram /v1/auth/grant failed (%s): %s", res.status_code, res.text)
        raise HTTPException(502, f"Deepgram auth failed: {res.text}")
    data = res.json()
    return {
        "token": data.get("access_token"),
        "expires_in": data.get("expires_in", DEEPGRAM_TOKEN_TTL_SECONDS),
    }


@app.post("/api/transcribe")
async def transcribe(request: Request):
    """
    Transcribe a short audio clip using OpenAI Whisper.

    Accepts a raw audio body (Content-Type: audio/webm, audio/mp4, etc.).
    The frontend sends the MediaRecorder blob directly as the request body —
    no multipart encoding, no python-multipart dependency needed.
    Returns { "text": "..." }.

    Used by the push-to-talk button in index.html — replaces the browser's
    Web Speech API which only works on HTTPS/localhost and depends on Google.
    Whisper works on any URL (HTTP LAN, ngrok, etc.) and any modern browser.
    """
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio body")

    # Whisper needs a filename with a recognisable extension so it picks
    # the right decoder. Sniff from Content-Type header; default to webm
    # (Chrome's MediaRecorder default).
    content_type = (request.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    ext_map = {
        "audio/webm": "webm",
        "audio/ogg":  "ogg",
        "audio/mp4":  "mp4",
        "audio/mpeg": "mp3",
        "audio/wav":  "wav",
        "audio/x-wav":"wav",
    }
    ext = ext_map.get(content_type, "webm")
    filename = f"recording.{ext}"

    try:
        import io
        transcription = await aclient.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, io.BytesIO(audio_bytes), content_type),
        )
        text = (transcription.text or "").strip()
        log.info("Whisper: %d bytes → %r", len(audio_bytes), text[:80])
        return {"text": text}
    except Exception as e:
        log.exception("Whisper transcription failed")
        raise HTTPException(500, f"Transcription failed: {e}")


@app.post("/api/calculate_spia")
async def calculate_spia_endpoint(req: SpiaReq):
    """
    Estimate Single Premium Immediate Annuity (SPIA) income.

    Returns the structured result from scripts.spia_calculator.calculate_spia.
    The calculator uses industry-average payout rates as of 2025–2026; the
    output is illustrative only and not a binding quote.
    """
    # Lazy import so backend startup doesn't depend on the calculator file
    # (and so the script stays self-contained for CLI use).
    import sys as _sys
    _scripts_dir = os.path.join(_HERE, "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    from spia_calculator import calculate_spia  # type: ignore

    try:
        result = calculate_spia(
            premium=req.premium,
            age=req.age,
            gender=req.gender,  # type: ignore[arg-type]
            payout_type=req.payout_type,  # type: ignore[arg-type]
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("SPIA: $%s @ age %s %s, %s → $%s/mo",
             req.premium, req.age, req.gender, req.payout_type,
             result["monthly_income"])
    return result


@app.post("/api/forget")
async def forget(payload: dict, request: Request):
    """Save a digest of the conversation, then clear the in-memory transcript.

    The digest call is fire-and-forget via asyncio.create_task so the tab-close
    on the client isn't blocked. If uvicorn shuts down before the task
    completes the digest is simply not written — no half-files because
    Path.write_text is atomic at the OS level.
    """
    sid = payload.get("session_id")
    if sid:
        turns = list(_history.get(sid, []))  # copy so the pop doesn't race the task
        user_id = _user_id(request)
        if turns:
            asyncio.create_task(save_session_digest(user_id, turns))
        _history.pop(sid, None)
    return {"ok": True}


# Serve the static frontend from the same origin.
@app.get("/")
async def index(request: Request):
    """Serve index.html and mint a session-scoped user_id cookie on first visit.

    The cookie is the stable identity for the per-user memory layer
    (see MEMORY_SUBSYSTEM.md). httponly=False so frontend JS can later
    read it for a 'this is me' / 'view your profile' UX. samesite=lax
    so an external referral doesn't strip it on the redirect.

    No max_age → session cookie: it dies when the browser tab/window closes,
    which is the right default for a demo where each visitor is a distinct
    person. Returning users who close and reopen the browser will get a new
    UUID (and thus a fresh-visitor experience). If persistent identity across
    browser restarts is needed later, restore max_age=USER_COOKIE_MAX_AGE."""
    response = FileResponse(os.path.join(_HERE, "index.html"))
    if not request.cookies.get(USER_COOKIE_NAME):
        response.set_cookie(
            USER_COOKIE_NAME,
            str(uuid.uuid4()),
            # No max_age → session cookie (cleared when browser closes)
            httponly=False,
            samesite="lax",
        )
    return response


@app.get("/new")
async def new_visitor(request: Request):
    """Force a fresh visitor identity by minting a new UUID cookie and
    redirecting to the app.

    Use this to simulate a new visitor without closing the browser:
        http://localhost:8000/new

    This is especially useful during demos or testing when Chrome's shared
    incognito cookie store would otherwise carry over a previous visitor's
    identity into a 'new' incognito window."""
    from fastapi.responses import RedirectResponse
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        USER_COOKIE_NAME,
        str(uuid.uuid4()),
        # Session cookie — no max_age
        httponly=False,
        samesite="lax",
    )
    log.info("New visitor forced via /new — fresh UUID minted")
    return response
