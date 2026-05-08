# Session Notes — LiveAvatar Advisor

**Last updated:** 2026-05-05 by Tom + Claude (Cowork on Mac 1)

This is a working summary of where the project stands. Read this if you're picking up the codebase on a new machine, in a new Claude Code session, or after time away. For deeper architectural notes, see `HANDOFF.md`.

## What this project is

A real-time conversational avatar of **Tom Olds** acting as a financial advisor specializing in retirement planning and annuities. The user (in a browser) asks a question; the avatar speaks back in Tom's voice, with answers grounded in a RAG corpus of Stan-the-Annuity-Man content.

## Stack (one screen)

```
Browser ─── push-to-talk (Web Speech API STT) ──→ /api/llm ──→ GPT-5.4
                                                       │
                                                       ↓
                                                   ChromaDB RAG retrieval
                                                   (annuity_docs corpus,
                                                    text-embedding-3-small)
                                                       │
                                                       ↓
                                                   reply text
                                                       │
                          ┌────────────────────────────┘
                          ↓
                   /api/tts → ElevenLabs (PCM 24kHz)
                          │
                          ↓
       browser chunks PCM (400ms then 1s) → session.repeatAudio()
                          │
                          ↓
       LiveAvatar (HeyGen's streaming product) renders Tom's
       trained custom avatar lip-syncing to ElevenLabs audio,
       streamed via LiveKit room into browser <video> element
```

## Current operational state

**Backend**: FastAPI in `advisor_backend.py`. Endpoints:
- `GET /` — serves `index.html`
- `GET /health` — diagnostic JSON (which keys loaded, which avatar id active, etc.)
- `POST /api/token` — mints LiveAvatar session token via `api.liveavatar.com/v1/sessions/token`
- `POST /api/llm` — RAG retrieval over ChromaDB + GPT-5.4 with the advisor system prompt + per-session memory
- `POST /api/tts` — ElevenLabs PCM 24kHz synthesis of arbitrary text
- `POST /api/forget` — clears in-memory transcript for a session_id

**Frontend**: `index.html` uses `@heygen/liveavatar-web-sdk@0.0.17` from esm.sh. Push-to-talk via Web Speech API, transcript pane shows turns, the `speakViaElevenLabs` helper chunks PCM and feeds `session.repeatAudio()`.

**Trained custom avatars on file** (in LiveAvatar account `olds.tom@gmail.com`, Starter plan + $49/mo Custom Avatar add-on):
- `c3ca3752-1804-4005-9b66-80398f1a8c3e` — **couch** variant (Tom on couch, navy blazer, window light, original training video). Created 2026-05-02.
- `e5953f75-f0b8-45b5-9bbc-b8b8c591d029` — **books** variant (same Tom, composited onto a bookshelf background via MediaPipe Tasks API segmentation). Created 2026-05-05. **Currently active in `.env`.**

**Voice**: ElevenLabs voice ID `8gfvBkrqr64Si4V5Q321` (Tom's clone, originally trained for the AudiblyLegibleInsights project, reused here).

**LLM**: GPT-5.4 (`gpt-5.4`), system prompt focused on annuity advisor persona — explicitly told NOT to refer the user to "another professional" (Tom IS the professional). Model is configurable via `LLM_MODEL` env var.

**RAG**: ChromaDB at `chroma_db/`, collection `annuity_docs`, embedded with `text-embedding-3-small`. Source corpus is in `annuity_docs/` (Stan the Annuity Man transcripts and related). Ingested via `scripts/ingest-docs.js` (Node) or `scripts/ingest.py` (Python).

**Auth and credentials** (`.env`, never committed):
- `OPENAI_API_KEY` — for GPT-5.4 + embeddings
- `LIVEAVATAR_API_KEY` — UUID-format key from `app.liveavatar.com/developers`. Distinct from the HeyGen API key (separate products despite shared branding).
- `HEYGEN_API_KEY` — `sk_V2_...` format key from app.heygen.com. Kept for completeness; not used by the running app.
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`
- `HEYGEN_AVATAR_ID` — currently the books avatar's UUID (env var name kept as-is for backward compat; the value is a LiveAvatar id, not a HeyGen avatar id).

## Public sharing

Demo runs on `localhost:8000` and is shared via **ngrok** (`ngrok http 8000`) for small-group testing. Free-tier ngrok URLs change on restart — paste the current `Forwarding:` URL when sharing. Mac must stay awake (`caffeinate -dimsu &`).

## Git state

GitHub repo: `https://github.com/toNyc2017/liveavatar-advisor` (private). Tagged milestones:
- `v0.1-working-advisor` — first working end-to-end (couch avatar, RAG-grounded)
- `v0.2-bookshelf-avatar` — adds bg replacement pipeline + books variant + ngrok demo

## Files NOT in the repo (transfer separately when moving to a new machine)

- `.env` — secrets. AirDrop or paste contents manually.
- `chroma_db/` — embedded corpus, ~77 MB. AirDrop the folder.
- `annuity_docs/` — source documents. Optional unless you want to re-ingest.
- `*.MOV`, `*.mp4` (training videos and intermediate composites). Optional unless you're going to retrain an avatar.

## Pending / next horizons (none blocking the demo)

- **Verify RAG quality**: ask Stan-specific questions ("What does Stan think about MYGAs?") and confirm answers reflect the corpus. Currently grounded but not formally validated.
- **System prompt tuning**: persona / compliance language. For production-customer-facing use, regulatory-CYA language should probably go back in (separate prompt for demo vs production).
- **Production hosting**: NYM project pattern (`~/Documents/nym/`) provides the static-site half via S3+CloudFront+GitHub Actions. Backend is the still-open question — Cloud Run or AWS App Runner for a real hosted demo. ngrok is fine for ad hoc small-group testing only.
- **Stable URL**: ngrok free changes per restart. Cloudflare Tunnel (free, with custom domain) is the upgrade path.
- **Auth gating**: NYM has a working Auth0 setup that could gate the avatar URL so only invited testers can hit it.
- **Second avatar variant (books)**: trained but not yet stress-tested in conversation. Tom may delete the couch one if books works well, to free the single Custom Avatar slot.
- **Background replacement script (`bg_replace.py`)**: works on this Mac with mediapipe>=0.10.30 and the new Tasks API. Auto-downloads `selfie_segmenter.tflite` to `~/.cache/mediapipe/` on first run.

## Lessons learned (gotchas worth not re-discovering)

1. **LiveAvatar ≠ HeyGen at the avatar-catalog level**, even though they share branding and the same API key. Avatars trained via HeyGen's Avatar V flow are NOT visible to LiveAvatar's streaming API. Always train/upload custom avatars through `app.liveavatar.com`, not `app.heygen.com`.
2. **The previous `.env` had the LiveAvatar API key incorrectly labeled as `HEYGEN_API_KEY`.** Days were lost to that mislabeling. Now both keys are present and correctly named; `LIVEAVATAR_API_KEY` is what the backend reads.
3. **LiveAvatar LITE mode does NOT include HeyGen TTS.** `session.repeat(text)` returns "not allowed in LITE mode". You MUST provide audio yourself via `session.repeatAudio(b64Pcm)` with PCM 24kHz 16-bit mono, base64-encoded, chunked at 400ms first / 1s subsequent. This is why we have the `/api/tts` → frontend chunk → `session.repeatAudio` flow.
4. **iPhone HDR/HEVC is not what avatar pipelines want.** Recordings need conversion to SDR rec.709 H.264. `scale=in_color_matrix=bt2020nc:in_range=tv:out_color_matrix=bt709:out_range=tv,format=yuv420p` is the working ffmpeg recipe; zscale-based tone mapping over-corrected for our HLG content and produced yellow casts.
5. **iPhone landscape rotation metadata is fine when ffmpeg auto-rotates** but breaks if you naively add `hflip,vflip` filters. Don't add explicit rotation unless you've confirmed the input wasn't auto-rotated.
6. **MediaPipe 0.10.30+ on Python 3.13/3.14 dropped the legacy `mp.solutions` API** in favor of the Tasks API. The current `bg_replace.py` uses the Tasks API. Don't try to pin to 0.10.18 on Python 3.14 — wheels don't exist.
7. **OpenCV's `VideoWriter_fourcc(*"mp4v")` writes MPEG-4 ASP, not H.264.** Output from `bg_replace.py` needs an ffmpeg post-pass to re-encode as H.264 before LiveAvatar will accept it.
8. **LiveAvatar custom avatars require structured training video**: 15s listening + ~2 min content + 15s idle. First take without this structure gets rejected.
9. **Custom avatar streaming is gated by plan tier.** $19/mo Starter + $49/mo Custom Avatar add-on supports a single 720p custom avatar. 1080p requires Enterprise. Plan accordingly.

## Quick-reference commands

**Start the app:**
```
cd ~/Documents/liveavatar-advisor && bash run.sh
```

**Public-share via ngrok (separate terminal):**
```
ngrok http 8000
```

**Health-check:**
```
curl -s http://localhost:8000/health
```

**Swap which custom avatar is active** (edit `.env`, comment one HEYGEN_AVATAR_ID line, uncomment the other, then Ctrl+C uvicorn and `bash run.sh`).

**Generate a new background composite** (requires mediapipe + opencv + numpy installed):
```
source venv/bin/activate
pip install "mediapipe>=0.10.30,<0.11" opencv-python-headless numpy   # one-time
python bg_replace.py SOURCE.mp4 OUTPUT.mp4 BACKGROUND.jpg
ffmpeg -i OUTPUT.mp4 -vcodec libx264 -preset slow -crf 18 -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv -acodec copy -movflags +faststart OUTPUT_h264.mp4
```

**Re-ingest annuity_docs into chroma_db:**
```
source venv/bin/activate
python scripts/ingest.py
# or
node scripts/ingest-docs.js
```
