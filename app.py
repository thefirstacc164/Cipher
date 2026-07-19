import os
import secrets
import bcrypt
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================
#   CONFIG
# ============================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️  WARNING: Supabase env vars not set!")

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
            return redirect(url_for("index"))
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

def current_user():
    if "user_id" not in session: return None
    res = sb.table("users").select("*").eq("id", session["user_id"]).execute()
    return res.data[0] if res.data else None

# ============================================
#   BEFORE REQUEST — IP BAN CHECK
# ============================================
@app.before_request
def check_ban():
    if request.path.startswith("/static") or request.path == "/health":
        return
    if sb and is_ip_banned(get_ip()):
        return "You are banned.", 403

# ============================================
#   ROUTES — PAGES
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
        return jsonify({"error": "Username must be 3+ chars, password 6+"}), 400

    # Check signups enabled
    settings = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
    if not settings["signups_enabled"] and not invite_code:
        return jsonify({"error": "Signups are currently disabled. You need an invite."}), 403

    # Check invite if provided
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

    # Check user exists
    exists = sb.table("users").select("id").eq("username", username).execute()
    if exists.data:
        return jsonify({"error": "Username taken"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # First user becomes owner
    user_count = sb.table("users").select("id", count="exact").execute().count or 0
    is_owner = user_count == 0

    new_user = sb.table("users").insert({
        "username": username,
        "password_hash": pw_hash,
        "is_owner": is_owner,
        "is_admin": is_owner,
        "last_ip": get_ip()
    }).execute().data[0]

    # Handle invite use
    if invite:
        sb.table("invite_links").update({"uses_count": invite["uses_count"] + 1}).eq("id", invite["id"]).execute()
        sb.table("invite_uses").insert({
            "invite_id": invite["id"],
            "user_id": new_user["id"],
            "ip_address": get_ip()
        }).execute()
        # Auto-add to conversation if invite has one
        if invite.get("auto_add_to_conversation"):
            sb.table("conversation_members").insert({
                "conversation_id": invite["auto_add_to_conversation"],
                "user_id": new_user["id"]
            }).execute()

    session["user_id"] = new_user["id"]
    return jsonify({"ok": True, "user": {"id": new_user["id"], "username": username, "is_owner": is_owner}})

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
        "notify_before_delete": u["notify_before_delete"]
    }})

# ============================================
#   CONVERSATIONS & MESSAGES
# ============================================
@app.route("/api/conversations")
@login_required
def list_conversations():
    uid = session["user_id"]
    # Get conversations user is in
    mem = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
    conv_ids = [m["conversation_id"] for m in mem]
    if not conv_ids:
        return jsonify({"conversations": []})
    convs = sb.table("conversations").select("*").in_("id", conv_ids).order("updated_at", desc=True).execute().data

    # Attach members
    for c in convs:
        members = sb.table("conversation_members").select("user_id,users(username)").eq("conversation_id", c["id"]).execute().data
        c["members"] = [{"id": m["user_id"], "username": m["users"]["username"]} for m in members if m.get("users")]
    return jsonify({"conversations": convs})

@app.route("/api/conversations/new", methods=["POST"])
@login_required
def new_conversation():
    data = request.json
    other_username = (data.get("username") or "").strip().lower()
    uid = session["user_id"]

    other = sb.table("users").select("id").eq("username", other_username).execute().data
    if not other:
        return jsonify({"error": "User not found"}), 404
    other_id = other[0]["id"]
    if other_id == uid:
        return jsonify({"error": "Cannot message yourself"}), 400

    # Check if DM already exists
    my_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
    their_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", other_id).execute().data
    my_ids = {c["conversation_id"] for c in my_convs}
    their_ids = {c["conversation_id"] for c in their_convs}
    shared = my_ids & their_ids
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

@app.route("/api/messages/<conv_id>")
@login_required
def get_messages(conv_id):
    uid = session["user_id"]
    m = sb.table("conversation_members").select("id").eq("conversation_id", conv_id).eq("user_id", uid).execute().data
    if not m: return jsonify({"error": "not a member"}), 403

    msgs = sb.table("messages").select("*,users:sender_id(username)").eq("conversation_id", conv_id).eq("deleted", False).order("created_at").execute().data
    return jsonify({"messages": msgs})

@app.route("/api/messages/<conv_id>", methods=["POST"])
@login_required
def send_message(conv_id):
    uid = session["user_id"]
    user = current_user()
    if not user.get("can_send_messages", True):
        return jsonify({"error": "You are not allowed to send messages"}), 403

    m = sb.table("conversation_members").select("id").eq("conversation_id", conv_id).eq("user_id", uid).execute().data
    if not m: return jsonify({"error": "not a member"}), 403

    data = request.json
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400

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
        "expires_at": expires_at
    }).execute().data[0]

    sb.table("conversations").update({"updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", conv_id).execute()
    return jsonify({"ok": True, "message": msg})

@app.route("/api/messages/<msg_id>/extend", methods=["POST"])
@login_required
def extend_message(msg_id):
    msg = sb.table("messages").select("*").eq("id", msg_id).execute().data
    if not msg: return jsonify({"error": "not found"}), 404
    new_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sb.table("messages").update({"expires_at": new_exp, "warning_sent": False}).eq("id", msg_id).execute()
    return jsonify({"ok": True})

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

    if not settings["invites_enabled"]:
        return jsonify({"error": "Invites disabled by admin"}), 403

    mode = settings["invite_creation_mode"]
    if mode == "admins_only" and not (user["is_admin"] or user["is_owner"]):
        return jsonify({"error": "Only admins can create invites"}), 403
    if not user.get("can_create_invites", True):
        return jsonify({"error": "You cannot create invites"}), 403

    data = request.json or {}
    max_uses = data.get("max_uses")
    expires_hours = data.get("expires_hours")

    exp = None
    if expires_hours:
        exp = (datetime.now(timezone.utc) + timedelta(hours=int(expires_hours))).isoformat()

    code = secrets.token_urlsafe(8)
    inv = sb.table("invite_links").insert({
        "code": code,
        "created_by": user["id"],
        "max_uses": max_uses,
        "expires_at": exp
    }).execute().data[0]
    return jsonify({"ok": True, "invite": inv, "url": request.host_url + "?invite=" + code})

@app.route("/api/invites/<inv_id>/revoke", methods=["POST"])
@login_required
def revoke_invite(inv_id):
    uid = session["user_id"]
    inv = sb.table("invite_links").select("*").eq("id", inv_id).execute().data
    if not inv: return jsonify({"error": "not found"}), 404
    user = current_user()
    if inv[0]["created_by"] != uid and not (user["is_admin"] or user["is_owner"]):
        return jsonify({"error": "forbidden"}), 403
    sb.table("invite_links").update({"revoked": True}).eq("id", inv_id).execute()
    return jsonify({"ok": True})

# ============================================
#   ADMIN PANEL
# ============================================
@app.route("/api/admin/users")
@admin_required
def admin_users():
    users = sb.table("users").select("id,username,is_admin,is_owner,suspended,can_create_invites,can_send_messages,keep_all_forever,last_ip,created_at,last_seen").order("created_at").execute().data
    return jsonify({"users": users})

@app.route("/api/admin/user/<uid>", methods=["POST"])
@admin_required
def admin_update_user(uid):
    data = request.json
    allowed = ["is_admin", "suspended", "can_create_invites", "can_send_messages", "can_upload_files", "keep_all_forever"]
    upd = {k: v for k, v in data.items() if k in allowed}
    if upd:
        sb.table("users").update(upd).eq("id", uid).execute()
    return jsonify({"ok": True})

@app.route("/api/admin/ban", methods=["POST"])
@admin_required
def admin_ban():
    data = request.json
    ip = data.get("ip")
    reason = data.get("reason", "")
    if not ip: return jsonify({"error": "no ip"}), 400
    sb.table("bans").upsert({
        "ip_address": ip, "reason": reason, "banned_by": session["user_id"]
    }).execute()
    return jsonify({"ok": True})

@app.route("/api/admin/unban", methods=["POST"])
@admin_required
def admin_unban():
    ip = request.json.get("ip")
    sb.table("bans").delete().eq("ip_address", ip).execute()
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
    allowed = ["site_name", "max_file_size_mb", "default_retention_days", "signups_enabled", "invites_enabled", "invite_creation_mode", "maintenance_mode"]
    upd = {k: v for k, v in data.items() if k in allowed}
    if upd:
        sb.table("admin_settings").update(upd).eq("id", 1).execute()
    return jsonify({"ok": True})

@app.route("/api/admin/conversation/<cid>/keep", methods=["POST"])
@admin_required
def admin_keep_conv(cid):
    keep = request.json.get("keep_forever", True)
    sb.table("conversations").update({"keep_forever": keep}).eq("id", cid).execute()
    if keep:
        sb.table("messages").update({"expires_at": None}).eq("conversation_id", cid).execute()
    return jsonify({"ok": True})

# ============================================
#   AUTO-DELETE SCHEDULER
# ============================================
def cleanup_expired():
    if not sb: return
    try:
        now = datetime.now(timezone.utc).isoformat()
        sb.table("messages").update({"deleted": True}).lt("expires_at", now).eq("deleted", False).execute()
        print(f"[cleanup] Ran at {now}")
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
