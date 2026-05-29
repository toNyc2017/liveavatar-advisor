#!/usr/bin/env python
"""Synthesize per-user MEMORY.md files from accumulated session digests.

Mirrors openclaw's `memory_compiler.py` pattern but scoped per-user. Reads
the raw session digests under `users/<user_id>/memory/*.md`, asks an LLM
to synthesize them into a single "what we know about this client" wiki
at `users/<user_id>/MEMORY.md`. The compiled file carries a "do not edit
manually" header — the raw memory layer is the writable side; the
compiled layer is regenerated from it.

See MEMORY_SUBSYSTEM.md for the full design.

Usage:
    # Recompile every user that has new digests since their last MEMORY.md
    python scripts/compile_memory.py

    # Recompile a specific user (e.g. for debugging)
    python scripts/compile_memory.py --user 7a3f...

    # Recompile ALL users regardless of whether anything changed
    python scripts/compile_memory.py --force

    # See what would be sent to the LLM without making the call
    python scripts/compile_memory.py --user 7a3f... --dry-run

Idempotent: safe to re-run. Each run rewrites users/<id>/MEMORY.md in
place. By default it skips users whose MEMORY.md is already newer than
every digest in their memory/ folder.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from openai import OpenAI
except ImportError:
    sys.exit("openai package not installed. Run: pip install openai")


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_DIR = _PROJECT_ROOT / "users"

LLM_MODEL_DEFAULT = os.getenv("LLM_MODEL", "gpt-5.4")

# Conservative cap on what we send to the LLM. gpt-5.4 supports much more,
# but we want predictable cost and latency. If a user has hundreds of
# digests, we truncate (keeping a head + the most recent tail) — the
# synthesis prompt is told to prioritize the most recent state when
# digests contradict.
MAX_INPUT_CHARS = 80_000  # ~20k tokens


_HEADER = """<!--
COMPILED ARTIFACT - DO NOT EDIT MANUALLY.

To change what this file says, add a new session digest under
  users/<user_id>/memory/
and re-run:
  python scripts/compile_memory.py --user <user_id>

For corrections that should override the synthesized facts (e.g. a name
the model spelled wrong), edit users/<user_id>/USER.md instead — the
backend reads USER.md alongside this file, and USER.md is the hand-
curated durable layer.
-->

"""


_SYNTHESIS_PROMPT = """You compile what a financial advisor (Tom Olds)
knows about a single client, drawn from chronologically dated session
digests. The output is a markdown wiki the advisor reads at the start
of each new conversation so they pick up where they left off.

Output EXACTLY this structure:

# Client memory

## Snapshot
2-3 sentences. Who is this person, what do they care about most, what
phase of life are they in. Aim for what a colleague would need to know
before walking into the next meeting cold.

## Profile
Bullet list of durable facts about them — age, family situation, work,
income / savings level when shared, accounts and holdings mentioned by
name and balance, stated risk tolerance, communication style and
preferences. Only what is explicitly in the digests; do not invent.
Prefer specifics: "$400k IRA at Fidelity, $180k 401(k) at current
employer" beats "significant retirement assets."

GAP-RELEVANT FACTS — when the digests contain any of these, record
them as separate, distinct lines. Never collapse essential and
discretionary spending into a single "spending" figure, and never
collapse guaranteed income sources into one number. The avatar's
core mental model is "Retirement Income Gap = Essential Expenses −
Guaranteed Income," and that math only works when these stay broken
out:
- Essential monthly spending (needs floor: housing, food, utilities,
  healthcare, insurance, transportation)
- Discretionary monthly spending (wants: travel, dining, hobbies,
  gifts, luxury)
- Guaranteed monthly income, by source (Social Security at age X,
  pension starting Y, existing annuity income, etc.)
- Retirement Income Gap, if computable from the above (state it
  explicitly as a dollar number per month, positive = unfilled gap,
  zero or negative = essentials already covered)

If the digests only ever recorded a lump-sum spending figure, keep it
as "total monthly spending ~$X" and surface "split essential vs.
discretionary" in Active questions.

## Conversation history
Chronological brief list, OLDEST FIRST. Each line:
  YYYY-MM-DD - topic - outcome
One line per session. If the same topic recurred, list each visit
separately.

## Active questions and outstanding decisions
What the client is currently trying to figure out, things they have not
decided yet, follow-ups they expect.

## Recommendations on file
What has been recommended or discussed as an option for them, and what
they have decided (accepted, rejected, deferred). One line each.

CONSTRAINTS:
- Voice: declarative third-person. NEVER write 'the client said,' 'in
  our last meeting,' 'the advisor explained.' State facts directly.
- Specifics over generalities, always.
- If a section truly has nothing to capture yet, write the heading and
  the single phrase "None on file yet."
- Total output under 600 words.
- When two digests contradict on the same fact (age changed, balance
  changed, decision changed), the MORE RECENT one wins.
- Do not add headings beyond ## level.
- No emojis. No code blocks. No tables.
"""


def _read_digests(user_dir: Path) -> tuple[list[Path], str]:
    """Return (paths_sorted_chronologically, concatenated_text)."""
    memory_dir = user_dir / "memory"
    if not memory_dir.exists():
        return [], ""
    paths = sorted(memory_dir.glob("*.md"))
    if not paths:
        return [], ""
    chunks = []
    for p in paths:
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            chunks.append(f"=== {p.name} ===\n{body}")
    return paths, "\n\n".join(chunks)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Keep the recent tail (most relevant) and a small head for context.
    head = text[: int(max_chars * 0.20)]
    tail = text[-int(max_chars * 0.75):]
    return head + "\n\n[... older digests truncated ...]\n\n" + tail


def _is_up_to_date(user_dir: Path, digest_paths: list[Path]) -> bool:
    """True if MEMORY.md is newer than every digest under memory/."""
    memory_path = user_dir / "MEMORY.md"
    if not memory_path.exists() or not digest_paths:
        return False
    mtime = memory_path.stat().st_mtime
    return all(p.stat().st_mtime <= mtime for p in digest_paths)


def compile_user(
    user_dir: Path,
    *,
    model: str,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """Returns one of: 'compiled', 'skipped_no_digests', 'skipped_up_to_date',
    'dry_ran', 'failed'."""
    paths, raw = _read_digests(user_dir)
    if not raw:
        return "skipped_no_digests"

    if not force and _is_up_to_date(user_dir, paths):
        return "skipped_up_to_date"

    raw = _truncate(raw, MAX_INPUT_CHARS)

    if dry_run:
        print(f"      [DRY RUN] {len(paths)} digest{'s' if len(paths) != 1 else ''}, "
              f"{len(raw):,} chars → would call {model}")
        print(f"      [DRY RUN] would write {user_dir / 'MEMORY.md'}")
        return "dry_ran"

    client = OpenAI()
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYNTHESIS_PROMPT},
                {"role": "user", "content": raw},
            ],
            temperature=0.2,
        )
        body = (completion.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"      ERROR: LLM call failed: {e}", file=sys.stderr)
        return "failed"

    if not body:
        print(f"      ERROR: LLM returned empty body", file=sys.stderr)
        return "failed"

    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        _HEADER
        + f"<!-- Generated {when} from {len(paths)} session "
          f"digest{'s' if len(paths) != 1 else ''}. -->\n\n"
    )
    out_path = user_dir / "MEMORY.md"
    out_path.write_text(header + body + "\n", encoding="utf-8")
    return "compiled"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--user", default=None,
        help="Compile only this user_id. Default: every user with digests.",
    )
    parser.add_argument(
        "--model", default=LLM_MODEL_DEFAULT,
        help=f"LLM model for synthesis (default: {LLM_MODEL_DEFAULT}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Recompile even if MEMORY.md is already newer than every digest.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without calling the LLM.",
    )
    args = parser.parse_args(argv)

    if not USERS_DIR.exists():
        print(f"No users/ directory at {USERS_DIR} — nothing to compile.")
        return 0

    if args.user:
        target = USERS_DIR / args.user
        if not target.exists():
            print(f"No such user folder: {target}", file=sys.stderr)
            return 1
        targets = [target]
    else:
        targets = [
            p for p in sorted(USERS_DIR.iterdir())
            if p.is_dir() and not p.name.startswith(".")
        ]

    if not targets:
        print(f"No user folders to compile in {USERS_DIR}")
        return 0

    flags = []
    if args.dry_run:
        flags.append("DRY RUN")
    if args.force:
        flags.append("FORCE")
    flag_str = f" ({', '.join(flags)})" if flags else ""

    print(f"Compiling {len(targets)} user folder{'s' if len(targets) != 1 else ''} "
          f"with {args.model}{flag_str}")
    print()

    counts = {"compiled": 0, "skipped_no_digests": 0,
              "skipped_up_to_date": 0, "dry_ran": 0, "failed": 0}
    for user_dir in targets:
        print(f"  {user_dir.name}:")
        result = compile_user(
            user_dir,
            model=args.model,
            dry_run=args.dry_run,
            force=args.force,
        )
        counts[result] += 1
        if result == "compiled":
            print(f"      → wrote {user_dir / 'MEMORY.md'}")
        elif result == "skipped_no_digests":
            print(f"      (no digests yet)")
        elif result == "skipped_up_to_date":
            print(f"      (MEMORY.md already up to date — use --force to recompile)")

    print()
    summary = [f"{n} {k}" for k, n in counts.items() if n]
    print(f"Done. {', '.join(summary) if summary else 'nothing to do'}.")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
