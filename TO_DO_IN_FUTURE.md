# TO DO IN FUTURE

Running list of work identified as worth doing but not done yet. Add
freely — by Tom or by Claude during a session. **Remove items when
they're implemented.** Git history is the archive; this file stays
current.

This file is indexed in `CLAUDE.md` so any future Claude session
working in this folder will discover it automatically.

---

## Open items

### Finish the App Runner deploy (manual steps remaining)

**Identified:** 2026-06-13. Scaffolding committed; the rest is keys
and clicks — see `infra/README.md` for the exact commands.

What's still to do, in order:

1. `aws ssm put-parameter` the four runtime secrets under
   `/liveavatar-advisor/` (OPENAI, LIVEAVATAR, ELEVENLABS, optionally
   DEEPGRAM).
2. Deploy `infra/bootstrap-oidc.yaml` once per AWS account.
3. Deploy `infra/apprunner.yaml`. Service will sit waiting for an
   image — that's fine.
4. Set the five GitHub repo Variables (`AWS_REGION`, `AWS_ACCOUNT_ID`,
   `AWS_DEPLOY_ROLE_ARN`, `ECR_REPO_NAME`, `APPRUNNER_SERVICE_ARN`).
5. `./scripts/seed_chroma_s3.sh` to upload the ChromaDB tarball to the
   seed bucket.
6. Push to `main` → first deploy runs end-to-end. The workflow waits
   for `/health` to return 200 before turning green.

Once `https://<id>.us-east-1.awsapprunner.com/health` is RUNNING, ngrok
goes away and the rest of `TO_DO_IN_FUTURE.md` becomes the priority.

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

- 2026-06-13 — Production deploy scaffolding for AWS App Runner. New
  `storage.py` abstracts USER.md/MEMORY.md/digest I/O behind a
  `UserStorage` interface (local-disk in dev, S3 in prod). New
  `_ensure_chroma_db_present()` hydrates ChromaDB from a seed tarball
  at cold start. Added `Dockerfile`, `.dockerignore`,
  `requirements-runtime.txt`, `infra/bootstrap-oidc.yaml`,
  `infra/apprunner.yaml`, `infra/README.md`,
  `.github/workflows/deploy-backend.yml`, and
  `scripts/seed_chroma_s3.sh`. Auth via GitHub OIDC, secrets via SSM
  Parameter Store, image tags `:latest` + `:sha-XXXX` for rollback.
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
