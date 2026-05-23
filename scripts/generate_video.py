#!/usr/bin/env python3
"""Generate a HeyGen avatar video from a script.

Pipeline:
  1. ElevenLabs TTS → MP3 bytes
  2. Upload MP3 to HeyGen asset store → audio_url  (raw binary POST to upload.heygen.com/v1/asset)
  3. Resolve background spec (color / remote image / local file)
  4. Submit render job (HeyGen v3) → video_id
  5. Poll status until completed
  6. Download MP4 to out_dir

Working configuration (last verified 2026-05-21):
  API:        HeyGen v3 — POST https://api.heygen.com/v3/videos
  Status:     GET  https://api.heygen.com/v3/videos/{video_id}
  Avatar:     Tom - BlankWall V  (avatar_id: 3e7ac45895cd4034a0d694270504e5c5)
              Set via HEYGEN_V_AVATAR_ID in .env
  Background: Pexels 6949365 conference room
              https://images.pexels.com/photos/6949365/pexels-photo-6949365.jpeg
  Voice:      ElevenLabs Yorkville2 (ELEVENLABS_VOICE_ID in .env)
              Synthesised → uploaded to HeyGen → passed as audio_url in payload
  Engine:     avatar_v  with remove_background: true
  Render key payload fields:
              type, avatar_id, audio_url, resolution, aspect_ratio,
              engine.type, remove_background, background.type/url, title

Usage:
  python3 scripts/generate_video.py \\
    --script-file scripts/retirement_intro.txt \\
    --background 'image:https://images.pexels.com/photos/6949365/pexels-photo-6949365.jpeg' \\
    --out-dir produced_videos/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

# ---------------------------------------------------------------------------
# Load .env from project root (two levels up from scripts/)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(_ROOT / ".env")
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.exists():
        for _line in env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib only (urllib.request)
# ---------------------------------------------------------------------------

def _http_json(url: str, *, method: str = "GET", headers: dict | None = None,
               body: bytes | None = None, timeout: int = 60) -> dict:
    req = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} from {url}:\n{err_body}") from exc


def _http_bytes(url: str, *, method: str = "GET", headers: dict | None = None,
                body: bytes | None = None, timeout: int = 60) -> bytes:
    req = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} from {url}:\n{err_body}") from exc


def _multipart_post(url: str, file_bytes: bytes, filename: str,
                    content_type: str, extra_headers: dict) -> dict:
    """POST multipart/form-data with a single 'file' field."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    body = crlf.join([
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
        file_bytes,
        f"--{boundary}--".encode(),
        b"",
    ])
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    headers.update(extra_headers)
    return _http_json(url, method="POST", headers=headers, body=body)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def synthesize_tts(text: str, voice_id: str, api_key: str, model: str) -> bytes:
    print("  [1/6] ElevenLabs TTS — synthesising speech...")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = json.dumps({
        "text": text,
        "model_id": model,
        "output_format": "mp3_44100_128",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }).encode()
    audio_bytes = _http_bytes(
        url, method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        body=payload,
    )
    print(f"  ✓ Audio synthesised: {len(audio_bytes):,} bytes")
    return audio_bytes


def upload_to_heygen(file_bytes: bytes, filename: str, content_type: str,
                     heygen_api_key: str) -> str:
    """Upload a file to HeyGen asset store, return the asset URL."""
    result = _http_json(
        "https://upload.heygen.com/v1/asset",
        method="POST",
        headers={
            "X-Api-Key": heygen_api_key,
            "Content-Type": content_type,
            "Content-Length": str(len(file_bytes)),
        },
        body=file_bytes,
    )
    data = result.get("data") or {}
    asset_url = data.get("url") or data.get("asset_url")
    if not asset_url:
        raise RuntimeError(f"No URL in HeyGen asset response: {json.dumps(result)}")
    return asset_url


def upload_audio(audio_bytes: bytes, heygen_api_key: str) -> str:
    print("  [2/6] HeyGen — uploading audio asset...")
    audio_url = upload_to_heygen(audio_bytes, "tts_audio.mp3", "audio/mpeg", heygen_api_key)
    print(f"  ✓ Audio uploaded: {audio_url}")
    return audio_url


def resolve_background(spec: str, heygen_api_key: str) -> dict:
    print(f"  [3/6] Background — resolving '{spec}'...")
    if spec.startswith("color:"):
        bg = {"type": "color", "value": spec[6:]}
    elif spec.startswith("image:"):
        target = spec[6:]
        if target.startswith("http://") or target.startswith("https://"):
            bg = {"type": "image", "url": target}
        else:
            url = upload_to_heygen(Path(target).read_bytes(), Path(target).name,
                                   "image/jpeg", heygen_api_key)
            bg = {"type": "image", "url": url}
    elif spec.startswith("video:"):
        target = spec[6:]
        if target.startswith("http://") or target.startswith("https://"):
            bg = {"type": "video", "url": target}
        else:
            url = upload_to_heygen(Path(target).read_bytes(), Path(target).name,
                                   "video/mp4", heygen_api_key)
            bg = {"type": "video", "url": url}
    else:
        raise ValueError(f"Unrecognised background spec: {spec!r}. "
                         "Use color:#hex, image:URL, image:/path, video:URL, or video:/path")
    print(f"  ✓ Background: {bg}")
    return bg


def submit_render(avatar_id: str, avatar_style: str, audio_url: str,
                  background: dict, width: int, height: int,
                  heygen_api_key: str) -> str:
    print("  [4/6] HeyGen — submitting render job...")
    payload = json.dumps({
        "type": "avatar",
        "avatar_id": avatar_id,
        "audio_url": audio_url,
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "engine": {"type": "avatar_v"},
        "remove_background": True,
        "background": background,
        "title": "Tom Olds - Scripted",
    }).encode()
    result = _http_json(
        "https://api.heygen.com/v3/videos",
        method="POST",
        headers={
            "X-Api-Key": heygen_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body=payload,
    )
    data = result.get("data") or {}
    video_id = data.get("video_id")
    if not video_id:
        raise RuntimeError(f"No video_id in render response: {json.dumps(result)}")
    print(f"  ✓ Render submitted. video_id: {video_id}")
    return video_id


def poll_status(video_id: str, heygen_api_key: str, timeout: int = 1800) -> str:
    print("  [5/6] HeyGen — polling render status...")
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout:
            raise TimeoutError(f"Render timed out after {timeout}s")
        result = _http_json(
            f"https://api.heygen.com/v3/videos/{video_id}",
            headers={"X-Api-Key": heygen_api_key},
        )
        data = result.get("data") or {}
        status = data.get("status", "unknown")
        progress = data.get("progress") or 0
        print(f"    [{elapsed:4d}s] status={status:<12}  progress={progress}%")
        if status == "completed":
            video_url = data.get("video_url")
            if not video_url:
                raise RuntimeError(f"Status completed but no video_url in response: {result}")
            print(f"  ✓ Render complete.")
            return video_url
        if status == "failed":
            detail = data.get("error") or data.get("message") or ""
            raise RuntimeError(f"HeyGen render failed. Detail: {detail}\nFull response: {result}")
        time.sleep(10)


def download_video(video_url: str, video_id: str, out_dir: Path) -> Path:
    print(f"  [6/6] Downloading MP4...")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{video_id}.mp4"
    data = _http_bytes(video_url, timeout=300)
    out_path.write_bytes(data)
    size_mb = len(data) / 1_048_576
    print(f"  ✓ Saved: {out_path.resolve()}  ({size_mb:.1f} MB)")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a HeyGen avatar video from a script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--script", help="Inline script text")
    src.add_argument("--script-file", type=Path, metavar="PATH",
                     help="Read script from file")
    parser.add_argument("--avatar-id",
                        default=os.environ.get("HEYGEN_V_AVATAR_ID",
                                               "dc94bf879cd4471b9f4b6f8a5d9c7e95"),
                        help="HeyGen avatar_id (reads HEYGEN_V_AVATAR_ID)")
    parser.add_argument("--avatar-style", default="normal",
                        choices=["normal", "circle", "close_up"])
    parser.add_argument("--background", default="color:#f0f0f0",
                        metavar="SPEC",
                        help="color:#hex | image:URL | image:PATH | video:URL | video:PATH")
    parser.add_argument("--voice-id",
                        default=os.environ.get("ELEVENLABS_VOICE_ID",
                                               "8gfvBkrqr64Si4V5Q321"),
                        help="ElevenLabs voice_id")
    parser.add_argument("--el-model", default="eleven_flash_v2_5",
                        help="ElevenLabs model id")
    parser.add_argument("--out-dir", type=Path, default=Path("produced_videos"),
                        help="Output directory for MP4")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args(argv)

    heygen_key = os.environ.get("HEYGEN_API_KEY", "").strip()
    el_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()

    if not heygen_key:
        print("ERROR: HEYGEN_API_KEY not set in environment or .env", file=sys.stderr)
        return 1
    if not el_key:
        print("ERROR: ELEVENLABS_API_KEY not set in environment or .env", file=sys.stderr)
        return 1

    if args.script_file:
        if not args.script_file.exists():
            print(f"ERROR: script file not found: {args.script_file}", file=sys.stderr)
            return 1
        script_text = args.script_file.read_text(encoding="utf-8").strip()
    else:
        script_text = args.script.strip()

    print(f"\n{'='*60}")
    print(f"  Script:  {len(script_text)} chars")
    print(f"  Avatar:  {args.avatar_id} ({args.avatar_style})")
    print(f"  Voice:   {args.voice_id}")
    print(f"  Model:   {args.el_model}")
    print(f"  Size:    {args.width}x{args.height}")
    print(f"  BG:      {args.background}")
    print(f"  Out dir: {args.out_dir}")
    print(f"{'='*60}\n")

    try:
        audio_bytes = synthesize_tts(script_text, args.voice_id, el_key, args.el_model)
        audio_url = upload_audio(audio_bytes, heygen_key)
        background = resolve_background(args.background, heygen_key)
        video_id = submit_render(
            args.avatar_id, args.avatar_style, audio_url,
            background, args.width, args.height, heygen_key,
        )
        video_url = poll_status(video_id, heygen_key)
        out_path = download_video(video_url, video_id, args.out_dir)
    except (RuntimeError, ValueError, TimeoutError, FileNotFoundError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    print(f"\n{'='*60}")
    print(f"  ✓ Video ready: {out_path.resolve()}")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
