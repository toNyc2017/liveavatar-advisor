# Setup instructions for openclaw — make the video pipeline phone-callable

**Audience:** the openclaw agent on Tom's MacBook.
**Goal:** let Tom invoke the `transcript-to-deliverables` pipeline from a
short natural-language message (typed from his phone), without him
having to type any paths or flags.

When Tom hands you this document, do the two setup steps in **Section 2**,
then confirm with Tom using the verification in **Section 3**.

---

## 1. Context

Tom has a Python skill called `transcript-to-deliverables` at:

```
~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/
```

It takes a YouTube URL and produces four files (transcript, analysis
JSON, executive email summary, and a styled `.pptx` deck) into a folder
auto-derived from the channel name (e.g. `'Nate B Jones'` → `natebjones/`
under the project root).

The whole pipeline runs from a single command:

```bash
cd ~/Documents/liveavatar-advisor && venv/bin/python skills/transcript-to-deliverables/from_url.py '<URL>'
```

Note the `venv/bin/python` form — it skips the `source venv/bin/activate`
step but still uses the venv's Python, which is what makes `openai`,
`python-pptx`, `yt-dlp`, etc. resolvable. This matters when an agent
runs the command, because subprocess invocations don't always inherit
activated environment state cleanly.

The script accepts a few optional overrides:

- `--folder NAME` — override the auto-derived folder slug
- `--audience executive|board|client|internal` — tone of the email + analysis
- `--num-slides N` — approximate target slide count (default 5)

Re-running with the same URL is safe — the fetch step dedupes by
`video_id` via a per-folder `.manifest.json`, so subsequent runs skip
the YouTube round-trip and just regenerate the deliverables. Useful
when iterating.

---

## 2. What to set up

### 2a. Create a Cowork skill that responds to natural-language triggers

Create this file (mkdir the folder first):

```
~/.openclaw/workspace/skills/process-video/SKILL.md
```

with exactly this content:

````markdown
---
name: process-video
description: "Take a YouTube URL and produce a transcript, analysis JSON, executive email summary, and a styled PowerPoint deck. Trigger whenever Tom shares a YouTube URL with intent to summarize or visualize it — phrases like 'process this video', 'process this URL', 'make slides for', 'summarize this youtube video', 'run the video pipeline', 'turn this into a deck', or a bare YouTube URL plus clear intent. Works from desktop or phone."
---

# Process a YouTube video into transcript + email + slide deck

When Tom shares a YouTube URL and asks to process / summarize / make
slides for / turn into a deck — or any natural variant — run the
`transcript-to-deliverables` pipeline.

## The command

```bash
cd ~/Documents/liveavatar-advisor && venv/bin/python skills/transcript-to-deliverables/from_url.py '<URL>'
```

Substitute Tom's URL into the `<URL>` placeholder. Always quote the URL
in single quotes so the `?v=...&t=...` query params don't get parsed by
the shell.

The script:

- Probes yt-dlp for the channel name + title
- Derives a folder slug from the channel (e.g. 'Nate B Jones' →
  `natebjones/` under the project root)
- Fetches the transcript (free YouTube auto-captions preferred,
  Whisper fallback if captions absent)
- Calls the analysis + email + slides build
- Reports the four resulting filenames + sizes

## After it finishes

1. Print the wrapper's final file list (which the wrapper outputs at
   the end).
2. From the new `analysis.json`, print just the `headline` plus
   `visual.kind` for each concept so Tom can scan the visual choices
   at a glance.
3. Tell Tom which folder the wrapper picked — so he knows where on the
   filesystem to look.

## Overrides Tom might request

Add the appropriate flag to the command if he asks:

| What Tom says | Add to the command |
|---|---|
| "into folder X" / "put it in X/" | `--folder X` |
| "for clients" / "client-facing" | `--audience client` |
| "for the board" | `--audience board` |
| "internal version" / "casual" | `--audience internal` |
| "longer deck" / "more slides" | `--num-slides 7` |
| "shorter deck" / "fewer slides" | `--num-slides 4` |

If he doesn't specify, run the bare command with no overrides — the
defaults are tuned for an executive audience and ~5 slides.

## When NOT to trigger

- Tom shares a YouTube URL but asks a different question about it
  ("who's the host of this channel?", "what's the title of this
  video?") — answer that question, do not run the pipeline.
- Tom shares a non-YouTube URL — explain that the pipeline is
  YouTube-specific.

## If something fails

The wrapper writes the failure reason to stderr. Pass it back to Tom
verbatim; do not try to retry or work around it without checking with
him first. Common causes:

- yt-dlp can't reach YouTube (rare on his Mac, common in sandboxed
  environments)
- the video has no auto-captions and Whisper isn't reachable (out of
  quota, no API key)
- the python-pptx render fails on a malformed analysis JSON (this
  shouldn't happen — the script is defensive — but if it does, the
  partial analysis.json + email_summary.md will still be on disk)
````

### 2b. (Optional but nice) Add a shell alias

For when Tom is at a real terminal and wants the command to be one word
plus a URL. Append this line to `~/.zshrc`:

```bash
alias vid='cd ~/Documents/liveavatar-advisor && venv/bin/python skills/transcript-to-deliverables/from_url.py'
```

Then `source ~/.zshrc` once, or open a new terminal. From then on:

```bash
vid 'https://www.youtube.com/watch?v=...'
```

does the same thing.

---

## 3. Verification — please run these before reporting "done"

1. **Confirm the skill file exists.** Run `ls ~/.openclaw/workspace/skills/process-video/SKILL.md` and confirm.

2. **Confirm the wrapper script is reachable.** Run:
   ```bash
   ls ~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/from_url.py
   ~/Documents/liveavatar-advisor/venv/bin/python --version
   ```
   Both should succeed.

3. **Dry-run the trigger language in your own head.** Read the
   `description` field in the new SKILL.md and confirm it would
   trigger on each of these phone-typed phrasings:
   - "process this video: https://www.youtube.com/watch?v=XXXX"
   - "make slides for https://youtu.be/XXXX"
   - "summarize this youtube video: https://www.youtube.com/watch?v=XXXX"
   - "https://www.youtube.com/watch?v=XXXX — turn this into a deck"

   If any of those feel ambiguous, tighten the description before
   reporting back.

4. **Report back to Tom.** Tell him:
   - The skill is installed at the path above.
   - The shell alias is (or is not) installed.
   - One example trigger phrase he can paste from his phone to test.

---

## 4. Reference — what the underlying skill actually does

If you want to inspect or modify the pipeline itself, the source files are:

| File | Purpose |
|---|---|
| `~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/SKILL.md` | Skill design + full usage docs |
| `~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/from_url.py` | One-shot URL entry point (this is what gets called) |
| `~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/build.py` | Orchestrates analysis → email → slides |
| `~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/prompts.py` | LLM prompts for analysis + email |
| `~/Documents/liveavatar-advisor/skills/transcript-to-deliverables/render_pptx.py` | python-pptx slide renderer + visual drawers |
| `~/Documents/liveavatar-advisor/scripts/fetch_youtube_episodes.py` | yt-dlp + caption + Whisper-fallback fetch |

Do NOT modify any of these as part of this setup — that's project work,
not skill installation. If Tom asks to change behavior (different
default audience, different number of slides, different palette,
different output format), check with him before touching the project
files.
