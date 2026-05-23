"""
Fetch + transcribe YouTube videos for ingestion into the avatar's RAG corpus.

Uses yt-dlp for download (same approach as Tom's existing AudiblyLegibleInsights/
Next.py code), ffmpeg for compression to fit Whisper's 25 MB upload cap, and
OpenAI Whisper for transcription. Output transcripts land in annuity_docs/
with a configurable filename prefix so the existing scripts/ingest.py picks
them up automatically.

Modes:

  Single video:
      python scripts/fetch_youtube_episodes.py --url 'https://www.youtube.com/watch?v=pL9ClgkD5jk'

  A list of URLs from a file:
      python scripts/fetch_youtube_episodes.py --urls-file my_videos.txt

  A whole channel (latest N):
      python scripts/fetch_youtube_episodes.py \\
          --channel 'https://www.youtube.com/@StanTheAnnuityMan' \\
          --episodes 30

Re-running is safe: every successfully transcribed video gets recorded in
podcast_audio/.youtube_manifest.json by its YouTube video_id, so subsequent
runs skip what's already done. No duplicate Whisper charges.

After this finishes, re-run the existing ingest to pull the new transcripts
into ChromaDB:
    python scripts/ingest.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from openai import OpenAI

# yt-dlp can be used via its Python API; matches the existing pattern more
# cleanly than subprocess and gives us structured metadata (title, upload_date,
# id) without parsing CLI output.
try:
    import yt_dlp  # type: ignore
except ImportError:
    sys.exit(
        "yt-dlp not installed. Run:\n"
        "    pip install yt-dlp\n"
        "or add it to requirements.txt and `pip install -r requirements.txt`."
    )

load_dotenv()


# ---- Video URL helpers -----------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-char YouTube video ID from any flavor of YouTube URL."""
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


# ---- Manifest --------------------------------------------------------------

def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: could not read manifest {path}: {e}", file=sys.stderr)
        return {}


def _save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


# ---- Slug helper -----------------------------------------------------------

def slugify(title: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", title).strip().lower()
    s = re.sub(r"[\s-]+", "_", s)
    return s[:maxlen] or "video"


# ---- YouTube captions (free, fast) -----------------------------------------
#
# Pattern borrowed from Tom's existing `youtube-to-slides` OpenClaw skill:
# nearly every YouTube video has auto-generated captions which yt-dlp can pull
# without downloading any audio. This is free (no Whisper API charges), fast
# (a few seconds vs minutes), and quality is good enough for RAG retrieval.
# Whisper becomes a fallback when captions aren't available.


def fetch_captions(url: str, work_dir: Path) -> Optional[tuple[str, str]]:
    """Try to pull YouTube's auto-generated captions for a video. Returns
    (clean_transcript_text, video_id) if successful, None if captions aren't
    available."""
    work_dir.mkdir(parents=True, exist_ok=True)
    vid = extract_video_id(url) or "video"
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB", "en-orig"],
        "subtitlesformat": "vtt",
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        vid = info.get("id", vid)
    except Exception as e:
        print(f"    caption fetch failed: {e}", file=sys.stderr)
        return None

    # Find the VTT file yt-dlp wrote (any English variant)
    vtt_path = None
    for p in sorted(work_dir.glob(f"{vid}.*.vtt")):
        vtt_path = p
        break
    if not vtt_path or not vtt_path.exists():
        return None

    text = _parse_vtt(vtt_path)
    # Clean up the .vtt sidecar — we only need the parsed text
    try:
        vtt_path.unlink()
    except Exception:
        pass
    if not text.strip():
        return None
    return text, vid


def _parse_vtt(path: Path) -> str:
    """Convert a YouTube .vtt caption file into clean prose. Strips WEBVTT
    headers, timestamps, and consecutive duplicate lines (auto-captions
    often repeat words as the on-screen text scrolls)."""
    import html
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    last = ""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        if "-->" in s:
            continue  # timestamp line
        if re.fullmatch(r"\d+", s):
            continue  # cue number
        # Strip inline timing tags like <00:00:01.500><c>word</c>
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s).strip()
        if not s or s == last:
            continue
        out.append(s)
        last = s
    return " ".join(out)


# ---- yt-dlp download + metadata --------------------------------------------

def fetch_video_metadata_and_audio(url: str, audio_dir: Path, download: bool = True) -> Optional[dict]:
    """Use yt-dlp to fetch metadata for `url` and (optionally) download the
    audio. Returns the info dict yt-dlp produces (title, id, upload_date,
    duration, ext, etc.) plus a resolved `local_audio_path` if downloaded."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(audio_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as e:
        print(f"    yt-dlp failed: {e}", file=sys.stderr)
        return None

    if not download:
        return info

    # Resolve the actual file path (extension is determined by yt-dlp at download time)
    video_id = info.get("id")
    ext = info.get("ext", "m4a")
    local_path = audio_dir / f"{video_id}.{ext}"
    if not local_path.exists():
        # Sometimes yt-dlp renames; scan for the id
        candidates = list(audio_dir.glob(f"{video_id}.*"))
        # Filter out our compressed sidecar names
        candidates = [p for p in candidates if not p.name.endswith(".compressed.mp3")]
        if candidates:
            local_path = candidates[0]
        else:
            print(f"    downloaded file not found for {video_id}", file=sys.stderr)
            return None
    info["local_audio_path"] = str(local_path)
    return info


def enumerate_channel_videos(channel_url: str, limit: int) -> list[dict]:
    """Use yt-dlp to list (in feed order, i.e. newest first) up to `limit`
    videos from a channel without downloading any audio yet."""
    opts = {
        "extract_flat": True,
        "playlistend": limit,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    entries = info.get("entries") or []
    # Each entry may itself be a playlist (Videos / Shorts / Live tabs) — flatten
    flat: list[dict] = []
    for e in entries:
        if e.get("entries"):
            flat.extend(e["entries"][:limit])
        else:
            flat.append(e)
        if len(flat) >= limit:
            break
    # Normalize: each item has at least id + title + url
    out = []
    for e in flat[:limit]:
        if not e:
            continue
        vid = e.get("id") or extract_video_id(e.get("url", ""))
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.get("title", vid),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


# ---- Audio compression -----------------------------------------------------

def compress_for_whisper(src: Path, dst: Path) -> bool:
    """ffmpeg → 32 kbps mono 16 kHz. Fits Whisper's 25 MB cap even on multi-hour
    videos and doesn't measurably hurt speech-to-text quality."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1",
                "-ar", "16000",
                "-ab", "32k",
                str(dst),
            ],
            check=True,
        )
        return dst.exists() and dst.stat().st_size > 1000
    except Exception as e:
        print(f"    compression failed: {e}", file=sys.stderr)
        return False


# ---- Transcription ---------------------------------------------------------

def transcribe(audio_path: Path, client: OpenAI) -> Optional[str]:
    try:
        with open(audio_path, "rb") as f:
            return client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
    except Exception as e:
        print(f"    transcribe failed: {e}", file=sys.stderr)
        return None


# ---- Main ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="A single YouTube video URL.")
    source.add_argument("--urls-file", help="Path to a text file with one YouTube URL per line.")
    source.add_argument("--channel", help="YouTube channel URL — fetches the latest N videos.")

    parser.add_argument("--episodes", type=int, default=30,
                        help="Limit when using --channel (default: 30). Ignored for single URLs.")
    parser.add_argument("--podcast", default="Stan The Annuity Man",
                        help="Display label written to each transcript's metadata header "
                             "(default: 'Stan The Annuity Man').")

    here = Path(__file__).parent.parent  # project root
    parser.add_argument("--audio-dir", default=str(here / "podcast_audio"),
                        help="Directory for downloaded + compressed audio.")
    parser.add_argument("--transcript-dir", default=str(here / "annuity_docs"),
                        help="Directory for transcripts (must be flat in annuity_docs/ so "
                             "the existing ingest.py picks them up).")
    parser.add_argument("--filename-prefix", default="stan_youtube_",
                        help="Prefix applied to transcript filenames.")
    parser.add_argument("--no-date", action="store_true",
                        help="Omit publication date from the transcript filename. "
                             "By default the filename is <prefix><YYYY-MM-DD>_<slug>.txt "
                             "so episodes sort chronologically when stored together.")
    parser.add_argument("--delete-audio", action="store_true",
                        help="Delete audio files after successful transcription.")
    parser.add_argument("--manifest", default=None,
                        help="Manifest JSON path (default: <audio-dir>/.youtube_manifest.json).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the manifest and re-process every video.")
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir).expanduser().resolve()
    transcript_dir = Path(args.transcript_dir).expanduser().resolve()
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = Path(args.manifest).expanduser() if args.manifest else (audio_dir / ".youtube_manifest.json")
    manifest = _load_manifest(manifest_path)
    if manifest and not args.force:
        print(f"Loaded manifest: {len(manifest)} previously-processed videos\n  ({manifest_path})")

    # Resolve the list of (url, expected_id, optional_title) to process
    work_items: list[dict] = []
    if args.url:
        vid = extract_video_id(args.url)
        if not vid:
            sys.exit(f"Could not parse video ID from URL: {args.url}")
        work_items.append({"url": args.url, "id": vid, "title": None})
    elif args.urls_file:
        for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            vid = extract_video_id(line)
            if not vid:
                print(f"WARN: skipping unparseable line: {line}", file=sys.stderr)
                continue
            work_items.append({"url": line, "id": vid, "title": None})
    elif args.channel:
        print(f"Enumerating channel: {args.channel} (latest {args.episodes})...")
        videos = enumerate_channel_videos(args.channel, args.episodes)
        if not videos:
            sys.exit("No videos found on the channel.")
        print(f"  found {len(videos)} videos")
        for v in videos:
            work_items.append({"url": v["url"], "id": v["id"], "title": v["title"]})

    if not work_items:
        sys.exit("Nothing to do.")

    client = OpenAI()
    n_done, n_skipped, n_failed = 0, 0, 0

    for i, item in enumerate(work_items, 1):
        url, vid = item["url"], item["id"]
        header = f"[{i}/{len(work_items)}] {vid}"

        if not args.force and vid in manifest:
            print(f"{header} — already in manifest, skip")
            n_skipped += 1
            continue

        # ── Pull metadata first (no audio download yet) so we have title/date
        print(f"{header}\n    ⓘ  fetching metadata")
        info = fetch_video_metadata_and_audio(url, audio_dir, download=False)
        if not info:
            n_failed += 1
            continue

        title = info.get("title") or item.get("title") or vid
        slug = slugify(title)
        published = info.get("upload_date", "")  # YYYYMMDD format from yt-dlp
        if published and len(published) == 8:
            published = f"{published[:4]}-{published[4:6]}-{published[6:8]}"

        # Date prefix in the filename keeps multi-episode folders sortable.
        date_part = f"{published}_" if (published and not args.no_date) else ""
        txt_path = transcript_dir / f"{args.filename_prefix}{date_part}{slug}.txt"
        if txt_path.exists() and not args.force:
            print(f"    transcript file already on disk → recording in manifest")
            manifest[vid] = {
                "video_id": vid,
                "url": url,
                "title": title,
                "published": published,
                "slug": slug,
                "transcript_path": str(txt_path.relative_to(here)),
                "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "method": "(existing file)",
                "note": "reconciled from existing file",
            }
            _save_manifest(manifest_path, manifest)
            n_skipped += 1
            continue

        # ── Strategy 1 (free + fast): try YouTube auto-captions via yt-dlp
        text: Optional[str] = None
        method = ""
        print(f"    📝  trying YouTube auto-captions")
        cap_result = fetch_captions(url, audio_dir)
        if cap_result:
            text = cap_result[0]
            method = "youtube_captions"
            print(f"    ✓ got captions ({len(text)} chars) — no audio download or Whisper needed")

        # ── Strategy 2 (paid + slow): fall back to audio + Whisper
        if not text:
            print(f"    captions not available — falling back to audio + Whisper")
            print(f"    ↓ downloading audio")
            audio_info = fetch_video_metadata_and_audio(url, audio_dir, download=True)
            if not audio_info:
                n_failed += 1
                continue
            audio_path = Path(audio_info["local_audio_path"])
            compressed_path = audio_dir / f"{vid}.compressed.mp3"
            if not compressed_path.exists():
                print(f"    🗜  compressing → 32k mono for Whisper")
                if not compress_for_whisper(audio_path, compressed_path):
                    n_failed += 1
                    continue
            size_mb = compressed_path.stat().st_size / 1024 / 1024
            if size_mb > 24:
                print(f"    ✗ compressed file still {size_mb:.1f} MB — would need to split")
                n_failed += 1
                continue
            print(f"    🎙  transcribing with Whisper ({size_mb:.1f} MB compressed)")
            text = transcribe(compressed_path, client)
            if not text:
                n_failed += 1
                continue
            method = "whisper"
            if args.delete_audio:
                audio_path.unlink(missing_ok=True)
                compressed_path.unlink(missing_ok=True)

        # ── Save transcript
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n")
            if published:
                f.write(f"Published: {published}\n")
            f.write(f"Source: {args.podcast} (YouTube)\n")
            f.write(f"Video URL: https://www.youtube.com/watch?v={vid}\n")
            f.write(f"Transcript method: {method}\n\n")
            f.write(text.strip())
            f.write("\n")

        manifest[vid] = {
            "video_id": vid,
            "url": url,
            "title": title,
            "published": published,
            "slug": slug,
            "transcript_path": str(txt_path.relative_to(here)),
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": method,
        }
        _save_manifest(manifest_path, manifest)
        n_done += 1
        print(f"    ✓ saved → {txt_path.relative_to(here)}  [{method}]")
        time.sleep(0.5)  # polite pacing

    print(f"\n══════════════════════════════════════")
    print(f"  Done.   transcribed: {n_done}")
    print(f"          skipped:     {n_skipped}")
    print(f"          failed:      {n_failed}")
    print(f"  Transcripts:  {transcript_dir}")
    print(f"  Manifest:     {manifest_path}")
    print(f"\nNext: re-run ingest to add the new transcripts to ChromaDB:")
    print(f"  python scripts/ingest.py")
    print(f"Then bounce uvicorn (Ctrl+C, bash run.sh).")


if __name__ == "__main__":
    main()
