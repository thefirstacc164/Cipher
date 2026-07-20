import os
import secrets
import bcrypt
import base64
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================
#   CONFIG
# ============================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL else None

# ============================================
#   HELPERS
# ============================================
def get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()

def is_ip_banned(ip):
    if not sb: return False
    res = sb.table("bans").select("*").eq("ip_address", ip).execute()
    if not res.data: return False
    ban = res.data[0]
    if ban.get("expires_at"):
        if datetime.fromisoformat(ban["expires_at"].replace("Z","+00:00")) < datetime.now(timezone.utc):
            return False
    return True

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "unauthorized"}), 401
        u = sb.table("users").select("is_admin,is_owner").eq("id", session["user_id"]).execute().data
        if not u or not (u[0]["is_admin"] or u[0]["is_owner"]):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper

def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "unauthorized"}), 401
        u = sb.table("users").select("is_owner").eq("id", session["user_id"]).execute().data
        if not u or not u[0]["is_owner"]:
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper

def current_user():
    if "user_id" not in session: return None
    res = sb.table("users").select("*").eq("id", session["user_id"]).execute()
    return res.data[0] if res.data else None

def audit(action, target_type=None, target_id=None, details=None):
    u = current_user()
    if not u: return
    sb.table("audit_log").insert({
        "admin_id": u["id"],
        "admin_username": u["username"],
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id) if target_id else None,
        "details": details,
        "ip_address": get_ip()
    }).execute()

# ============================================
#   BEFORE REQUEST
# ============================================
@app.before_request
def check_ban():
    if request.path.startswith("/static") or request.path == "/health":
        return
    if sb and is_ip_banned(get_ip()):
        return "You are banned.", 403

# ============================================
#   PAGES
# ============================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

# ============================================
#   AUTH
# ============================================
@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    invite_code = data.get("invite_code")

    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username 3+ chars, password 6+"}), 400

    settings = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
    if not settings["signups_enabled"] and not invite_code:
        return jsonify({"error": "Signups disabled. Invite required."}), 403

    invite = None
    if invite_code:
        inv_res = sb.table("invite_links").select("*").eq("code", invite_code).execute()
        if not inv_res.data:
            return jsonify({"error": "Invalid invite code"}), 400
        invite = inv_res.data[0]
        if invite["revoked"]:
            return jsonify({"error": "Invite revoked"}), 400
        if invite["expires_at"] and datetime.fromisoformat(invite["expires_at"].replace("Z","+00:00")) < datetime.now(timezone.utc):
            return jsonify({"error": "Invite expired"}), 400
        if invite["max_uses"] and invite["uses_count"] >= invite["max_uses"]:
            return jsonify({"error": "Invite maxed out"}), 400

    exists = sb.table("users").select("id").eq("username", username).execute()
    if exists.data:
        return jsonify({"error": "Username taken"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_count = sb.table("users").select("id", count="exact").execute().count or 0
    is_owner = user_count == 0

    # Generate recovery phrase (12 random words)
    wordlist = ["cipher","shadow","vault","echo","raven","cobalt","onyx","quartz","zenith","void","phantom","spectre","glacier","aurora","nebula","cosmos","pulse","matrix","binary","protocol","enigma","riddle","token","cypher","stealth","cascade","fracture","circuit","emblem","kernel"]
    phrase = " ".join(secrets.choice(wordlist) for _ in range(12))
    phrase_hash = bcrypt.hashpw(phrase.encode(), bcrypt.gensalt()).decode()

    # Generate recovery key file content
    recovery_key = secrets.token_hex(64)
    key_hash = bcrypt.hashpw(recovery_key.encode(), bcrypt.gensalt()).decode()

    new_user = sb.table("users").insert({
        "username": username,
        "password_hash": pw_hash,
        "is_owner": is_owner,
        "is_admin": is_owner,
        "last_ip": get_ip(),
        "recovery_phrase": phrase_hash
    }).execute().data[0]

    sb.table("recovery_keys").insert({
        "user_id": new_user["id"],
        "key_hash": key_hash,
        "method": "file"
    }).execute()

    if invite:
        sb.table("invite_links").update({"uses_count": invite["uses_count"] + 1}).eq("id", invite["id"]).execute()
        sb.table("invite_uses").insert({
            "invite_id": invite["id"],
            "user_id": new_user["id"],
            "ip_address": get_ip()
        }).execute()

    session["user_id"] = new_user["id"]
    return jsonify({
        "ok": True,
        "user": {"id": new_user["id"], "username": username, "is_owner": is_owner},
        "recovery_phrase": phrase,
        "recovery_key": recovery_key
    })

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    res = sb.table("users").select("*").eq("username", username).execute()
    if not res.data:
        return jsonify({"error": "Invalid credentials"}), 400
    user = res.data[0]

    if user.get("suspended"):
        return jsonify({"error": "Account suspended"}), 403

    # Check active mute/ban punishments
    pun = sb.table("user_punishments").select("*").eq("user_id", user["id"]).eq("active", True).eq("type", "ban").execute().data
    if pun:
        return jsonify({"error": "Account banned: " + (pun[0].get("reason") or "no reason")}), 403

    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 400

    sb.table("users").update({"last_ip": get_ip(), "last_seen": datetime.now(timezone.utc).isoformat()}).eq("id", user["id"]).execute()
    session["user_id"] = user["id"]
    return jsonify({"ok": True, "user": {"id": user["id"], "username": user["username"], "is_owner": user["is_owner"], "is_admin": user["is_admin"]}})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def me():
    u = current_user()
    if not u: return jsonify({"user": None})
    return jsonify({"user": {
        "id": u["id"], "username": u["username"],
        "is_owner": u["is_owner"], "is_admin": u["is_admin"],
        "keep_all_forever": u["keep_all_forever"],
        "notify_before_delete": u["notify_before_delete"],
        "nickname_color": u.get("nickname_color", "#00d9ff"),
        "theme_color": u.get("theme_color", "#00d9ff"),
        "anonymous_mode": u.get("anonymous_mode", False)
    }})

# ============================================
#   PASSWORD RECOVERY
# ============================================
@app.route("/api/recover/phrase", methods=["POST"])
def recover_phrase():
    data = request.json
    username = (data.get("username") or "").strip().lower()
    phrase = (data.get("phrase") or "").strip().lower()
    new_password = data.get("new_password") or ""

    if len(new_password) < 6:
        return jsonify({"error": "New password too short"}), 400

    res = sb.table("users").select("id,recovery_phrase").eq("username", username).execute().data
    if not res: return jsonify({"error": "User not found"}), 404
    user = res[0]
    if not user.get("recovery_phrase"):
        return jsonify({"error": "No recovery phrase set"}), 400
    if not bcrypt.checkpw(phrase.encode(), user["recovery_phrase"].encode()):
        return jsonify({"error": "Wrong recovery phrase"}), 400

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    sb.table("users").update({"password_hash": new_hash}).eq("id", user["id"]).execute()
    return jsonify({"ok": True})

@app.route("/api/recover/key", methods=["POST"])
def recover_key():
    data = request.json
    username = (data.get("username") or "").strip().lower()
    key = (data.get("key") or "").strip()
    new_password = data.get("new_password") or ""

    if len(new_password) < 6:
        return jsonify({"error": "New password too short"}), 400

    user_res = sb.table("users").select("id").eq("username", username).execute().data
    if not user_res: return jsonify({"error": "User not found"}), 404
    uid = user_res[0]["id"]

    keys = sb.table("recovery_keys").select("key_hash").eq("user_id", uid).execute().data
    if not keys: return jsonify({"error": "No recovery key set"}), 400

    for k in keys:
        if bcrypt.checkpw(key.encode(), k["key_hash"].encode()):
            new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
            sb.table("users").update({"password_hash": new_hash}).eq("id", uid).execute()
            return jsonify({"ok": True})

    return jsonify({"error": "Invalid recovery key"}), 400

# ============================================
#   PROFILE / SETTINGS
# ============================================
@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    data = request.json
    allowed = ["nickname_color", "theme_color", "anonymous_mode", "notify_before_delete", "keep_all_forever"]
    upd = {k: v for k, v in data.items() if k in allowed}
    if upd:
        sb.table("users").update(upd).eq("id", session["user_id"]).execute()
    return jsonify({"ok": True})

@app.route("/api/change_password", methods=["POST"])
@login_required
def change_password():
    data = request.json
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    if len(new) < 6: return jsonify({"error": "New password too short"}), 400
    u = current_user()
    if not bcrypt.checkpw(old.encode(), u["password_hash"].encode()):
        return jsonify({"error": "Wrong current password"}), 400
    new_hash = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    sb.table("users").update({"password_hash": new_hash}).eq("id", u["id"]).execute()
    return jsonify({"ok": True})

@app.route("/api/panic", methods=["POST"])
@login_required
def panic():
    uid = session["user_id"]
    sb.table("messages").delete().eq("sender_id", uid).execute()
    sb.table("users").delete().eq("id", uid).execute()
    session.clear()
    return jsonify({"ok": True})

# ============================================
#   CONVERSATIONS
# ============================================
@app.route("/api/conversations")
@login_required
def list_conversations():
    uid = session["user_id"]
    mem = sb.table("conversation_members").select("conversation_id,muted").eq("user_id", uid).execute().data
    if not mem: return jsonify({"conversations": []})
    conv_ids = [m["conversation_id"] for m in mem]
    muted_map = {m["conversation_id"]: m.get("muted", False) for m in mem}
    convs = sb.table("conversations").select("*").in_("id", conv_ids).order("updated_at", desc=True).execute().data
    for c in convs:
        members = sb.table("conversation_members").select("user_id,users(username,nickname_color)").eq("conversation_id", c["id"]).execute().data
        c["members"] = [{"id": m["user_id"], "username": m["users"]["username"], "nickname_color": m["users"].get("nickname_color", "#00d9ff")} for m in members if m.get("users")]
        c["muted"] = muted_map.get(c["id"], False)
    return jsonify({"conversations": convs})

@app.route("/api/conversations/new", methods=["POST"])
@login_required
def new_conversation():
    data = request.json
    other_username = (data.get("username") or "").strip().lower()
    uid = session["user_id"]
    other = sb.table("users").select("id").eq("username", other_username).execute().data
    if not other: return jsonify({"error": "User not found"}), 404
    other_id = other[0]["id"]
    if other_id == uid: return jsonify({"error": "Cannot message yourself"}), 400

    my_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
    their_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", other_id).execute().data
    shared = {c["conversation_id"] for c in my_convs} & {c["conversation_id"] for c in their_convs}
    if shared:
        for cid in shared:
            c = sb.table("conversations").select("is_group").eq("id", cid).execute().data
            if c and not c[0]["is_group"]:
                return jsonify({"ok": True, "conversation_id": cid})

    conv = sb.table("conversations").insert({"created_by": uid, "is_group": False}).execute().data[0]
    sb.table("conversation_members").insert([
        {"conversation_id": conv["id"], "user_id": uid},
        {"conversation_id": conv["id"], "user_id": other_id}
    ]).execute()
    return jsonify({"ok": True, "conversation_id": conv["id"]})

@app.route("/api/conversations/<cid>/leave", methods=["POST"])
@login_required
def leave_conv(cid):
    sb.table("conversation_members").delete().eq("conversation_id", cid).eq("user_id", session["user_id"]).execute()
    remaining = sb.table("conversation_members").select("id", count="exact").eq("conversation_id", cid).execute().count or 0
    if remaining == 0:
        sb.table("conversations").delete().eq("id", cid).execute()
    return jsonify({"ok": True})

@app.route("/api/conversations/<cid>/mute", methods=["POST"])
@login_required
def mute_conv(cid):
    muted = request.json.get("muted", True)
    sb.table("conversation_members").update({"muted": muted}).eq("conversation_id", cid).eq("user_id", session["user_id"]).execute()
    return jsonify({"ok": True})

@app.route("/api/conversations/<cid>/export")
@login_required
def export_conv(cid):
    uid = session["user_id"]
    m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
    if not m: return "Forbidden", 403
    msgs = sb.table("messages").select("*,users:sender_id(username)").eq("conversation_id", cid).eq("deleted", False).order("created_at").execute().data
    lines = ["=== CIPHER CONVERSATION EXPORT ===", f"Exported: {datetime.now(timezone.utc).isoformat()}", ""]
    for m in msgs:
        who = m["users"]["username"] if m.get("users") else "unknown"
        when = m["created_at"]
        content = m.get("content", "") or ("[image]" if m.get("image_url") else "")
        lines.append(f"[{when}] {who}: {content}")
    text = "\n".join(lines)
    return Response(text, mimetype="text/plain", headers={"Content-Disposition": f"attachment; filename=cipher_{cid[:8]}.txt"})

# ============================================
#   MESSAGES
# ============================================
@app.route("/api/messages/<conv_id>")
@login_required
def get_messages(conv_id):
    uid = session["user_id"]
    m = sb.table("conversation_members").select("id").eq("conversation_id", conv_id).eq("user_id", uid).execute().data
    if not m: return jsonify({"error": "not a member"}), 403

    msgs = sb.table("messages").select("*,users:sender_id(username,nickname_color)").eq("conversation_id", conv_id).eq("deleted", False).order("created_at").execute().data

    # Get reactions and reads
    msg_ids = [m["id"] for m in msgs]
    reactions = []
    reads = []
    if msg_ids:
        reactions = sb.table("message_reactions").select("*,users(username)").in_("message_id", msg_ids).execute().data
        reads = sb.table("message_reads").select("*,users(username)").in_("message_id", msg_ids).execute().data

    react_map = {}
    for r in reactions:
        react_map.setdefault(r["message_id"], []).append({"emoji": r["emoji"], "user": r["users"]["username"]})
    read_map = {}
    for r in reads:
        read_map.setdefault(r["message_id"], []).append(r["users"]["username"])

    for m in msgs:
        m["reactions"] = react_map.get(m["id"], [])
        m["read_by"] = read_map.get(m["id"], [])

    # Mark all as read by current user
    for m in msgs:
        if m["sender_id"] != uid:
            try:
                sb.table("message_reads").insert({"message_id": m["id"], "user_id": uid}).execute()
            except: pass

    return jsonify({"messages": msgs})

@app.route("/api/messages/<conv_id>", methods=["POST"])
@login_required
def send_message(conv_id):
    uid = session["user_id"]
    user = current_user()
    if not user.get("can_send_messages", True):
        return jsonify({"error": "You cannot send messages"}), 403

    # Check for active mute punishment
    pun = sb.table("user_punishments").select("*").eq("user_id", uid).eq("active", True).eq("type", "mute").execute().data
    if pun:
        return jsonify({"error": "You are muted: " + (pun[0].get("reason") or "no reason")}), 403

    m = sb.table("conversation_members").select("id").eq("conversation_id", conv_id).eq("user_id", uid).execute().data
    if not m: return jsonify({"error": "not a member"}), 403

    data = request.json
    content = (data.get("content") or "").strip()
    image_data = data.get("image_data")
    is_anon = data.get("anonymous", False) and user.get("anonymous_mode", False)

    if not content and not image_data:
        return jsonify({"error": "Empty message"}), 400

    image_url = None
    if image_data:
        try:
            if "," in image_data:
                image_data = image_data.split(",")[1]
            img_bytes = base64.b64decode(image_data)
            if len(img_bytes) > 5 * 1024 * 1024:
                return jsonify({"error": "Image too large (max 5MB)"}), 400
            filename = f"{uid}/{secrets.token_hex(8)}.jpg"
            sb.storage.from_("cipher-images").upload(filename, img_bytes, {"content-type": "image/jpeg"})
            image_url = sb.storage.from_("cipher-images").get_public_url(filename)
        except Exception as e:
            return jsonify({"error": f"Upload failed: {str(e)}"}), 500

    conv = sb.table("conversations").select("keep_forever").eq("id", conv_id).execute().data[0]
    settings = sb.table("admin_settings").select("default_retention_days").eq("id", 1).execute().data[0]
    if conv["keep_forever"] or user.get("keep_all_forever"):
        expires_at = None
    else:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=settings["default_retention_days"])).isoformat()

    msg = sb.table("messages").insert({
        "conversation_id": conv_id,
        "sender_id": uid,
        "content": content,
        "image_url": image_url,
        "is_anonymous": is_anon,
        "expires_at": expires_at
    }).execute().data[0]

    sb.table("conversations").update({"updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", conv_id).execute()
    return jsonify({"ok": True, "message": msg})

@app.route("/api/messages/<msg_id>/react", methods=["POST"])
@login_required
def react_message(msg_id):
    emoji = request.json.get("emoji", "").strip()
    if not emoji or len(emoji) > 8: return jsonify({"error": "invalid emoji"}), 400
    uid = session["user_id"]
    existing = sb.table("message_reactions").select("id").eq("message_id", msg_id).eq("user_id", uid).eq("emoji", emoji).execute().data
    if existing:
        sb.table("message_reactions").delete().eq("id", existing[0]["id"]).execute()
    else:
        sb.table("message_reactions").insert({"message_id": msg_id, "user_id": uid, "emoji": emoji}).execute()
    return jsonify({"ok": True})

@app.route("/api/messages/<msg_id>/extend", methods=["POST"])
@login_required
def extend_message(msg_id):
    new_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sb.table("messages").update({"expires_at": new_exp, "warning_sent": False}).eq("id", msg_id).execute()
    return jsonify({"ok": True})

# ============================================
#   TYPING STATUS
# ============================================
@app.route("/api/typing/<conv_id>", methods=["POST"])
@login_required
def set_typing(conv_id):
    uid = session["user_id"]
    sb.table("typing_status").upsert({
        "user_id": uid, "conversation_id": conv_id,
        "started_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    return jsonify({"ok": True})

@app.route("/api/typing/<conv_id>", methods=["GET"])
@login_required
def get_typing(conv_id):
    uid = session["user_id"]
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    typing = sb.table("typing_status").select("*,users(username)").eq("conversation_id", conv_id).gt("started_at", cutoff).execute().data
    users = [t["users"]["username"] for t in typing if t["user_id"] != uid and t.get("users")]
    return jsonify({"typing": users})

# ============================================
#   SEARCH
# ============================================
@app.route("/api/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2: return jsonify({"results": []})
    uid = session["user_id"]
    mem = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
    conv_ids = [m["conversation_id"] for m in mem]
    if not conv_ids: return jsonify({"results": []})
    results = sb.table("messages").select("*,users:sender_id(username)").in_("conversation_id", conv_ids).eq("deleted", False).ilike("content", f"%{q}%").order("created_at", desc=True).limit(50).execute().data
    return jsonify({"results": results})

# ============================================
#   INVITES
# ============================================
@app.route("/api/invites", methods=["GET"])
@login_required
def list_invites():
    uid = session["user_id"]
    invs = sb.table("invite_links").select("*").eq("created_by", uid).order("created_at", desc=True).execute().data
    return jsonify({"invites": invs})

@app.route("/api/invites", methods=["POST"])
@login_required
def create_invite():
    user = current_user()
    settings = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
    if not settings["invites_enabled"]: return jsonify({"error": "Invites disabled"}), 403
    mode = settings["invite_creation_mode"]
    if mode == "admins_only" and not (user["is_admin"] or user["is_owner"]):
        return jsonify({"error": "Only admins can invite"}), 403
    if not user.get("can_create_invites", True):
        return jsonify({"error": "You cannot create invites"}), 403

    data = request.json or {}
    max_uses = data.get("max_uses")
    expires_hours = data.get("expires_hours")
    exp = (datetime.now(timezone.utc) + timedelta(hours=int(expires_hours))).isoformat() if expires_hours else None
    code = secrets.token_urlsafe(8)
    inv = sb.table("invite_links").insert({
        "code": code, "created_by": user["id"], "max_uses": max_uses, "expires_at": exp
    }).execute().data[0]
    return jsonify({"ok": True, "invite": inv, "url": request.host_url + "?invite=" + code})

@app.route("/api/invites/<inv_id>/revoke", methods=["POST"])
@login_required
def revoke_invite(inv_id):
    uid = session["user_id"]
    user = current_user()
    inv = sb.table("invite_links").select("*").eq("id", inv_id).execute().data
    if not inv: return jsonify({"error": "not found"}), 404
    if inv[0]["created_by"] != uid and not (user["is_admin"] or user["is_owner"]):
        return jsonify({"error": "forbidden"}), 403
    sb.table("invite_links").update({"revoked": True}).eq("id", inv_id).execute()
    return jsonify({"ok": True})

# ============================================
#   ANNOUNCEMENTS
# ============================================
@app.route("/api/announcements")
def get_announcements():
    anns = sb.table("announcements").select("*").eq("active", True).order("created_at", desc=True).limit(3).execute().data
    return jsonify({"announcements": anns})

@app.route("/api/announcements", methods=["POST"])
@admin_required
def create_announcement():
    data = request.json
    ann = sb.table("announcements").insert({
        "title": data.get("title", ""),
        "content": data.get("content", ""),
        "priority": data.get("priority", "info"),
        "created_by": session["user_id"]
    }).execute().data[0]
    audit("create_announcement", "announcement", ann["id"], data.get("title"))
    return jsonify({"ok": True, "announcement": ann})

@app.route("/api/announcements/<ann_id>", methods=["DELETE"])
@admin_required
def delete_announcement(ann_id):
    sb.table("announcements").update({"active": False}).eq("id", ann_id).execute()
    audit("delete_announcement", "announcement", ann_id)
    return jsonify({"ok": True})

# ============================================
#   ADMIN
# ============================================
@app.route("/api/admin/users")
@admin_required
def admin_users():
    users = sb.table("users").select("id,username,is_admin,is_owner,suspended,can_create_invites,can_send_messages,keep_all_forever,last_ip,created_at,last_seen,nickname_color").order("created_at").execute().data
    return jsonify({"users": users})

@app.route("/api/admin/user/<uid>", methods=["POST"])
@admin_required
def admin_update_user(uid):
    data = request.json
    allowed = ["is_admin", "suspended", "can_create_invites", "can_send_messages", "can_upload_files", "keep_all_forever"]
    upd = {k: v for k, v in data.items() if k in allowed}
    if upd:
        sb.table("users").update(upd).eq("id", uid).execute()
        audit("update_user", "user", uid, str(upd))
    return jsonify({"ok": True})

@app.route("/api/admin/user/<uid>/reset_password", methods=["POST"])
@admin_required
def admin_reset_password(uid):
    new_pass = secrets.token_urlsafe(12)
    new_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
    sb.table("users").update({"password_hash": new_hash}).eq("id", uid).execute()
    audit("reset_password", "user", uid)
    return jsonify({"ok": True, "new_password": new_pass})

@app.route("/api/admin/user/<uid>/punish", methods=["POST"])
@admin_required
def admin_punish(uid):
    data = request.json
    ptype = data.get("type")
    reason = data.get("reason", "")
    hours = data.get("hours")
    if ptype not in ["warn", "mute", "ban"]:
        return jsonify({"error": "invalid type"}), 400
    exp = (datetime.now(timezone.utc) + timedelta(hours=int(hours))).isoformat() if hours else None
    sb.table("user_punishments").insert({
        "user_id": uid, "punished_by": session["user_id"],
        "type": ptype, "reason": reason, "expires_at": exp
    }).execute()
    audit(f"punish_{ptype}", "user", uid, reason)
    return jsonify({"ok": True})

@app.route("/api/admin/user/<uid>/punishments", methods=["GET"])
@admin_required
def get_punishments(uid):
    puns = sb.table("user_punishments").select("*").eq("user_id", uid).order("created_at", desc=True).execute().data
    return jsonify({"punishments": puns})

@app.route("/api/admin/punishment/<pid>/remove", methods=["POST"])
@admin_required
def remove_punishment(pid):
    sb.table("user_punishments").update({"active": False}).eq("id", pid).execute()
    audit("remove_punishment", "punishment", pid)
    return jsonify({"ok": True})

@app.route("/api/admin/ban", methods=["POST"])
@admin_required
def admin_ban():
    data = request.json
    ip = data.get("ip"); reason = data.get("reason", "")
    if not ip: return jsonify({"error": "no ip"}), 400
    sb.table("bans").upsert({"ip_address": ip, "reason": reason, "banned_by": session["user_id"]}).execute()
    audit("ban_ip", "ip", ip, reason)
    return jsonify({"ok": True})

@app.route("/api/admin/unban", methods=["POST"])
@admin_required
def admin_unban():
    ip = request.json.get("ip")
    sb.table("bans").delete().eq("ip_address", ip).execute()
    audit("unban_ip", "ip", ip)
    return jsonify({"ok": True})

@app.route("/api/admin/bans")
@admin_required
def list_bans():
    bans = sb.table("bans").select("*").order("created_at", desc=True).execute().data
    return jsonify({"bans": bans})

@app.route("/api/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings_route():
    if request.method == "GET":
        s = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
        return jsonify({"settings": s})
    data = request.json
    allowed = ["site_name", "max_file_size_mb", "default_retention_days", "signups_enabled", "invites_enabled", "invite_creation_mode", "maintenance_mode", "registration_message"]
    upd = {k: v for k, v in data.items() if k in allowed}
    if upd:
        sb.table("admin_settings").update(upd).eq("id", 1).execute()
        audit("update_settings", "settings", 1, str(upd))
    return jsonify({"ok": True})

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    users_count = sb.table("users").select("id", count="exact").execute().count or 0
    msgs_count = sb.table("messages").select("id", count="exact").eq("deleted", False).execute().count or 0
    convs_count = sb.table("conversations").select("id", count="exact").execute().count or 0
    bans_count = sb.table("bans").select("id", count="exact").execute().count or 0
    active_24h = sb.table("users").select("id", count="exact").gt("last_seen", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()).execute().count or 0
    return jsonify({
        "users": users_count, "messages": msgs_count,
        "conversations": convs_count, "bans": bans_count,
        "active_24h": active_24h
    })

@app.route("/api/admin/audit")
@admin_required
def get_audit():
    logs = sb.table("audit_log").select("*").order("created_at", desc=True).limit(100).execute().data
    return jsonify({"logs": logs})

@app.route("/api/admin/conversation/<cid>/keep", methods=["POST"])
@admin_required
def admin_keep_conv(cid):
    keep = request.json.get("keep_forever", True)
    sb.table("conversations").update({"keep_forever": keep}).eq("id", cid).execute()
    if keep:
        sb.table("messages").update({"expires_at": None}).eq("conversation_id", cid).execute()
    audit("keep_conversation", "conversation", cid, str(keep))
    return jsonify({"ok": True})

# ============================================
#   AUTO-DELETE
# ============================================
def cleanup_expired():
    if not sb: return
    try:
        now = datetime.now(timezone.utc).isoformat()
        sb.table("messages").update({"deleted": True}).lt("expires_at", now).eq("deleted", False).execute()
        # Cleanup expired punishments
        sb.table("user_punishments").update({"active": False}).lt("expires_at", now).eq("active", True).execute()
        print(f"[cleanup] {now}")
    except Exception as e:
        print(f"[cleanup] Error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_expired, "interval", hours=1)
scheduler.start()

# ============================================
#   MAIN
# ============================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
