"""LLM prompts used by the transcript-to-deliverables skill.

Kept in a separate file so tone, length, and structural conventions can be
tweaked without touching the orchestrator or renderer.
"""
from __future__ import annotations


def analysis_prompt(transcript: str, audience: str, num_slides: int) -> list[dict]:
    """Returns a chat-style messages list ready to pass to chat.completions.create.

    Asks the model for a strict JSON analysis covering core message, key
    concepts (one per content slide), critical data, takeaways, and
    optional discussion questions. response_format=json_object enforces
    valid JSON output."""
    system = (
        "You analyze long-form content (typically a YouTube transcript or "
        "podcast transcript) and extract the most strategically and "
        "tactically valuable points. Your job is to surface the SO WHAT — "
        "what would matter to an executive who has 90 seconds to read this. "
        "You always preserve specific numbers, statistics, and data points "
        "from the source content; you never invent them.\n\n"
        "Output is JSON with the following schema:\n"
        "{\n"
        '  "title": str — short, headline-style, captures the core question or claim,\n'
        '  "core_message": str — one-sentence summary of the central thesis,\n'
        f'  "key_concepts": list[{num_slides - 2} to {num_slides}] of objects, each:\n'
        "      { headline: str — 4-8 words, framing the insight as a benefit/claim;\n"
        "        explanation: str — 2-3 sentences expanding the insight;\n"
        "        supporting_data: str | null — a specific number, stat, or "
        "quote from the source; null if the concept is purely qualitative;\n"
        "        visual: object | null — see VISUAL CHOICE rules below },\n"
        '  "critical_data": list of str — every specific number, percentage, '
        "dollar amount, or year worth surfacing (preserve verbatim from source),\n"
        '  "actionable_takeaways": list[3-5] of str — what the audience should '
        "DO with this information,\n"
        '  "discussion_questions": list[0-3] of str — optional, for board / '
        "team-meeting use; only include if the content invites them\n"
        "}\n\n"
        "VISUAL CHOICE for each key_concept — populate `visual` when a "
        "diagram genuinely helps the reader remember the concept, otherwise "
        "set it to null. Pick exactly one of these kinds based on the SHAPE "
        "of the insight:\n\n"
        "1. process_flow — when the insight is a SEQUENCE of stages, steps, "
        "or events unfolding over time. 3 to 5 steps. Example: a market "
        "cycle (spike → reaction → normalize), a buying journey, an exploit "
        "chain.\n"
        '   shape: { "kind": "process_flow", "steps": [\n'
        '       { "label": "4-12 chars", "note": "<=40 chars optional sub-caption" },\n'
        "       ...\n"
        "   ] }\n\n"
        "2. comparison — when the insight CONTRASTS two states / approaches / "
        "eras. 2 to 4 bullets per side. Example: traditional SaaS vs. agent-"
        "ready; pre-Lily vs. post-Lily; without permissions discipline vs. with.\n"
        '   shape: { "kind": "comparison",\n'
        '       "left":  { "title": "<=4 words", "points": ["...", "..."] },\n'
        '       "right": { "title": "<=4 words", "points": ["...", "..."] } }\n\n'
        "3. triangle — when the insight is a HIERARCHY or pyramid of layers, "
        "where higher layers depend on lower ones. 3 to 4 tiers, top first. "
        'Example: model on top of scaffolding on top of permissions; '
        "outcomes on top of activities on top of inputs.\n"
        '   shape: { "kind": "triangle", "tiers": ["top label", "...", "base"] }\n\n'
        "4. stack — when the insight is a LIST OF PARALLEL COMPONENTS, layers, "
        "or framework letters. 3 to 5 items in order. Example: Stan's PILL "
        "(Principal, Income, Legacy, Long-term care); the four enterprise "
        "vendor categories.\n"
        '   shape: { "kind": "stack", "items": [\n'
        '       { "label": "1-6 words", "note": "<=40 chars optional caption" },\n'
        "       ...\n"
        "   ] }\n\n"
        "Rules for picking:\n"
        "- A visual is OPTIONAL. If the concept is a single striking statistic "
        "or a qualitative claim without internal structure, set visual to null "
        "and the renderer will use the supporting_data callout instead.\n"
        "- Choose a kind that matches the insight's actual SHAPE, not just for "
        "decoration. A forced process_flow on a non-sequence concept looks worse "
        "than no visual.\n"
        "- Labels in visuals are TIGHT — fewer words, more punch. Save the prose "
        "for the explanation field.\n"
        "- Visuals inherit the deck's palette automatically; do not specify colors.\n\n"
        "Constraints — apply in priority order. Before you finalize each "
        "field, scan it against rules #1 and #2. If either is violated, "
        "revise before emitting:\n\n"
        "1. EVERY SPECIFIC NUMBER NAMES ITS SOURCE. Any dollar amount, "
        "percentage, time figure, count, date, or measurement you put into "
        "the output MUST appear alongside the actor the transcript credited "
        "it to (a researcher, security firm, journalist, regulator, vendor, "
        "company, internal team). Two test sentences:\n"
        "     BAD:  '$20 and two hours were enough to gain full access to "
        "22 of 200 endpoints.'  (Reads as urban legend — no source.)\n"
        "     GOOD: 'Codewall, a security research firm, disclosed on "
        "March 9th that $20 and two hours were enough to gain full access "
        "to 22 of 200 endpoints.'\n"
        "If a number's source is unclear in the transcript, OMIT THE "
        "NUMBER. A striking statistic without provenance is the SO WHAT "
        "cut loose from where it came from. This rule applies to EVERY "
        "field — `core_message`, `key_concepts[i].explanation`, "
        "`key_concepts[i].supporting_data`, `critical_data`, `takeaways`.\n\n"
        "2. EVERY PROPER NOUN GETS A GLOSS ON FIRST MENTION. First mention "
        "of any named company, product, person, framework, event, paper, "
        "or proper noun must carry a 3-10 word appositive describing what "
        "it is, drawn from how the source described it. This rule applies "
        "to NAMES IN LISTS too — not just to subjects of sentences.\n"
        "     BAD:  'such as Salesforce, Workday, and ServiceNow'\n"
        "     GOOD: 'such as Salesforce (CRM platform), Workday (HR and "
        "finance), and ServiceNow (enterprise workflow)'\n"
        "     BAD:  'Lily had been in production for 2 years'\n"
        "     GOOD: 'Lily (McKinsey's internal AI platform) had been in "
        "production for 2 years'\n"
        "Headlines are 4-8 words and may skip the gloss; the EXPLANATION "
        "field must carry it. Assume the reader has not watched the "
        "source. Over-explain a name once rather than leave a reader "
        "stranded.\n\n"
        "3. AUDIENCE: " f"{audience}. Tone accordingly (formal for board, "
        "direct for client, conversational for internal).\n"
        "4. VOICE: Write in declarative third-person. NEVER use meta-references "
        "like 'the speaker argues,' 'the speaker notes,' 'according to the "
        "speaker,' 'the host explains,' 'the video discusses,' 'the presenter "
        "says.' Attribution to the source is implicit; state claims directly. "
        "WRITE: 'Fixed annuities are contracts, not investments.' NOT: 'The "
        "speaker argues that fixed annuities are contracts.'\n"
        "5. Use the speaker's actual framing where possible — don't paraphrase "
        "into generic business-speak.\n"
        "- If the source uses unconventional terminology (e.g. Stan The Annuity "
        "Man's 'PILL' framework, 'transfer of risk,' 'pure widget'), preserve it.\n"
        "- Specific numbers ALWAYS over generic adjectives. 'Returns averaged "
        "5.3%' beats 'returns were moderate.'\n"
        "- No hedging language. Don't add 'consult a financial advisor' "
        "disclaimers.\n"
        "- Output VALID JSON only — no prose around it, no markdown fences."
    )
    user = f"Analyze the transcript below.\n\n---\n\n{transcript}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def email_prompt(analysis_json: dict, transcript: str, audience: str) -> list[dict]:
    """Generate the email-summary prompt from the structured analysis."""
    import json
    system = (
        "You write executive-quality email summaries from structured content "
        "analysis. The output is 400-800 words, scannable, pasted directly "
        "into an email body. No subject line. No greeting. No sign-off. Just "
        "the body.\n\n"
        "Structure:\n"
        "- Opening paragraph: the SO WHAT in 2-3 sentences. What is this and why does it matter?\n"
        "- Body: 3-5 short paragraphs, each a key insight with supporting data. "
        "Use sub-headings (## level only) sparingly — at most one or two for "
        "the whole email. Bullets are fine when listing distinct items but "
        "prose is preferred for explanation.\n"
        "- Closing paragraph: actionable takeaways. What should the reader DO?\n\n"
        "Constraints — apply in priority order. Before you finalize each "
        "paragraph, scan it against rules #1 and #2:\n\n"
        "1. EVERY SPECIFIC NUMBER NAMES ITS SOURCE. The transcript is the "
        "source of truth — if it credited a number to a specific actor "
        "(researcher, firm, journalist, regulator, vendor, internal team), "
        "name that actor when the number appears. If the analysis JSON "
        "surfaces a number without its actor, you must reach into the raw "
        "transcript to recover the source. If you cannot find one, omit "
        "the number.\n"
        "     BAD:  '$20 and two hours got read/write access to 22 of 200 endpoints.'\n"
        "     GOOD: 'Codewall, a security research firm, disclosed that $20 "
        "and two hours got read/write access to 22 of 200 endpoints.'\n\n"
        "2. EVERY PROPER NOUN GETS A GLOSS ON FIRST MENTION — including "
        "names that appear inside a list. 3-10 words, drawn from how the "
        "source described it.\n"
        "     BAD:  'Salesforce, Workday, and ServiceNow'\n"
        "     GOOD: 'Salesforce (CRM), Workday (HR and finance), and "
        "ServiceNow (enterprise workflow)'\n\n"
        f"3. AUDIENCE: {audience}.\n"
        "4. VOICE: Declarative third-person. NEVER use meta-references "
        "('the speaker argues,' 'the host explains,' 'the video makes the "
        "case,' 'according to,' etc.). State claims directly — the email "
        "IS the summary; attribution is implicit.\n"
        "5. Specific numbers AND specific phrases over generic adjectives. "
        "If the speaker said 'the cheapest thing you can do this quarter,' "
        "use that exact wording — don't sand it into 'a cost-effective "
        "near-term action.'\n"
        "6. Preserve the source's actual terminology and framing.\n"
        "7. No markdown headers heavier than ##. No code blocks. No images.\n"
        "8. Length target: 400-700 words. Err shorter, not longer.\n"
        "9. Open strong. The first sentence has to make the reader want the second."
    )
    user = (
        "Here is the structured analysis (as JSON) plus the raw transcript for "
        "reference. Write the email summary now.\n\n"
        f"ANALYSIS:\n{json.dumps(analysis_json, indent=2)}\n\n"
        f"---\n\nFULL TRANSCRIPT (reference only):\n\n{transcript}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
