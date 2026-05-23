---

# How to Generate a Scripted Avatar Video

## One-command usage

```bash
cd ~/Documents/liveavatar-advisor
venv/bin/python3 scripts/generate_video.py \
  --script-file scripts/MY_SCRIPT.txt \
  --background 'image:https://images.pexels.com/photos/6949365/pexels-photo-6949365.jpeg' \
  --out-dir produced_videos/
```

Or with an inline script:

```bash
venv/bin/python3 scripts/generate_video.py \
  --script 'Hi, I am Tom Olds. Here is my message...' \
  --background 'image:https://images.pexels.com/photos/6949365/pexels-photo-6949365.jpeg' \
  --out-dir produced_videos/
```

The finished MP4 downloads automatically to `produced_videos/`.

---

## What the pipeline does

1. Reads the script from `--script` or `--script-file`
2. Sends text to **ElevenLabs** (Yorkville2 voice) → returns MP3 audio
3. Uploads MP3 to HeyGen as an asset → gets back an `audio_url`
4. Submits a render job to **HeyGen v3 API** (`POST /v3/videos`)
5. Polls `GET /v3/videos/{video_id}` every 10s until complete
6. Downloads the finished MP4 to `--out-dir`

---

## Working configuration (do not change these without re-testing)

| Setting | Value |
|---|---|
| Avatar | Tom - BlankWall V |
| Avatar ID | `3e7ac45895cd4034a0d694270504e5c5` |
| Avatar trained from | `Tom_BlankWall_05212026_SDR_smooth_40.mp4` |
| Voice | ElevenLabs Yorkville2 |
| ElevenLabs voice_id | `8gfvBkrqr64Si4V5Q321` |
| ElevenLabs model | `eleven_flash_v2_5` |
| HeyGen API | v3 — `POST https://api.heygen.com/v3/videos` |
| HeyGen engine | `avatar_v` |
| Background removal | `remove_background: true` |
| Default background | Pexels 6949365 — conference room with high-rise windows |
| Background URL | `https://images.pexels.com/photos/6949365/pexels-photo-6949365.jpeg` |
| Output size | 1280×720 (16:9 landscape) |
| Cost per video | ~20 HeyGen API credits |

---

## .env variables used

- `HEYGEN_API_KEY` — HeyGen API key (v3)
- `HEYGEN_V_AVATAR_ID` — Tom BlankWall avatar ID (`3e7ac45895cd4034a0d694270504e5c5`)
- `ELEVENLABS_API_KEY` — ElevenLabs API key
- `ELEVENLABS_VOICE_ID` — Yorkville2 (`8gfvBkrqr64Si4V5Q321`)

Do NOT use `HEYGEN_AVATAR_ID` for scripted videos — those are LiveAvatar-only IDs.

---

## Changing the background

Pass any public image URL:

```bash
--background 'image:https://YOUR_IMAGE_URL_HERE.jpeg'
```

Or a solid color:

```bash
--background 'color:#1a2b3c'
```

---

## Writing a new script

1. Create a `.txt` file in `scripts/` with your spoken text
2. Pass it with `--script-file scripts/your_script.txt`
3. Keep scripts under ~500 words for a 3–4 minute video

---

## Key lessons learned (avoid repeating)

- **Use HeyGen v3 API** (`/v3/videos`), NOT v2 (`/v2/video/generate`). The v2 API does not support `remove_background` and has a completely different payload structure.
- **Audio goes in `audio_url`**, not `voice_id`. In v3 these are separate fields.
- **Background goes as** `{"url": "..."}` at the top level with `remove_background: true`. Do not nest it inside a `character` object.
- **The avatar must be trained from blank-wall footage** for background removal to work. Sofa/couch footage does not key out cleanly.
- **`HEYGEN_AVATAR_ID`** in `.env` is for the LiveAvatar real-time product only — do not pass it to the scripted video script.

---

## If something breaks

1. Check API credits: `app.heygen.com/settings?nav=API` — add funds if "50 of 50 used"
2. Check the avatar is still active: `app.heygen.com/avatar/my-avatars`
3. Run with `--dry-run` (add a `sys.exit()` after the payload print) to inspect the JSON before spending credits
4. The ElevenLabs step is free to test — only the HeyGen render costs credits
