# Transcript → Deliverables Skill

Take a transcript (any `.txt` from `annuity_docs/` — Stan podcast, your own
content, anything ingested into the RAG) and produce three deliverables an
executive or client could actually use:

1. **`analysis.json`** — structured extraction of the core message, key
   insights, data/statistics, and recommended actions. Used as input to the
   other two deliverables; also useful standalone for note-taking.
2. **`email_summary.md`** — 1–2 page executive summary ready to paste into
   an email or Slack message. Opens with the "SO WHAT", three to five
   substantive bullets with explanations, the critical numbers, and clear
   actionable takeaways.
3. **`slides.pptx`** — a styled PowerPoint deck (typically 4–6 slides) with
   a real design language, not Pandoc-default bullet lists. Title slide,
   content slides with consistent typography and a financial-services-credible
   color palette, optional callout boxes for headline numbers.

## When to use it

- After ingesting a new Stan podcast video, generate a one-pager to share
  with colleagues to decide whether the content is worth a longer dive
- After a client meeting, turn the recording's transcript into prep notes
  + a follow-up email
- Distill any long-form annuity content (Wade Pfau papers, regulator notes,
  competitor positioning) into something a busy executive will actually read

## Why this is better than `pandoc`

`pandoc` Markdown-to-PPTX produces a deck that looks like a 2003 academic
handout — flat white slides, default Liberation Serif font, naked bullet
lists, no visual hierarchy. The output is functional but unembarrassing.

This skill instead uses `python-pptx` with deliberate design:
- Consistent typography (Helvetica/Calibri family, scaled by role)
- Three-color palette (navy primary, mid-gray secondary, single accent for
  callouts) — credible for financial services without being boring
- Content-aware layouts — slides with a single big number look different
  from slides with three insights, and slides with a structural insight
  get a shape-based diagram (see Visuals below)
- Title slide with source attribution and date
- Visible visual hierarchy: title → subtitle → bullets → supporting detail

## Visuals on concept slides

The analysis LLM picks an optional `visual` per concept from a small
library. The renderer draws it deterministically with python-pptx shapes
— no AI-generated raster images, no external assets — so every visual
inherits the deck palette automatically and stays consistent across runs.

Four kinds are supported, each chosen by the LLM based on the SHAPE of
the insight:

- **process_flow** — 3-5 labeled rounded-rectangle boxes connected by
  rust right-arrows. Used when the concept is a sequence (cycle,
  journey, exploit chain, market reaction).
- **comparison** — two columns side by side, each with a title + 2-4
  bullet points, separated by a hairline divider. Used for before/after,
  without/with, vendor-A-vs-B contrasts.
- **triangle** — pyramid with 3-4 tiers, narrowest at top. Used for
  hierarchies / dependency stacks where higher layers depend on lower.
- **stack** — horizontal row of N labeled blocks (alternating navy and
  gray, rust top-edge accents) for parallel-component frameworks like
  Stan's PILL (Principal, Income, Legacy, Long-term care).

A concept's `visual` may also be `null`. When it is, the slide falls
back to the existing navy callout box carrying the `supporting_data`
stat. When both `visual` and `supporting_data` are present, the visual
takes the lower half of the slide and the stat becomes a small
caption-row beneath the explanation ("KEY DATA · $20, two hours").

To retheme visuals, edit the STYLE dict at the top of `render_pptx.py`
— it controls the colors and fonts used for shape compositions as well
as for text.

## How to run

### One-shot from a YouTube URL (recommended)

```bash
source venv/bin/activate
python skills/transcript-to-deliverables/from_url.py 'https://www.youtube.com/watch?v=...'
```

That's the whole command. The wrapper probes yt-dlp for the channel
name + title, derives a folder slug from the channel (e.g.
`'Nate B Jones'` → `natebjones/`, `'Moonshots Podcast'` → `moonshots/`),
runs the fetch + the build in sequence, and reports the four resulting
files (`<date>_<slug>.txt`, `<date>_<slug>_analysis.json`,
`<date>_<slug>_email_summary.md`, `<date>_<slug>_slides.pptx`).

Re-running with the same URL is safe: the fetch step dedupes by
video_id via `<folder>/.manifest.json`, so only the build step does any
work on re-runs — useful when iterating on prompts.

Common overrides:

| Flag | Default | Description |
|------|---------|-------------|
| `--folder NAME` | derived from channel | Override the folder name. Use when the slugified channel isn't what you want (`--folder moonshots` instead of `peterdiamandismoonshots`). |
| `--audience LABEL` | `executive` | Persona for tone: `executive`, `board`, `client`, `internal`. |
| `--num-slides N` | `5` | Approximate target slide count. |
| `--model NAME` | env `LLM_MODEL` | LLM used for analysis + summary. |

### Lower-level: build deliverables from an existing transcript

When you already have a transcript on disk (from a different source, or
because you've edited one):

```bash
python skills/transcript-to-deliverables/build.py \
    --transcript natebjones/2026-05-15_some_episode.txt \
    --output-dir natebjones
```

By default deliverables sit alongside the transcript with the same
basename prefix:

| Flag | Default | Description |
|------|---------|-------------|
| `--transcript PATH` | (required) | Path to the input `.txt` |
| `--output-dir PATH` | `deliverables/<slug>/` | Where the three files land |
| `--filename-prefix STR` | derived from transcript stem | Prefix on the three output files. `''` for legacy flat names. |
| `--num-slides N` | `5` | Approximate target slide count. |
| `--audience LABEL` | `executive` | Persona for tone. |
| `--model NAME` | `gpt-5.4` | LLM used for analysis + summary generation. |

## Outputs in detail

### `analysis.json`

```json
{
  "title": "Are Fixed Annuities A Good Investment?",
  "core_message": "Fixed annuities aren't investments — they're contracts...",
  "key_concepts": [
    { "headline": "...", "explanation": "...", "supporting_data": "..." },
    ...
  ],
  "critical_data": [
    "12% of US households own at least one annuity",
    ...
  ],
  "actionable_takeaways": ["..."],
  "discussion_questions": ["..."],
  "source": { "title": "...", "url": "...", "published": "..." }
}
```

### `email_summary.md`

400–800 words, scannable, optimized for email body. Opens with the SO WHAT,
moves into key insights with brief explanations, surfaces critical data,
closes with takeaways. No markdown headers heavier than `##` so it pastes
cleanly.

### `slides.pptx`

4–6 slides typically, designed for an executive read-out:

- Slide 1: title + subtitle + source attribution + date
- Slides 2 to N–1: one concept per slide, with a headline, 2–4 supporting
  bullets, and (when present) a callout box highlighting the key data point
- Slide N: takeaways + (if asked) discussion questions

## Maintenance notes

- The renderer is in `render_pptx.py` and is intentionally lightweight —
  if you want to swap in a different color palette or font, edit the
  `STYLE` dict at the top.
- The LLM prompts are in `prompts.py` — tweak them to change tone, length,
  or what counts as "critical data."
- The script does NOT call the LiveAvatar / ElevenLabs / ChromaDB stack.
  This is a pure transcript → deliverables pipeline. It can be run on any
  machine with Python + the dependencies in `requirements.txt`.
