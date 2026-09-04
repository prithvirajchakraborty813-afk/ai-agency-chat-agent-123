#!/usr/bin/env python3
"""
inbox.py — Gmail-style web UI for browsing every lead conversation and
manually taking over one when the bot needs correcting mid-chat.

WHY THIS EXISTS: chat_agent.py already stores the full message history
per conversation (db_storage's `conversations` table, in the `history`
list inside the JSONB data), but there was no way to browse it — only
the final order/payment outcome was ever visible (via Brevo logs / the
owner-alert emails). This adds:
  - a conversation list (like a Gmail inbox), newest activity first
  - a full thread view per lead, read top-to-bottom like a chat log
  - a "take over" reply box: typing here sends a real message to the
    lead through the normal channel (WhatsApp or email, whichever that
    lead uses) AND pauses the bot for that lead so it can't talk over
    a manual correction — until "Resume bot" is pressed

AUTH: every route requires ?key=<INBOX_SECRET> (or an `X-Inbox-Key`
header) matching the INBOX_SECRET env var. This sits on the same public
Render URL as the webhook, so treat INBOX_SECRET like a password — long,
random, not reused. If INBOX_SECRET is unset, every route fails closed
(401 for everyone) rather than open.

INTEGRATION: registered into chat_agent.py's existing Flask `app` as a
blueprint (see the app.register_blueprint(inbox_bp) line added there),
so this rides on the same process/port — nothing new to deploy.

The other half of "take over" — actually pausing the bot's auto-replies
for a conversation once human_takeover is set here — lives in
chat_agent.py itself (handle_lead_message / handle_post_lock_message /
handle_escalation each check convo.get("human_takeover") right after
loading the conversation, before generating or sending anything).
"""

from __future__ import annotations

import os
from functools import wraps

from flask import Blueprint, request, jsonify, Response

import db_storage
import email_sender

inbox_bp = Blueprint("inbox", __name__)

INBOX_SECRET = os.environ.get("INBOX_SECRET", "")


def _check_auth() -> bool:
    if not INBOX_SECRET:
        # Fail closed: an unset secret locks everyone out, not everyone in.
        return False
    supplied = request.args.get("key") or request.headers.get("X-Inbox-Key", "")
    return supplied == INBOX_SECRET


def require_key(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped


def _is_notification_convo(convo_id: str) -> bool:
    """True if this convo_id is an email: address matching the same
    notification-sender rules email_sender.py uses to skip auto-replying
    in the first place. Reused here so old junk conversations that were
    created before those domains were added to the filter (or before this
    filter existed at all) stop cluttering the inbox list too — their
    history rows stay in the DB untouched, they're just hidden from view.
    Non-email convo_ids (WhatsApp phone numbers) always pass through."""
    if not convo_id.startswith("email:"):
        return False
    addr = convo_id[len("email:"):].strip().lower()
    if "@" not in addr:
        return False
    local_part, domain = addr.split("@", 1)
    if local_part in email_sender.NOREPLY_LOCAL_PARTS:
        return True
    return any(
        domain == d or domain.endswith("." + d)
        for d in email_sender.KNOWN_NOTIFICATION_DOMAINS
    )


def _preview(history: list) -> str:
    if not history:
        return "(no messages yet)"
    text = history[-1].get("text", "")
    return (text[:90] + "…") if len(text) > 90 else text


@inbox_bp.route("/inbox/api/conversations", methods=["GET"])
@require_key
def api_list_conversations():
    show_all = request.args.get("all") == "1"
    convos = db_storage.load_all_conversations()
    out = []
    for convo_id, convo in convos.items():
        if not show_all and _is_notification_convo(convo_id):
            continue
        history = convo.get("history", [])
        out.append({
            "id": convo_id,
            "stage": convo.get("stage", ""),
            "human_takeover": bool(convo.get("human_takeover", False)),
            "last_message": _preview(history),
            "last_at": history[-1]["at"] if history else convo.get("created_at", ""),
            "message_count": len(history),
        })
    out.sort(key=lambda c: c["last_at"], reverse=True)
    return jsonify(out)


@inbox_bp.route("/inbox/api/conversation/<path:convo_id>", methods=["GET"])
@require_key
def api_get_conversation(convo_id):
    convo = db_storage.load_conversation(convo_id)
    if convo is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(convo)


@inbox_bp.route("/inbox/api/conversation/<path:convo_id>/send", methods=["POST"])
@require_key
def api_send_message(convo_id):
    # Imported here (not at module top) since chat_agent.py imports this
    # blueprint — importing chat_agent at module load time would be a
    # circular import.
    import chat_agent

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    convo = db_storage.load_conversation(convo_id)
    if convo is None:
        return jsonify({"error": "not found"}), 404

    ok = chat_agent.send_whatsapp(convo_id, text)  # handles WhatsApp vs email: automatically
    if not ok:
        return jsonify({"error": "send failed — check server logs"}), 502

    chat_agent.append_history(convo_id, "owner", text)

    convo = db_storage.load_conversation(convo_id)
    convo["human_takeover"] = True
    db_storage.save_conversation(convo_id, convo)

    return jsonify({"ok": True, "human_takeover": True})


@inbox_bp.route("/inbox/api/conversation/<path:convo_id>/resume", methods=["POST"])
@require_key
def api_resume(convo_id):
    convo = db_storage.load_conversation(convo_id)
    if convo is None:
        return jsonify({"error": "not found"}), 404
    convo["human_takeover"] = False
    db_storage.save_conversation(convo_id, convo)
    return jsonify({"ok": True, "human_takeover": False})


@inbox_bp.route("/inbox", methods=["GET"])
def inbox_page():
    # The HTML shell itself is not gated server-side — it just prompts
    # for a key and stores it in localStorage, then every API call above
    # carries that key. A wrong/missing key gets a clean prompt instead
    # of a raw 401 page. The API routes are the real security boundary.
    return Response(INBOX_HTML, mimetype="text/html")


INBOX_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Inbox</title>
<style>
  :root {
    --bg: #0f1115; --panel: #161923; --border: #262b3a;
    --text: #e7e9ee; --muted: #8b93a7; --accent: #5b8cff;
    --lead: #1f2330; --agent: #223a2c; --owner: #3a2c22;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         background:var(--bg); color:var(--text); height:100vh; overflow:hidden; }
  #keyGate { position:fixed; inset:0; background:var(--bg); display:flex;
             align-items:center; justify-content:center; z-index:10; }
  #keyGate.hidden { display:none; }
  #keyGate input { padding:10px 12px; border-radius:8px; border:1px solid var(--border);
                    background:var(--panel); color:var(--text); width:280px; }
  #keyGate button { padding:10px 16px; border-radius:8px; border:none;
                     background:var(--accent); color:white; margin-left:8px; cursor:pointer; }
  #app { display:flex; height:100vh; }
  #list { width:340px; border-right:1px solid var(--border); overflow-y:auto; flex-shrink:0; }
  #list .item { padding:12px 16px; border-bottom:1px solid var(--border); cursor:pointer; }
  #list .item:hover { background:var(--panel); }
  #list .item.active { background:var(--panel); border-left:3px solid var(--accent); }
  #list .id { font-weight:600; font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #list .preview { color:var(--muted); font-size:12.5px; margin-top:3px; overflow:hidden;
                    text-overflow:ellipsis; white-space:nowrap; }
  #list .meta { font-size:11px; color:var(--muted); margin-top:4px; display:flex; gap:8px; }
  .badge { padding:1px 7px; border-radius:10px; font-size:10.5px; background:#2a2f3f; }
  .badge.paused { background:#5a3a1e; color:#ffb877; }
  #thread { flex:1; display:flex; flex-direction:column; min-width:0; }
  #threadHeader { padding:14px 20px; border-bottom:1px solid var(--border);
                  display:flex; justify-content:space-between; align-items:center; }
  #threadHeader h2 { font-size:15px; margin:0; font-weight:600; }
  #threadHeader .sub { color:var(--muted); font-size:12px; margin-top:2px; }
  #resumeBtn { padding:6px 12px; border-radius:6px; border:1px solid var(--border);
               background:transparent; color:var(--text); cursor:pointer; font-size:12.5px; }
  #resumeBtn.show { display:inline-block; } #resumeBtn.hidden { display:none; }
  #messages { flex:1; overflow-y:auto; padding:20px; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:65%; padding:9px 13px; border-radius:12px; font-size:14px; line-height:1.4; }
  .bubble .who { font-size:10.5px; color:var(--muted); margin-bottom:3px; text-transform:uppercase; letter-spacing:.03em; }
  .bubble.lead { align-self:flex-start; background:var(--lead); border-bottom-left-radius:3px; }
  .bubble.agent { align-self:flex-end; background:var(--agent); border-bottom-right-radius:3px; }
  .bubble.owner { align-self:flex-end; background:var(--owner); border-bottom-right-radius:3px; }
  #composer { padding:14px 20px; border-top:1px solid var(--border); display:flex; gap:10px; }
  #composer textarea { flex:1; resize:none; padding:10px 12px; border-radius:8px;
                        border:1px solid var(--border); background:var(--panel); color:var(--text);
                        font-family:inherit; font-size:14px; height:44px; }
  #composer button { padding:0 20px; border-radius:8px; border:none; background:var(--accent);
                      color:white; cursor:pointer; font-size:14px; }
  #composer button:disabled { opacity:.5; cursor:not-allowed; }
  #empty { flex:1; display:flex; align-items:center; justify-content:center; color:var(--muted); }
</style>
</head>
<body>

<div id="keyGate">
  <div>
    <div style="margin-bottom:10px; color:var(--muted);">Enter inbox key</div>
    <input id="keyInput" type="password" placeholder="INBOX_SECRET">
    <button onclick="saveKey()">Enter</button>
  </div>
</div>

<div id="app">
  <div id="list">
    <div id="listHeader" style="padding:10px 16px; border-bottom:1px solid var(--border); font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px;">
      <input type="checkbox" id="showAllChk" onchange="loadList()"> Show notification/spam senders too
    </div>
    <div id="listItems"></div>
  </div>
  <div id="thread">
    <div id="empty">Select a conversation</div>
  </div>
</div>

<script>
let KEY = localStorage.getItem('inbox_key') || '';
let activeId = null;
let pollTimer = null;

function saveKey() {
  KEY = document.getElementById('keyInput').value.trim();
  localStorage.setItem('inbox_key', KEY);
  document.getElementById('keyGate').classList.add('hidden');
  loadList();
}
if (KEY) { document.getElementById('keyGate').classList.add('hidden'); loadList(); }

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'X-Inbox-Key': KEY, 'Content-Type': 'application/json'}, opts.headers || {});
  const res = await fetch(path + (path.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(KEY), opts);
  if (res.status === 401) {
    document.getElementById('keyGate').classList.remove('hidden');
    throw new Error('unauthorized');
  }
  return res.json();
}

async function loadList() {
  try {
    const showAll = document.getElementById('showAllChk').checked;
    const convos = await api('/inbox/api/conversations' + (showAll ? '?all=1' : ''));
    const list = document.getElementById('listItems');
    list.innerHTML = '';
    convos.forEach(c => {
      const div = document.createElement('div');
      div.className = 'item' + (c.id === activeId ? ' active' : '');
      div.onclick = () => openConvo(c.id);
      const when = c.last_at ? new Date(c.last_at).toLocaleString() : '';
      div.innerHTML = `
        <div class="id">${escapeHtml(c.id)}</div>
        <div class="preview">${escapeHtml(c.last_message)}</div>
        <div class="meta">
          <span class="badge">${escapeHtml(c.stage)}</span>
          ${c.human_takeover ? '<span class="badge paused">paused — you have it</span>' : ''}
          <span>${when}</span>
        </div>`;
      list.appendChild(div);
    });
  } catch (e) { /* unauthorized already handled */ }
}

async function openConvo(id) {
  activeId = id;
  await loadList();
  const convo = await api('/inbox/api/conversation/' + encodeURIComponent(id));
  renderThread(id, convo);
}

function renderThread(id, convo) {
  const thread = document.getElementById('thread');
  const paused = !!convo.human_takeover;
  thread.innerHTML = `
    <div id="threadHeader">
      <div>
        <h2>${escapeHtml(id)}</h2>
        <div class="sub">stage: ${escapeHtml(convo.stage || '')} · ${(convo.history || []).length} messages</div>
      </div>
      <button id="resumeBtn" class="${paused ? 'show' : 'hidden'}" onclick="resumeBot('${escapeJs(id)}')">Resume bot</button>
    </div>
    <div id="messages"></div>
    <div id="composer">
      <textarea id="composerInput" placeholder="Type a message to send as yourself…"></textarea>
      <button id="sendBtn" onclick="sendMessage('${escapeJs(id)}')">Send</button>
    </div>`;
  const msgs = document.getElementById('messages');
  (convo.history || []).forEach(h => {
    const b = document.createElement('div');
    b.className = 'bubble ' + (h.role === 'lead' ? 'lead' : (h.role === 'owner' ? 'owner' : 'agent'));
    const who = h.role === 'lead' ? 'Lead' : (h.role === 'owner' ? 'You (manual)' : 'Bot');
    b.innerHTML = `<div class="who">${who} · ${new Date(h.at).toLocaleString()}</div>${escapeHtml(h.text)}`;
    msgs.appendChild(b);
  });
  msgs.scrollTop = msgs.scrollHeight;
}

async function sendMessage(id) {
  const input = document.getElementById('composerInput');
  const text = input.value.trim();
  if (!text) return;
  const btn = document.getElementById('sendBtn');
  btn.disabled = true;
  try {
    const res = await api('/inbox/api/conversation/' + encodeURIComponent(id) + '/send', {
      method: 'POST', body: JSON.stringify({text})
    });
    if (res.error) { alert(res.error); return; }
    input.value = '';
    await openConvo(id);
  } finally { btn.disabled = false; }
}

async function resumeBot(id) {
  await api('/inbox/api/conversation/' + encodeURIComponent(id) + '/resume', {method: 'POST'});
  await openConvo(id);
}

function escapeHtml(s) {
  return (s || '').toString().replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
function escapeJs(s) { return (s || '').replace(/'/g, "\\'"); }

// Refresh the list every 15s so replies that come in while you're
// browsing show up without a manual reload.
pollTimer = setInterval(loadList, 15000);
</script>

</body>
</html>
"""
