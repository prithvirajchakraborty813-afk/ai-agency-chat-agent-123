# Gmail fallback channel — setup

`send_proposals.py` tries Gmail as a fallback when a lead has no phone
number, or when the WhatsApp send to them fails, **provided the lead
has a `domain` value** in `proposals.csv`. Read the "EMAIL FALLBACK"
note at the top of `email_sender.py` first — the important part:
**there is no real email address captured anywhere in this pipeline,
only a website domain.** This guesses `info@domain` then
`contact@domain` and sends to the first one Gmail's SMTP server
accepts. That's a real send attempt, not a confirmed delivery — it
doesn't mean the address exists or a human reads it, and leads with no
`domain` on file (common — many real leads have no website) get no
email attempt at all. Treat this as a genuine fallback for the leads
WhatsApp can't reach, not a channel you'd rely on for most of your list.

## Setup (10 minutes, no PAN/KYC — a personal Gmail account, free)

1. Turn on 2-Step Verification on the Gmail account you want to send
   from: myaccount.google.com/security
2. Generate an App Password: myaccount.google.com/apppasswords —
   pick "Mail" as the app, any device name. You'll get a 16-character
   code. **This is not your normal Gmail password** — use the app
   password everywhere below.
3. Set as env vars (Render → your service → Environment), or pass as
   CLI flags to `send_proposals.py`:
   ```
   GMAIL_ADDRESS=youraddress@gmail.com
   GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
   ```
4. Run as usual — the fallback kicks in automatically for leads with
   no phone or a failed WhatsApp send:
   ```bash
   python send_proposals.py --in proposals.csv --dry-run
   ```
   The dry run will show `WOULD EMAIL (guessed [...])` lines for any
   lead that would fall back to email, so you can sanity-check which
   leads have a domain before running for real.

To turn it off without unsetting the env vars, add `--no-email-fallback`.

## Limits worth knowing

Personal Gmail accounts cap outbound at roughly 500 emails/day (2,000
on Google Workspace) — far above anything this pipeline sends at
current volume, but worth knowing if it scales up.

## Replies now work automatically

`chat_agent.py` has a `/poll-email` route that checks the Gmail inbox
over IMAP and feeds any new reply into the exact same conversation
engine WhatsApp uses — Gemini, the catalog, escalation detection, and
owner-approval all behave identically. This is wired into the daily
GitHub Actions run as a step right after the lead chain, so it checks
**once per day**, not continuously — email doesn't need WhatsApp's
near-real-time responsiveness, and continuous polling would need a
background thread that's more failure-prone to run on Render for
comparatively little benefit at your current volume.

### Extra setup needed for replies

1. Set two more GitHub Actions secrets (Settings → Secrets and
   variables → Actions), alongside the ones already there:
   ```
   RENDER_APP_URL=https://your-service.onrender.com   (no trailing slash)
   POLL_EMAIL_SECRET=<any random string you make up>
   ```
2. Set the same `POLL_EMAIL_SECRET` value as an env var on the Render
   service running `chat_agent.py` (Render → your service →
   Environment) — it has to match on both sides. This stops anyone who
   finds your Render URL from being able to trigger the poll route
   themselves.
3. `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` also need to be set on the
   Render service (not just wherever `send_proposals.py` runs) —
   `chat_agent.py` sends replies from the same Gmail account.

### Important limitation, carried over from sending

A reply is matched back to a lead by **domain**, not the exact address
it came from — e.g. a reply from `owner@acmeclinic.com` matches the
same conversation as one from `manager@acmeclinic.com`, because the
pipeline never captured a specific person's email, only a guessed
info@/contact@ address per lead. If two different people at the same
business email in, they'll land in one shared conversation. Accepted
tradeoff — the alternative was no email reply-handling at all.

### What still doesn't happen

No read receipts, no "typing" indicator, no threading by Gmail
subject/Message-ID — each reply is treated as a fresh message in that
lead's ongoing conversation, same as a WhatsApp text. If you want true
email-thread awareness (quoting, subject-line matching) later, that's
a bigger separate change, not what's built here.

## Owner alerts over email (for when WhatsApp itself is down/restricted)

Every "Ready to propose" ping, order confirmation, and reply to your
own `APPROVE`/`SETPRICE` commands normally goes to your own WhatsApp
(`OWNER_PHONE`). If your WhatsApp number gets restricted — which is a
real risk with WAHA's unofficial automation, see the earlier WhatsApp
discussion — you can switch owner alerts to email instead, without
touching how leads themselves are messaged.

### Setup

Add two more env vars, on Render (same service as `chat_agent.py`):
```
OWNER_EMAIL=your-real-inbox@example.com
OWNER_NOTIFY_CHANNEL=email
```
Leave `OWNER_NOTIFY_CHANNEL` unset (or set to `whatsapp`, the default)
to go back to WhatsApp once your number is healthy again — this is a
manual switch, not automatic failover, since a temporary WAHA API
error and an actual restriction look identical from inside the code
and shouldn't silently swap channels on their own.

### How it works

- Every owner ping (`send_whatsapp(OWNER_PHONE, ...)` calls, unchanged
  everywhere else in the file) gets redirected to an email to
  `OWNER_EMAIL` instead, when the switch is on.
- You reply to that email the same way you'd reply on WhatsApp —
  `APPROVE <phone>`, `SETPRICE <phone> <amount>` — as the email body.
  `/poll-email` recognizes a reply from `OWNER_EMAIL` specifically and
  routes it to the owner-command handler instead of the lead-chat
  engine, so this only works once **the daily poll runs** — it is
  not instant like WhatsApp. If an approval is urgent, this isn't a
  substitute for a working WhatsApp number, just a way to not be
  fully blind while it's restricted.
- Use `OWNER_EMAIL`, not `GMAIL_ADDRESS`, for your own inbox if
  they're different accounts — `GMAIL_ADDRESS` is the account that
  *sends and polls*, `OWNER_EMAIL` is the address alerts land on and
  the address `/poll-email` treats as "the owner, not a lead". They
  can be the same address if you want to send from and read alerts
  in one inbox.
