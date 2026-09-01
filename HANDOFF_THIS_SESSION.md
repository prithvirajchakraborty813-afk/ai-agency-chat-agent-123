# Handoff — mid-session checkpoint (catalog-first rebuild)

This is a **partial** rebuild of `chat_agent.py`, stopped intentionally
midway so you (Dona) can hand this zip to a fresh Claude instance instead
of waiting. Nothing here has been run for real yet — syntax-checked only.

## DONE this session

1. **`tiers_agency.json`** — added `setup_price_fixed` to each tier:
   Starter ₹7,000, Growth ₹19,000, Custom `null` (stays quote-based,
   by design — can't be a clickable catalog price).

2. **Escalation detection** (`chat_agent.py`) — new, code-level (not
   Gemini): `detect_escalation_request()` checks incoming lead text
   against a keyword list ("call me", "real person", "human", "talk to
   owner", etc). If matched, `handle_escalation()` fires immediately,
   at any stage: pings the owner with the lead's exact message, and
   sends the lead a fixed honest reply — WhatsApp-only, no calls, agent
   won't auto-reply further to that message. This check runs in the
   webhook handler *before* the stage-based lead logic, so it fires
   even during warm-up.

3. **System prompt updated** — `CHAT_SYSTEM_PROMPT` now explicitly
   tells Gemini it has no calling capability and must never agree to
   or imply a phone call, even outside the exact keyword matches above
   (defense in depth).

4. **New stage machine wired up through package selection:**
   - `warming_up` (renamed from `negotiating`) — Gemini chats freely,
     using the new `WARMUP_RESPONSE_SCHEMA` (just `reply_to_lead` +
     `ready_for_catalog` — no pricing logic here yet).
   - `catalog_shown` — once Gemini sets `ready_for_catalog: true`,
     `build_catalog_message()` sends a fixed-price catalog (code-built
     text, not Gemini-generated, so prices can't drift). Lead's reply
     is matched to a tier via `match_selected_tier()` — explicit
     number/name matching in code, not Gemini, since a wrong match
     here would misquote a price.
   - `package_selected` — confirmation sent, `selected_tier` and
     `fixed_price` saved on the conversation record. **Scope
     negotiation logic from here is NOT wired up yet — see below.**

## NOT done — pick this up next

**The main remaining piece: `package_selected` stage handling.**
Right now a lead's message in this stage just gets logged
(`print(...)`) and nothing is sent back. Needs:

- Call Gemini with `CHAT_RESPONSE_SCHEMA` (already exists, kept from
  before — see the `TODO` comment right above it in the file) to
  negotiate remaining scope details and decide `ready_to_propose`.
- When ready: `set_stage(phone, "awaiting_owner_approval", ...)` and
  ping the owner with the summary — same pattern as before, but now
  the price is *already fixed* from the catalog pick, so no
  ambiguity to resolve.

**Second piece: simplify `handle_owner_message()`'s `APPROVE` handler.**
Currently it always asks the owner to `SETPRICE` a number in a range.
That's now wrong for Starter/Growth (price is already locked from the
catalog step) — `APPROVE` should read `convo["fixed_price"]` and go
straight to `order_contract.create_order()` + send the contract, no
round-trip. **Keep `SETPRICE` as a fallback path only for Custom**
(where `fixed_price` is `None` and a human judgment call is still
needed) — check `convo.get("fixed_price")` first; if it's set, auto-
charge; if `None`, fall back to the existing SETPRICE ask.

**Nothing has been tested yet** — not even the parts marked "done."
Once both pieces above are wired up, this still needs everything from
the original handoff's steps 5–6: a real (non-mocked) WhatsApp
dry-run, and a real ₹1-then-full-amount UPI test, before touching a
real customer.

## Also still open (from before, unrelated to this session's work)

- `OWNER_UPI_ID` — a UPI ID was shared this session
  (`xman06544@okaxis`, display name "Xman 123"). Worth confirming with
  Dona whether that's the real production UPI ID or a test account
  before wiring it in — the name doesn't obviously match a business,
  so don't assume it's final without asking.
- Real customer name capture — still unresolved (phone number used as
  identifier for now).
- Stalled/never-ready conversation detection — still unresolved.
