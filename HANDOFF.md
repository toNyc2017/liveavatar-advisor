# LiveAvatar Advisor — Handoff Doc

> Read this first if you're picking up this project (likely a Claude Code
> session). It captures the protocol, the prior wrong turns, and what's
> next, so you don't have to re-derive any of it.
>
> **Before doing anything else: inventory what's already in this repo.**
> `.env`, the original code in `~/.openclaw/workspace/skills/liveavatar-advisor/`,
> any hardcoded constants, any TODO comments. The prior Cowork-session agent
> burned two rounds telling Tom to "set this in .env" for things that were
> already hardcoded in his original code (e.g. the avatar id). Grep first,
> recommend later.

## TL;DR

A back-and-forth voice conversation with a HeyGen **LiveAvatar** acting as
an annuity advisor. **LITE mode**: backend mints a session token; frontend
uses the official `@heygen/liveavatar-web-sdk` to attach to the LiveKit
room and send command events. Text replies come from the LLM on the backend (default GPT-5.4);
the avatar speaks them via HeyGen's built-in voice (`session.repeat(text)`).

## Repo state

- `advisor_backend.py` — minimal FastAPI: `/api/token`, `/api/llm`, `/api/forget`, `/health`, `/`. Targets the **real** `api.liveavatar.com` endpoint. Was previously rewritten against the deprecated `api.heygen.com/v1/streaming.*` endpoints; that version was wrong and has been replaced.
- `index.html` — uses `@heygen/liveavatar-web-sdk@0.0.17` from esm.sh. Push-to-talk via Web Speech API, transcript pane, no LiveKit/WebSocket plumbing of our own (the SDK does it).
- `requirements.txt`, `run.sh`, `list_avatars.py`, `.env.template`, `.gitignore`, `README.md` — supporting files.
- `.env` — has the user's keys (OPENAI_API_KEY, HEYGEN_API_KEY); needs `HEYGEN_AVATAR_ID` filled in.

## What was wrong with the original code

The starting code (preserved in `~/.openclaw/workspace/skills/liveavatar-advisor/` until Tom deletes it) was *closer* to correct than my first rewrite gave it credit for. Specifics:

1. **Right vendor, right API, wrong audio framing.** It hit `https://api.liveavatar.com/v1/sessions/token` (real) and `/v1/sessions/start` (real). It read `livekit_url`, `livekit_client_token`, `ws_url` from the response — those *are* the real `SessionInfo` field names.
2. **Wrong way to make the avatar speak.** It generated PCM via ElevenLabs and shoveled raw chunks into the `controlWs` as `{ type: "agent.speak", audio: <base64> }`. The real LITE-mode protocol is JSON `CommandEvent` messages with `event_type` like `"avatar.speak_audio"` or `"avatar.speak_text"`, sent over the LiveKit data channel (topic `agent-control`) **or** the parallel WebSocket — whichever the SDK has open. See "Protocol cheatsheet" below.
3. **`mode: "LITE"`** in the token request was correct.

## What was wrong with my (Cowork-session) first rewrite

I made it worse before making it better. I took the failing `api.liveavatar.com` calls at face value, web-searched, didn't notice LiveAvatar is real, and rewrote everything against the **deprecated** Interactive Avatar endpoints on `api.heygen.com/v1/streaming.*`. Those were sunset on **March 31, 2026**. That rewrite is gone now. The current file is a clean skeleton against the right API.

## Protocol cheatsheet (extracted from `@heygen/liveavatar-web-sdk@0.0.17`)

### HTTP

| Step | Method + URL | Auth | Body | Returns |
|---|---|---|---|---|
| Create session token | `POST https://api.liveavatar.com/v1/sessions/token` | header `X-Api-Key: <HEYGEN_API_KEY>` | `{ "avatar_id": "...", "mode": "LITE" }` | `{ data: { token: "..." } }` |
| Start session | `POST https://api.liveavatar.com/v1/sessions/start` | header `Authorization: Bearer <session_token>` | (none) | `{ data: SessionInfo }` |
| Stop session | `POST https://api.liveavatar.com/v1/sessions/stop` | bearer | (none) | `{ ok }` |
| Keep alive | `POST https://api.liveavatar.com/v1/sessions/keep-alive` | bearer | (none) | `{ ok }` |

The SDK does `/v1/sessions/start`, `/sessions/stop`, `/sessions/keep-alive` itself. Backend only needs to do `/v1/sessions/token`.

### `SessionInfo` shape

```ts
{ session_id: string,
  max_session_duration: number | null,
  livekit_url?: string,
  livekit_client_token?: string,
  ws_url?: string }
```

### Command events (frontend → server)

Sent as JSON via either:
- LiveKit data channel: `room.localParticipant.publishData(...)` with `topic: "agent-control"` (constant `LIVEKIT_COMMAND_CHANNEL_TOPIC`)
- Or the WebSocket at `ws_url` if present (preferred for binary-heavy flows like `avatar.speak_audio`)

| `event_type` | Payload | What it does |
|---|---|---|
| `avatar.speak_text` | `{ text }` | Avatar speaks the text in HeyGen's voice. SDK method: `session.repeat(text)`. |
| `avatar.speak_response` | `{ text }` | Same, semantically marks "this is a response to a user turn". SDK method: `session.message(text)`. |
| `avatar.speak_audio` | `{ audio }` | Avatar lip-syncs to OUR audio. `audio` is **base64-encoded PCM 24kHz 16-bit mono**, **chunked**: first chunk 400ms (19,200 bytes raw → larger after base64), subsequent chunks 1s (48,000 bytes each). MUST use the WebSocket transport. SDK method: `session.repeatAudio(b64Chunk)`. |
| `avatar.start_listening` / `avatar.stop_listening` | (none) | Toggle whether the avatar is in "listening" pose. SDK methods: `session.startListening()`, `session.stopListening()`. |
| `avatar.interrupt` | (none) | Cut off whatever the avatar is currently saying. SDK method: `session.interrupt()`. |
| `session.update` / `session.stop` | varies | Internal session control. |

There are also push-to-talk command events for FULL mode (`user.start_push_to_talk`, `user.stop_push_to_talk`) sent by the SDK's `VoiceChat` class — don't worry about these in LITE mode.

### Server events (server → frontend)

Subscribe via the SDK's typed emitter:

```ts
session.on(AgentEventsEnum.AVATAR_TRANSCRIPTION_CHUNK, (e) => /* e.text */);
session.on(AgentEventsEnum.AVATAR_SPEAK_ENDED, () => {});
session.on(AgentEventsEnum.USER_TRANSCRIPTION, (e) => /* e.text */);
session.on(SessionEvent.SESSION_STREAM_READY, () => session.attach(videoEl));
session.on(SessionEvent.SESSION_DISCONNECTED, (reason) => {});
```

Full enum: `SESSION_UPDATED`, `SESSION_STATE_UPDATED`, `USER_SPEAK_STARTED`, `USER_SPEAK_ENDED`, `USER_TRANSCRIPTION`, `USER_TRANSCRIPTION_CHUNK`, `AVATAR_TRANSCRIPTION`, `AVATAR_TRANSCRIPTION_CHUNK`, `AVATAR_SPEAK_STARTED`, `AVATAR_SPEAK_ENDED`, `ELEVENLABS_AGENT_EVENT`, `SESSION_STOPPED`.

### Audio chunking (only if you keep ElevenLabs in the loop)

Reproduced from the SDK's `audio_utils.ts`:

> 24,000 Hz, 16-bit mono → 1 sample = 2 bytes.
> 400 ms = 19,200 bytes, 1 s = 48,000 bytes.
> First chunk 400 ms (low time-to-first-audio), then 1 s chunks afterward.
> Each chunk is base64-encoded and sent as `{ event_type: "avatar.speak_audio", audio: <b64> }` over the WebSocket.

ElevenLabs `output_format="pcm_24000"` produces exactly this byte stream, so no re-sampling needed.

## Architecture decision: HeyGen voice vs ElevenLabs voice

**Default (current code):** `session.repeat(text)` — HeyGen does TTS. Simplest, lowest-latency.

**Alternative if Tom wants the ElevenLabs voice:** keep `/api/llm` as-is, add a `/api/tts` endpoint that streams ElevenLabs PCM chunks to the browser, browser feeds chunks into `session.repeatAudio(...)`. The chunking logic is non-trivial — copy it from the SDK's `audio_utils.ts` rather than re-deriving.

## What's likely still broken / to verify

These are best-guesses, not confirmed — verify from your live terminal:

1. **The `/v1/sessions/token` body shape.** I'm using `{ avatar_id, mode }`. The docs imply that's right but the response format may want `quality`, `voice`, etc. Run `curl` against it once with the user's key to check. If it 400s, look at the message.
2. **`response.code === 1000` is the success sentinel.** The SDK checks `data.code !== SUCCESS_CODE (1000)`. Make sure our backend is checking that too — currently `advisor_backend.py` only checks HTTP status. (HTTP 200 with `code: 5xx` would be a silent failure.)
3. **Avatar must be streaming-eligible.** `list_avatars.py` lists them. If empty, the user's HeyGen plan doesn't include LiveAvatar.
4. **HEYGEN_AVATAR_ID is currently empty in `.env`.** Tom needs to run `python list_avatars.py` and paste an id in.
5. **The "Not Found" error Tom hit at localhost:8000.** That was FastAPI's 404 for an un-registered `GET /`. Almost certainly he was running the *old* `advisor_backend.py` from the `skills/` folder (which had only `/api/token`, `/api/start`, `/ws/avatar`). Confirm before iterating: `lsof -nP -iTCP:8000 | head` and check the working dir of that pid.

## Next steps for the Claude Code session

In rough order. **Verify each step in your real terminal + browser before moving on.**

1. **Sanity-check the env.**
   ```bash
   cd ~/Documents/liveavatar-advisor    # or wherever Tom moved it
   ./run.sh
   curl -s http://localhost:8000/health | jq
   ```
   Expect `have_heygen_key: true`, `have_openai_key: true`, `avatar_id: ...`.

2. **Get an avatar id.**
   ```bash
   source venv/bin/activate
   python list_avatars.py
   ```
   Pick one, set `HEYGEN_AVATAR_ID=...` in `.env`, restart `./run.sh`.

3. **Validate `/api/token` directly.**
   ```bash
   curl -s -X POST http://localhost:8000/api/token | jq
   ```
   Expect `{ "session_token": "..." }`. If it errors, the message tells you exactly what LiveAvatar refused (avatar not on plan, mode invalid, etc.). Add a `code != 1000` check in the backend if needed.

4. **Open the page in Chrome with DevTools.** If you have Claude in Chrome installed as an MCP, drive it from the agent: navigate, click *Start session*, watch `read_console_messages` and `read_network_requests`.
   Confirm in order: video appears → text input enabled → typing a question → LLM call to `/api/llm` → SDK calls `session.repeat(reply)` → avatar speaks → `AVATAR_TRANSCRIPTION_CHUNK` events stream into the transcript pane.

5. **Push-to-talk.** Confirm Web Speech API works (Chrome only). The session needs to be served from `http://localhost` or `https://`, not `file://`.

6. **Once the basic loop works, decide:**
   - Use HeyGen voice (current default) → done.
   - Switch to ElevenLabs voice → add `/api/tts` streaming endpoint with the audio-chunking from "Audio chunking" above; switch frontend from `session.repeat(reply)` to looping `session.repeatAudio(chunk)` calls.
   - Add RAG: chunk annuity product docs, embed with OpenAI, store in OpenSearch/pgvector, retrieve top-k inside `/api/llm` before the LLM call.

7. **For Tom's AWS-serverless production target:** the backend is just two stateless endpoints (`/api/token`, `/api/llm`). Trivial as Lambda + API Gateway. The transcript memory needs to move out of process — DynamoDB keyed by `session_id` is the obvious fit.

## Useful sources

- LiveAvatar docs: https://docs.liveavatar.com/
- LITE Mode lifecycle: https://docs.liveavatar.com/docs/custom-mode-life-cycle
- LITE Mode events: https://docs.liveavatar.com/docs/custom-mode-events
- Migration note (Interactive Avatar → LiveAvatar, sunset Mar 31 2026): https://help.heygen.com/en/articles/12758516-introducing-liveavatar
- Official SDK: https://www.npmjs.com/package/@heygen/liveavatar-web-sdk
- SDK repo: https://github.com/heygen-com/live-avatar-js-sdk
