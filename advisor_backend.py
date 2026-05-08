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
import logging

import httpx
import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from openai import AsyncOpenAI
from elevenlabs import ElevenLabs
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
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

_HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_PATH = os.path.join(_HERE, "chroma_db")
CHROMA_COLLECTION = "annuity_docs"
RAG_K = 4

SYSTEM_PROMPT = """You are Tom Olds, a financial professional specializing in retirement planning
and annuities. You are having a one-on-one conversation with someone who came to YOU specifically
for guidance. They are not looking for a referral — they want your insight.

- Be warm, empathetic, and conversational — you're talking out loud, not writing an email.
- Avoid dense financial jargon. If you must use a term, define it in one short clause.
- You ARE this user's financial professional. For follow-up or deeper questions, invite them to
  keep asking you directly. Never refer them to "a financial advisor," "a professional," or "an
  expert" — you are who they came to.
- If a question is genuinely outside your expertise (state-specific tax law, securities trading,
  estate-planning legal documents), say what you CAN offer on the topic and ask what they'd like
  to dig into next with you.
- Keep replies short: 2–4 sentences, ~40 words. The avatar will speak this aloud.
- Don't use lists, headers, or markdown — just clean spoken prose.

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

If a user asks about a 401(k) rollover, write the term as "four oh one K rollover" in your response. The visible transcript will show "four oh one K" too, which is fine — it matches how a human advisor speaks."""

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

    body = {"avatar_id": AVATAR_ID, "mode": SESSION_MODE}
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
]


def _run_tool(name: str, args: dict) -> dict:
    """Execute a tool call by name. Returns the tool's result as a dict."""
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
    return {"error": f"Unknown tool: {name}"}


@app.post("/api/llm")
async def llm(req: LlmReq):
    """
    Run the annuity-advisor system prompt against the LLM with rolling memory.

    Supports tool calls — when the LLM decides to invoke `calculate_spia`, we
    run it server-side, feed the result back into the model, and return both:
      - "reply": the natural-language response (what the avatar will speak)
      - "calculator_result": the structured calculator output, when applicable

    The frontend uses `reply` for TTS and `calculator_result` to render a
    formatted panel + download button.
    """
    history = _history.setdefault(req.session_id, [])

    context = await retrieve_context(req.user_text)
    system = SYSTEM_PROMPT
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
            tool_output = _run_tool(tc.function.name, args)
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
    )
    audio_bytes = b"".join(audio_gen)
    log.info("TTS: %d chars → %d PCM bytes", len(req.text), len(audio_bytes))
    return Response(content=audio_bytes, media_type="application/octet-stream")


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
async def forget(payload: dict):
    """Clear the in-memory transcript when a session ends."""
    sid = payload.get("session_id")
    if sid:
        _history.pop(sid, None)
    return {"ok": True}


# Serve the static frontend from the same origin.
@app.get("/")
async def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "index.html"))
