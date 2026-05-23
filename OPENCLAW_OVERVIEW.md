# Project Overview — for openclaw

**Project root:** `/Users/olds/Documents/liveavatar-advisor`
**Last meaningful revision of this doc:** 2026-05-17

Tom hands you this file when he wants you to help on this project. Read
to orient, then act. When you need more depth on any topic, follow the
pointers at the bottom — each linked doc is canonical for its area.

---

## What this project is

A photorealistic, voice-driven advisor avatar (Tom Olds' face and voice,
synthesized) that holds back-and-forth conversations with prospective
clients about retirement income and annuities. It's not a chatbot —
it's a video-and-voice experience that approximates sitting across from
a human advisor.

The technology stack underneath:

```
Browser  ──── push-to-talk + transcript UI (index.html)
   │
   │ POST /api/converse-stream
   ▼
FastAPI backend (advisor_backend.py)
   ├── Retrieval-augmented generation over 4,500+ Stan The Annuity Man
   │   transcript chunks (ChromaDB at chroma_db/)
   ├── GPT-5.4 with function-calling tools:
   │       - calculate_spia        (annuity income estimator)
   │       - save_client_profile   (writes USER.md on first introduction)
   ├── Streaming pipeline: LLM tokens → sentences → ElevenLabs Flash TTS
   │   → PCM bytes → SSE → browser → HeyGen LiveAvatar (LITE mode)
   ├── Per-user memory subsystem (markdown files under users/)
   └── Access logging (logs/access.jsonl, IP-aware, rotating)
```

Voice = ElevenLabs cloned "Yorkville2" voice. Avatar = a custom
LiveAvatar trained on Tom's own face. RAG corpus = ~580 documents,
mostly Stan The Annuity Man podcast/blog content, embedded with OpenAI
`text-embedding-3-small`.

The product positioning — captured in `SOUL.md` — is that the avatar is
a **future retirement guide, not a product explainer**. Discovery
before product. Always frame in contrast (without/with guaranteed
income). Weave in the five retirement risks (longevity, market,
sequence-of-returns, inflation, long-term care) as conversational hooks,
not academic citations.

---

## File map at the project root

| File / dir | What it is | Touch? |
|---|---|---|
| `advisor_backend.py` | FastAPI server. The brain — routes, system prompt, tool dispatch, RAG, streaming, memory hooks, access logging. | Carefully. |
| `index.html` | Single-page frontend. Push-to-talk, transcript pane, calculator panel + downloads, LiveAvatar SDK wiring. | Carefully. |
| `run.sh` | One-shot launcher. Creates venv if missing, installs deps, starts uvicorn with `--proxy-headers`. | Yes when needed. |
| `requirements.txt` | Python dependencies. | When adding a package. |
| `bg_replace.py` | Standalone MediaPipe utility for background replacement on avatar source videos. | Rarely. |
| `list_avatars.py` | CLI helper to enumerate LiveAvatar avatars and pick an `HEYGEN_AVATAR_ID`. | Rarely. |
| `.env` | API keys (gitignored). OPENAI_API_KEY, LIVEAVATAR_API_KEY, HEYGEN_AVATAR_ID, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID. | Never commit. |
| `scripts/` | Operational scripts — see below. | Yes. |
| `skills/transcript-to-deliverables/` | Self-contained skill: YouTube URL → transcript + analysis JSON + email + PPTX deck. | Yes. |
| `chroma_db/` | The RAG vector store (gitignored). Rebuild via `scripts/ingest.py`. | Don't commit. |
| `annuity_docs/` | Source transcripts for the RAG corpus (gitignored — licensing + size). | Don't commit. |
| `users/` | Per-client memory folders. **Never commit — contains personal info.** | Read carefully. |
| `logs/` | Access-log JSONL (rotating, IP addresses, gitignored). | Read for audit. |
| `natebjones/` / `moonshots/` | Output folders from the transcript-to-deliverables skill (gitignored). | Read. |
| `venv/` | Python virtual environment (gitignored). Use `venv/bin/python` for scripts. | Don't commit. |

### Scripts (`scripts/`)

| Script | Purpose |
|---|---|
| `fetch_youtube_episodes.py` | Pulls YouTube auto-captions (free) or falls back to Whisper. Saves to `<dir>/<date>_<slug>.txt`. |
| `spia_calculator.py` | Pure-Python SPIA income estimator. Called by the LLM `calculate_spia` tool and `/api/calculate_spia`. |
| `ingest.py` | Rebuilds the ChromaDB collection from `annuity_docs/`. Run after adding new transcripts. |
| `compile_memory.py` | CLI per-user memory compiler. Reads `users/<id>/memory/*.md`, writes `users/<id>/MEMORY.md`. Mostly redundant now (auto-runs after each session) but useful for batch recompiles + dry-runs. |

### Skills (`skills/transcript-to-deliverables/`)

A self-contained sub-project. **Always invoke through `from_url.py`** for
the one-shot URL → all-deliverables flow:

```bash
venv/bin/python skills/transcript-to-deliverables/from_url.py 'https://www.youtube.com/watch?v=...'
```

The wrapper probes yt-dlp, derives a folder slug from the channel name,
runs `fetch_youtube_episodes.py`, then `build.py` (which orchestrates
analysis → email → slides). All outputs land in a single folder named
after the channel.

Internal files:
- `SKILL.md` — usage docs + design rationale
- `from_url.py` — one-shot URL entry point
- `build.py` — orchestrator (transcript → analysis JSON → email → PPTX)
- `prompts.py` — analysis + email LLM prompts (where the voice rules and
  visual-kind selection logic live)
- `render_pptx.py` — python-pptx renderer + four shape-based visual
  drawers (process_flow / comparison / triangle / stack)

---

## How to run things

### Start the avatar backend

```bash
cd ~/Documents/liveavatar-advisor
bash run.sh
```

Server listens on `http://localhost:8000`. Frontend opens automatically
at the same URL. Use **incognito** for clean cookie state when testing.

If port 8000 is held by a previous instance:

```bash
lsof -ti :8000 | xargs kill -9 2>/dev/null; bash run.sh
```

### Process a YouTube video through the skill

One command:

```bash
venv/bin/python skills/transcript-to-deliverables/from_url.py 'URL'
```

Optional flags: `--folder NAME` overrides the auto-derived folder.
`--audience executive|board|client|internal` shifts tone.
`--num-slides N` changes deck length. Default audience is `executive`,
default slides is 5.

### Inspect access logs

```bash
# Live tail in human-readable form
tail -f logs/access.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    print(f\"{e['ts']}  {e['ip']:18s}  {e['method']:4s} {e['path']:30s}  → {e['status']}  ({e['elapsed_ms']}ms)  uid={e['user_id'][:8]}\")"

# All requests from a specific IP
jq -c 'select(.ip == "X.X.X.X")' logs/access.jsonl

# Per-endpoint hit counts
jq -r '.path' logs/access.jsonl | sort | uniq -c | sort -rn
```

### Recompile a user's MEMORY.md manually (rarely needed)

The compile auto-runs after each session-end. But for batch recompiles
across many users (e.g., after a prompt tweak), or to dry-run:

```bash
venv/bin/python scripts/compile_memory.py --dry-run
venv/bin/python scripts/compile_memory.py --user <uuid>
venv/bin/python scripts/compile_memory.py --force        # all users, force
```

### Re-ingest the RAG corpus

After adding new transcripts to `annuity_docs/`:

```bash
venv/bin/python scripts/ingest.py
# then restart uvicorn so the collection reloads
```

---

## What NOT to touch without checking with Tom first

- **`.env`** — API keys live here. Mishandling can leak credentials and
  rack up bills.
- **`chroma_db/`** — deleting it destroys the embedded RAG corpus.
  Rebuildable via `scripts/ingest.py` if needed, costs OpenAI embedding
  tokens.
- **`users/<uuid>/`** — client memory. Reading is fine. Writing /
  deleting requires explicit go-ahead because it contains personal info
  and is the canonical record of past conversations.
- **`SYSTEM_PROMPT` in `advisor_backend.py`** — the avatar's persona +
  approach. Changes here directly shape every conversation. The intent
  layer is `SOUL.md`; SYSTEM_PROMPT is the implementation. When updating
  one, update the other to match.
- **The streaming-endpoint logic in `/api/converse-stream`** — sentence
  detection, TTS chunking, tool-call buffer, history append. Fragile
  by nature of being real-time. Read `MEMORY_BACKEND_PROPOSAL.md` and
  the change log in `MEMORY_SUBSYSTEM.md` before changing anything in
  this code path.
- **The LiveAvatar SDK calls in `index.html`** — `repeatAudio` PCM
  chunk sizing (first 400ms, then 1s) is required by HeyGen's protocol.
  See `HANDOFF.md` for the full LITE-mode protocol details.

---

## Current state (what's in flight)

Recent work, newest first — for the full change log see
`MEMORY_SUBSYSTEM.md`:

- **Avatar reframe to "future retirement guide"** (2026-05-17) — colleague
  feedback codified in `SOUL.md` and reflected in `SYSTEM_PROMPT`'s new
  `APPROACH` section. Discovery before product, five risks as
  conversational hooks, without/with contrast framing. Calculator is
  now the payoff of a gap-analysis conversation, not its substitute.
- **Auto-compile after digest** (2026-05-17) — `save_session_digest()`
  now chains `compile_user_memory()` so MEMORY.md is fresh by the next
  session. Was previously manual; live testing showed early-stage
  clients got no continuity.
- **Access logging middleware** (2026-05-17) — per-request JSON line to
  `logs/access.jsonl` with IP, user_id, method, path, status, ms.
- **First-visit identity bootstrap** (2026-05-16) — `save_client_profile`
  LLM tool fires when a new client introduces themselves; writes
  `users/<id>/USER.md`.
- **Per-user memory compiler** (2026-05-16) — `scripts/compile_memory.py`
  synthesizes `memory/*.md` digests into `MEMORY.md`.
- **Memory subsystem Phase 1 backend integration** (2026-05-12) —
  cookie-based identity, `load_user_context()`, `save_session_digest()`.
- **transcript-to-deliverables skill** (2026-05-15 → 2026-05-17) —
  YouTube → polished deck pipeline with shape-based visual compositions.
  Phone-callable via the `process-video` Cowork skill (see
  `OPENCLAW_SETUP.md`).

Pending: gap-analysis visualization (task #67), supervisor view across
all clients, scheduled compile cron, magic-link claim-conversation flow.

---

## Pointer table — the canonical doc for each topic

| Topic | Read |
|---|---|
| Product positioning + avatar role + design rationale | `SOUL.md` |
| Per-user memory subsystem (full design + change log) | `MEMORY_SUBSYSTEM.md` |
| Memory subsystem backend integration spec | `MEMORY_BACKEND_PROPOSAL.md` |
| Phone-trigger setup for the video pipeline | `OPENCLAW_SETUP.md` |
| LiveAvatar LITE-mode protocol details | `HANDOFF.md` |
| Strategic / lead-gen positioning briefing | `PROJECT_DESCRIPTION.md` |
| Quickstart for a human first-timer | `README.md` |
| The transcript-to-deliverables skill itself | `skills/transcript-to-deliverables/SKILL.md` |
| Session notes (working journal) | `SESSION_NOTES.md` |

---

## How to ask Tom for permission when uncertain

When you're about to do something that this overview marks as "don't
touch without checking" — or anything that mutates `users/`, `.env`,
or production-shape config — pause and ask. A one-line check-in like
"about to overwrite `users/<id>/USER.md` with X — proceed?" is much
better than rolling back a wrong write.

For everything else (reading files, running scripts, inspecting logs,
fixing typos, exploring), proceed normally.
