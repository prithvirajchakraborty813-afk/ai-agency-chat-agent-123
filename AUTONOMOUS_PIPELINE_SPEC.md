# Autonomous Agency Pipeline — Build Spec

## The vision, stated plainly

Every night, ~8–9pm, with no manual steps:

1. Gemini finds new business leads
2. Auto-qualifies them
3. Auto-messages them on WhatsApp
4. An AI has the full back-and-forth conversation — answers questions, discovers needs, negotiates
5. When the lead says yes, AI drafts final terms and **pings Billo to approve** (one tap)
6. On approval, confirmation + payment link auto-sent to the lead
7. Billo picks up from there personally — messages the client about implementation specifics, builds it, delivers it

Steps 1–6 run with zero manual work except the one approval tap. Step 7 stays human, permanently — that's the actual paid work.

**The one guardrail, confirmed and staying:** no binding terms or payment request goes out without Billo tapping approve first. This was already worked through once (see call_agent.py history) and reconfirmed today — the AI drafts, Billo approves, then it sends. Not negotiable without a real reason to revisit it.

---

## What already exists (don't rebuild)

| Piece | File | Status |
|---|---|---|
| Find leads | `gemini_maps_finder.py`, `gemini_search_finder.py` | Working |
| Dedupe/merge | `merge_prospects.py` | Working, trusted |
| Qualify leads | `gemini_vertex_qualifier.py` | Working |
| Draft proposal + pick tier | `proposal_generator.py` + `tiers_agency.json` | Working, verified |
| Send WhatsApp message | Whapi.Cloud + a Python script | **Confirmed working today** — real message sent and received |

Everything above already runs as separate commands. Nothing here needs rebuilding — it needs *chaining* and *scheduling*.

---

## What's genuinely new

### 1. The scheduler (the "8–9pm, runs itself" part)
Something has to trigger the whole chain daily without Billo opening a terminal. Two real options://
- **Windows Task Scheduler** running a `.bat` file that calls each script in order — simplest, works today, no new tools.
- **n8n**, self-hosted — better fit once step 4 (the conversation agent) exists, since n8n is built for exactly this kind of wait-for-reply, branch-on-answer logic. Already the plan for Phase 3 per the original project guide.

**Recommendation: start with Task Scheduler for steps 1–3 (find → qualify → propose → first message), move to n8n once step 4 exists** — no point standing up n8n before there's a reason to wait on anything.

### 2. The reply-conversation agent (genuinely new, the biggest piece)
This doesn't exist anywhere yet. Needs:
- A webhook receiver (small always-on server) that catches incoming WhatsApp replies from Whapi.Cloud in real time
- Per-lead conversation memory — Gemini needs to remember what's already been said to *this specific* lead across multiple messages, not treat each reply as a fresh conversation
- A system prompt that knows: the three pricing tiers, what's actually being sold, how to answer FAQs, and — critically — **never states final agreed terms as binding**, only "drafts" them for Billo's review
- Detection logic for "this lead just said yes" → triggers the approval step

### 3. The approval step
- When the agent detects agreement, it drafts final terms as text
- Sends *Billo* (not the lead) a WhatsApp/email notification with those drafted terms and a way to say yes/no
- Only on Billo's yes does the confirmation + payment link go to the actual lead

### 4. Payment link generation
Not yet designed. Needs a payment method that works without a PAN (worth checking — UPI collect links / payment gateways may have lighter KYC than full business registration; this needs its own research pass before building).

---

## Suggested build order

1. ✅ Prove WhatsApp send works — **done today**
2. Build the first-touch sender: reads `proposals.csv`, sends each real lead their actual proposal text, paced, logged — small, immediately useful, no new infrastructure
3. Build the webhook receiver — just log incoming replies at first, prove Billo can *see* a reply arrive in real time
4. Build the conversation agent — Gemini + per-lead memory + the tier/pricing system prompt, replying automatically to logged messages
5. Build the "detect yes → notify Billo → wait for tap → send confirmation" approval loop
6. Research + wire up a no-PAN payment link method
7. Wrap steps 1–3 (find/qualify/propose) in a daily scheduled job
8. Only once 2–7 are solid: extend the schedule to also trigger step 4's first message automatically, closing the full loop

Each step is independently useful and testable — nothing here requires the whole system to exist before any part of it earns its keep.
