# Project memory — LiveAvatar Advisor

This file is auto-loaded by Claude sessions working in this folder. It
is the discovery index for the project's canonical docs. Read it
first; it points you at everything else.

## Read these at the start of any non-trivial session

- **`SOUL.md`** — design intent and worldview for the avatar. The "why"
  behind every conversational behavior. When this drifts from runtime,
  SOUL wins.
- **`advisor_backend.py`** (specifically `SYSTEM_PROMPT`, near the top)
  — what the LLM actually sees at runtime. Must stay aligned with SOUL.
- **`SESSION_NOTES.md`** — current operational state of the project.
  Stack, what works, what's running.
- **`HANDOFF.md`** — protocol details, prior wrong turns, and the
  long-form picking-up-cold guide.
- **`MEMORY_SUBSYSTEM.md`** — how per-visitor memory (USER.md +
  MEMORY.md + session digests) works in production today (Phase 1).
- **`MEMORY_BACKEND_PROPOSAL.md`** — proposed Phase 2 memory backend.
- **`TO_DO_IN_FUTURE.md`** — running list of deferred work. **Add to
  this whenever you (or Tom) identify something worth doing later but
  choose not to do now. Remove items as they're implemented.** This
  is the index of "things we promised ourselves we'd remember."

## House conventions

- **SOUL beats SYSTEM_PROMPT.** When they drift, update the runtime
  prompt to match SOUL, not the other way around.
- **Pronunciation matters.** Everything the avatar says is read aloud
  by TTS. Write numbers, percentages, and acronyms the way they
  *sound*: "four oh one K" not "401(k)"; "five percent" not "5%";
  "I R A" not "IRA". The PRONUNCIATION section of `SYSTEM_PROMPT` is
  canonical.
- **Never sell a gap that doesn't exist.** If the client's guaranteed
  income already covers their essentials, the avatar says so plainly
  and pivots the conversation to legacy, taxes, or lifestyle. This is
  the integrity guardrail in SOUL's "organizing principle" section.
- **Memory is free-text today.** Durable client facts live in
  `users/<user_id>/USER.md` + `MEMORY.md`, both produced by LLM
  prompts (the digest in `advisor_backend.py`, the compiler in
  `scripts/compile_memory.py`). A structured store may come later
  (see `TO_DO_IN_FUTURE.md`); until then, the prompts themselves are
  where information loss is prevented.
