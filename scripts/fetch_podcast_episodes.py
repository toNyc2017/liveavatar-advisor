"""
Fetch + transcribe the most recent N episodes of a podcast and save transcripts
ready for ChromaDB ingestion.

Stitches together patterns from Tom's existing projects:
  - iTunes API RSS finder pattern (AudiblyLegibleInsights/simple_rss_finder.py)
  - Episode download + RSS parsing (AudiblyLegibleInsights/enhanced_podcast_downloader.py)
  - Whisper transcription wrapper (WorldView/transcribe_audio.py)

Pipeline:
  1. Resolve podcast name → RSS feed URL via iTunes Search API
  2. Parse RSS, take latest N episodes
  3. For each: download MP3 → ffmpeg-compress for Whisper size limit → transcribe
  4. Save transcripts with a metadata header to <transcript_dir>/<slug>.txt

After this script finishes, run the existing ChromaDB ingester to add the new
transcripts to the avatar's knowledge base. From the project root:

    source venv/bin/activate
    python scripts/ingest.py

Usage:
    python scripts/fetch_podcast_episodes.py \
        --podcast "Fun with Annuities" \
        --episodes 30

Cost: Whisper API is $0.006/min of audio. 30 × ~45min episodes ≈ $8.10 total.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


# ---- Episode manifest ------------------------------------------------------
#
# We keep a JSON file recording every episode we've successfully transcribed.
# Subsequent runs skip any episode whose GUID is in the manifest, so we never
# re-download or re-spend Whisper credits on something we already have. The
# manifest is keyed by episode GUID where available, falling back to a hash
# of the audio URL when a feed doesn't provide GUIDs.

def _episode_key(episode: dict) -> str:
    """Stable identifier for an episode across runs. Prefers GUID, falls back
    to a hash of the audio URL."""
    guid = (episode.get("guid") or "").strip()
    if guid:
        return guid
    return "url:" + hashlib.sha1(episode["url"].encode("utf-8")).hexdigest()[:16]


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


# ---- iTunes RSS resolver ---------------------------------------------------

def find_rss_url(podcast_name: str) -> Optional[str]:
    """Look up a podcast's RSS feed URL via the iTunes Search API."""
    r = requests.get(
        "https://itunes.apple.com/search",
        params={"term": podcast_name, "entity": "podcast", "limit": 1},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("resultCount", 0) == 0:
        return None
    return data["results"][0].get("feedUrl")


# ---- RSS parsing -----------------------------------------------------------

def parse_episodes(rss_url: str, limit: int) -> list[dict]:
    """Return the latest N episodes from an RSS feed.

    Each entry: { title, published (str), url (audio enclosure), guid }
    """
    feed = feedparser.parse(rss_url)
    episodes = []
    for entry in feed.entries[:limit]:
        audio_url = None
        # Check enclosures (RSS-standard place for the audio file)
        for enc in entry.get("enclosures", []) or []:
            href = enc.get("href") or enc.get("url")
            if href and (enc.get("type", "").startswith("audio/") or href.endswith((".mp3", ".m4a"))):
                audio_url = href
                break
        # Fallback: scan links
        if not audio_url:
            for link in entry.get("links", []) or []:
                if link.get("type", "").startswith("audio/"):
                    audio_url = link.get("href")
                    break
        if not audio_url:
            continue
        episodes.append({
            "title": entry.title,
            "published": entry.get("published", ""),
            "url": audio_url,
            "guid": entry.get("id", "") or entry.get("guid", ""),
        })
    return episodes


# ---- File helpers ----------------------------------------------------------

def slugify(title: str, maxlen: int = 80) -> str:
    """Filesystem-safe slug from an episode title."""
    s = re.sub(r"[^\w\s-]", "", title).strip().lower()
    s = re.sub(r"[\s-]+", "_", s)
    return s[:maxlen] or "episode"


# ---- Audio download + compression ------------------------------------------

def download_mp3(url: str, dest: Path) -> bool:
    """Stream-download an audio file. Returns True if it lands on disk."""
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return dest.exists() and dest.stat().st_size > 1000
    except Exception as e:
        print(f"    download failed: {e}", file=sys.stderr)
        if dest.exists():
            dest.unlink()
        return False


def compress_for_whisper(src: Path, dst: Path) -> bool:
    """Re-encode to 32 kbps mono 16 kHz so even hour-long episodes fit Whisper's
    25 MB upload cap. ffmpeg must be on PATH.

    Whisper actually works well at this bitrate — it was designed for speech and
    has been used with telephone-quality (8 kHz / 16 kbps) audio successfully.
    """
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1",       # mono
                "-ar", "16000",   # 16 kHz sample rate (Whisper-friendly)
                "-ab", "32k",     # 32 kbps bitrate
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
    """Send to OpenAI Whisper and return plain text."""
    try:
        with open(audio_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        return transcript
    except Exception as e:
        print(f"    transcribe failed: {e}", file=sys.stderr)
        return None


# ---- Main driver -----------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--podcast", default="Fun with Annuities",
                        help='Podcast name as searched on iTunes (default: "Fun with Annuities").')
    parser.add_argument("--episodes", type=int, default=30,
                        help="Number of latest episodes to fetch (default: 30).")
    parser.add_argument("--rss", help="Override iTunes lookup with an explicit RSS URL.")
    here = Path(__file__).parent.parent  # project root
    parser.add_argument("--audio-dir", default=str(here / "podcast_audio"),
                        help="Directory for downloaded + compressed audio (default: ./podcast_audio).")
    parser.add_argument("--transcript-dir", default=str(here / "annuity_docs"),
                        help="Directory for transcripts ready for ChromaDB ingest "
                             "(default: ./annuity_docs — the existing ingest.py "
                             "uses flat directory iteration, so files must live "
                             "at the root of annuity_docs/ to be picked up).")
    parser.add_argument("--filename-prefix", default="stan_podcast_",
                        help="Prefix applied to transcript filenames so podcast episodes "
                             "are easy to distinguish from other annuity_docs/ content.")
    parser.add_argument("--delete-audio", action="store_true",
                        help="Delete the audio file after a successful transcription.")
    parser.add_argument("--manifest", default=None,
                        help="Path to the episode manifest JSON (default: "
                             "<audio-dir>/.podcast_manifest.json). Used to skip "
                             "episodes already transcribed on a previous run.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the manifest and re-process every episode in the feed window.")
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir).expanduser()
    transcript_dir = Path(args.transcript_dir).expanduser()
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = Path(args.manifest).expanduser() if args.manifest else (audio_dir / ".podcast_manifest.json")
    manifest = _load_manifest(manifest_path)
    if manifest and not args.force:
        print(f"Loaded manifest: {len(manifest)} previously-processed episodes\n  ({manifest_path})")

    # 1. Resolve RSS
    if args.rss:
        rss_url = args.rss
        print(f"Using provided RSS: {rss_url}")
    else:
        print(f"Looking up RSS for '{args.podcast}' via iTunes...")
        rss_url = find_rss_url(args.podcast)
        if not rss_url:
            sys.exit(f"  ✗ Could not find RSS for '{args.podcast}'")
        print(f"  ✓ {rss_url}")

    # 2. Parse episodes
    print(f"\nFetching latest {args.episodes} episodes from feed...")
    episodes = parse_episodes(rss_url, args.episodes)
    if not episodes:
        sys.exit("  ✗ No episodes found")
    print(f"  ✓ {len(episodes)} episodes parsed")

    # 3. Process each episode
    client = OpenAI()
    n_done, n_skipped, n_failed = 0, 0, 0

    for i, ep in enumerate(episodes, 1):
        slug = slugify(ep["title"])
        txt_path = transcript_dir / f"{args.filename_prefix}{slug}.txt"
        mp3_path = audio_dir / f"{slug}.mp3"
        compressed_path = audio_dir / f"{slug}.compressed.mp3"

        header = f"[{i}/{len(episodes)}] {ep['title'][:65]}"

        # Manifest-based dedup — preferred since it survives file moves / rename.
        ep_key = _episode_key(ep)
        if not args.force and ep_key in manifest:
            print(f"{header} — already in manifest, skip")
            n_skipped += 1
            continue

        # Secondary dedup: if transcript file exists on disk but isn't in the
        # manifest (e.g. manifest got deleted), reinstate it in the manifest
        # without re-downloading.
        if txt_path.exists():
            print(f"{header} — transcript file already on disk, recording in manifest")
            manifest[ep_key] = {
                "title": ep["title"],
                "published": ep["published"],
                "guid": ep.get("guid", ""),
                "url": ep["url"],
                "slug": slug,
                "transcript_path": str(txt_path.relative_to(here)),
                "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": "reconciled from existing file",
            }
            _save_manifest(manifest_path, manifest)
            n_skipped += 1
            continue

        # Download if needed
        if not mp3_path.exists() or mp3_path.stat().st_size < 1000:
            print(f"{header}\n    ↓ downloading...")
            if not download_mp3(ep["url"], mp3_path):
                n_failed += 1
                continue
            time.sleep(0.5)  # be polite to the host

        # Compress for Whisper (always — even small files are fine)
        if not compressed_path.exists():
            print(f"    🗜  compressing to 32k mono for Whisper...")
            if not compress_for_whisper(mp3_path, compressed_path):
                n_failed += 1
                continue
        size_mb = compressed_path.stat().st_size / 1024 / 1024
        if size_mb > 24:
            print(f"    ✗ compressed file still {size_mb:.1f} MB — try a longer episode-splitting approach")
            n_failed += 1
            continue

        # Transcribe
        print(f"    🎙  transcribing ({size_mb:.1f} MB compressed)...")
        text = transcribe(compressed_path, client)
        if not text:
            n_failed += 1
            continue

        # Save with a metadata header so RAG retrieval can cite episode + date
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"# {ep['title']}\n")
            if ep["published"]:
                f.write(f"Published: {ep['published']}\n")
            f.write(f"Source: {args.podcast} Podcast (Stan The Annuity Man)\n")
            f.write(f"Audio URL: {ep['url']}\n\n")
            f.write(text.strip())
            f.write("\n")

        # Record in manifest so subsequent runs skip this one
        manifest[ep_key] = {
            "title": ep["title"],
            "published": ep["published"],
            "guid": ep.get("guid", ""),
            "url": ep["url"],
            "slug": slug,
            "transcript_path": str(txt_path.relative_to(here)),
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_manifest(manifest_path, manifest)

        n_done += 1
        print(f"    ✓ saved → {txt_path.relative_to(here)}")

        # Optional cleanup
        if args.delete_audio:
            mp3_path.unlink(missing_ok=True)
            compressed_path.unlink(missing_ok=True)

    # 4. Summary
    print(f"\n══════════════════════════════════════")
    print(f"  Done.   transcribed: {n_done}")
    print(f"          skipped:     {n_skipped}")
    print(f"          failed:      {n_failed}")
    print(f"  Transcripts in: {transcript_dir}")
    print(f"\nNext step: re-run the ChromaDB ingestion to index the new transcripts:")
    print(f"  cd {here}")
    print(f"  source venv/bin/activate")
    print(f"  python scripts/ingest.py")
    print(f"\nThen restart uvicorn (Ctrl+C, bash run.sh). New episodes will be available to the advisor immediately.")


if __name__ == "__main__":
    main()
