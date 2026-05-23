#!/usr/bin/env python
"""One-shot: YouTube URL → transcript + analysis + email + slides.

The full pipeline in one command. Folder name is auto-derived from the
channel name; everything else is deduced from yt-dlp metadata.

Usage:
    python skills/transcript-to-deliverables/from_url.py URL
    python skills/transcript-to-deliverables/from_url.py URL --folder moonshots
    python skills/transcript-to-deliverables/from_url.py URL --audience client --num-slides 7

Examples:
    # Auto: channel 'Nate B Jones' → folder 'natebjones'
    python skills/transcript-to-deliverables/from_url.py 'https://www.youtube.com/watch?v=...'

    # Explicit folder (when the auto-derived slug isn't what you want):
    python skills/transcript-to-deliverables/from_url.py 'https://www.youtube.com/watch?v=...' \\
        --folder moonshots
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

try:
    import yt_dlp  # type: ignore
except ImportError:
    sys.exit(
        "yt-dlp not installed. Run:\n"
        "    pip install yt-dlp\n"
        "or activate the project venv first."
    )

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FETCH_SCRIPT = _PROJECT_ROOT / "scripts" / "fetch_youtube_episodes.py"
_BUILD_SCRIPT = _PROJECT_ROOT / "skills" / "transcript-to-deliverables" / "build.py"


# Common channel suffixes that add noise to a folder slug. Stripped before
# slugifying so "Moonshots Podcast" becomes "moonshots" rather than
# "moonshotspodcast".
_NOISE_WORDS = re.compile(
    r"\b(podcast|channel|official|tv|show|series|network|media)\b",
    re.IGNORECASE,
)


def slugify_folder(channel_name: str) -> str:
    """Convert a channel name to a short lowercase folder slug.

    Examples:
        'Nate B Jones'              -> 'natebjones'
        'Moonshots Podcast'         -> 'moonshots'
        'Stan The Annuity Man'      -> 'stantheannuityman'
        'Peter Diamandis - Moonshots' -> 'peterdiamandismoonshots'
    """
    cleaned = _NOISE_WORDS.sub("", channel_name)
    s = re.sub(r"[^A-Za-z0-9]+", "", cleaned).lower()
    return s or "videos"


def probe_metadata(url: str) -> dict:
    """Quick yt-dlp metadata probe — no audio download, no captions yet."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--folder", default=None,
        help="Override the auto-derived folder name. By default the channel "
             "name is slugified (e.g. 'Nate B Jones' → 'natebjones').",
    )
    parser.add_argument(
        "--audience", default="executive",
        choices=["executive", "board", "client", "internal"],
        help="Persona for tone (default: executive).",
    )
    parser.add_argument(
        "--num-slides", type=int, default=5,
        help="Approximate target slide count (default: 5).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Override LLM model for analysis + summary (default: env LLM_MODEL).",
    )
    args = parser.parse_args(argv)

    # ── Probe ──
    print(f"Probing {args.url} ...")
    try:
        info = probe_metadata(args.url)
    except Exception as e:
        sys.exit(f"yt-dlp probe failed: {e}")

    channel = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or "videos"
    )
    title = info.get("title") or "(untitled)"
    upload_date = info.get("upload_date", "")
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    folder_name = args.folder or slugify_folder(channel)
    folder_path = _PROJECT_ROOT / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    print(f"  channel: {channel}")
    print(f"  title:   {title}")
    if upload_date:
        print(f"  date:    {upload_date}")
    suffix = "" if args.folder else "  (override with --folder NAME)"
    print(f"  folder:  {folder_name}{suffix}")
    print()

    # ── Step 1: fetch transcript ──
    print("[1/2] Fetching transcript ...")
    fetch_cmd = [
        sys.executable, str(_FETCH_SCRIPT),
        "--url", args.url,
        "--transcript-dir", str(folder_path),
        "--filename-prefix", "",
        "--podcast", channel,
        "--manifest", str(folder_path / ".manifest.json"),
        "--audio-dir", str(folder_path / "_audio"),
    ]
    r = subprocess.run(fetch_cmd)
    if r.returncode != 0:
        print(f"fetch step exited {r.returncode}", file=sys.stderr)
        return r.returncode

    # Locate the transcript that just appeared (or that already existed
    # for this URL — the manifest dedupes by video_id). Most-recently-
    # modified .txt in the folder wins.
    candidates = sorted(
        [p for p in folder_path.glob("*.txt") if not p.name.startswith(".")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        sys.exit("ERROR: fetch finished but no transcript .txt found in target folder")
    transcript = candidates[0]
    print(f"  → transcript: {transcript.name}")
    print()

    # ── Step 2: build deliverables ──
    print("[2/2] Generating analysis + email + slides ...")
    build_cmd = [
        sys.executable, str(_BUILD_SCRIPT),
        "--transcript", str(transcript),
        "--output-dir", str(folder_path),
        "--audience", args.audience,
        "--num-slides", str(args.num_slides),
    ]
    if args.model:
        build_cmd += ["--model", args.model]

    r = subprocess.run(build_cmd)
    if r.returncode != 0:
        print(f"build step exited {r.returncode}", file=sys.stderr)
        return r.returncode

    # ── Report ──
    print()
    print(f"Done. Files in {folder_name}/:")
    stem = transcript.stem
    for ext in ("txt", "json", "md", "pptx"):
        for f in sorted(folder_path.glob(f"{stem}*.{ext}")):
            size = f.stat().st_size
            print(f"  {f.name}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
