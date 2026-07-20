"""
============================================================
  CIPHER v1.0.0 — Backend
  Solo project by Stepundrik
============================================================
"""

import os
import io
import base64
import json
import secrets
import bcrypt
import pyotp
import qrcode
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, Response, send_file
)
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
#   APP CONFIG
# ============================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️  WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing!")
    sb = None
else:
    sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ============================================================
#   HELPERS
# ============================================================
def get_ip():
    """Get client IP, respecting X-Forwarded-For header."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_ip_banned(ip):
    """Check if an IP is banned. Handles expired bans gracefully."""
    if not sb or not ip:
        return False
    try:
        res = sb.table("bans").select("*").eq("ip_address", ip).execute()
        if not res.data:
            return False
        ban = res.data[0]
        if ban.get("expires_at"):
            exp = datetime.fromisoformat(ban["expires_at"].replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                # Expired, auto-cleanup
                sb.table("bans").delete().eq("ip_address", ip).execute()
                return False
        return True
    except Exception as e:
        print(f"[is_ip_banned] {e}")
        return False


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not signed in"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not signed in"}), 401
        try:
            u = sb.table("users").select("is_admin,is_owner").eq("id", session["user_id"]).execute().data
            if not u or not (u[0].get("is_admin") or u[0].get("is_owner")):
                return jsonify({"error": "Admins only"}), 403
        except Exception:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not signed in"}), 401
        try:
            u = sb.table("users").select("is_owner").eq("id", session["user_id"]).execute().data
            if not u or not u[0].get("is_owner"):
                return jsonify({"error": "Owner only"}), 403
        except Exception:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    try:
        res = sb.table("users").select("*").eq("id", session["user_id"]).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def audit(action, target_type=None, target_id=None, details=None):
    """Log admin actions for the audit trail."""
    try:
        u = current_user()
        if not u:
            return
        sb.table("audit_log").insert({
            "admin_id": u["id"],
            "admin_username": u["username"],
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id) if target_id else None,
            "details": details,
            "ip_address": get_ip()
        }).execute()
    except Exception as e:
        print(f"[audit] {e}")


def generate_recovery_phrase():
    """Generate a random 12-word recovery phrase."""
    wordlist = [
        "cipher", "shadow", "vault", "echo", "raven", "cobalt", "onyx", "quartz",
        "zenith", "void", "phantom", "spectre", "glacier", "aurora", "nebula",
        "cosmos", "pulse", "matrix", "binary", "protocol", "enigma", "riddle",
        "token", "cypher", "stealth", "cascade", "fracture", "circuit", "emblem",
        "kernel", "beacon", "vector", "prism", "static", "signal", "orbit",
        "helix", "photon", "quantum", "vertex"
    ]
    return " ".join(secrets.choice(wordlist) for _ in range(12))


# ============================================================
#   BEFORE-REQUEST: IP BAN CHECK
# ============================================================
@app.before_request
def check_before_request():
    # Whitelist paths — never blocked
    open_paths = ["/static", "/health", "/favicon.ico"]
    for p in open_paths:
        if request.path.startswith(p):
            return

    # IP ban check
    ip = get_ip()
    if sb and ip and is_ip_banned(ip):
        # CRITICAL: Owners and admins are never blocked by IP ban
        if "user_id" in session:
            try:
                u = sb.table("users").select("is_owner,is_admin").eq("id", session["user_id"]).execute().data
                if u and (u[0].get("is_owner") or u[0].get("is_admin")):
                    return  # allow through
            except Exception:
                pass
        return jsonify({"error": "Your IP is banned from this service.", "banned": True}), 403


# ============================================================
#   BASIC ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_iso(), "version": "1.0.0"})


# ============================================================
#   AUTH — SIGNUP
# ============================================================
@app.route("/api/signup", methods=["POST"])
def signup():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        password = data.get("password") or ""
        invite_code = (data.get("invite_code") or "").strip()

        # Validation
        if len(username) < 3:
            return jsonify({"error": "Username must be at least 3 characters"}), 400
        if len(username) > 20:
            return jsonify({"error": "Username must be 20 characters or fewer"}), 400
        if not username.replace("_", "").isalnum():
            return jsonify({"error": "Username can only contain letters, numbers, and underscores"}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400

        # Check global settings
        settings_res = sb.table("admin_settings").select("*").eq("id", 1).execute().data
        settings = settings_res[0] if settings_res else {}
        if not settings.get("signups_enabled", True) and not invite_code:
            return jsonify({"error": "Signups are currently disabled. You need an invite code."}), 403

        # Validate invite if provided
        invite = None
        if invite_code:
            inv_res = sb.table("invite_links").select("*").eq("code", invite_code).execute().data
            if not inv_res:
                return jsonify({"error": "Invalid invite code"}), 400
            invite = inv_res[0]
            if invite.get("revoked"):
                return jsonify({"error": "This invite has been revoked"}), 400
            if invite.get("expires_at"):
                exp = datetime.fromisoformat(invite["expires_at"].replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc):
                    return jsonify({"error": "This invite has expired"}), 400
            if invite.get("max_uses") and invite.get("uses_count", 0) >= invite["max_uses"]:
                return jsonify({"error": "This invite has reached its maximum uses"}), 400

        # Uniqueness check
        exists = sb.table("users").select("id").eq("username", username).execute().data
        if exists:
            return jsonify({"error": "Username already taken"}), 400

        # Hash password
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        # First-ever user becomes owner
        user_count = sb.table("users").select("id", count="exact").execute().count or 0
        is_owner = (user_count == 0)

        # Generate recovery phrase + key
        phrase = generate_recovery_phrase()
        phrase_hash = bcrypt.hashpw(phrase.encode(), bcrypt.gensalt()).decode()
        recovery_key = secrets.token_hex(48)
        key_hash = bcrypt.hashpw(recovery_key.encode(), bcrypt.gensalt()).decode()

        # Insert user
        new_user = sb.table("users").insert({
            "username": username,
            "password_hash": pw_hash,
            "is_owner": is_owner,
            "is_admin": is_owner,
            "last_ip": get_ip(),
            "recovery_phrase": phrase_hash,
            "nickname_color": "#00d9ff",
            "theme_color": "#00d9ff"
        }).execute().data[0]

        # Insert recovery key
        sb.table("recovery_keys").insert({
            "user_id": new_user["id"],
            "key_hash": key_hash,
            "method": "file"
        }).execute()

        # Handle invite
        if invite:
            sb.table("invite_links").update({
                "uses_count": invite.get("uses_count", 0) + 1
            }).eq("id", invite["id"]).execute()
            sb.table("invite_uses").insert({
                "invite_id": invite["id"],
                "user_id": new_user["id"],
                "ip_address": get_ip()
            }).execute()

        # Log in
        session.permanent = True
        session["user_id"] = new_user["id"]

        return jsonify({
            "ok": True,
            "user": {
                "id": new_user["id"],
                "username": username,
                "is_owner": is_owner,
                "is_admin": is_owner
            },
            "recovery_phrase": phrase,
            "recovery_key": recovery_key
        })
    except Exception as e:
        print(f"[signup] {e}")
        return jsonify({"error": f"Something went wrong: {str(e)}"}), 500


# ============================================================
#   AUTH — LOGIN (with optional 2FA)
# ============================================================
@app.route("/api/login", methods=["POST"])
def login():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        password = data.get("password") or ""
        totp_code = (data.get("totp") or "").strip()

        res = sb.table("users").select("*").eq("username", username).execute().data
        if not res:
            return jsonify({"error": "Invalid username or password"}), 400
        user = res[0]

        if user.get("suspended"):
            return jsonify({"error": "This account has been suspended"}), 403

        # Check active ban punishment
        try:
            pun = sb.table("user_punishments").select("*").eq("user_id", user["id"]).eq("active", True).eq("type", "ban").execute().data
            if pun:
                # Check if expired
                p = pun[0]
                still_active = True
                if p.get("expires_at"):
                    exp = datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00"))
                    if exp < datetime.now(timezone.utc):
                        sb.table("user_punishments").update({"active": False}).eq("id", p["id"]).execute()
                        still_active = False
                if still_active:
                    reason = p.get("reason") or "No reason given"
                    return jsonify({"error": f"Account banned: {reason}"}), 403
        except Exception:
            pass

        # Verify password
        if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return jsonify({"error": "Invalid username or password"}), 400

        # 2FA check
        if user.get("totp_enabled") and user.get("totp_secret"):
            if not totp_code:
                return jsonify({"needs_2fa": True}), 200
            totp = pyotp.TOTP(user["totp_secret"])
            if not totp.verify(totp_code, valid_window=1):
                return jsonify({"error": "Invalid 2FA code", "needs_2fa": True}), 400

        # Update last seen
        sb.table("users").update({
            "last_ip": get_ip(),
            "last_seen": now_iso()
        }).eq("id", user["id"]).execute()

        session.permanent = True
        session["user_id"] = user["id"]

        return jsonify({
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "is_owner": user.get("is_owner", False),
                "is_admin": user.get("is_admin", False)
            }
        })
    except Exception as e:
        print(f"[login] {e}")
        return jsonify({"error": f"Login failed: {str(e)}"}), 500


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({
        "user": {
            "id": u["id"],
            "username": u["username"],
            "is_owner": u.get("is_owner", False),
            "is_admin": u.get("is_admin", False),
            "keep_all_forever": u.get("keep_all_forever", False),
            "notify_before_delete": u.get("notify_before_delete", True),
            "nickname_color": u.get("nickname_color") or "#00d9ff",
            "theme_color": u.get("theme_color") or "#00d9ff",
            "anonymous_mode": u.get("anonymous_mode", False),
            "totp_enabled": u.get("totp_enabled", False)
        }
    })


# ============================================================
#   PASSWORD RECOVERY
# ============================================================
@app.route("/api/recover/phrase", methods=["POST"])
def recover_phrase():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        phrase = (data.get("phrase") or "").strip().lower()
        new_password = data.get("new_password") or ""

        if len(new_password) < 6:
            return jsonify({"error": "New password must be at least 6 characters"}), 400
        if not phrase:
            return jsonify({"error": "Recovery phrase is required"}), 400

        res = sb.table("users").select("id,recovery_phrase").eq("username", username).execute().data
        if not res:
            return jsonify({"error": "User not found"}), 404

        user = res[0]
        if not user.get("recovery_phrase"):
            return jsonify({"error": "No recovery phrase was set for this account"}), 400

        if not bcrypt.checkpw(phrase.encode(), user["recovery_phrase"].encode()):
            return jsonify({"error": "Incorrect recovery phrase"}), 400

        new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        sb.table("users").update({"password_hash": new_hash}).eq("id", user["id"]).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/recover/key", methods=["POST"])
def recover_key():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        key = (data.get("key") or "").strip()
        new_password = data.get("new_password") or ""

        if len(new_password) < 6:
            return jsonify({"error": "New password must be at least 6 characters"}), 400
        if not key:
            return jsonify({"error": "Recovery key is required"}), 400

        user_res = sb.table("users").select("id").eq("username", username).execute().data
        if not user_res:
            return jsonify({"error": "User not found"}), 404
        uid = user_res[0]["id"]

        keys = sb.table("recovery_keys").select("key_hash").eq("user_id", uid).execute().data
        if not keys:
            return jsonify({"error": "No recovery key was set for this account"}), 400

        for k in keys:
            try:
                if bcrypt.checkpw(key.encode(), k["key_hash"].encode()):
                    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
                    sb.table("users").update({"password_hash": new_hash}).eq("id", uid).execute()
                    return jsonify({"ok": True})
            except Exception:
                continue

        return jsonify({"error": "Invalid recovery key"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   PROFILE / SETTINGS
# ============================================================
@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    try:
        data = request.json or {}
        allowed = ["nickname_color", "theme_color", "anonymous_mode",
                   "notify_before_delete", "keep_all_forever"]
        upd = {}
        for k in allowed:
            if k in data:
                upd[k] = data[k]
        if upd:
            sb.table("users").update(upd).eq("id", session["user_id"]).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/change_password", methods=["POST"])
@login_required
def change_password():
    try:
        data = request.json or {}
        old = data.get("old_password") or ""
        new = data.get("new_password") or ""

        if len(new) < 6:
            return jsonify({"error": "New password must be at least 6 characters"}), 400

        u = current_user()
        if not u:
            return jsonify({"error": "Session expired"}), 401

        if not bcrypt.checkpw(old.encode(), u["password_hash"].encode()):
            return jsonify({"error": "Current password is incorrect"}), 400

        new_hash = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
        sb.table("users").update({"password_hash": new_hash}).eq("id", u["id"]).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete_account", methods=["POST"])
@login_required
def delete_account():
    """Fully delete user account. Requires typing DELETE to confirm."""
    try:
        data = request.json or {}
        if data.get("confirm") != "DELETE":
            return jsonify({"error": "Confirmation text does not match"}), 400

        uid = session["user_id"]

        # Mark messages as deleted (keep for others in conversation)
        sb.table("messages").update({
            "deleted": True,
            "content": "[deleted]",
            "image_url": None
        }).eq("sender_id", uid).execute()

        # Remove from conversations
        sb.table("conversation_members").delete().eq("user_id", uid).execute()

        # Delete related data
        sb.table("recovery_keys").delete().eq("user_id", uid).execute()
        sb.table("message_reactions").delete().eq("user_id", uid).execute()
        sb.table("message_reads").delete().eq("user_id", uid).execute()
        sb.table("typing_status").delete().eq("user_id", uid).execute()

        # Delete user
        sb.table("users").delete().eq("id", uid).execute()

        session.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   2FA SETUP
# ============================================================
@app.route("/api/2fa/setup", methods=["POST"])
@login_required
def setup_2fa():
    """Generate a new TOTP secret + QR code."""
    try:
        u = current_user()
        secret = pyotp.random_base32()
        # Store TEMPORARILY in session, only save to DB when confirmed
        session["pending_totp_secret"] = secret

        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name=u["username"], issuer_name="Cipher")

        # Generate QR code as base64 PNG
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#00d9ff", back_color="#0b0f16")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()

        return jsonify({
            "ok": True,
            "secret": secret,
            "qr": f"data:image/png;base64,{qr_b64}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/2fa/enable", methods=["POST"])
@login_required
def enable_2fa():
    """Confirm 2FA setup by verifying a code."""
    try:
        data = request.json or {}
        code = (data.get("code") or "").strip()
        secret = session.get("pending_totp_secret")
        if not secret:
            return jsonify({"error": "Please start 2FA setup first"}), 400

        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return jsonify({"error": "Invalid code, try again"}), 400

        sb.table("users").update({
            "totp_secret": secret,
            "totp_enabled": True
        }).eq("id", session["user_id"]).execute()
        session.pop("pending_totp_secret", None)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/2fa/disable", methods=["POST"])
@login_required
def disable_2fa():
    """Disable 2FA (requires current password for security)."""
    try:
        data = request.json or {}
        password = data.get("password") or ""
        u = current_user()
        if not bcrypt.checkpw(password.encode(), u["password_hash"].encode()):
            return jsonify({"error": "Password incorrect"}), 400
        sb.table("users").update({
            "totp_secret": None,
            "totp_enabled": False
        }).eq("id", u["id"]).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   CONVERSATIONS
# ============================================================
@app.route("/api/conversations")
@login_required
def list_conversations():
    try:
        uid = session["user_id"]
        mem = sb.table("conversation_members").select("conversation_id,muted,is_group_admin").eq("user_id", uid).execute().data
        if not mem:
            return jsonify({"conversations": []})

        conv_ids = [m["conversation_id"] for m in mem]
        muted_map = {m["conversation_id"]: m.get("muted", False) for m in mem}
        gadmin_map = {m["conversation_id"]: m.get("is_group_admin", False) for m in mem}

        convs = sb.table("conversations").select("*").in_("id", conv_ids).order("updated_at", desc=True).execute().data

        for c in convs:
            try:
                members = sb.table("conversation_members").select("user_id,is_group_admin,users(username,nickname_color)").eq("conversation_id", c["id"]).execute().data
                c["members"] = []
                for m in members:
                    if m.get("users"):
                        c["members"].append({
                            "id": m["user_id"],
                            "username": m["users"]["username"],
                            "nickname_color": m["users"].get("nickname_color") or "#00d9ff",
                            "is_group_admin": m.get("is_group_admin", False)
                        })
                c["muted"] = muted_map.get(c["id"], False)
                c["i_am_group_admin"] = gadmin_map.get(c["id"], False)

                # Last message preview
                last = sb.table("messages").select("content,created_at,image_url").eq("conversation_id", c["id"]).eq("deleted", False).order("created_at", desc=True).limit(1).execute().data
                if last:
                    preview = last[0].get("content", "") or ("📷 Image" if last[0].get("image_url") else "")
                    c["last_message"] = (preview[:50] + "…") if len(preview) > 50 else preview
                    c["last_time"] = last[0]["created_at"]
                else:
                    c["last_message"] = ""
                    c["last_time"] = c.get("created_at", "")
            except Exception as e:
                print(f"[list_conv item] {e}")
                c["members"] = []
                c["muted"] = False
                c["last_message"] = ""
                c["last_time"] = c.get("created_at", "")

        return jsonify({"conversations": convs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/new_dm", methods=["POST"])
@login_required
def new_dm():
    """Create or open a 1-on-1 conversation."""
    try:
        data = request.json or {}
        other_username = (data.get("username") or "").strip().lower()
        uid = session["user_id"]

        other = sb.table("users").select("id,username").eq("username", other_username).execute().data
        if not other:
            return jsonify({"error": "User not found"}), 404
        other_id = other[0]["id"]
        if other_id == uid:
            return jsonify({"error": "You cannot chat with yourself"}), 400

        # Check for existing DM
        my_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
        their_convs = sb.table("conversation_members").select("conversation_id").eq("user_id", other_id).execute().data
        shared = {c["conversation_id"] for c in my_convs} & {c["conversation_id"] for c in their_convs}
        for cid in shared:
            c = sb.table("conversations").select("is_group").eq("id", cid).execute().data
            if c and not c[0].get("is_group", False):
                return jsonify({"ok": True, "conversation_id": cid})

        conv = sb.table("conversations").insert({
            "created_by": uid,
            "is_group": False,
            "updated_at": now_iso()
        }).execute().data[0]

        sb.table("conversation_members").insert([
            {"conversation_id": conv["id"], "user_id": uid},
            {"conversation_id": conv["id"], "user_id": other_id}
        ]).execute()

        return jsonify({"ok": True, "conversation_id": conv["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/new_group", methods=["POST"])
@login_required
def new_group():
    """Create a group chat with multiple users."""
    try:
        data = request.json or {}
        name = (data.get("name") or "").strip()
        usernames = data.get("usernames") or []
        uid = session["user_id"]

        if not name:
            return jsonify({"error": "Group name is required"}), 400
        if not isinstance(usernames, list) or len(usernames) < 1:
            return jsonify({"error": "At least one other member is required"}), 400

        user_ids = set()
        for un in usernames:
            un = (un or "").strip().lower()
            if not un:
                continue
            u = sb.table("users").select("id").eq("username", un).execute().data
            if u:
                user_ids.add(u[0]["id"])

        if not user_ids:
            return jsonify({"error": "No valid users found"}), 400

        # Add creator as admin
        conv = sb.table("conversations").insert({
            "created_by": uid,
            "is_group": True,
            "name": name[:60],
            "updated_at": now_iso()
        }).execute().data[0]

        members = [{"conversation_id": conv["id"], "user_id": uid, "is_group_admin": True}]
        for u in user_ids:
            if u != uid:
                members.append({"conversation_id": conv["id"], "user_id": u, "is_group_admin": False})
        sb.table("conversation_members").insert(members).execute()

        return jsonify({"ok": True, "conversation_id": conv["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/add_member", methods=["POST"])
@login_required
def add_group_member(cid):
    try:
        uid = session["user_id"]
        username = ((request.json or {}).get("username") or "").strip().lower()
        # Must be group admin
        mem = sb.table("conversation_members").select("is_group_admin").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not mem or not mem[0].get("is_group_admin"):
            return jsonify({"error": "Only group admins can add members"}), 403
        target = sb.table("users").select("id").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404
        # Check not already member
        existing = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", target[0]["id"]).execute().data
        if existing:
            return jsonify({"error": "User already in group"}), 400
        sb.table("conversation_members").insert({
            "conversation_id": cid,
            "user_id": target[0]["id"],
            "is_group_admin": False
        }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/remove_member", methods=["POST"])
@login_required
def remove_group_member(cid):
    try:
        uid = session["user_id"]
        target_id = ((request.json or {}).get("user_id") or "").strip()
        mem = sb.table("conversation_members").select("is_group_admin").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not mem or not mem[0].get("is_group_admin"):
            return jsonify({"error": "Only group admins can remove members"}), 403
        sb.table("conversation_members").delete().eq("conversation_id", cid).eq("user_id", target_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/rename", methods=["POST"])
@login_required
def rename_group(cid):
    try:
        uid = session["user_id"]
        new_name = ((request.json or {}).get("name") or "").strip()[:60]
        if not new_name:
            return jsonify({"error": "Name required"}), 400
        mem = sb.table("conversation_members").select("is_group_admin").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not mem or not mem[0].get("is_group_admin"):
            return jsonify({"error": "Only group admins can rename"}), 403
        sb.table("conversations").update({"name": new_name}).eq("id", cid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/leave", methods=["POST"])
@login_required
def leave_conv(cid):
    try:
        uid = session["user_id"]
        sb.table("conversation_members").delete().eq("conversation_id", cid).eq("user_id", uid).execute()
        remaining = sb.table("conversation_members").select("id", count="exact").eq("conversation_id", cid).execute().count or 0
        if remaining == 0:
            sb.table("messages").delete().eq("conversation_id", cid).execute()
            sb.table("conversations").delete().eq("id", cid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/mute", methods=["POST"])
@login_required
def mute_conv(cid):
    try:
        muted = bool((request.json or {}).get("muted", True))
        sb.table("conversation_members").update({"muted": muted}).eq("conversation_id", cid).eq("user_id", session["user_id"]).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/export")
@login_required
def export_conv(cid):
    try:
        uid = session["user_id"]
        m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not m:
            return "Forbidden", 403
        msgs = sb.table("messages").select("*,users:sender_id(username)").eq("conversation_id", cid).eq("deleted", False).order("created_at").execute().data
        lines = [
            "=" * 60,
            "  CIPHER — Conversation Export",
            f"  Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 60,
            ""
        ]
        for msg in msgs:
            who = "unknown"
            if msg.get("users"):
                who = msg["users"].get("username", "unknown")
            when = datetime.fromisoformat(msg["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            content = msg.get("content") or ("[image]" if msg.get("image_url") else "[empty]")
            lines.append(f"[{when}] {who}: {content}")
        lines.append("")
        lines.append("— End of export —")
        text = "\n".join(lines)
        return Response(text, mimetype="text/plain", headers={
            "Content-Disposition": f"attachment; filename=cipher_export_{cid[:8]}.txt"
        })
    except Exception as e:
        return f"Error: {e}", 500


# ============================================================
#   MESSAGES
# ============================================================
@app.route("/api/messages/<cid>")
@login_required
def get_messages(cid):
    try:
        uid = session["user_id"]
        m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not m:
            return jsonify({"error": "Not a member of this conversation"}), 403

        msgs = sb.table("messages").select("*,users:sender_id(username,nickname_color)").eq("conversation_id", cid).eq("deleted", False).order("created_at").limit(200).execute().data

        msg_ids = [msg["id"] for msg in msgs]
        reactions_map = {}
        reads_map = {}

        if msg_ids:
            try:
                reactions = sb.table("message_reactions").select("message_id,emoji,user_id,users(username)").in_("message_id", msg_ids).execute().data
                for r in reactions:
                    uname = "?"
                    if r.get("users"):
                        uname = r["users"].get("username", "?")
                    reactions_map.setdefault(r["message_id"], []).append({
                        "emoji": r["emoji"],
                        "user_id": r["user_id"],
                        "username": uname
                    })
            except Exception:
                pass
            try:
                reads = sb.table("message_reads").select("message_id,user_id,users(username)").in_("message_id", msg_ids).execute().data
                for r in reads:
                    uname = "?"
                    if r.get("users"):
                        uname = r["users"].get("username", "?")
                    reads_map.setdefault(r["message_id"], []).append(uname)
            except Exception:
                pass

        # Also compute warnings for messages expiring in <48h
        soon = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        expiring_soon = 0

        for msg in msgs:
            msg["reactions"] = reactions_map.get(msg["id"], [])
            msg["read_by"] = reads_map.get(msg["id"], [])
            if msg.get("expires_at") and msg["expires_at"] < soon:
                expiring_soon += 1

        # Auto-mark as read
        for msg in msgs:
            if msg["sender_id"] != uid:
                try:
                    sb.table("message_reads").upsert({
                        "message_id": msg["id"],
                        "user_id": uid
                    }, on_conflict="message_id,user_id").execute()
                except Exception:
                    pass

        return jsonify({
            "messages": msgs,
            "expiring_soon": expiring_soon
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/<cid>", methods=["POST"])
@login_required
def send_message(cid):
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401

        if not user.get("can_send_messages", True):
            return jsonify({"error": "You are not allowed to send messages"}), 403

        # Active mute?
        try:
            pun = sb.table("user_punishments").select("*").eq("user_id", uid).eq("active", True).eq("type", "mute").execute().data
            for p in pun:
                still_active = True
                if p.get("expires_at"):
                    exp = datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00"))
                    if exp < datetime.now(timezone.utc):
                        sb.table("user_punishments").update({"active": False}).eq("id", p["id"]).execute()
                        still_active = False
                if still_active:
                    return jsonify({"error": f"You are muted: {p.get('reason','no reason')}"}), 403
        except Exception:
            pass

        m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not m:
            return jsonify({"error": "Not a member of this conversation"}), 403

        data = request.json or {}
        content = (data.get("content") or "").strip()[:4000]
        image_data = data.get("image_data")

        if not content and not image_data:
            return jsonify({"error": "Empty message"}), 400

        image_url = None
        if image_data:
            try:
                if "," in image_data:
                    image_data = image_data.split(",", 1)[1]
                img_bytes = base64.b64decode(image_data)
                if len(img_bytes) > 5 * 1024 * 1024:
                    return jsonify({"error": "Image too large (max 5 MB)"}), 400
                filename = f"{uid}/{secrets.token_hex(12)}.jpg"
                sb.storage.from_("cipher-images").upload(
                    filename, img_bytes,
                    {"content-type": "image/jpeg"}
                )
                image_url = sb.storage.from_("cipher-images").get_public_url(filename)
            except Exception as e:
                return jsonify({"error": f"Image upload failed: {str(e)}"}), 500

        # Determine expiry
        conv = sb.table("conversations").select("keep_forever").eq("id", cid).execute().data
        settings = sb.table("admin_settings").select("default_retention_days").eq("id", 1).execute().data
        keep = conv[0].get("keep_forever", False) if conv else False
        days = settings[0].get("default_retention_days", 30) if settings else 30

        if keep or user.get("keep_all_forever", False):
            expires_at = None
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

        is_anon = bool(data.get("anonymous")) and user.get("anonymous_mode", False)

        msg = sb.table("messages").insert({
            "conversation_id": cid,
            "sender_id": uid,
            "content": content if content else None,
            "image_url": image_url,
            "is_anonymous": is_anon,
            "expires_at": expires_at
        }).execute().data[0]

        sb.table("conversations").update({"updated_at": now_iso()}).eq("id", cid).execute()
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/<msg_id>/react", methods=["POST"])
@login_required
def react_message(msg_id):
    try:
        emoji = ((request.json or {}).get("emoji") or "").strip()
        if not emoji or len(emoji) > 8:
            return jsonify({"error": "Invalid emoji"}), 400
        uid = session["user_id"]
        existing = sb.table("message_reactions").select("id").eq("message_id", msg_id).eq("user_id", uid).eq("emoji", emoji).execute().data
        if existing:
            sb.table("message_reactions").delete().eq("id", existing[0]["id"]).execute()
        else:
            sb.table("message_reactions").insert({
                "message_id": msg_id,
                "user_id": uid,
                "emoji": emoji
            }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/<msg_id>/extend", methods=["POST"])
@login_required
def extend_message(msg_id):
    try:
        new_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        sb.table("messages").update({
            "expires_at": new_exp,
            "warning_sent": False
        }).eq("id", msg_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/extend_all", methods=["POST"])
@login_required
def extend_all(cid):
    """Extend all expiring-soon messages in this conversation by 30 days."""
    try:
        uid = session["user_id"]
        m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not m:
            return jsonify({"error": "Forbidden"}), 403
        new_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        soon = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        sb.table("messages").update({
            "expires_at": new_exp,
            "warning_sent": False
        }).eq("conversation_id", cid).lt("expires_at", soon).execute()
        # Mark the warning dismissed
        try:
            sb.table("message_warnings").upsert({
                "user_id": uid,
                "conversation_id": cid,
                "dismissed": True
            }, on_conflict="user_id,conversation_id").execute()
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   TYPING
# ============================================================
@app.route("/api/typing/<cid>", methods=["POST"])
@login_required
def set_typing(cid):
    try:
        sb.table("typing_status").upsert({
            "user_id": session["user_id"],
            "conversation_id": cid,
            "started_at": now_iso()
        }).execute()
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/typing/<cid>")
@login_required
def get_typing(cid):
    try:
        uid = session["user_id"]
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        typing = sb.table("typing_status").select("user_id,users(username)").eq("conversation_id", cid).gt("started_at", cutoff).execute().data
        users = []
        for t in typing:
            if t["user_id"] != uid and t.get("users"):
                users.append(t["users"]["username"])
        return jsonify({"typing": users})
    except Exception:
        return jsonify({"typing": []})


# ============================================================
#   SEARCH
# ============================================================
@app.route("/api/search")
@login_required
def search():
    try:
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify({"results": []})
        uid = session["user_id"]
        mem = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
        conv_ids = [m["conversation_id"] for m in mem]
        if not conv_ids:
            return jsonify({"results": []})
        results = sb.table("messages").select("*,users:sender_id(username)").in_("conversation_id", conv_ids).eq("deleted", False).ilike("content", f"%{q}%").order("created_at", desc=True).limit(30).execute().data
        return jsonify({"results": results})
    except Exception:
        return jsonify({"results": []})


# ============================================================
#   INVITES
# ============================================================
@app.route("/api/invites")
@login_required
def list_invites():
    try:
        uid = session["user_id"]
        invs = sb.table("invite_links").select("*").eq("created_by", uid).order("created_at", desc=True).execute().data
        return jsonify({"invites": invs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/invites", methods=["POST"])
@login_required
def create_invite():
    try:
        user = current_user()
        settings = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
        if not settings.get("invites_enabled", True):
            return jsonify({"error": "Invites are currently disabled"}), 403
        mode = settings.get("invite_creation_mode", "everyone")
        if mode == "admins_only" and not (user.get("is_admin") or user.get("is_owner")):
            return jsonify({"error": "Only admins can create invites"}), 403
        if not user.get("can_create_invites", True):
            return jsonify({"error": "You cannot create invites"}), 403

        data = request.json or {}
        max_uses = data.get("max_uses")
        expires_hours = data.get("expires_hours")
        exp = None
        if expires_hours:
            try:
                exp = (datetime.now(timezone.utc) + timedelta(hours=int(expires_hours))).isoformat()
            except Exception:
                pass
        code = secrets.token_urlsafe(8)
        inv = sb.table("invite_links").insert({
            "code": code,
            "created_by": user["id"],
            "max_uses": max_uses,
            "expires_at": exp
        }).execute().data[0]
        return jsonify({
            "ok": True,
            "invite": inv,
            "url": request.host_url.rstrip("/") + "/?invite=" + code
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/invites/<inv_id>/revoke", methods=["POST"])
@login_required
def revoke_invite(inv_id):
    try:
        uid = session["user_id"]
        user = current_user()
        inv = sb.table("invite_links").select("*").eq("id", inv_id).execute().data
        if not inv:
            return jsonify({"error": "Invite not found"}), 404
        if inv[0]["created_by"] != uid and not (user.get("is_admin") or user.get("is_owner")):
            return jsonify({"error": "Not your invite"}), 403
        sb.table("invite_links").update({"revoked": True}).eq("id", inv_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   ANNOUNCEMENTS
# ============================================================
@app.route("/api/announcements")
def get_announcements():
    try:
        anns = sb.table("announcements").select("*").eq("active", True).order("created_at", desc=True).limit(5).execute().data
        return jsonify({"announcements": anns})
    except Exception:
        return jsonify({"announcements": []})


@app.route("/api/announcements", methods=["POST"])
@admin_required
def create_announcement():
    try:
        data = request.json or {}
        title = (data.get("title") or "").strip()
        content = (data.get("content") or "").strip()
        priority = data.get("priority", "info")
        if not title or not content:
            return jsonify({"error": "Title and content required"}), 400
        if priority not in ("info", "warn", "critical"):
            priority = "info"
        ann = sb.table("announcements").insert({
            "title": title[:80],
            "content": content[:280],
            "priority": priority,
            "created_by": session["user_id"]
        }).execute().data[0]
        audit("create_announcement", "announcement", ann["id"], title)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/announcements/<ann_id>", methods=["DELETE"])
@admin_required
def delete_announcement(ann_id):
    try:
        sb.table("announcements").update({"active": False}).eq("id", ann_id).execute()
        audit("delete_announcement", "announcement", ann_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   ADMIN
# ============================================================
@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    try:
        users_count = sb.table("users").select("id", count="exact").execute().count or 0
        msgs_count = sb.table("messages").select("id", count="exact").eq("deleted", False).execute().count or 0
        convs_count = sb.table("conversations").select("id", count="exact").execute().count or 0
        bans_count = sb.table("bans").select("id", count="exact").execute().count or 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        active_24h = sb.table("users").select("id", count="exact").gt("last_seen", cutoff).execute().count or 0
        return jsonify({
            "users": users_count,
            "messages": msgs_count,
            "conversations": convs_count,
            "bans": bans_count,
            "active_24h": active_24h
        })
    except Exception:
        return jsonify({"users": 0, "messages": 0, "conversations": 0, "bans": 0, "active_24h": 0})


@app.route("/api/admin/users")
@admin_required
def admin_users():
    try:
        users = sb.table("users").select("id,username,is_admin,is_owner,suspended,can_create_invites,can_send_messages,keep_all_forever,last_ip,created_at,last_seen,nickname_color,totp_enabled").order("created_at").execute().data
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>", methods=["POST"])
@admin_required
def admin_update_user(uid):
    try:
        data = request.json or {}
        allowed = ["is_admin", "suspended", "can_create_invites", "can_send_messages",
                   "can_upload_files", "keep_all_forever"]
        upd = {k: v for k, v in data.items() if k in allowed}
        if upd:
            sb.table("users").update(upd).eq("id", uid).execute()
            audit("update_user", "user", uid, str(upd))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/reset_password", methods=["POST"])
@admin_required
def admin_reset_password(uid):
    try:
        new_pass = secrets.token_urlsafe(10)
        new_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
        sb.table("users").update({"password_hash": new_hash}).eq("id", uid).execute()
        audit("reset_password", "user", uid)
        return jsonify({"ok": True, "new_password": new_pass})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/punish", methods=["POST"])
@admin_required
def admin_punish(uid):
    try:
        data = request.json or {}
        ptype = data.get("type")
        reason = (data.get("reason") or "").strip()
        hours = data.get("hours")
        if ptype not in ("warn", "mute", "ban"):
            return jsonify({"error": "Invalid punishment type"}), 400
        exp = None
        if hours:
            try:
                exp = (datetime.now(timezone.utc) + timedelta(hours=int(hours))).isoformat()
            except Exception:
                pass
        sb.table("user_punishments").insert({
            "user_id": uid,
            "punished_by": session["user_id"],
            "type": ptype,
            "reason": reason,
            "expires_at": exp
        }).execute()
        audit(f"punish_{ptype}", "user", uid, reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/punishments")
@admin_required
def get_punishments(uid):
    try:
        puns = sb.table("user_punishments").select("*").eq("user_id", uid).order("created_at", desc=True).execute().data
        return jsonify({"punishments": puns})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/punishment/<pid>/remove", methods=["POST"])
@admin_required
def remove_punishment(pid):
    try:
        sb.table("user_punishments").update({"active": False}).eq("id", pid).execute()
        audit("remove_punishment", "punishment", pid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/ban", methods=["POST"])
@admin_required
def admin_ban():
    try:
        data = request.json or {}
        ip = (data.get("ip") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not ip:
            return jsonify({"error": "IP address required"}), 400
        sb.table("bans").upsert({
            "ip_address": ip,
            "reason": reason,
            "banned_by": session["user_id"]
        }).execute()
        audit("ban_ip", "ip", ip, reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/unban", methods=["POST"])
@admin_required
def admin_unban():
    try:
        ip = ((request.json or {}).get("ip") or "").strip()
        if not ip:
            return jsonify({"error": "IP required"}), 400
        sb.table("bans").delete().eq("ip_address", ip).execute()
        audit("unban_ip", "ip", ip)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/bans")
@admin_required
def list_bans():
    try:
        bans = sb.table("bans").select("*").order("created_at", desc=True).execute().data
        return jsonify({"bans": bans})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings_route():
    try:
        if request.method == "GET":
            s = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
            return jsonify({"settings": s})
        data = request.json or {}
        allowed = ["site_name", "max_file_size_mb", "default_retention_days",
                   "signups_enabled", "invites_enabled", "invite_creation_mode",
                   "maintenance_mode", "registration_message"]
        upd = {k: v for k, v in data.items() if k in allowed}
        if upd:
            sb.table("admin_settings").update(upd).eq("id", 1).execute()
            audit("update_settings", "settings", 1, str(upd))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/audit")
@admin_required
def get_audit_log():
    try:
        logs = sb.table("audit_log").select("*").order("created_at", desc=True).limit(150).execute().data
        return jsonify({"logs": logs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/conversations")
@admin_required
def admin_conversations():
    try:
        convs = sb.table("conversations").select("*").order("updated_at", desc=True).limit(100).execute().data
        # Attach member usernames
        for c in convs:
            try:
                members = sb.table("conversation_members").select("users(username)").eq("conversation_id", c["id"]).execute().data
                c["member_names"] = [m["users"]["username"] for m in members if m.get("users")]
            except Exception:
                c["member_names"] = []
        return jsonify({"conversations": convs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/conversation/<cid>/keep", methods=["POST"])
@admin_required
def admin_keep_conv(cid):
    try:
        keep = bool((request.json or {}).get("keep_forever", True))
        sb.table("conversations").update({"keep_forever": keep}).eq("id", cid).execute()
        if keep:
            sb.table("messages").update({"expires_at": None}).eq("conversation_id", cid).execute()
        audit("toggle_keep_forever", "conversation", cid, str(keep))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   BACKGROUND CLEANUP SCHEDULER
# ============================================================
def cleanup_task():
    """Runs hourly. Cleans up expired data."""
    if not sb:
        return
    try:
        now = now_iso()
        # Soft-delete expired messages
        sb.table("messages").update({"deleted": True}).lt("expires_at", now).eq("deleted", False).execute()
        # Deactivate expired punishments
        sb.table("user_punishments").update({"active": False}).lt("expires_at", now).eq("active", True).execute()
        # Purge stale typing rows
        stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        sb.table("typing_status").delete().lt("started_at", stale).execute()
        # Purge expired bans
        sb.table("bans").delete().lt("expires_at", now).execute()
        print(f"[cleanup] Ran at {now}")
    except Exception as e:
        print(f"[cleanup] {e}")


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(cleanup_task, "interval", minutes=30, next_run_time=datetime.now())
scheduler.start()


# ============================================================
#   MAIN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
