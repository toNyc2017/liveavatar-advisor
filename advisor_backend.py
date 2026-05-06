"""
Annuity Advisor — LiveAvatar (HeyGen) backend.

Architecture (see HANDOFF.md for the full reasoning):

  Browser ── /api/token ──> backend ── POST api.liveavatar.com/v1/sessions/token ──> { session_token }
  Browser ── new LiveAvatarSession(session_token).start()  (uses @heygen/liveavatar-web-sdk)
            │
            └── frontend then either:
                a) calls session.repeat(text)   → avatar speaks in HeyGen's voice  (simplest)
                b) calls session.repeatAudio(b64_pcm_chunk) → avatar lip-syncs to OUR audio

  Browser ── /api/llm ──> backend asks GPT-4o for the reply text, returns it.
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

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")

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
- Don't use lists, headers, or markdown — just clean spoken prose."""

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


@app.post("/api/llm")
async def llm(req: LlmReq):
    """
    Run the annuity-advisor system prompt against the LLM with rolling memory.
    Returns plain text — the FRONTEND decides how to deliver it to the avatar
    (session.repeat() for HeyGen voice, or stream synthesized PCM via session.repeatAudio()).
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

    messages = [{"role": "system", "content": system}]
    messages.extend(history[-MAX_TURNS:])
    messages.append({"role": "user", "content": req.user_text})

    completion = await aclient.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.6,
    )
    reply = (completion.choices[0].message.content or "").strip()
    if not reply:
        raise HTTPException(502, "LLM returned an empty reply")

    history.append({"role": "user", "content": req.user_text})
    history.append({"role": "assistant", "content": reply})
    return {"reply": reply}


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
    audio_gen = client.text_to_speech.convert(
        voice_id=req.voice_id or ELEVENLABS_VOICE_ID,
        text=req.text,
        model_id="eleven_turbo_v2",
        output_format="pcm_24000",
    )
    audio_bytes = b"".join(audio_gen)
    log.info("TTS: %d chars → %d PCM bytes", len(req.text), len(audio_bytes))
    return Response(content=audio_bytes, media_type="application/octet-stream")


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
