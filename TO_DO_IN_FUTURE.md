# TO DO IN FUTURE

Running list of work identified as worth doing but not done yet. Add
freely — by Tom or by Claude during a session. **Remove items when
they're implemented.** Git history is the archive; this file stays
current.

This file is indexed in `CLAUDE.md` so any future Claude session
working in this folder will discover it automatically.

---

## Open items

### Structured gap-summary store / tool

**Identified:** 2026-05-26 (needs-vs-wants worldview update).
**Status:** Not blocking conversation; becomes urgent when we build
the visualization artifact.

Today, the avatar's needs/wants split and guaranteed-income
breakdowns live as free-text bullets in `MEMORY.md`. The digest and
compile prompts now preserve the split, so the avatar can keep the
mental model coherent across sessions just from text.

For the conversation to drive a **visual** gap-analysis artifact (the
chart panel referenced in SOUL.md's "what this implies we still need
to build"), the avatar will need *structured* access to:

- `essential_monthly_spending`
- `discretionary_monthly_spending`
- `guaranteed_income_sources: [{ source, monthly_amount, start_age }]`
- `retirement_income_gap` (derived = essentials − total guaranteed)

Two implementation paths:

1. **New LLM tool `save_gap_facts`** — analogous to
   `save_client_profile`, writing a small JSON file (e.g.
   `users/<user_id>/gap.json`) alongside `USER.md`. More robust,
   structured from the start.
2. **`get_client_gap_summary` parser** — regex-parses `MEMORY.md` for
   the labeled lines the compiler now emits. Faster to ship, fragile
   to prompt drift.

Recommendation when we revisit: option 1.

### Gap-analysis visualization chart panel

**Source:** SOUL.md, "What this implies we still need to build."
**Original tag:** Task #67.

A chart panel similar to the SPIA calculator results panel, showing
monthly desired spending vs. expected income from Social Security /
pension / portfolio drawdown, with and without an annuity income
floor. The visual equivalent of the without/with contrast frame the
avatar already uses verbally.

Depends on the structured gap-summary store above.

### Discovery tracker

**Source:** SOUL.md, "What this implies we still need to build."

Some way to indicate to the avatar (and to Tom as supervisor) which
of the foundational facts have been gathered for a given client, and
which are still outstanding. Probably surfaces in `USER.md` as a
checklist that the avatar updates via tool calls.

### Risk-pause moments

**Source:** SOUL.md, "What this implies we still need to build."

Possibly a UI affordance — a small panel or sidebar callout — that
surfaces when the avatar names a specific risk (longevity, market,
sequence-of-returns, inflation, long-term care). Gives the client a
visual anchor for the risk being discussed rather than just hearing
it spoken.

---

## Done (recent, for context)

- 2026-05-26 — Voice and bearing pass: defined the avatar's voice
  recipe as Munger spine + Hanks acknowledgment + dry contextual wit
  inside a lively-but-corporate register. Added a Voice and bearing
  section to SOUL.md and rebuilt the top of SYSTEM_PROMPT with two
  before/after calibration examples. Loosened word target from 40 to
  50-65 to give rhythm room.
- 2026-05-26 — Wove "guaranteed income covers needs / investment
  income covers wants" worldview into SOUL.md and SYSTEM_PROMPT.
  Updated digest + compile prompts to preserve the needs/wants split
  in MEMORY.md.
- 2026-05-26 — Created this file and `CLAUDE.md` so future sessions
  discover the deferred-work list automatically.
