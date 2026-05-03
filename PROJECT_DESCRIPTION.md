# LiveAvatar Annuity Advisor — Project Description

**Document purpose:** Technical and strategic briefing for an LLM agent constructing a broader lead generation initiative description. Written to be machine-readable and comprehensive.

---

## What It Is

The LiveAvatar Annuity Advisor is a conversational AI system in which a photorealistic, lip-synced digital avatar acts as a knowledgeable annuity education assistant. A prospective client visits a web page, sees a human-looking advisor on screen, and holds a back-and-forth spoken conversation with it about annuities and retirement income planning. The avatar speaks in a natural voice, listens to follow-up questions, and responds with grounded, document-backed answers in real time.

It is not a chatbot with a text box. It is a video-based, voice-driven experience that closely approximates sitting across from a human financial educator.

---

## Technical Architecture

### Components

**Frontend (`index.html`)**
A single-page web application served at `localhost:8000` (deployable to any static host or CDN). No framework dependencies. Uses HeyGen's official `@heygen/liveavatar-web-sdk` (v0.0.17) to manage the avatar session. Supports both text input and push-to-talk via the browser's Web Speech API.

**Backend (`advisor_backend.py`)**
A Python FastAPI server with four active endpoints:

| Endpoint | Role |
|---|---|
| `POST /api/token` | Mints a short-lived HeyGen LiveAvatar session token; browser uses this to connect to the avatar room |
| `POST /api/llm` | Receives user question → retrieves relevant document chunks from ChromaDB → constructs a RAG-augmented prompt → calls GPT-4o → returns the reply text |
| `POST /api/tts` | Converts reply text to speech via ElevenLabs (`eleven_turbo_v2`, 24kHz PCM) using a custom cloned voice |
| `POST /api/forget` | Clears per-session conversation memory when a session ends |

**Avatar layer (HeyGen LiveAvatar, LITE mode)**
A photorealistic male avatar rendered via HeyGen's LiveKit-based infrastructure. The backend sends PCM audio chunks to the avatar via `session.repeatAudio()`, and HeyGen's server drives lip-sync frame-by-frame in real time. The avatar's video stream arrives in the browser over WebRTC. LITE mode was chosen for cost efficiency; it supports bring-your-own-audio, which is how the custom ElevenLabs voice is used.

**Voice (ElevenLabs)**
A custom cloned voice ("Yorkville2", voice ID `8gfvBkrqr64Si4V5Q321`) synthesizes all avatar speech. The `eleven_turbo_v2` model is used for low latency. Audio is streamed to the avatar as chunked PCM: first chunk 400ms, subsequent chunks 1 second each, matching HeyGen's lip-sync timing requirements.

**Knowledge base (ChromaDB + RAG)**
- **Corpus:** ~580 documents in `annuity_docs/` — a mix of Stan the Annuity Man podcast transcripts, blog posts, and structured summaries covering fixed index annuities, MYGAs, hybrid pensions, income riders, Social Security timing, annuity carrier evaluation, and related retirement income topics. Also includes academic/educational content from Wade Pfau (retirement income researcher).
- **Vector store:** ChromaDB (`PersistentClient`), collection `annuity_docs`, 4,524 chunks, embedded with OpenAI `text-embedding-3-small` (1000-char chunks, 200-char overlap).
- **Retrieval:** At query time, the user's question is embedded and the top-4 most semantically relevant chunks are injected into the GPT-4o system prompt as grounding context. This is standard RAG (Retrieval-Augmented Generation).
- **LLM:** GPT-4o with a persona prompt constraining replies to 2–4 spoken sentences, conversational tone, no markdown, no personalized financial advice.

**Conversation memory**
Per-session rolling transcript (last 12 turns) stored in-process, keyed by session UUID. Enables multi-turn follow-up questions within a session.

---

## Knowledge Domain

The system has been trained (via RAG) on content covering:

- Fixed index annuities (FIAs): mechanics, caps, participation rates, floor guarantees, market crash protection
- Multi-year guaranteed annuities (MYGAs): structure, comparison to CDs, rate environment considerations
- Income riders: how they work, guaranteed lifetime withdrawal benefits (GLWB), costs
- Hybrid pensions: private pension construction using annuities
- Annuity carriers: how to evaluate financial strength, ratings agencies, state guaranty associations
- Social Security optimization: timing strategies, interaction with annuity income
- Surrender charges, fees, and hidden costs
- Legacy planning with annuities: beneficiary options, death benefits
- Tax treatment of annuities: qualified vs. non-qualified, tax deferral
- Common misconceptions and sales tactics (grounded in Stan the Annuity Man's consumer-advocacy perspective)
- Academic retirement income research (Wade Pfau): safe withdrawal rates, annuities vs. bonds, sequence-of-returns risk

---

## Current Capabilities

- **Real-time voice conversation** with a photorealistic avatar, sub-5-second response latency (LLM + TTS combined)
- **Document-grounded answers:** responds from the corpus, not purely from GPT-4o's parametric knowledge
- **Multi-turn memory:** follows a conversation thread, handles follow-up questions and pronoun references
- **Push-to-talk and text input:** browser mic via Web Speech API, or typed questions
- **Graceful degradation:** if RAG retrieval fails, falls back to base LLM without crashing
- **Guardrailed persona:** explicitly declines to give binding personalized financial advice; positions itself as an educator

---

## What It Cannot Currently Do (Honest Limitations)

- **No lead capture:** no CRM integration, no form submission, no email/phone collection — the conversation ends and nothing is persisted externally
- **No product quoting:** cannot pull live annuity rates or quote specific products
- **No human handoff:** no live chat escalation, no calendar booking, no "talk to a real advisor" flow
- **No authentication:** sessions are anonymous and stateless across browser reloads
- **Single avatar persona:** one visual identity; not yet configurable per brand or agent
- **No analytics:** no transcript logging, no question tracking, no conversion funnel visibility
- **Voice transcript is one-directional:** avatar's spoken reply appears in the UI, but user's spoken input is transcribed client-side only (not persisted)
- **LITE mode lip-sync limitation:** avatar speaks but doesn't have full gesture/body language range (FULL mode enables richer expression at higher cost)

---

## Role in the Annuity Sales Value Chain

### Where It Sits

The system is positioned at the **top and middle of the funnel** — education and qualification — not at the point of sale.

```
Awareness → [Education / Engagement] → Qualification → Advisor Handoff → Sale
                      ↑
              LiveAvatar Advisor sits here
```

### Specific Value Chain Functions It Can Serve

**1. Cold traffic education (top of funnel)**
A prospect who arrives from a Google search, social ad, or podcast mention and doesn't yet understand what an annuity is can have a no-pressure, self-paced conversation that explains the basics. This replaces or supplements static landing page content with an interactive, trust-building experience. The avatar persona (knowledgeable but non-pushy) is intentionally designed around Stan the Annuity Man's consumer-advocacy voice, which resonates with skeptical self-directed investors.

**2. Pre-qualification (middle of funnel)**
By asking "how does a fixed index annuity work?" or "what happens if my carrier goes bankrupt?", a prospect self-selects their level of sophistication and intent. The questions they ask reveal where they are in the buying journey. This signal (even if currently unlogged) could be captured and routed to advisors with rich context: "this prospect asked about 7-year surrender schedules and MYGA rates — they're close to a decision."

**3. Objection handling at scale**
The corpus includes explicit objection-handling content (e.g., misconceptions about annuities, surrender charge explanations, carrier safety questions). An IMO or FMO could deploy this to handle the 80% of pre-sales questions that advisors answer repeatedly, freeing advisors for higher-value conversations.

**4. 24/7 availability**
A prospect researching annuities at 11pm on a Sunday gets the same quality of educational response as one who calls during business hours — without requiring advisor time.

**5. Brand-differentiated lead nurturing**
Unlike generic AI chatbots, the avatar creates a memorable, high-trust interaction. A prospect who has "met" the avatar advisor is warmer by the time they speak to a human. The custom voice and visual identity can be branded per IMO, FMO, or carrier.

**6. Content leverage**
The RAG corpus is built from existing content assets (Stan the Annuity Man transcripts, blog posts). This turns content marketing investment into an interactive knowledge base rather than static pages that prospects may or may not read.

---

## Near-Term Extensions That Would Complete the Value Chain

These are not built yet but are architecturally straightforward given the current foundation:

| Extension | Value Chain Impact |
|---|---|
| **Lead capture form** (name, email, phone, net worth bracket) triggered at natural conversation pause | Converts engaged prospects into CRM records |
| **Transcript logging** (per-session conversation stored to database) | Enables advisor context briefing before callback |
| **Calendar booking integration** (Calendly or similar) | Closes the loop from education to scheduled call |
| **Question analytics dashboard** | Surfaces most common questions/objections corpus-wide; informs content strategy |
| **Multi-brand deployment** | Different avatar personas, voices, and corpora per IMO/FMO/carrier brand |
| **Product rate API integration** | Advisor-mode: live MYGA/FIA rate lookup during or after conversation |
| **SMS/email follow-up trigger** | Post-conversation nurture sequence initiated automatically |

---

## Infrastructure and Deployment Notes

- **Current state:** local development server (`uvicorn`, port 8000, macOS)
- **Production path:** stateless FastAPI backend is trivially deployable as AWS Lambda + API Gateway or a containerized service (Railway, Fly.io, ECS). The two main stateful dependencies — ChromaDB (file-based) and in-process conversation history — both have straightforward cloud equivalents (S3-backed ChromaDB or a managed vector DB; DynamoDB for session state)
- **Operating costs (estimated at scale):** HeyGen LITE session minutes + ElevenLabs TTS characters + OpenAI embedding + GPT-4o tokens. At typical lead-gen volumes (hundreds of sessions/day, not thousands) this runs well under $1 per completed conversation
- **No proprietary lock-in:** LLM (GPT-4o → swappable), TTS (ElevenLabs → swappable), avatar (HeyGen → swappable if they ship competing SDK). The RAG corpus and conversation design are the durable assets

---

## Summary Statement (for LLM downstream consumption)

The LiveAvatar Annuity Advisor is a production-ready prototype of an AI-powered, voice-driven, avatar-based annuity education assistant. It combines a photorealistic lip-synced avatar (HeyGen), a custom cloned voice (ElevenLabs), retrieval-augmented generation over a 4,500-chunk annuity knowledge base, and GPT-4o for conversational reasoning. It is designed for top-of-funnel prospect education and middle-of-funnel qualification in the annuity distribution channel. It does not yet capture leads or integrate with CRM, but those extensions are architecturally straightforward. Its core proposition is delivering consistent, expert-level annuity education at scale, 24/7, with a human-feeling interaction quality that static content and basic chatbots cannot match.
