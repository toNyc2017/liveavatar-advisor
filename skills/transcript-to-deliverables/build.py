#!/usr/bin/env python
"""Transcript → deliverables orchestrator.

Run from the project root with the venv activated:

    python skills/transcript-to-deliverables/build.py \\
        --transcript annuity_docs/stan_youtube_<slug>.txt

Produces three files in deliverables/<slug>/:
    - analysis.json
    - email_summary.md
    - slides.pptx
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Make sibling files (prompts.py, render_pptx.py) importable when run directly.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from prompts import analysis_prompt, email_prompt  # noqa: E402
from render_pptx import render_pptx  # noqa: E402

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_METADATA_HEADER_RE = re.compile(r"^\s*[#=-]{3,}\s*$")


def _slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text[:80] or "deliverable"


def _read_transcript(path: Path) -> tuple[str, dict]:
    """Return (transcript_body, source_metadata).

    Many of our transcripts start with a metadata header block — a few lines
    of "Title:", "URL:", "Published:", etc., then a separator, then the
    actual transcript. We strip the header into a source dict and return
    just the body to the LLM (saves tokens, sharpens analysis).
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    source: dict = {}
    body_start = 0
    # Heuristic: scan the first ~40 lines for "Key: value" pairs followed by
    # a separator line. If we hit a separator, everything after is body.
    for i, line in enumerate(lines[:40]):
        if _METADATA_HEADER_RE.match(line):
            body_start = i + 1
            continue
        m = re.match(r"^([A-Za-z][\w \-]{1,30})\s*:\s*(.+)$", line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            source[key] = m.group(2).strip()
        elif line.strip() == "":
            continue
        else:
            # First real prose line — assume header is over.
            if source or body_start:
                body_start = i
            break

    body = "\n".join(lines[body_start:]).strip() or raw.strip()
    return body, source


def _truncate_for_llm(text: str, max_chars: int = 80_000) -> str:
    if len(text) <= max_chars:
        return text
    # Keep head and tail so we don't lose the conclusion.
    head = text[: int(max_chars * 0.65)]
    tail = text[-int(max_chars * 0.30):]
    return head + "\n\n[... transcript truncated ...]\n\n" + tail


def _openai_client():
    from openai import OpenAI
    return OpenAI()


def _call_llm_json(client, model: str, messages: list[dict]) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


def _call_llm_text(client, model: str, messages: list[dict]) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcript → analysis + email + slides")
    parser.add_argument("--transcript", required=True, type=Path, help="Path to input .txt")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Where to write the three outputs (default: deliverables/<slug>/)")
    parser.add_argument("--num-slides", type=int, default=5,
                        help="Approximate target slide count (default: 5)")
    parser.add_argument("--audience", default="executive",
                        choices=["executive", "board", "client", "internal"],
                        help="Persona for tone (default: executive)")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-5.4"),
                        help="LLM used for analysis + summary (default: gpt-5.4)")
    parser.add_argument("--filename-prefix", default=None,
                        help="Prefix applied to all three output filenames. Default: "
                             "derived from the transcript stem so the deliverables sit "
                             "alongside the transcript as <stem>_analysis.json, "
                             "<stem>_email_summary.md, <stem>_slides.pptx. Pass an "
                             "empty string ('') to keep the legacy flat names.")
    args = parser.parse_args(argv)

    transcript_path: Path = args.transcript
    if not transcript_path.exists():
        print(f"ERROR: transcript not found: {transcript_path}", file=sys.stderr)
        return 1

    body, source = _read_transcript(transcript_path)
    if not body.strip():
        print(f"ERROR: transcript is empty after metadata strip: {transcript_path}", file=sys.stderr)
        return 1

    print(f"Read transcript ({len(body):,} chars) from {transcript_path}")
    if source:
        print(f"  Source metadata: { {k: source[k] for k in list(source)[:3]} }{'…' if len(source) > 3 else ''}")

    truncated = _truncate_for_llm(body)

    # Output dir
    slug = _slugify(transcript_path.stem)
    out_dir: Path = args.output_dir or (Path("deliverables") / slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing outputs to {out_dir}/")

    # Filename prefix: default is derived from the transcript stem so deliverables
    # for many episodes can live in the same folder without overwriting each other.
    if args.filename_prefix is None:
        prefix = f"{transcript_path.stem}_"
    else:
        prefix = args.filename_prefix
    if prefix:
        print(f"  filename prefix: {prefix!r}")

    client = _openai_client()

    # 1) Analysis JSON
    print(f"  [1/3] Analysis with {args.model}...")
    analysis = _call_llm_json(
        client,
        args.model,
        analysis_prompt(truncated, args.audience, args.num_slides),
    )
    # Attach source metadata for the title slide and for downstream reference.
    if source:
        analysis.setdefault("source", source)

    analysis_path = out_dir / f"{prefix}analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"        wrote {analysis_path}")

    # 2) Email summary
    print(f"  [2/3] Email summary with {args.model}...")
    email_md = _call_llm_text(
        client,
        args.model,
        email_prompt(analysis, truncated, args.audience),
    )
    email_path = out_dir / f"{prefix}email_summary.md"
    email_path.write_text(email_md + "\n", encoding="utf-8")
    print(f"        wrote {email_path}")

    # 3) Slides
    print(f"  [3/3] Slides via python-pptx...")
    slides_path = out_dir / f"{prefix}slides.pptx"
    render_pptx(analysis, slides_path, source=analysis.get("source") or source)
    print(f"        wrote {slides_path}")

    print()
    print("Done.")
    print(f"  analysis : {analysis_path}")
    print(f"  email    : {email_path}")
    print(f"  slides   : {slides_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
