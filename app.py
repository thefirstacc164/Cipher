"""
============================================================
  CIPHER v1.1.0 — Backend
  Solo project by Stepundrik
============================================================
"""

import os
import io
import re
import base64
import hashlib
import secrets
import bcrypt
import pyotp
import qrcode
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template,
    session, Response
)
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
#   APP CONFIG
# ============================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
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
#   POLLING CONFIG
# ============================================================
POLL_CONFIG = {
    "active_ms": 3000,
    "idle_ms": 8000,
    "very_idle_ms": 15000,
    "hidden_ms": 30000,
    "idle_after_cycles": 5,
    "very_idle_after_cycles": 20
}

# ============================================================
#   ANTI-SPAM CONFIG
# ============================================================
SPAM_WINDOW_SECONDS = 10
SPAM_MSG_THRESHOLD = 8
SPAM_DUPLICATE_THRESHOLD = 3
SPAM_WARNING_COOLDOWN = 30
THROTTLE_DELAY_MS = {0: 0, 1: 0, 2: 3000, 3: 3000, 4: 0, 5: 0}

# ============================================================
#   COLOR NAME MAPPING (for dynamic credits joke)
# ============================================================
COLOR_NAMES = {
    (0, 217, 255): "cyan",
    (239, 68, 68): "red",
    (245, 158, 11): "orange",
    (234, 179, 8): "yellow",
    (34, 197, 94): "green",
    (59, 130, 246): "blue",
    (139, 92, 246): "purple",
    (236, 72, 153): "pink",
    (244, 63, 94): "rose",
    (255, 255, 255): "white",
    (0, 0, 0): "black",
    (107, 114, 128): "gray",
    (99, 102, 241): "indigo",
    (20, 184, 166): "teal",
    (249, 115, 22): "orange",
}

def get_color_name(hex_color):
    """Get nearest color name from a hex code by RGB distance."""
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return "cyan"
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except ValueError:
        return "cyan"
    best = "cyan"
    best_dist = float("inf")
    for (cr, cg, cb), name in COLOR_NAMES.items():
        d = (cr - r)**2 + (cg - g)**2 + (cb - b)**2
        if d < best_dist:
            best_dist = d
            best = name
    return best

# ============================================================
#   HELPERS
# ============================================================
def get_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hash_content(content):
    return hashlib.md5((content or "").encode()).hexdigest()[:16]


def is_immune(user):
    """Owner is always immune. Others must be in immunity_list."""
    if not user:
        return False
    if user.get("is_owner"):
        return True
    try:
        res = sb.table("immunity_list").select("id").eq("username", user["username"]).execute().data
        return len(res) > 0
    except Exception:
        return False


def is_ip_banned(ip):
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
                sb.table("bans").delete().eq("ip_address", ip).execute()
                return False
        return True
    except Exception:
        return False


def get_admin_permissions(user_id):
    """Fetch granular permissions for an admin. Returns dict with all perms as booleans."""
    try:
        res = sb.table("admin_permissions").select("*").eq("user_id", user_id).execute().data
        if res:
            return res[0]
    except Exception:
        pass
    # Default: everything false
    return {
        "can_view_messages": False,
        "can_approve_affiliates": False,
        "can_create_announcements": False,
        "can_ban_ips": False,
        "can_suspend_ban_users": False,
        "can_reset_passwords": False,
        "can_manage_shop_items": False,
        "can_manage_admins": False
    }


def has_permission(user, permission_key):
    """Check if user has a specific permission. Owner bypasses all."""
    if not user:
        return False
    if user.get("is_owner"):
        return True
    if not user.get("is_admin"):
        return False
    perms = get_admin_permissions(user["id"])
    return perms.get(permission_key, False)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not signed in"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Any admin or owner."""
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


def permission_required(perm_key):
    """Decorator: requires user to be owner OR admin with specific permission."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Not signed in"}), 401
            u = current_user()
            if not u:
                return jsonify({"error": "Session invalid"}), 401
            if not has_permission(u, perm_key):
                return jsonify({"error": f"You don't have permission ({perm_key})"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def current_user():
    if "user_id" not in session:
        return None
    try:
        res = sb.table("users").select("*").eq("id", session["user_id"]).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def audit(action, target_type=None, target_id=None, details=None):
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
    wordlist = [
        "cipher", "shadow", "vault", "echo", "raven", "cobalt", "onyx", "quartz",
        "zenith", "void", "phantom", "spectre", "glacier", "aurora", "nebula",
        "cosmos", "pulse", "matrix", "binary", "protocol", "enigma", "riddle",
        "token", "cypher", "stealth", "cascade", "fracture", "circuit", "emblem",
        "kernel", "beacon", "vector", "prism", "static", "signal", "orbit",
        "helix", "photon", "quantum", "vertex"
    ]
    return " ".join(secrets.choice(wordlist) for _ in range(12))


def award_shards(user_id, amount, transaction_type, description, related_table=None, related_id=None, created_by=None):
    """Add shards to a user's balance and log the transaction. Returns new balance."""
    try:
        # Get current balance
        user_res = sb.table("users").select("shards").eq("id", user_id).execute().data
        if not user_res:
            return None
        current = user_res[0].get("shards", 0) or 0
        new_balance = current + amount
        if new_balance < 0:
            new_balance = 0
        # Update balance
        sb.table("users").update({"shards": new_balance}).eq("id", user_id).execute()
        # Log transaction
        sb.table("shard_transactions").insert({
            "user_id": user_id,
            "amount": amount,
            "balance_after": new_balance,
            "transaction_type": transaction_type,
            "description": description,
            "related_table": related_table,
            "related_id": str(related_id) if related_id else None,
            "created_by": created_by
        }).execute()
        return new_balance
    except Exception as e:
        print(f"[award_shards] {e}")
        return None


def ensure_user_profile(user_id):
    """Make sure user_profiles row exists for this user. Creates one if not."""
    try:
        existing = sb.table("user_profiles").select("user_id").eq("user_id", user_id).execute().data
        if not existing:
            sb.table("user_profiles").insert({
                "user_id": user_id,
                "bio": "",
                "active_effects": [],
                "active_badges": []
            }).execute()
    except Exception as e:
        print(f"[ensure_user_profile] {e}")


# ============================================================
#   ANTI-SPAM
# ============================================================
def check_spam(user, content):
    """
    Returns: (throttle_delay_ms, warning_message_or_None)
    Immune users always pass.
    """
    if is_immune(user):
        return 0, None

    uid = user["id"]
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(seconds=SPAM_WINDOW_SECONDS)).isoformat()

    level = user.get("throttle_level", 0) or 0
    throttle_until = user.get("throttle_until")

    if throttle_until:
        try:
            exp = datetime.fromisoformat(throttle_until.replace("Z", "+00:00"))
            if exp < now and level in (2,):
                sb.table("users").update({
                    "throttle_level": 1,
                    "throttle_until": None
                }).eq("id", uid).execute()
                level = 1
        except Exception:
            pass

    current_delay = THROTTLE_DELAY_MS.get(level, 0)

    content_hash = hash_content(content)
    try:
        sb.table("recent_messages").insert({
            "user_id": uid,
            "content_hash": content_hash
        }).execute()
    except Exception:
        pass

    try:
        recent = sb.table("recent_messages").select("content_hash").eq("user_id", uid).gt("created_at", window_start).execute().data
    except Exception:
        recent = []

    msg_count = len(recent)
    duplicate_count = sum(1 for r in recent if r["content_hash"] == content_hash)

    is_spam = False
    reason = None
    if msg_count > SPAM_MSG_THRESHOLD:
        is_spam = True
        reason = f"{msg_count} messages in {SPAM_WINDOW_SECONDS}s"
    elif duplicate_count >= SPAM_DUPLICATE_THRESHOLD:
        is_spam = True
        reason = f"{duplicate_count} identical messages"

    if not is_spam:
        return current_delay, None

    last_warn = user.get("last_warning_at")
    if last_warn:
        try:
            lw = datetime.fromisoformat(last_warn.replace("Z", "+00:00"))
            if (now - lw).total_seconds() < SPAM_WARNING_COOLDOWN:
                return current_delay, None
        except Exception:
            pass

    warnings = (user.get("spam_warnings", 0) or 0) + 1

    warning_msg = ""
    updates = {
        "spam_warnings": warnings,
        "last_warning_at": now.isoformat()
    }

    if warnings == 1:
        warning_msg = "⚠️ Slow down. Warning 1 of 5. If you continue, your account will be throttled."
    elif warnings == 2:
        updates["throttle_level"] = 2
        updates["throttle_until"] = (now + timedelta(days=30)).isoformat()
        warning_msg = "⚠️ Warning 2 of 5. Your account is now throttled for 30 days — your messages will send with a delay."
    elif warnings == 3:
        updates["throttle_level"] = 3
        updates["throttle_until"] = None
        warning_msg = "⚠️ Warning 3 of 5. The throttle on your account is now PERMANENT."
    elif warnings == 4:
        updates["throttle_level"] = 4
        try:
            sb.table("user_punishments").insert({
                "user_id": uid,
                "punished_by": None,
                "type": "ban",
                "reason": "Auto-ban: 4th spam warning",
                "expires_at": (now + timedelta(hours=24)).isoformat()
            }).execute()
        except Exception:
            pass
        warning_msg = "🚫 Warning 4 of 5. You are BANNED for 24 hours."
    elif warnings >= 5:
        updates["throttle_level"] = 5
        try:
            sb.table("bans").upsert({
                "ip_address": get_ip(),
                "reason": "Auto-ban: 5th spam warning",
                "banned_by": None
            }).execute()
        except Exception:
            pass
        warning_msg = "🚫 Warning 5 of 5. Your IP is now PERMANENTLY BANNED. Goodbye."

    try:
        sb.table("users").update(updates).eq("id", uid).execute()
    except Exception:
        pass

    try:
        sb.table("spam_events").insert({
            "user_id": uid,
            "message_count": msg_count,
            "trigger_reason": reason,
            "warning_number": warnings
        }).execute()
    except Exception:
        pass

    delay = THROTTLE_DELAY_MS.get(updates.get("throttle_level", level), current_delay)
    return delay, warning_msg


# ============================================================
#   BEFORE-REQUEST: IP BAN + SESSION ENFORCEMENT (FIXES B2!)
# ============================================================
@app.before_request
def check_before_request():
    open_paths = ["/static", "/health", "/favicon.ico"]
    for p in open_paths:
        if request.path.startswith(p):
            return

    ip = get_ip()

    # IP ban check (with immunity bypass)
    if sb and ip and is_ip_banned(ip):
        if "user_id" in session:
            try:
                u = sb.table("users").select("*").eq("id", session["user_id"]).execute().data
                if u and (u[0].get("is_owner") or u[0].get("is_admin") or is_immune(u[0])):
                    pass  # allowed through
                else:
                    return jsonify({"error": "Your IP is banned from this service.", "banned": True}), 403
            except Exception:
                return jsonify({"error": "Your IP is banned from this service.", "banned": True}), 403
        else:
            return jsonify({"error": "Your IP is banned from this service.", "banned": True}), 403

    # Session enforcement: if logged in, check suspend/ban status on EVERY request
    if "user_id" in session:
        # Skip enforcement on logout endpoint so users can always log out
        if request.path == "/api/logout":
            return
        try:
            u = sb.table("users").select("*").eq("id", session["user_id"]).execute().data
            if not u:
                # User no longer exists — kill session
                session.clear()
                return jsonify({"error": "Session expired", "logout": True}), 401
            user = u[0]

            # Owner and immune users bypass all checks
            if user.get("is_owner") or is_immune(user):
                return

            # Suspended?
            if user.get("suspended"):
                # Check if suspended_until exists and expired
                suspended_until = user.get("suspended_until")
                if suspended_until:
                    try:
                        exp = datetime.fromisoformat(suspended_until.replace("Z", "+00:00"))
                        if exp < datetime.now(timezone.utc):
                            # Suspension expired, lift it
                            sb.table("users").update({
                                "suspended": False,
                                "suspended_until": None,
                                "suspension_reason": None
                            }).eq("id", user["id"]).execute()
                        else:
                            session.clear()
                            reason = user.get("suspension_reason") or "no reason given"
                            return jsonify({
                                "error": f"Your account has been suspended: {reason}",
                                "logout": True,
                                "suspended": True
                            }), 403
                    except Exception:
                        session.clear()
                        return jsonify({"error": "Your account is suspended", "logout": True}), 403
                else:
                    session.clear()
                    return jsonify({"error": "Your account is suspended", "logout": True}), 403

            # Active ban punishment?
            try:
                puns = sb.table("user_punishments").select("*").eq("user_id", user["id"]).eq("active", True).eq("type", "ban").execute().data
                for p in puns:
                    still_active = True
                    if p.get("expires_at"):
                        exp = datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00"))
                        if exp < datetime.now(timezone.utc):
                            sb.table("user_punishments").update({"active": False}).eq("id", p["id"]).execute()
                            still_active = False
                    if still_active:
                        session.clear()
                        reason = p.get("reason") or "no reason given"
                        return jsonify({
                            "error": f"Your account is banned: {reason}",
                            "logout": True,
                            "banned": True
                        }), 403
            except Exception:
                pass
        except Exception as e:
            print(f"[session_enforce] {e}")


# ============================================================
#   BASIC ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_iso(), "version": "1.1.0"})
  # ============================================================
#   AUTH — SIGNUP (with ToS acceptance + affiliate code support)
# ============================================================
@app.route("/api/signup", methods=["POST"])
def signup():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        password = data.get("password") or ""
        invite_code = (data.get("invite_code") or "").strip()
        affiliate_code = (data.get("affiliate_code") or "").strip()
        accepted_tos = data.get("accepted_tos", False)

        # Validate
        if len(username) < 3:
            return jsonify({"error": "Username must be at least 3 characters"}), 400
        if len(username) > 20:
            return jsonify({"error": "Username must be 20 characters or fewer"}), 400
        if not username.replace("_", "").isalnum():
            return jsonify({"error": "Username can only contain letters, numbers, and underscores"}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        if not accepted_tos:
            return jsonify({"error": "You must accept the Terms of Service"}), 400

        settings_res = sb.table("admin_settings").select("*").eq("id", 1).execute().data
        settings = settings_res[0] if settings_res else {}
        if not settings.get("signups_enabled", True) and not invite_code:
            return jsonify({"error": "Signups are currently disabled. You need an invite code."}), 403

        # Validate invite code if provided
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

        # Validate affiliate code if provided
        affiliate = None
        if affiliate_code:
            aff_res = sb.table("affiliate_codes").select("*").ilike("code", affiliate_code).execute().data
            if not aff_res:
                return jsonify({"error": "Invalid affiliate code"}), 400
            affiliate = aff_res[0]
            if affiliate.get("revoked"):
                return jsonify({"error": "This affiliate code has been revoked"}), 400
            if not affiliate.get("approved"):
                return jsonify({"error": "This affiliate code is not active yet"}), 400

        exists = sb.table("users").select("id").eq("username", username).execute().data
        if exists:
            return jsonify({"error": "Username already taken"}), 400

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user_count = sb.table("users").select("id", count="exact").execute().count or 0
        is_owner = (user_count == 0)

        phrase = generate_recovery_phrase()
        phrase_hash = bcrypt.hashpw(phrase.encode(), bcrypt.gensalt()).decode()
        recovery_key = secrets.token_hex(48)
        key_hash = bcrypt.hashpw(recovery_key.encode(), bcrypt.gensalt()).decode()

        signup_ip = get_ip()

        new_user = sb.table("users").insert({
            "username": username,
            "password_hash": pw_hash,
            "is_owner": is_owner,
            "is_admin": is_owner,
            "last_ip": signup_ip,
            "recovery_phrase": phrase_hash,
            "nickname_color": "#00d9ff",
            "theme_color": "#00d9ff"
        }).execute().data[0]

        # Recovery key
        sb.table("recovery_keys").insert({
            "user_id": new_user["id"],
            "key_hash": key_hash,
            "method": "file"
        }).execute()

        # ToS acceptance
        try:
            sb.table("terms_acceptance").insert({
                "user_id": new_user["id"],
                "accepted_version": "v1.1.0",
                "ip": signup_ip,
                "user_agent": request.headers.get("User-Agent", "")[:500]
            }).execute()
        except Exception as e:
            print(f"[signup tos] {e}")

        # User profile
        ensure_user_profile(new_user["id"])

        # Owner admin permissions (all granted implicitly, but we insert row for consistency)
        if is_owner:
            try:
                sb.table("admin_permissions").insert({
                    "user_id": new_user["id"],
                    "can_view_messages": True,
                    "can_approve_affiliates": True,
                    "can_create_announcements": True,
                    "can_ban_ips": True,
                    "can_suspend_ban_users": True,
                    "can_reset_passwords": True,
                    "can_manage_shop_items": True,
                    "can_manage_admins": True
                }).execute()
            except Exception:
                pass

        # Handle invite
        if invite:
            sb.table("invite_links").update({
                "uses_count": invite.get("uses_count", 0) + 1
            }).eq("id", invite["id"]).execute()
            sb.table("invite_uses").insert({
                "invite_id": invite["id"],
                "user_id": new_user["id"],
                "ip_address": signup_ip
            }).execute()

        # Handle affiliate — award shards to referrer
        if affiliate:
            # Self-referral protection: check if referrer's last_ip matches signup IP
            referrer = sb.table("users").select("id,username,last_ip").eq("id", affiliate["user_id"]).execute().data
            if referrer:
                referrer_row = referrer[0]
                if referrer_row["id"] == new_user["id"]:
                    pass  # can't self-refer, but just skip silently
                elif referrer_row.get("last_ip") and referrer_row.get("last_ip") == signup_ip:
                    pass  # same IP, likely self-refer attempt, skip
                else:
                    shards_amount = settings.get("shards_per_referral", 10) or 10
                    # Record affiliate use
                    try:
                        sb.table("affiliate_uses").insert({
                            "code_id": affiliate["id"],
                            "referrer_id": affiliate["user_id"],
                            "referred_user_id": new_user["id"],
                            "shards_awarded": shards_amount,
                            "signup_ip": signup_ip
                        }).execute()
                        # Update affiliate stats
                        sb.table("affiliate_codes").update({
                            "uses": (affiliate.get("uses", 0) or 0) + 1,
                            "total_earned": (affiliate.get("total_earned", 0) or 0) + shards_amount
                        }).eq("id", affiliate["id"]).execute()
                        # Award shards
                        award_shards(
                            affiliate["user_id"],
                            shards_amount,
                            "referral",
                            f"Referral: {username} signed up with your code",
                            related_table="affiliate_uses",
                            related_id=affiliate["id"]
                        )
                    except Exception as e:
                        print(f"[affiliate award] {e}")

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

        # Suspension (only if not immune)
        if user.get("suspended") and not is_immune(user):
            reason = user.get("suspension_reason") or "no reason given"
            return jsonify({"error": f"This account is suspended: {reason}"}), 403

        # Ban punishment (only if not immune)
        if not is_immune(user):
            try:
                pun = sb.table("user_punishments").select("*").eq("user_id", user["id"]).eq("active", True).eq("type", "ban").execute().data
                if pun:
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

        if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return jsonify({"error": "Invalid username or password"}), 400

        # 2FA challenge
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

        # Ensure profile row exists
        ensure_user_profile(user["id"])

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
        return jsonify({"user": None, "poll_config": POLL_CONFIG})

    # Get profile data
    profile = {}
    try:
        prof_res = sb.table("user_profiles").select("*").eq("user_id", u["id"]).execute().data
        if prof_res:
            profile = prof_res[0]
    except Exception:
        pass

    # Get permissions if admin
    permissions = {}
    if u.get("is_admin") or u.get("is_owner"):
        permissions = get_admin_permissions(u["id"])
        # Owner has all permissions implicitly
        if u.get("is_owner"):
            for key in ["can_view_messages", "can_approve_affiliates", "can_create_announcements",
                        "can_ban_ips", "can_suspend_ban_users", "can_reset_passwords",
                        "can_manage_shop_items", "can_manage_admins"]:
                permissions[key] = True

    return jsonify({
        "user": {
            "id": u["id"],
            "username": u["username"],
            "is_owner": u.get("is_owner", False),
            "is_admin": u.get("is_admin", False),
            "is_immune": is_immune(u),
            "keep_all_forever": u.get("keep_all_forever", False),
            "notify_before_delete": u.get("notify_before_delete", True),
            "nickname_color": u.get("nickname_color") or "#00d9ff",
            "theme_color": u.get("theme_color") or "#00d9ff",
            "anonymous_mode": u.get("anonymous_mode", False),
            "totp_enabled": u.get("totp_enabled", False),
            "shards": u.get("shards", 0) or 0,
            "leaderboard_opt_out": u.get("leaderboard_opt_out", False),
            "bubble_color": u.get("bubble_color"),
            "name_font": u.get("name_font"),
            "msg_animation": u.get("msg_animation"),
            # Profile data
            "bio": profile.get("bio", ""),
            "avatar_url": profile.get("avatar_url"),
            "banner_color": profile.get("banner_color"),
            "active_effects": profile.get("active_effects", []) or [],
            "active_badges": profile.get("active_badges", []) or [],
            "active_bubble_color": profile.get("active_bubble_color"),
            "active_nickname_font": profile.get("active_nickname_font"),
            "active_message_animation": profile.get("active_message_animation"),
            # Permissions
            "permissions": permissions,
            # Dynamic credits joke color name
            "theme_color_name": get_color_name(u.get("theme_color") or "#00d9ff")
        },
        "poll_config": POLL_CONFIG
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
        # Fields on users table
        user_allowed = ["nickname_color", "theme_color", "anonymous_mode",
                        "notify_before_delete", "keep_all_forever",
                        "leaderboard_opt_out"]
        user_upd = {k: v for k, v in data.items() if k in user_allowed}
        if user_upd:
            sb.table("users").update(user_upd).eq("id", session["user_id"]).execute()

        # Fields on user_profiles table
        profile_allowed = ["bio", "banner_color"]
        profile_upd = {k: v for k, v in data.items() if k in profile_allowed}
        if profile_upd:
            ensure_user_profile(session["user_id"])
            # Validate bio length
            if "bio" in profile_upd and profile_upd["bio"] and len(profile_upd["bio"]) > 160:
                profile_upd["bio"] = profile_upd["bio"][:160]
            sb.table("user_profiles").update(profile_upd).eq("user_id", session["user_id"]).execute()

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
    try:
        data = request.json or {}
        if data.get("confirm") != "DELETE":
            return jsonify({"error": "Confirmation text does not match"}), 400

        uid = session["user_id"]

        # Owner cannot self-delete via this
        u = current_user()
        if u and u.get("is_owner"):
            return jsonify({"error": "The owner account cannot be deleted from within the app. Contact support."}), 403

        # Mark messages deleted (preserves them for other participants)
        sb.table("messages").update({
            "deleted": True,
            "content": "[deleted]",
            "image_url": None
        }).eq("sender_id", uid).execute()

        # Remove from all tables
        sb.table("conversation_members").delete().eq("user_id", uid).execute()
        sb.table("recovery_keys").delete().eq("user_id", uid).execute()
        sb.table("message_reactions").delete().eq("user_id", uid).execute()
        sb.table("message_reads").delete().eq("user_id", uid).execute()
        sb.table("typing_status").delete().eq("user_id", uid).execute()
        sb.table("recent_messages").delete().eq("user_id", uid).execute()
        sb.table("user_profiles").delete().eq("user_id", uid).execute()
        sb.table("user_purchases").delete().eq("user_id", uid).execute()
        sb.table("shard_transactions").delete().eq("user_id", uid).execute()
        sb.table("terms_acceptance").delete().eq("user_id", uid).execute()
        sb.table("admin_permissions").delete().eq("user_id", uid).execute()
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
    try:
        u = current_user()
        secret = pyotp.random_base32()
        session["pending_totp_secret"] = secret

        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name=u["username"], issuer_name="Cipher")

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
                last = sb.table("messages").select("content,created_at,image_url,is_anonymous").eq("conversation_id", c["id"]).eq("deleted", False).order("created_at", desc=True).limit(1).execute().data
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
        mem = sb.table("conversation_members").select("is_group_admin").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not mem or not mem[0].get("is_group_admin"):
            return jsonify({"error": "Only group admins can add members"}), 403
        target = sb.table("users").select("id").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404
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
            # Anonymous mode: hide the sender's name in exports too
            if msg.get("is_anonymous"):
                who = "Anonymous"
            else:
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
#   MESSAGES (with anonymous fix B1)
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

        soon = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        expiring_soon = 0

        # ============================================================
        # ANONYMOUS FIX (B1): If message is_anonymous, strip sender data
        # UNLESS the viewer is the sender themselves (they see their own msgs normally)
        # ============================================================
        for msg in msgs:
            msg["reactions"] = reactions_map.get(msg["id"], [])
            msg["read_by"] = reads_map.get(msg["id"], [])

            if msg.get("is_anonymous") and msg.get("sender_id") != uid:
                # Hide sender identity from recipients
                msg["users"] = {"username": "Anonymous", "nickname_color": "#8892a6"}
                # Also strip the internal sender_id so client can't correlate
                # But we keep it internally for the sender's own view detection
                msg["sender_id_hidden"] = True
                msg["sender_id"] = None

            if msg.get("expires_at") and msg["expires_at"] < soon:
                expiring_soon += 1

        # Mark as read
        for msg in msgs:
            actual_sender = msg.get("sender_id")
            if actual_sender and actual_sender != uid:
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

        if not user.get("can_send_messages", True) and not is_immune(user):
            return jsonify({"error": "You are not allowed to send messages"}), 403

        # Active mute (immune users bypass)
        if not is_immune(user):
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

        # ANTI-SPAM CHECK
        throttle_delay, warning = check_spam(user, content)

        try:
            fresh = sb.table("users").select("throttle_level").eq("id", uid).execute().data
            if fresh and fresh[0].get("throttle_level", 0) >= 4:
                return jsonify({
                    "error": warning or "Your account has been restricted.",
                    "blocked": True
                }), 403
        except Exception:
            pass

        # ============================================================
        # ANONYMOUS FIX (B1): Read anonymous_mode from user profile
        # Message is marked is_anonymous automatically if user has mode on
        # ============================================================
        is_anon = bool(user.get("anonymous_mode", False))

        # Image upload
        image_url = None
        if image_data:
            try:
                # Check for perk_upload_10mb_30_days (allows 10MB uploads)
                max_size = 5 * 1024 * 1024
                try:
                    perk_check = sb.table("user_purchases").select("expires_at,shop_items!inner(item_key)").eq("user_id", uid).execute().data
                    for pu in perk_check:
                        item = pu.get("shop_items")
                        if item and item.get("item_key") == "perk_upload_10mb_30_days":
                            # Check not expired
                            if pu.get("expires_at"):
                                exp = datetime.fromisoformat(pu["expires_at"].replace("Z", "+00:00"))
                                if exp > datetime.now(timezone.utc):
                                    max_size = 10 * 1024 * 1024
                                    break
                            else:
                                max_size = 10 * 1024 * 1024
                                break
                except Exception:
                    pass

                if "," in image_data:
                    image_data = image_data.split(",", 1)[1]
                img_bytes = base64.b64decode(image_data)
                if len(img_bytes) > max_size:
                    mb = max_size // (1024 * 1024)
                    return jsonify({"error": f"Image too large (max {mb} MB)"}), 400
                filename = f"{uid}/{secrets.token_hex(12)}.jpg"
                sb.storage.from_("cipher-images").upload(
                    filename, img_bytes,
                    {"content-type": "image/jpeg"}
                )
                image_url = sb.storage.from_("cipher-images").get_public_url(filename)
            except Exception as e:
                return jsonify({"error": f"Image upload failed: {str(e)}"}), 500

        # Retention
        conv = sb.table("conversations").select("keep_forever").eq("id", cid).execute().data
        settings = sb.table("admin_settings").select("default_retention_days").eq("id", 1).execute().data
        keep = conv[0].get("keep_forever", False) if conv else False
        days = settings[0].get("default_retention_days", 30) if settings else 30

        # Check for perk_retention_30_days (adds 30 days to retention)
        try:
            perks = sb.table("user_purchases").select("expires_at,shop_items!inner(item_key)").eq("user_id", uid).execute().data
            for pu in perks:
                item = pu.get("shop_items")
                if item and item.get("item_key") == "perk_retention_30_days":
                    if pu.get("expires_at"):
                        exp = datetime.fromisoformat(pu["expires_at"].replace("Z", "+00:00"))
                        if exp > datetime.now(timezone.utc):
                            days += 30
                            break
                    else:
                        days += 30
                        break
        except Exception:
            pass

        if keep or user.get("keep_all_forever", False):
            expires_at = None
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

        msg = sb.table("messages").insert({
            "conversation_id": cid,
            "sender_id": uid,
            "content": content if content else None,
            "image_url": image_url,
            "is_anonymous": is_anon,
            "expires_at": expires_at
        }).execute().data[0]

        sb.table("conversations").update({"updated_at": now_iso()}).eq("id", cid).execute()

        response = {"ok": True, "message": msg}
        if warning:
            response["warning"] = warning
        if throttle_delay > 0:
            response["throttle_delay"] = throttle_delay
        return jsonify(response)
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

        # Apply anonymous filter to search results too
        for r in results:
            if r.get("is_anonymous") and r.get("sender_id") != uid:
                r["users"] = {"username": "Anonymous"}
                r["sender_id"] = None

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
        if not settings.get("invites_enabled", True) and not is_immune(user):
            return jsonify({"error": "Invites are currently disabled"}), 403
        mode = settings.get("invite_creation_mode", "everyone")
        if mode == "admins_only" and not (user.get("is_admin") or user.get("is_owner")):
            return jsonify({"error": "Only admins can create invites"}), 403
        if not user.get("can_create_invites", True) and not is_immune(user):
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
@permission_required("can_create_announcements")
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
@permission_required("can_create_announcements")
def delete_announcement(ann_id):
    try:
        sb.table("announcements").update({"active": False}).eq("id", ann_id).execute()
        audit("delete_announcement", "announcement", ann_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
      # ============================================================
#   TERMS OF SERVICE — content endpoint
# ============================================================

TOS_CONTENT = """# Terms of Service

**Version 1.1.0 — Last updated August 2026**

Welcome to Cipher. By using this service, you agree to these terms. Please read them carefully.

## 1. What Cipher Is

Cipher is a private messaging platform. You can send text messages, images, and communicate with other users through direct or group chats.

## 2. Message Retention

Messages are automatically deleted after 30 days by default (this may be adjusted). You can:
- Extend individual messages or all messages in a chat by 30 more days
- Keep all your own messages forever (setting in your profile)
- Export any conversation as a text file before it's deleted

## 3. Privacy & Message Access

Your messages are private between you and the other participants in each conversation.

**Important disclosure:** The owner of this service retains the ability to access message contents in cases of abuse investigation or serious rule violations. This is used only for moderation. When administrators access messages, the access is logged with a reason. When the owner accesses messages, no log is kept.

By using Cipher, you understand and accept this reality. Cipher is not end-to-end encrypted.

## 4. Anonymous Mode

Anonymous mode hides your username from message recipients (they see "Anonymous" instead of your real name). However:
- Your identity is still known to the server owner
- Anonymous mode does not make you truly invisible
- It is a social feature, not a security feature

## 5. Accounts

- You are responsible for keeping your password and recovery keys safe
- We provide a 12-word recovery phrase and a downloadable recovery key file at signup
- We cannot recover your account without these
- You can enable two-factor authentication for extra security

## 6. Conduct Rules

You may not use Cipher for:
- Harassment, threats, or bullying
- Spam or automated messaging
- Illegal content or activities
- Sharing others' private information without consent
- Impersonating others in bad faith

Violations may result in warnings, muting, banning, or account deletion.

## 7. Affiliate Program

You may create affiliate codes to invite friends. When they sign up with your code, you earn Shards. Rules:
- No self-referring (creating alt accounts to earn Shards)
- No paying users to sign up with your code
- Abuse of the affiliate system results in Shard removal and possible account action

## 8. Shards & Shop

Shards are a virtual currency with no real-world value. You earn them by referring users. You spend them on cosmetic items and small conveniences.

- Purchases are final — no refunds
- Cosmetic items are visible to other users
- Perks that last a set number of days will expire at that time
- The owner may adjust prices or remove items

## 9. Data Deletion

You can delete your account at any time from your settings. This will:
- Permanently remove your account
- Mark your messages as deleted (they will show "[deleted]" to other participants)
- Remove all your Shards, purchases, and profile data

Some information may remain in system logs for a limited period.

## 10. Changes to Terms

These terms may be updated. Continued use of Cipher after changes means acceptance of the new terms.

## 11. Contact

This is a solo project. There is no formal support channel. Use the service at your own risk.

---

By clicking "Accept" during signup, you confirm you have read and agreed to these terms.
"""

@app.route("/api/tos")
def get_tos():
    return jsonify({"tos": TOS_CONTENT, "version": "v1.1.0"})


# ============================================================
#   ADMIN — STATS
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


# ============================================================
#   ADMIN — USERS (with search + pagination — improvements I1, I2)
# ============================================================
@app.route("/api/admin/users")
@admin_required
def admin_users():
    try:
        search_q = (request.args.get("search") or "").strip().lower()
        try:
            offset = int(request.args.get("offset", 0))
            limit = min(int(request.args.get("limit", 20)), 100)
        except ValueError:
            offset, limit = 0, 20

        query = sb.table("users").select(
            "id,username,is_admin,is_owner,suspended,suspended_until,suspension_reason,"
            "can_create_invites,can_send_messages,keep_all_forever,last_ip,created_at,"
            "last_seen,nickname_color,totp_enabled,spam_warnings,throttle_level,shards",
            count="exact"
        )

        if search_q:
            query = query.ilike("username", f"%{search_q}%")

        result = query.order("created_at").range(offset, offset + limit - 1).execute()
        users = result.data
        total = result.count or 0

        # Mark immune
        try:
            imm = sb.table("immunity_list").select("username").execute().data
            immune_set = {i["username"] for i in imm}
        except Exception:
            immune_set = set()

        for u in users:
            u["is_immune"] = u.get("is_owner", False) or u["username"] in immune_set

        return jsonify({
            "users": users,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + len(users)) < total
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>", methods=["POST"])
@admin_required
def admin_update_user(uid):
    try:
        me = current_user()
        target = sb.table("users").select("*").eq("id", uid).execute().data
        if target and is_immune(target[0]) and not me.get("is_owner"):
            return jsonify({"error": "This user is immune and cannot be modified"}), 403

        data = request.json or {}
        allowed = ["is_admin", "suspended", "can_create_invites", "can_send_messages",
                   "can_upload_files", "keep_all_forever"]
        upd = {k: v for k, v in data.items() if k in allowed}

        # Permission checks
        if "suspended" in upd and not has_permission(me, "can_suspend_ban_users"):
            return jsonify({"error": "You don't have permission to suspend users"}), 403
        if "is_admin" in upd and not has_permission(me, "can_manage_admins"):
            return jsonify({"error": "You don't have permission to manage admins"}), 403

        # If setting is_admin=true, ensure admin_permissions row exists (default all off)
        if upd.get("is_admin") is True:
            try:
                existing_perms = sb.table("admin_permissions").select("user_id").eq("user_id", uid).execute().data
                if not existing_perms:
                    sb.table("admin_permissions").insert({
                        "user_id": uid,
                        "granted_by": me["id"]
                    }).execute()
            except Exception as e:
                print(f"[admin perms init] {e}")

        # If demoting to non-admin, remove permissions
        if upd.get("is_admin") is False:
            try:
                sb.table("admin_permissions").delete().eq("user_id", uid).execute()
            except Exception:
                pass

        if upd:
            sb.table("users").update(upd).eq("id", uid).execute()
            audit("update_user", "user", uid, str(upd))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/reset_password", methods=["POST"])
@permission_required("can_reset_passwords")
def admin_reset_password(uid):
    try:
        me = current_user()
        target = sb.table("users").select("*").eq("id", uid).execute().data
        if target and is_immune(target[0]) and not me.get("is_owner"):
            return jsonify({"error": "This user is immune"}), 403
        new_pass = secrets.token_urlsafe(10)
        new_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
        sb.table("users").update({"password_hash": new_hash}).eq("id", uid).execute()
        audit("reset_password", "user", uid)
        return jsonify({"ok": True, "new_password": new_pass})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/punish", methods=["POST"])
@permission_required("can_suspend_ban_users")
def admin_punish(uid):
    try:
        me = current_user()
        target = sb.table("users").select("*").eq("id", uid).execute().data
        if target and is_immune(target[0]):
            return jsonify({"error": "This user is immune to punishments"}), 403
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


# NEW: Improvement I3 — Ban user account (creates a permanent-ban punishment)
@app.route("/api/admin/user/<uid>/ban_account", methods=["POST"])
@permission_required("can_suspend_ban_users")
def admin_ban_account(uid):
    try:
        me = current_user()
        target = sb.table("users").select("*").eq("id", uid).execute().data
        if target and is_immune(target[0]):
            return jsonify({"error": "This user is immune"}), 403
        data = request.json or {}
        reason = (data.get("reason") or "").strip() or "No reason given"
        hours = data.get("hours")
        exp = None
        if hours:
            try:
                exp = (datetime.now(timezone.utc) + timedelta(hours=int(hours))).isoformat()
            except Exception:
                pass
        sb.table("user_punishments").insert({
            "user_id": uid,
            "punished_by": session["user_id"],
            "type": "ban",
            "reason": reason,
            "expires_at": exp
        }).execute()
        audit("ban_account", "user", uid, reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# NEW: Improvement I4 — Owner-only delete other user's account
@app.route("/api/admin/user/<uid>/delete", methods=["POST"])
@owner_required
def owner_delete_user(uid):
    try:
        data = request.json or {}
        target = sb.table("users").select("username,is_owner").eq("id", uid).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404
        target_user = target[0]
        expected_confirm = f"DELETE @{target_user['username']}"
        if data.get("confirm") != expected_confirm:
            return jsonify({"error": f"You must type '{expected_confirm}' exactly to confirm"}), 400
        if target_user.get("is_owner"):
            return jsonify({"error": "Cannot delete the owner account"}), 403
        if is_immune({"is_owner": False, "username": target_user["username"]}):
            return jsonify({"error": "Cannot delete immune users. Remove from immunity list first."}), 403

        # Cascade delete
        sb.table("messages").update({
            "deleted": True,
            "content": "[deleted by admin]",
            "image_url": None
        }).eq("sender_id", uid).execute()
        sb.table("conversation_members").delete().eq("user_id", uid).execute()
        sb.table("recovery_keys").delete().eq("user_id", uid).execute()
        sb.table("message_reactions").delete().eq("user_id", uid).execute()
        sb.table("message_reads").delete().eq("user_id", uid).execute()
        sb.table("typing_status").delete().eq("user_id", uid).execute()
        sb.table("recent_messages").delete().eq("user_id", uid).execute()
        sb.table("user_profiles").delete().eq("user_id", uid).execute()
        sb.table("user_purchases").delete().eq("user_id", uid).execute()
        sb.table("shard_transactions").delete().eq("user_id", uid).execute()
        sb.table("terms_acceptance").delete().eq("user_id", uid).execute()
        sb.table("admin_permissions").delete().eq("user_id", uid).execute()
        sb.table("users").delete().eq("id", uid).execute()

        audit("delete_user_account", "user", uid, target_user["username"])
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
@permission_required("can_suspend_ban_users")
def remove_punishment(pid):
    try:
        sb.table("user_punishments").update({"active": False}).eq("id", pid).execute()
        audit("remove_punishment", "punishment", pid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   ADMIN — IP BANS
# ============================================================
@app.route("/api/admin/ban", methods=["POST"])
@permission_required("can_ban_ips")
def admin_ban():
    try:
        data = request.json or {}
        ip = (data.get("ip") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not ip:
            return jsonify({"error": "IP address required"}), 400

        try:
            users_at_ip = sb.table("users").select("*").eq("last_ip", ip).execute().data
            for u in users_at_ip:
                if is_immune(u):
                    return jsonify({"error": f"Cannot ban this IP — belongs to immune user @{u['username']}"}), 403
        except Exception:
            pass

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
@permission_required("can_ban_ips")
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


# ============================================================
#   ADMIN — GLOBAL SETTINGS
# ============================================================
@app.route("/api/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings_route():
    try:
        if request.method == "GET":
            s = sb.table("admin_settings").select("*").eq("id", 1).execute().data[0]
            return jsonify({"settings": s})

        # POST: only owner or those who can manage things
        me = current_user()
        if not me.get("is_owner"):
            return jsonify({"error": "Only owner can change global settings"}), 403

        data = request.json or {}
        allowed = ["site_name", "max_file_size_mb", "default_retention_days",
                   "signups_enabled", "invites_enabled", "invite_creation_mode",
                   "maintenance_mode", "registration_message",
                   "shards_per_referral", "affiliate_mode"]
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


@app.route("/api/admin/spam_events")
@admin_required
def admin_spam_events():
    try:
        events = sb.table("spam_events").select("*,users(username)").order("created_at", desc=True).limit(100).execute().data
        return jsonify({"events": events})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   ADMIN — ADMIN RIGHTS MANAGEMENT (N7, N8 — owner only)
# ============================================================
@app.route("/api/admin/admin_rights")
@owner_required
def list_admin_rights():
    """List all admins and their permissions. Owner only."""
    try:
        admins = sb.table("users").select("id,username,is_owner,nickname_color").eq("is_admin", True).order("username").execute().data
        result = []
        for a in admins:
            perms = get_admin_permissions(a["id"])
            result.append({
                "id": a["id"],
                "username": a["username"],
                "is_owner": a.get("is_owner", False),
                "nickname_color": a.get("nickname_color") or "#00d9ff",
                "permissions": {
                    "can_view_messages": perms.get("can_view_messages", False),
                    "can_approve_affiliates": perms.get("can_approve_affiliates", False),
                    "can_create_announcements": perms.get("can_create_announcements", False),
                    "can_ban_ips": perms.get("can_ban_ips", False),
                    "can_suspend_ban_users": perms.get("can_suspend_ban_users", False),
                    "can_reset_passwords": perms.get("can_reset_passwords", False),
                    "can_manage_shop_items": perms.get("can_manage_shop_items", False),
                    "can_manage_admins": perms.get("can_manage_admins", False)
                }
            })
        return jsonify({"admins": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/admin_rights/<uid>", methods=["POST"])
@owner_required
def update_admin_rights(uid):
    """Update permissions for an admin. Owner only."""
    try:
        data = request.json or {}
        # Ensure they're an admin
        u = sb.table("users").select("is_admin,is_owner").eq("id", uid).execute().data
        if not u or not u[0].get("is_admin"):
            return jsonify({"error": "User is not an admin"}), 400
        if u[0].get("is_owner"):
            return jsonify({"error": "Owner permissions cannot be edited"}), 400

        allowed = ["can_view_messages", "can_approve_affiliates", "can_create_announcements",
                   "can_ban_ips", "can_suspend_ban_users", "can_reset_passwords",
                   "can_manage_shop_items", "can_manage_admins"]
        upd = {k: bool(v) for k, v in data.items() if k in allowed}
        upd["updated_at"] = now_iso()

        # Ensure a row exists
        existing = sb.table("admin_permissions").select("user_id").eq("user_id", uid).execute().data
        if not existing:
            insert_data = {"user_id": uid, "granted_by": session["user_id"]}
            insert_data.update(upd)
            sb.table("admin_permissions").insert(insert_data).execute()
        else:
            sb.table("admin_permissions").update(upd).eq("user_id", uid).execute()

        audit("update_admin_rights", "user", uid, str(upd))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   EMERGENCY MESSAGE VIEWER (God-Mode)
# ============================================================
@app.route("/api/admin/emergency/search")
@admin_required
def emergency_search_users():
    """Search users by username for the emergency viewer."""
    try:
        u = current_user()
        # Owner OR admin with can_view_messages permission
        if not (u.get("is_owner") or has_permission(u, "can_view_messages")):
            return jsonify({"error": "You don't have permission for the emergency viewer"}), 403

        q = (request.args.get("q") or "").strip().lower()
        if len(q) < 1:
            return jsonify({"users": []})
        users = sb.table("users").select("id,username,nickname_color,last_seen").ilike("username", f"%{q}%").limit(30).execute().data
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/emergency/user_conversations/<target_uid>")
@admin_required
def emergency_user_conversations(target_uid):
    """List all conversations a target user is in."""
    try:
        u = current_user()
        if not (u.get("is_owner") or has_permission(u, "can_view_messages")):
            return jsonify({"error": "Permission denied"}), 403

        mem = sb.table("conversation_members").select("conversation_id,conversations(id,name,is_group,created_at,updated_at)").eq("user_id", target_uid).execute().data
        convs = []
        for m in mem:
            c = m.get("conversations")
            if c:
                # Get other members' usernames
                others = sb.table("conversation_members").select("users(username)").eq("conversation_id", c["id"]).execute().data
                member_names = [x["users"]["username"] for x in others if x.get("users")]
                convs.append({
                    "id": c["id"],
                    "name": c.get("name"),
                    "is_group": c.get("is_group", False),
                    "created_at": c.get("created_at"),
                    "updated_at": c.get("updated_at"),
                    "members": member_names
                })
        return jsonify({"conversations": convs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/emergency/messages", methods=["POST"])
@admin_required
def emergency_view_messages():
    """View messages in a conversation. Requires reason if admin (not owner)."""
    try:
        u = current_user()
        if not (u.get("is_owner") or has_permission(u, "can_view_messages")):
            return jsonify({"error": "Permission denied"}), 403

        data = request.json or {}
        cid = (data.get("conversation_id") or "").strip()
        target_uid = (data.get("target_user_id") or "").strip() or None
        reason = (data.get("reason") or "").strip()

        if not cid:
            return jsonify({"error": "Conversation ID required"}), 400

        # If not owner, reason is REQUIRED and min 10 chars
        if not u.get("is_owner"):
            if len(reason) < 10:
                return jsonify({"error": "Reason must be at least 10 characters"}), 400

        # Fetch messages (including deleted ones for emergency review — with anonymous UNMASKED)
        msgs = sb.table("messages").select("*,users:sender_id(username,nickname_color)").eq("conversation_id", cid).order("created_at").execute().data

        # Emergency viewer sees ALL messages including anonymous ones with real sender info
        # No anonymization applied here — that's the whole point of emergency access

        # Log the access (only for non-owners)
        if not u.get("is_owner"):
            try:
                sb.table("message_access_log").insert({
                    "viewer_id": u["id"],
                    "target_user_id": target_uid,
                    "conversation_id": cid,
                    "reason": reason,
                    "ip": get_ip()
                }).execute()
            except Exception as e:
                print(f"[access log] {e}")

        return jsonify({"messages": msgs, "silent": u.get("is_owner", False)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/emergency/access_log")
@owner_required
def emergency_access_log():
    """Owner-only: view all admin access to messages."""
    try:
        logs = sb.table("message_access_log").select("*,viewer:viewer_id(username),target:target_user_id(username)").order("created_at", desc=True).limit(200).execute().data
        result = []
        for l in logs:
            result.append({
                "id": l["id"],
                "viewer_username": l.get("viewer", {}).get("username") if l.get("viewer") else "?",
                "target_username": l.get("target", {}).get("username") if l.get("target") else None,
                "conversation_id": l.get("conversation_id"),
                "reason": l.get("reason"),
                "ip": l.get("ip"),
                "created_at": l.get("created_at")
            })
        return jsonify({"logs": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   SHOP
# ============================================================
@app.route("/api/shop/items")
@login_required
def list_shop_items():
    """List all enabled shop items."""
    try:
        items = sb.table("shop_items").select("*").eq("enabled", True).order("category").order("sort_order").execute().data

        # Get user's purchases to mark as owned
        uid = session["user_id"]
        purchases = sb.table("user_purchases").select("item_id,equipped,expires_at").eq("user_id", uid).execute().data
        purchased_ids = {p["item_id"]: p for p in purchases}

        for item in items:
            pdata = purchased_ids.get(item["id"])
            if pdata:
                item["owned"] = True
                item["equipped"] = pdata.get("equipped", False)
                item["expires_at"] = pdata.get("expires_at")
            else:
                item["owned"] = False
                item["equipped"] = False
                item["expires_at"] = None

        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shop/buy/<item_id>", methods=["POST"])
@login_required
def shop_buy(item_id):
    """Purchase a shop item."""
    try:
        uid = session["user_id"]
        u = current_user()

        # Get item
        item_res = sb.table("shop_items").select("*").eq("id", item_id).execute().data
        if not item_res:
            return jsonify({"error": "Item not found"}), 404
        item = item_res[0]
        if not item.get("enabled"):
            return jsonify({"error": "This item is not available"}), 400

        # Check already owned (only if one-time)
        existing = sb.table("user_purchases").select("id").eq("user_id", uid).eq("item_id", item_id).execute().data
        if existing:
            # For duration-based items, allow rebuying to extend, otherwise reject
            item_key = item.get("item_key", "")
            duration_items = ("perk_retention_30_days", "perk_upload_10mb_30_days")
            if item_key not in duration_items:
                return jsonify({"error": "You already own this item"}), 400

        # Check balance
        price = item.get("price", 0) or 0
        balance = u.get("shards", 0) or 0
        if balance < price:
            return jsonify({"error": f"Not enough Shards (need {price}, have {balance})"}), 400

        # Deduct shards
        new_balance = award_shards(
            uid,
            -price,
            "purchase",
            f"Bought: {item['name']}",
            related_table="shop_items",
            related_id=item["id"]
        )

        # Determine expiry for duration-based items
        expires_at = None
        item_key = item.get("item_key", "")
        if item_key == "perk_retention_30_days" or item_key == "perk_upload_10mb_30_days":
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

        # Record purchase (or update if re-buying duration item)
        if existing:
            sb.table("user_purchases").update({
                "purchased_at": now_iso(),
                "expires_at": expires_at,
                "price_paid": price
            }).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("user_purchases").insert({
                "user_id": uid,
                "item_id": item_id,
                "price_paid": price,
                "expires_at": expires_at
            }).execute()

        return jsonify({"ok": True, "new_balance": new_balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shop/equip/<item_id>", methods=["POST"])
@login_required
def shop_equip(item_id):
    """Equip or unequip an owned item."""
    try:
        uid = session["user_id"]
        equip = bool((request.json or {}).get("equip", True))

        # Check owned
        purch = sb.table("user_purchases").select("id,equipped").eq("user_id", uid).eq("item_id", item_id).execute().data
        if not purch:
            return jsonify({"error": "You don't own this item"}), 400

        # Get item details
        item_res = sb.table("shop_items").select("*").eq("id", item_id).execute().data
        if not item_res:
            return jsonify({"error": "Item not found"}), 404
        item = item_res[0]
        item_key = item.get("item_key", "")
        category = item.get("category", "")
        effect_key = item.get("effect_key")

        # Update purchase equipped state
        sb.table("user_purchases").update({"equipped": equip}).eq("user_id", uid).eq("item_id", item_id).execute()

        # Ensure user_profiles exists
        ensure_user_profile(uid)

        # Fetch current profile
        profile_res = sb.table("user_profiles").select("*").eq("user_id", uid).execute().data
        profile = profile_res[0] if profile_res else {}

        # Handle by category
        if category == "avatar_effects":
            # Only one avatar effect at a time — unequip others
            active_effects = list(profile.get("active_effects", []) or [])
            # Remove any other effect from same category
            all_effects = sb.table("shop_items").select("effect_key,id").eq("category", "avatar_effects").execute().data
            other_effect_keys = [e["effect_key"] for e in all_effects if e["effect_key"] and e["id"] != item_id]

            if equip:
                # Remove other effects, add this one
                active_effects = [ek for ek in active_effects if ek not in other_effect_keys]
                if effect_key and effect_key not in active_effects:
                    active_effects.append(effect_key)
                # Also unequip other effect purchases
                for other in all_effects:
                    if other["id"] != item_id:
                        sb.table("user_purchases").update({"equipped": False}).eq("user_id", uid).eq("item_id", other["id"]).execute()
            else:
                if effect_key:
                    active_effects = [ek for ek in active_effects if ek != effect_key]

            sb.table("user_profiles").update({"active_effects": active_effects}).eq("user_id", uid).execute()

        elif category == "badges":
            active_badges = list(profile.get("active_badges", []) or [])
            if equip:
                if effect_key and effect_key not in active_badges:
                    active_badges.append(effect_key)
            else:
                if effect_key:
                    active_badges = [b for b in active_badges if b != effect_key]
            sb.table("user_profiles").update({"active_badges": active_badges}).eq("user_id", uid).execute()

        elif category == "chat":
            # chat_bubble_colors, chat_nickname_font, chat_send_animation
            if item_key == "chat_bubble_colors":
                color = (request.json or {}).get("value")
                new_val = color if (equip and color) else None
                sb.table("user_profiles").update({"active_bubble_color": new_val}).eq("user_id", uid).execute()
                sb.table("users").update({"bubble_color": new_val}).eq("id", uid).execute()
            elif item_key == "chat_nickname_font":
                font = (request.json or {}).get("value") or "italic"
                new_val = font if equip else None
                sb.table("user_profiles").update({"active_nickname_font": new_val}).eq("user_id", uid).execute()
                sb.table("users").update({"name_font": new_val}).eq("id", uid).execute()
            elif item_key == "chat_send_animation":
                anim = (request.json or {}).get("value") or "slide"
                new_val = anim if equip else None
                sb.table("user_profiles").update({"active_message_animation": new_val}).eq("user_id", uid).execute()
                sb.table("users").update({"msg_animation": new_val}).eq("id", uid).execute()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shop/upload_avatar", methods=["POST"])
@login_required
def shop_upload_avatar():
    """Upload a custom profile picture (requires profile_picture_upload purchase)."""
    try:
        uid = session["user_id"]

        # Check ownership
        owned = sb.table("user_purchases").select("id,shop_items!inner(item_key)").eq("user_id", uid).execute().data
        has_perk = False
        for p in owned:
            item = p.get("shop_items")
            if item and item.get("item_key") == "profile_picture_upload":
                has_perk = True
                break
        if not has_perk:
            return jsonify({"error": "You need to buy 'Profile Picture' from the shop first"}), 403

        data = request.json or {}
        image_data = data.get("image_data")
        if not image_data:
            return jsonify({"error": "No image provided"}), 400

        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        img_bytes = base64.b64decode(image_data)
        if len(img_bytes) > 512 * 1024:
            return jsonify({"error": "Image too large (max 500KB after compression)"}), 400

        filename = f"{uid}/avatar_{secrets.token_hex(8)}.jpg"
        sb.storage.from_("cipher-avatars").upload(
            filename, img_bytes, {"content-type": "image/jpeg"}
        )
        url = sb.storage.from_("cipher-avatars").get_public_url(filename)

        ensure_user_profile(uid)
        sb.table("user_profiles").update({"avatar_url": url}).eq("user_id", uid).execute()

        return jsonify({"ok": True, "avatar_url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   SHOP MANAGEMENT (owner + permitted admins)
# ============================================================
@app.route("/api/admin/shop/items", methods=["GET"])
@permission_required("can_manage_shop_items")
def admin_list_shop_items():
    try:
        items = sb.table("shop_items").select("*").order("category").order("sort_order").execute().data
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/shop/items/<item_id>", methods=["POST"])
@permission_required("can_manage_shop_items")
def admin_update_shop_item(item_id):
    try:
        data = request.json or {}
        allowed = ["name", "description", "price", "enabled", "sort_order", "icon"]
        upd = {k: v for k, v in data.items() if k in allowed}
        upd["updated_at"] = now_iso()
        sb.table("shop_items").update(upd).eq("id", item_id).execute()
        audit("update_shop_item", "shop_item", item_id, str(upd))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   AFFILIATE SYSTEM
# ============================================================
@app.route("/api/affiliate/my_codes")
@login_required
def my_affiliate_codes():
    try:
        uid = session["user_id"]
        codes = sb.table("affiliate_codes").select("*").eq("user_id", uid).order("created_at", desc=True).execute().data
        return jsonify({"codes": codes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/affiliate/create", methods=["POST"])
@login_required
def create_affiliate_code():
    try:
        uid = session["user_id"]
        u = current_user()
        data = request.json or {}
        code = (data.get("code") or "").strip()
        reason = (data.get("reason") or "").strip()

        # Validate code format (4-20 alphanumeric + underscore)
        if not re.match(r"^[A-Za-z0-9_]{4,20}$", code):
            return jsonify({"error": "Code must be 4-20 characters (letters, numbers, underscore)"}), 400

        # Check uniqueness (case-insensitive)
        existing = sb.table("affiliate_codes").select("id").ilike("code", code).execute().data
        if existing:
            return jsonify({"error": "This code is already taken"}), 400

        # Get affiliate mode
        settings = sb.table("admin_settings").select("affiliate_mode").eq("id", 1).execute().data[0]
        mode = settings.get("affiliate_mode", "everyone")

        # Determine approval status based on mode
        if mode == "owner_only" and not u.get("is_owner"):
            return jsonify({"error": "Only the owner can create affiliate codes right now"}), 403

        if mode == "requires_approval" and not u.get("is_owner"):
            if len(reason) < 20:
                return jsonify({"error": "Please provide a reason (at least 20 characters)"}), 400
            approved = False
        else:
            approved = True

        sb.table("affiliate_codes").insert({
            "user_id": uid,
            "code": code,
            "approved": approved,
            "reason": reason if not approved else None
        }).execute()

        return jsonify({"ok": True, "approved": approved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/affiliate/revoke/<code_id>", methods=["POST"])
@login_required
def revoke_affiliate_code(code_id):
    try:
        uid = session["user_id"]
        code = sb.table("affiliate_codes").select("user_id").eq("id", code_id).execute().data
        if not code:
            return jsonify({"error": "Code not found"}), 404
        u = current_user()
        if code[0]["user_id"] != uid and not (u.get("is_owner") or has_permission(u, "can_approve_affiliates")):
            return jsonify({"error": "Not your code"}), 403
        sb.table("affiliate_codes").update({
            "revoked": True,
            "revoked_at": now_iso()
        }).eq("id", code_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/affiliate/pending")
@permission_required("can_approve_affiliates")
def list_pending_affiliates():
    try:
        pending = sb.table("affiliate_codes").select("*,users(username)").eq("approved", False).eq("revoked", False).order("created_at", desc=True).execute().data
        return jsonify({"pending": pending})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/affiliate/<code_id>/approve", methods=["POST"])
@permission_required("can_approve_affiliates")
def approve_affiliate(code_id):
    try:
        sb.table("affiliate_codes").update({
            "approved": True,
            "approved_by": session["user_id"],
            "approved_at": now_iso()
        }).eq("id", code_id).execute()
        audit("approve_affiliate", "affiliate_code", code_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/affiliate/<code_id>/reject", methods=["POST"])
@permission_required("can_approve_affiliates")
def reject_affiliate(code_id):
    try:
        reason = ((request.json or {}).get("reason") or "").strip()
        sb.table("affiliate_codes").update({
            "rejected_by": session["user_id"],
            "rejected_at": now_iso(),
            "rejection_reason": reason
        }).eq("id", code_id).execute()
        audit("reject_affiliate", "affiliate_code", code_id, reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   SHARDS — TRANSACTION HISTORY + LEADERBOARD
# ============================================================
@app.route("/api/shards/history")
@login_required
def shard_history():
    try:
        uid = session["user_id"]
        tx = sb.table("shard_transactions").select("*").eq("user_id", uid).order("created_at", desc=True).limit(100).execute().data
        return jsonify({"transactions": tx})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/leaderboard")
@login_required
def leaderboard():
    try:
        sort = request.args.get("sort", "shards")
        query = sb.table("users").select("id,username,shards,nickname_color,created_at").eq("leaderboard_opt_out", False)

        if sort == "newest":
            query = query.order("created_at", desc=True)
        elif sort == "oldest":
            query = query.order("created_at")
        else:
            query = query.order("shards", desc=True)

        users = query.limit(50).execute().data

        # Enrich with profile data + referral counts
        for u in users:
            # Profile
            try:
                p = sb.table("user_profiles").select("avatar_url,active_effects,active_badges").eq("user_id", u["id"]).execute().data
                if p:
                    u["avatar_url"] = p[0].get("avatar_url")
                    u["active_effects"] = p[0].get("active_effects", []) or []
                    u["active_badges"] = p[0].get("active_badges", []) or []
                else:
                    u["avatar_url"] = None
                    u["active_effects"] = []
                    u["active_badges"] = []
            except Exception:
                u["avatar_url"] = None
                u["active_effects"] = []
                u["active_badges"] = []
            # Referral count
            try:
                ref = sb.table("affiliate_uses").select("id", count="exact").eq("referrer_id", u["id"]).execute()
                u["referrals"] = ref.count or 0
            except Exception:
                u["referrals"] = 0

        if sort == "referrals":
            users.sort(key=lambda x: x["referrals"], reverse=True)

        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   PROFILE CARD (view any user's public profile)
# ============================================================
@app.route("/api/profile/<username>")
@login_required
def get_public_profile(username):
    try:
        username = (username or "").strip().lower()
        u = sb.table("users").select("id,username,nickname_color,created_at,shards,leaderboard_opt_out").eq("username", username).execute().data
        if not u:
            return jsonify({"error": "User not found"}), 404
        user = u[0]

        # Get profile
        p = sb.table("user_profiles").select("*").eq("user_id", user["id"]).execute().data
        profile = p[0] if p else {}

        # Get referral count
        try:
            ref = sb.table("affiliate_uses").select("id", count="exact").eq("referrer_id", user["id"]).execute()
            referrals = ref.count or 0
        except Exception:
            referrals = 0

        opted_out = user.get("leaderboard_opt_out", False)

        return jsonify({
            "user": {
                "username": user["username"],
                "nickname_color": user.get("nickname_color") or "#00d9ff",
                "created_at": user.get("created_at"),
                "bio": profile.get("bio", ""),
                "avatar_url": profile.get("avatar_url"),
                "banner_color": profile.get("banner_color"),
                "active_effects": profile.get("active_effects", []) or [],
                "active_badges": profile.get("active_badges", []) or [],
                "shards": None if opted_out else (user.get("shards", 0) or 0),
                "referrals": None if opted_out else referrals,
                "hidden": opted_out
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   BACKGROUND CLEANUP SCHEDULER
# ============================================================
def cleanup_task():
    if not sb:
        return
    try:
        now = now_iso()
        sb.table("messages").update({"deleted": True}).lt("expires_at", now).eq("deleted", False).execute()
        sb.table("user_punishments").update({"active": False}).lt("expires_at", now).eq("active", True).execute()
        stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        sb.table("typing_status").delete().lt("started_at", stale).execute()
        old_rm = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        sb.table("recent_messages").delete().lt("created_at", old_rm).execute()
        sb.table("bans").delete().lt("expires_at", now).execute()
        # Expire duration-based purchases (they stay in DB but frontend checks expires_at)
        print(f"[cleanup] Ran at {now}")
    except Exception as e:
        print(f"[cleanup] {e}")


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(cleanup_task, "interval", minutes=15, next_run_time=datetime.now())
scheduler.start()


# ============================================================
#   MAIN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
