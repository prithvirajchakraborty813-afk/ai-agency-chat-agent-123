# AI Agency Automation Project — Complete Guide (Updated)

**Purpose:** Hand this to a fresh Claude session so it understands the whole project instantly, with zero prior context. This is an update of an earlier version of this same guide — real progress has been made since then (Agent #5 built and tested, a pricing model locked in, a serious bug found and fixed in production data). Assume nothing — explain every step like the user has never touched a terminal, because functionally, that's close to true even though real working code now exists.

---

## Who this is for

The user is a diploma engineering student in India (see his profile for more), self-taught, comfortable pasting and running terminal commands but not writing code independently. He builds by describing what he wants, running what Claude gives him, and reporting back errors/screenshots. **He has explicitly said he is "not so technical"** — when explaining anything conceptual (architecture, what a piece of code does, business/pricing logic), use plain, jargon-free language with concrete analogies, not just correct terminology. This applies as much to *explaining the system* as it does to giving exact commands.

He does **not** want to be on live sales/discovery calls himself — he says he's "clumsy" and will fumble on sharp questions in the moment. Design around this: he wants to review things in writing, on his own time, never live.

He does not have a PAN (Indian tax ID) and can't get one currently (settled context, don't probe). This means **he cannot legally receive payment from clients in countries/via platforms that require PAN-linked KYC.** Default all targeting to **India-only** clients until he says this has changed. This is also the actual gate on "when can I start selling" — more on that below.

---

## The critical conceptual clarification — read this before anything else

Earlier sessions (including the user himself) conflated two different things. Get this right immediately if he brings it up again:

- **The 14-agent plan (below) is internal infrastructure.** It's the machinery that runs *his agency* — finds leads, qualifies them, drafts outreach, closes deals, then (later) tracks delivery/reporting/invoicing for his own clients. A client never sees or uses any of these 14 agents. **He keeps all 14, forever, for every client.**
- **What he actually sells is a separate, 15th thing**: one custom AI agent (a WhatsApp/website bot), built fresh for each client's business, using the same underlying tech patterns (Gemini + Vertex AI + ADC) but a completely different deliverable — it answers *the client's customers*, not his.
- The three pricing tiers (Starter/Growth/Custom, below) describe **that one client-facing agent**, not the 14-agent stack. He doesn't need to pre-build all three tiers before selling — he builds whichever tier a client actually picks, after they sign.

If a fresh session doesn't nail this distinction fast, the user will get confused and start planning to build things he doesn't need to build.

---

## Pricing (locked in, see `tiers_agency.json`)

Three tiers for the client-facing agent (India SMB market, solo operator, benchmarked against real 2026 India automation/chatbot pricing):

| Tier | Setup | Monthly | Scope |
|---|---|---|---|
| **Starter** | ₹6,000–8,000 one-time | ₹2,499–3,999/month | One channel (WhatsApp or website). FAQs, booking/inquiry capture, basic follow-up. Best fit: single-location small businesses. |
| **Growth** | ₹15,000–25,000 one-time | ₹5,999–9,999/month | Multi-channel (WhatsApp + website + email). Lead capture into a CRM/sheet, monthly reporting. Best fit: multi-location or higher-volume businesses. |
| **Custom** | Quote-based | ₹10,000+/month | Multi-location franchises, manufacturers/retailers needing inventory-adjacent automation. Scoped per client, not pre-fixed. |

Decided principles behind this:
- First 2–3 clients should get "founding client" pricing below these numbers, explicitly in exchange for a testimonial/reference permission — cheaper client acquisition than ads, and feeds the Phase 5 referral/testimonial agents later.
- Recurring monthly retainer is the core of the pricing, not the one-time setup fee — matches his stated goal of recurring revenue.
- **Website building is a separate, optional add-on**, not bundled into any tier — many of his real leads have no website (see the `no-website--*` synthetic keys in his prospects CSVs). Suggested: ₹8,000–12,000 one-time for a minimal 3–4 page template-based site (just enough to host the booking widget), not a full web-design offering — he's an AI automation agency, not a web dev shop.

---

## The actual business idea

Solo AI automation agency: find small-business leads → qualify → AI agent contacts them, discovers needs, proposes/closes a deal → he personally does the implementation work → recurring-revenue mechanics (reporting, upsells, referrals) layered on once he has real clients.

This pivoted from an earlier goal of finding leads for his Character.AI-style companion app — same pipeline repurposed. A separate pharmacy B2B platform called PHARMANET (66-agent idea origin, since scoped down) is another, earlier, unrelated project. Don't conflate them.

### The 14-agent full lifecycle (internal infrastructure — see clarification above)

**Phase 1 — built and proven working:**
1. Lead Gen
2. Research/Enrichment
3. Lead Scoring
4. Outreach/SDR draft

**Phase 2 — in progress:**
5. **Proposal Generator — BUILT AND TESTED, working.** (`proposal_generator.py` + `tiers_agency.json`, see below.)
6. Onboarding Agent — not yet built.

**Phase 3 — the chat-closing piece, design finalized but not yet built:**
7. Chat Agent (discovery + propose + close over WhatsApp/email, with one human approval checkpoint — see below)

**Phase 4 — build once he has a first real client:**
8. Delivery Status Agent
9. Client Reporting Agent
10. Invoice/Payment Reminder Agent

**Phase 5 — growth, once he has 2-3 clients:**
11. Testimonial/Case-Study Agent
12. Referral Agent
13. Upsell/Renewal Agent
14. Competitor Watch / Objection-Handling Agent (lowest priority)

### The critical design decision on #7 (Chat Agent) — do not build this differently without re-confirming with the user

He originally wanted a fully autonomous AI that finds leads, calls them, and closes a binding deal with zero human involvement. Claude pushed back — an unsupervised AI stating prices and getting agreement creates real legal/business risk.

**Agreed final design, explicitly confirmed by the user (updated: WhatsApp + email only, no voice/Vapi):**
```
Lead qualified
    ↓
Chat agent (WhatsApp + Gemini) — discovers needs, proposes price/scope via
message, gets written interest (no calling)
    ↓
Agent drafts written terms → sends order summary to the user via WhatsApp
    ↓
User glances at it, taps/replies "approve" — seconds, asynchronous, no live pressure, his own time
    ↓
Approved terms + payment link auto-sent to the customer as the final offer
    ↓
Customer pays → order booked → user does a short async message to gather implementation specifics
    ↓
User builds it
```

**Non-negotiable, explicitly re-confirmed:** the AI never sends final binding terms or takes payment without the human-approval step. Re-raise the original reasoning if asked to remove it; respect his final call if he still wants it removed after hearing that again.

**Channel note (updated):** originally designed around Vapi voice calling; the user has since decided against voice entirely — WhatsApp and email only, no phone calls anywhere in the pipeline. Don't reintroduce Vapi or voice-calling design without the user explicitly asking for it back.

Also confirmed: **first outreach touch is text/email, not a cold call** (~73-80% of B2B buyers prefer email first-touch per cited research). With voice now dropped, there is no call-based second-touch channel — the escalation path for non-responders (if any) still needs re-deciding; the email-first → wait → sequencer logic is still design-only, not built.

---

## Agent #5 (Proposal Generator) — what it is, how it works, what's been verified

**Files:** `proposal_generator.py`, `tiers_agency.json`

Takes `qualified.csv` (output of the qualifier), and for every row with `qualified=True`, asks Gemini to (1) recommend which of the three tiers fits that business, and (2) draft a ready-to-send proposal message referencing their specific pain point and the tier's real price.

**Key design choice, and why:** the model is given the exact tier prices from `tiers_agency.json` and told to use them verbatim — it does not invent numbers. If it somehow returns an unknown tier key, the script falls back to Starter and prints a warning rather than shipping a hallucinated price.

**Verified against his real first run (13 qualified leads → 12 proposals):**
- Zero price hallucination — every price in the output matched the config exactly.
- Tier logic works correctly: multi-location businesses (The Lassi Shop, Dwarakam Express) correctly got routed to Growth; single-location businesses correctly got Starter.
- Proposal quality is genuinely usable — specific, references real business details, soft low-pressure close.
- One lead (Shamz Clinic) didn't get a proposal in that run — checked, the row's data was fine, so this is almost certainly a transient Vertex AI hiccup (same category as the Pune 0-char issue the Maps finder had), not a script bug. Just rerun to pick up any missing rows.
- **Known minor polish item, not yet fixed:** a couple of proposals collapsed a price range down to a single number (e.g. "₹6,000" instead of "₹6,000–8,000"). Not wrong (it's the correct low end of the real range), but if the user wants every proposal to show the full range for negotiating room, the prompt needs a stricter instruction to that effect.

Usage:
```
python proposal_generator.py --in qualified.csv --out proposals.csv --project project-95bf86c6-f889-4996-ba3
```

---

## What actually gets built for a Starter-tier client (concrete, once he has a client)

Walked through in plain terms for the user already — repeat this shape if asked again, in non-jargon language:

1. **Pick the channel** with the client — WhatsApp or website widget. WhatsApp is usually faster for his leads (most have no existing website).
2. **Build their knowledge base** — a short interview covering hours, prices, services, policies. This becomes context fed to Gemini, not code.
3. **Build the conversational agent** — reuse the same `VertexGeminiClient` pattern already proven three times over (search finder, maps finder, qualifier, now proposal generator). One prompt answers FAQs from the knowledge base; a second `responseSchema`-enforced call extracts booking/inquiry details into structured JSON.
4. **Wire up the channel** — WhatsApp: register with Meta's WhatsApp Cloud API (needs business verification), point its webhook at a small endpoint. Website: a lightweight JS chat widget calling the same backend.
5. **Capture leads and notify the owner** — log every inquiry (Sheet/CSV) and send the business owner a short notification per inquiry.
6. **Deploy to run 24/7 on Google Cloud Run**, same GCP project he's already using. Important: **this sidesteps the ADC/service-account-key problem** — Cloud Run's attached service account authenticates to Vertex AI automatically, no key file needed, no org-policy conflict, unlike relying on his local machine's login session.
7. **Test before handoff** — several realistic conversations before the client's real customers see it.

Once built once, this is highly reusable — the second/third Starter client is mostly swapping in their knowledge base and redeploying, not rebuilding from scratch. That reuse is what makes the per-client pricing actually profitable for a solo operator.

---

## n8n vs Python — decision made, don't relitigate without new information

User asked whether to move the pipeline to n8n for a simpler "one prompt" experience. Decision reached:

- **Phase 1–2 (finder → merge → qualify → propose): stay in Python.** This is a linear batch job with no waiting or branching — n8n adds a UI layer without actually simplifying anything, and would mean maintaining a second tool on top of code that already works.
- **Phase 3 (Chat Agent + multi-touch sequencer): n8n genuinely fits there**, once built — that part needs webhooks, delays (wait for approval, wait for reply), and human-in-the-loop branching, which is exactly what n8n is built for and clunky to hand-roll in a script.
- **The auth concern that seemed like a blocker turned out not to be one**: ADC's refresh token is long-lived and refreshes itself quietly, same as it already does in his Python scripts. If he self-hosts n8n on his own machine (not n8n cloud), an Execute Command node running `gcloud auth application-default print-access-token` into an HTTP node's header works fine long-term.
- **Recommended architecture when Phase 3 arrives:** keep the existing Python scripts as "engines" (already debugged — schema enforcement, hallucination filtering, the domain-validator fix), and have n8n call them as nodes and handle the orchestration/waiting/branching around them, rather than rewriting that logic natively in n8n.

Immediate next build (before n8n is relevant) was offered as: a single orchestrator script/`.bat` file chaining finder → merge → qualifier → proposal generator into one command, since running four separate commands each time is real friction. Not yet built — offer again if he wants less manual typing.

---

## Full technical history — what was tried, what failed, what actually works

This section exists so a new Claude doesn't repeat hours of already-exhausted debugging. Trust this section over re-investigating from scratch.

### Data sources for finding companies — the long saga

Three "structured company database" sources were tried, in order, and **all three are dead ends**:

1. **Google Custom Search JSON API** — CONFIRMED PERMANENTLY DEAD. Google closed this to new customers as of 2025, full shutdown Jan 1 2027. Don't retry.
2. **Apollo.io** — code is correct, but company search requires Organization tier: $119/user/month, 3-seat minimum ≈ $357/month. Not viable for a solo operator.
3. **People Data Labs (PDL)** — did eventually work, after buying a domain (`companion-app.website`, ₹127) + free Zoho Mail hosting to get PDL's required "work email." Hit a low free-tier monthly quota (402 error) after test runs; resets monthly. `icp_finder.py` kept as fallback for when quota resets.

### The pivot: Gemini-native company finding (current primary approach)

Since PDL's quota is exhausted, current approach is Gemini + grounding tools on Vertex AI, billed against Google Cloud free-trial credit (~₹28,693, expires 28 Nov 2026).

- **`gemini_search_finder.py`** — "Grounding with Google Search." Works poorly for local-business discovery — returns 0 results or fabricates fictional companies. Kept as fallback with a hallucination-filter safety net.
- **`gemini_maps_finder.py`** — "Grounding with Google Maps." **This is the one that works** — queries Google's real 250M+ place database. Must loop over specific city-anchor lat/lng rather than one broad query (Maps grounding is location-anchored; broad queries barely use it). Real caveat seen in practice: near tech-hub cities like Bengaluru, Maps grounding can return 0 results because the top matches are AI/automation vendors themselves rather than the plain local businesses being searched for — that's a real limitation of that specific anchor, not a script bug. Occasional 0-char empty responses from an anchor are usually a transient API hiccup, not a real "no results" case — just retry that anchor.
- Model used throughout: `gemini-2.5-flash-lite` — confirmed-correct, currently-live model ID. `gemini-3.1-flash-lite` does not exist and 404s — don't reintroduce that string; verify any new model ID against Google's current docs before using it.

### A real, confirmed bug found and fixed in production data (domain-field validation)

Both `gemini_search_finder.py` and `gemini_maps_finder.py` had a synthetic-key fallback (`_synthetic_key`/`no-domain--*`/`no-website--*`) meant to give companies-with-no-website a unique dedup identifier, so `merge_prospects.py`'s domain-based dedup wouldn't collapse them together. But that fallback only fired on a truly *empty* domain string. In practice Gemini sometimes fills the domain field with explanatory text instead of leaving it empty — e.g. literally **"Not explicitly found in search results."** That's non-empty, so the fallback never fired.

**Confirmed against his real `prospects.csv`: 8 different companies shared that exact string as their "domain."** `merge_prospects.py` dedupes on domain, so 7 of those 8 real leads would have been silently dropped as false duplicates.

**Fix applied and verified** (both finder scripts): added a `_looks_like_domain()` shape validator (requires a dot, no spaces, matches a domain-shaped regex). Anything that fails it gets cleared and routed through the existing synthetic-key fallback instead of being trusted as a real domain. Unit-tested against the exact real junk string plus real domains — all pass. This is the current, correct behavior; don't revert it.

### Auth: how Vertex AI billing actually works on this account — read before touching auth again

The user's Google Cloud org has security policies that block the two normal/easy auth methods:
- Service-account JSON key creation is **disabled by org policy** — don't suggest it, it will fail.
- Plain API keys are **disabled by org policy** too.

**The only working auth method on this account is Application Default Credentials (ADC)** via the `gcloud` CLI:
```powershell
gcloud auth application-default login
gcloud config set project project-95bf86c6-f889-4996-ba3
```
Already working and set up on his machine. All `gemini_*.py` / `proposal_generator.py` scripts use `google.auth.default()` to pick this up automatically.

**Exception worth remembering:** if/when anything gets deployed to run continuously (e.g. a live client-facing bot on Cloud Run), a Cloud Run service's *attached* service account authenticates automatically without needing a key file — this sidesteps the org-policy block entirely, unlike trying to keep his local machine's ADC session alive for a 24/7 service.

**Project ID:** `project-95bf86c6-f889-4996-ba3` (display name varies — "My First Project"/"vertex-qualifier" — ID is the stable identifier, always use it in `--project` flags).

**Naming note:** "Vertex AI" is now branded "**Gemini Enterprise Agent Platform**" in Google's UI/docs (same product/APIs); IAM role "Vertex AI User" renamed to "**Agent Platform user**."

**Free grounded-request quota**: 1,500/day, up to 45,000/month. Model cost (non-grounded calls) is cheap: `gemini-2.5-flash-lite` runs roughly $0.25/$1.50 per million tokens (input/output).

### Other confirmed, already-fixed bugs — don't reintroduce them

- **Windows cp1252 encoding**: crashed CSV writing on non-English characters (e.g. Turkish "ğ"). Fix: explicit `encoding="utf-8"` on every CSV `open()` call. Applied across `icp_finder.py`, `nvidia_qualifier.py`, `sheets_sync.py`, `call_agent.py`, and all the newer Gemini-based scripts. Any new CSV-handling script must include this from the start.
- **Two Python installations on Windows**: `C:\Users\USER\AppData\Local\Python\pythoncore-3.14-64\` (where `pip install` normally lands) vs `C:\Users\USER\AppData\Local\Programs\Python\Python314\python.exe` (what `python` actually launches). If `pip install X` says "already satisfied" but the script still throws `ModuleNotFoundError`, install into the Programs path specifically:
```powershell
C:\Users\USER\AppData\Local\Programs\Python\Python314\python.exe -m ensurepip
C:\Users\USER\AppData\Local\Programs\Python\Python314\python.exe -m pip install <package>
```

---

## Current file inventory

All in one working folder on the user's Desktop (folder name not load-bearing — confirm he's `cd`'d into wherever these files live before running anything).

| File | Status | Purpose |
|---|---|---|
| `gemini_maps_finder.py` | **Working, patched, primary lead source** | Finds real local businesses via Google Maps grounding. Has the domain-validator fix. |
| `gemini_search_finder.py` | **Working, patched, secondary/fallback** | Web-search-grounded finder. Has the domain-validator fix. |
| `merge_prospects.py` | Working, user-authored | Combines/dedupes multiple prospects CSVs by domain. Trusted, don't rewrite. |
| `gemini_vertex_qualifier.py` | Working, primary qualifier | Scores leads, drafts outreach. |
| `proposal_generator.py` | **NEW — working, verified against real data** | Agent #5. Recommends a tier + drafts a ready-to-send proposal per qualified lead. |
| `tiers_agency.json` | **NEW — config** | The three pricing tiers (Starter/Growth/Custom), editable without touching code. |
| `icp_agency.json`, `icp_agency_india.json` | Config files | ICP criteria. India-only variant should be default given the PAN constraint. |
| `icp_finder.py` | Working, fallback only | Original PDL-based finder. Kept for when PDL's quota resets. Not in the most recent code export — confirm it still exists in his folder. |
| `sheets_sync.py` | Written, never actually run/tested | Needs ADC-based rework (same as the qualifier went through) before it'll work — service-account key auth is blocked. Not in the most recent code export — confirm it still exists in his folder. |
| `call_agent.py` | Dry-run tested only | Needs a significant rebuild to match the finalized design (human-approval checkpoint, email-first not call-first). Not in the most recent code export — confirm it still exists in his folder. |
| `nvidia_qualifier.py`, `gemini_qualifier.py` | Superseded/unused fallback | Pre-Gemini-pivot and AI-Studio-API-key versions respectively. Not part of the active pipeline. |
| `prospects*.csv`, `qualified.csv`, `proposals.csv`, `debug_test.csv` | Real output data | Genuine results from real runs, confirmed good quality. |

---

## What to actually do next (in order)

1. ~~Build Agent #5, the Proposal Generator~~ — **done, verified working.**
2. **Rebuild `call_agent.py` as `chat_agent.py`** to match the finalized design: discovery conversation over WhatsApp → proposes terms → drafts written terms → sends an approval notification to the user via WhatsApp (channel now decided) → waits for his one-tap/reply approval → only then sends final terms + payment link to the customer. Real rebuild, not a small patch. The webhook receiver (`webhook_receiver.py`) is now proven working end-to-end (real replies arrive, parse, and log reliably — a concurrent-write bug that silently dropped entries during message bursts was found and fixed with a file lock) — this is the foundation the chat agent's reply-listening will build on.
3. **Build the multi-touch sequencer**: first touch via text/email (reuse `outreach_draft`/the proposal text), wait for a response window. No voice escalation channel anymore (voice/Vapi dropped) — what happens for non-responders still needs deciding.
4. **Fix or rebuild `sheets_sync.py`'s auth**, or decide he doesn't need Sheets specifically and just keep local CSVs.
5. **Build one demo/portfolio agent** (for himself or a practice business) before pitching real prospects — something concrete to show, not just describe.
6. **Confirm the India-only payment path actually works end to end** given the PAN/KYC constraint — test with a dummy transaction before a real client, don't discover it's broken mid-deal.
7. Only after a first real client exists: build Phase 4 agents (delivery status, reporting, invoicing).
8. Optional, lower priority: a single orchestrator script/`.bat` file chaining the existing pipeline into one command, if the multi-command friction becomes annoying enough to justify it.

---

## Tone/approach notes for whoever picks this up

- The user responds very well to being walked through *exact* commands and screenshots — keep doing that, don't assume he'll infer steps.
- He's explicitly said he's **"not so technical"** — when explaining concepts (not just commands), use plain language and concrete analogies. Don't assume he'll parse terms like "webhook," "backend," "service account" without a plain-English gloss the first time.
- He's genuinely capable and has written real, careful code himself (`merge_prospects.py`, modifications to the finder scripts) — don't undersell his ability on things he's already shown he understands, but don't assume dev-tooling fluency either.
- When something breaks, past pattern is: get a screenshot, diagnose the *actual* error text rather than guessing, fix the real root cause, verify the fix compiles/runs locally before handing it back.
- Several early mistakes in this project came from guessing at API details (model names, endpoints, auth) without verifying against current docs — always verify against live sources for anything Google Cloud/Vertex/Gemini related.
- Re-raise the human-approval-checkpoint reasoning if asked to remove it, rather than silently complying — but respect his final call if he still wants it removed after hearing the reasoning again.
- When he uploads a code zip, actually check it against his real data (run the logic, look for real bugs) rather than just skimming — that's how the domain-validation bug and the proposal generator's price accuracy got caught.
