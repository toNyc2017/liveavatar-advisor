# Annuity Advisor — LiveAvatar

Back-and-forth voice conversation with a HeyGen **LiveAvatar** acting as an
annuity advisor. LITE mode, push-to-talk via the browser's Web Speech API,
GPT-5.4 for the brain, HeyGen for avatar video + voice + lip-sync.

> **If you're a Claude Code session picking this up, read [`HANDOFF.md`](./HANDOFF.md) first** — it has the full LITE-mode protocol, prior wrong turns, and a verification checklist.

## Architecture

```
Browser ── /api/token  ──> backend ──> POST api.liveavatar.com/v1/sessions/token
       <─ session_token ──┘                                       (avatar_id, mode=LITE)
Browser ── new LiveAvatarSession(token).start()  (official @heygen/liveavatar-web-sdk)
       <── LiveKit room (avatar video + audio) ──
Browser ── /api/llm ──> backend asks GPT-5.4 ──> reply text
Browser ── session.repeat(reply) ──> avatar speaks in HeyGen voice
```

No ElevenLabs, no audio plumbing on our end. The SDK handles all the
LiveKit / WebSocket / command-event details.

## Setup (one-time)

1. Make sure your HeyGen plan includes **LiveAvatar** (Interactive Avatar was sunset 2026-03-31; LiveAvatar is the replacement).
2. Fill in `.env`:
   ```bash
   cp .env.template .env
   # edit OPENAI_API_KEY, HEYGEN_API_KEY, HEYGEN_AVATAR_ID
   ```
3. List avatars to grab a `HEYGEN_AVATAR_ID`:
   ```bash
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   python list_avatars.py
   ```

## Run

```bash
./run.sh    # creates venv, installs deps, starts uvicorn on :8000
```

Open <http://localhost:8000> in **Chrome**. Click *Start session*. Then either:
- Hold the 🎤 button (or <kbd>Space</kbd>) to talk, release to send.
- Or type and press Enter.

## Files

| file | what it does |
|------|--------------|
| `advisor_backend.py` | FastAPI: `/api/token`, `/api/llm`, `/api/forget`, `/health`, `/`. Minimal — the SDK handles the heavy lifting client-side. |
| `index.html`         | Single-page frontend. Uses `@heygen/liveavatar-web-sdk` from esm.sh. Push-to-talk + transcript pane. |
| `list_avatars.py`    | List streaming-eligible avatars on your HeyGen account. |
| `run.sh`             | venv + uvicorn launcher. |
| `HANDOFF.md`         | **Required reading** for any agent picking up this project. Full protocol notes. |
| `.env.template`      | Required + optional env vars. |

## Common errors

- **`localhost:8000` returns `{"detail":"Not Found"}`** — uvicorn is up but didn't load the right `advisor_backend.py`. Likely an old version from `~/.openclaw/workspace/skills/liveavatar-advisor/` that has no `/` route. Check `lsof -nP -iTCP:8000` to see the pid, `ps -p <pid> -o command=` to see what file it's running.
- **`/api/token` returns 401/403** — `HEYGEN_API_KEY` is wrong, or your plan doesn't include LiveAvatar.
- **`/api/token` returns "avatar not available"** — `HEYGEN_AVATAR_ID` isn't on your plan. Run `list_avatars.py`.
- **Mic button greyed out** — not Chrome, or page not served from `http://localhost` or `https://`. Web Speech API requires a secure context.
- **Avatar appears but doesn't speak** — open DevTools, watch the WebSocket frames; you should see `avatar.speak_text` going out and `avatar.transcription.chunk` coming back.

## Production notes

For the AWS-serverless target: backend is two stateless endpoints (`/api/token`, `/api/llm`), trivial to deploy as Lambda + API Gateway. Transcript memory needs to move out of process — DynamoDB keyed by `session_id` is the obvious choice. The LiveKit room is HeyGen's infra, so no socket to keep open server-side.
