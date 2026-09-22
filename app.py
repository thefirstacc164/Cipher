"""
============================================================
  CIPHER v1.2.0 — Backend
  Solo project by Stepundrik
============================================================

  v1.2.0 adds Cipher HyperCrypt (client-side E2EE key handling),
  the Shards/Cores economies, friends, bots, anti-cheat and the
  performance work that makes the free Render tier feel fast.
"""

APP_VERSION = "1.2.0"

import os
import io
import re
import gzip
import json
import time
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
    session, Response, make_response
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
# Static assets are versioned in index.html (?v=1.2.0), so browsers can keep
# them for a day and a deploy invalidates them on its own.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(hours=24)
# Encrypted envelopes are larger than the plaintext they replace.
app.config["MAX_CONTENT_LENGTH"] = 18 * 1024 * 1024

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️  WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing!")
    sb = None
else:
    sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ============================================================
#   SPEED: SHORT-LIVED IN-PROCESS CACHES
#   The free Render tier is one small container, and every
#   request used to cost 2-3 round trips to Postgres just to
#   answer "who is this and are they banned?". These caches
#   collapse that to ~0 for the hot polling endpoints.
# ============================================================
CACHE_USER_SECONDS = 2.0        # session enforcement / current_user()
CACHE_IMMUNITY_SECONDS = 60.0   # immunity_list barely changes
CACHE_SETTINGS_SECONDS = 30.0   # admin_settings row
CACHE_SCHEMA_SECONDS = 900.0    # which columns exist
CACHE_BAN_SECONDS = 15.0        # IP ban lookups

_user_cache = {}
_immunity_cache = {}
_ip_ban_cache = {}
_settings_cache = {"at": 0.0, "row": None}
_schema_cache = {"at": 0.0, "cols": {}}
_schema_map = {"at": 0.0, "map": None}


def _cache_get(store, key, ttl):
    hit = store.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_set(store, key, value):
    store[key] = (time.time(), value)
    # keep the caches from growing without bound on a long-lived worker
    if len(store) > 4000:
        cutoff = time.time() - 120
        for k in [k for k, v in store.items() if v[0] < cutoff]:
            store.pop(k, None)


def invalidate_user_cache(user_id=None):
    """Drop cached user rows after a write so the next read is fresh."""
    if user_id is None:
        _user_cache.clear()
    else:
        _user_cache.pop(user_id, None)
        _immunity_cache.pop(user_id, None)


def invalidate_settings_cache():
    _settings_cache["at"] = 0.0
    _settings_cache["row"] = None


def get_settings(force=False):
    """admin_settings row id=1, cached. Never raises."""
    if not sb:
        return {}
    if not force and _settings_cache["row"] is not None and \
            (time.time() - _settings_cache["at"]) < CACHE_SETTINGS_SECONDS:
        return _settings_cache["row"]
    row = {}
    try:
        res = sb.table("admin_settings").select("*").eq("id", 1).execute().data
        row = res[0] if res else {}
    except Exception as e:
        print(f"[get_settings] {e}")
    _settings_cache["at"] = time.time()
    _settings_cache["row"] = row
    return row


def load_schema_map(force=False):
    """
    The whole database schema, in one request.

    PostgREST serves its OpenAPI document at the root, which lists every
    exposed table and its columns. Asking for that once is far cheaper than
    probing tables one by one, and — unlike a `limit(1)` probe — it works on
    tables that are currently empty.
    Returns {table: set(columns)} or None if the probe failed.
    """
    if not sb:
        return None
    if not force and _schema_map["map"] is not None and \
            (time.time() - _schema_map["at"]) < CACHE_SCHEMA_SECONDS:
        return _schema_map["map"]
    mapping = None
    try:
        res = sb.postgrest.get("/", headers={"Accept": "application/openapi+json"})
        spec = res.json() if hasattr(res, "json") else json.loads(getattr(res, "text", "{}"))
        defs = spec.get("definitions") or (spec.get("components") or {}).get("schemas") or {}
        mapping = {name: set(props.keys()) for name, props in defs.items()
                   if isinstance(props, dict)}
    except Exception as e:
        print(f"[schema probe] {e}")
        mapping = None
    if mapping:
        _schema_map["at"] = time.time()
        _schema_map["map"] = mapping
    return mapping


def table_columns(table_name):
    """
    Which columns does this table actually have? None means "we could not tell".

    Used by safe_update()/safe_insert() so a missing column degrades to
    "that one field didn't save" instead of a 500 — the exact failure behind
    "Could not find the 'name_font' column of 'users'".
    """
    if not sb:
        return None
    mapping = load_schema_map()
    if mapping is not None:
        cols = mapping.get(table_name)
        if cols:
            return cols
    cols = _cache_get(_schema_cache["cols"], table_name, CACHE_SCHEMA_SECONDS)
    if cols is not None:
        return cols
    try:
        probe = sb.table(table_name).select("*").limit(1).execute().data
        if probe:
            found = set(probe[0].keys())
            _cache_set(_schema_cache["cols"], table_name, found)
            return found
    except Exception:
        pass
    return None


def filter_known(table_name, payload):
    """Drop keys the live table doesn't have. Permissive when unknown."""
    cols = table_columns(table_name)
    if not cols:
        return dict(payload or {})
    return {k: v for k, v in (payload or {}).items() if k in cols}


def safe_update(table_name, payload, *filters):
    """
    UPDATE that can't blow up on a missing column.
    `filters` is a list of (column, value) equality pairs.
    Returns True if the query ran.
    """
    if not sb:
        return False
    clean = filter_known(table_name, payload)
    if not clean:
        return True
    try:
        q = sb.table(table_name).update(clean)
        for col, val in filters:
            q = q.eq(col, val)
        q.execute()
        return True
    except Exception as e:
        print(f"[safe_update {table_name}] {e}")
        return False


def missing_columns(table_name, cols):
    """
    Which of `cols` does this table not have? Empty when the schema could not
    be probed (we stay permissive rather than shutting features down over a
    failed probe). Money-adjacent endpoints use this to FAIL CLOSED: without
    the column that tracks a payout, paying out is farming, not a feature.
    """
    have = table_columns(table_name)
    if have is None:
        return []
    return [c for c in cols if c not in have]


def migration_error(missing):
    return jsonify({
        "error": "This feature needs the v1.2 database migration",
        "missing_columns": missing
    }), 503


def safe_insert(table_name, payload):
    """INSERT that can't blow up on a missing column. Returns the row or None."""
    if not sb:
        return None
    clean = filter_known(table_name, payload)
    try:
        res = sb.table(table_name).insert(clean).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"[safe_insert {table_name}] {e}")
        return None


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
#   CIPHER HYPERCRYPT
#   Messages are sealed in the browser. Everything the server
#   stores is an envelope: a version tag, the conversation key
#   fingerprint, a nonce and the ciphertext. The plaintext never
#   touches Render, Postgres or any network in between.
# ============================================================
CIPHER_ENVELOPE_PREFIX = "cph1:"
CIPHER_ENCRYPTED_PLACEHOLDER = "🔒 Encrypted message"
CIPHER_MAX_ENVELOPE = 40000     # generous: covers a long message + image tag
CIPHER_VAULT_BUCKET = os.getenv("CIPHER_VAULT_BUCKET", "cipher-vault")

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
    key = user.get("id") or user.get("username")
    cached = _cache_get(_immunity_cache, key, CACHE_IMMUNITY_SECONDS)
    if cached is not None:
        return cached
    result = False
    try:
        res = sb.table("immunity_list").select("id").eq("username", user["username"]).execute().data
        result = len(res) > 0
    except Exception:
        result = False
    _cache_set(_immunity_cache, key, result)
    return result


def is_ip_banned(ip):
    if not sb or not ip:
        return False
    cached = _cache_get(_ip_ban_cache, ip, CACHE_BAN_SECONDS)
    if cached is not None:
        return cached
    result = False
    try:
        res = sb.table("bans").select("*").eq("ip_address", ip).execute()
        if not res.data:
            result = False
        else:
            ban = res.data[0]
            if ban.get("expires_at"):
                exp = datetime.fromisoformat(ban["expires_at"].replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc):
                    sb.table("bans").delete().eq("ip_address", ip).execute()
                    result = False
                else:
                    result = True
            else:
                result = True
    except Exception:
        result = False
    _cache_set(_ip_ban_cache, ip, result)
    return result


def invalidate_ban_cache(ip=None):
    if ip is None:
        _ip_ban_cache.clear()
    else:
        _ip_ban_cache.pop(ip, None)


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
    return get_user_row(session["user_id"])


_LOOKUP_ERROR = object()


def get_user_row(user_id, on_error=None):
    """
    Full users row, cached for a couple of seconds.
    Returns None when the user is gone; returns `on_error` if the lookup itself
    failed, so callers can tell "no such user" from "database is having a day".
    """
    if not user_id or not sb:
        return on_error
    cached = _cache_get(_user_cache, user_id, CACHE_USER_SECONDS)
    if cached is not None:
        return cached or None
    try:
        res = sb.table("users").select("*").eq("id", user_id).execute()
        row = res.data[0] if res.data else None
    except Exception:
        return on_error
    _cache_set(_user_cache, user_id, row)
    return row


CACHE_PUNISH_SECONDS = 5.0
_punish_cache = {}


def invalidate_punish_cache(user_id=None):
    if user_id is None:
        _punish_cache.clear()
    else:
        _punish_cache.pop(user_id, None)


CACHE_PERKS_SECONDS = 30.0
_perks_cache = {}


def invalidate_perks_cache(user_id=None):
    if user_id is None:
        _perks_cache.clear()
    else:
        _perks_cache.pop(user_id, None)


def active_perks(user_id):
    """
    {item_key: expires_at_or_None} for everything this user currently owns.
    One cached query instead of the two identical ones every send used to run.
    """
    if not user_id:
        return {}
    cached = _cache_get(_perks_cache, user_id, CACHE_PERKS_SECONDS)
    if cached is not None:
        return cached
    out = {}
    try:
        rows = sb.table("user_purchases").select("expires_at,shop_items!inner(item_key)") \
            .eq("user_id", user_id).execute().data
        for pu in rows or []:
            item = pu.get("shop_items") or {}
            key = item.get("item_key")
            if not key:
                continue
            exp = pu.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                        continue  # lapsed
                except Exception:
                    pass
            out[key] = exp
    except Exception as e:
        print(f"[active_perks] {e}")
        return {}
    _cache_set(_perks_cache, user_id, out)
    return out


def active_ban_punishment(user_id):
    """
    The user's currently-active ban punishment, or None.
    Expired rows are deactivated as a side effect (same as v1.1 did inline).
    """
    if not user_id:
        return None
    cached = _cache_get(_punish_cache, user_id, CACHE_PUNISH_SECONDS)
    if cached is not None:
        return cached or None
    found = None
    try:
        puns = sb.table("user_punishments").select("*").eq("user_id", user_id) \
            .eq("active", True).eq("type", "ban").execute().data
        for p in puns or []:
            still_active = True
            if p.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00"))
                    if exp < datetime.now(timezone.utc):
                        sb.table("user_punishments").update({"active": False}).eq("id", p["id"]).execute()
                        still_active = False
                except Exception:
                    pass
            if still_active and not found:
                found = p
    except Exception:
        return None
    _cache_set(_punish_cache, user_id, found)
    return found


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
        invalidate_user_cache(user_id)
        # Log transaction
        safe_insert("shard_transactions", {
            "user_id": user_id,
            "amount": amount,
            "balance_after": new_balance,
            "transaction_type": transaction_type,
            "description": description,
            "related_table": related_table,
            "related_id": str(related_id) if related_id else None,
            "created_by": created_by
        })
        # Weekly earning counter drives the Weekly Bronze→Diamond badges
        if amount > 0:
            bump_weekly_earned(user_id, amount)
        return new_balance
    except Exception as e:
        print(f"[award_shards] {e}")
        return None


def award_cores(user_id, amount, transaction_type, description, granted_by=None,
                related_type=None, related_id=None, metadata=None):
    """Cores are the scarce, hand-granted currency. Returns the new balance."""
    try:
        user_res = sb.table("users").select("cores").eq("id", user_id).execute().data
        if not user_res:
            return None
        current = user_res[0].get("cores", 0) or 0
        new_balance = current + amount
        if new_balance < 0:
            new_balance = 0
        sb.table("users").update({"cores": new_balance}).eq("id", user_id).execute()
        invalidate_user_cache(user_id)
        safe_insert("core_transactions", {
            "user_id": user_id,
            "amount": amount,
            "balance_after": new_balance,
            "transaction_type": transaction_type,
            "description": description,
            "granted_by": granted_by,
            "related_type": related_type,
            "related_id": str(related_id) if related_id else None,
            "metadata": metadata
        })
        return new_balance
    except Exception as e:
        print(f"[award_cores] {e}")
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
        invalidate_user_cache(uid)
        invalidate_punish_cache(uid)
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
                u0 = get_user_row(session["user_id"])
                if u0 and (u0.get("is_owner") or u0.get("is_admin") or is_immune(u0)):
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
            user = get_user_row(session["user_id"], on_error=_LOOKUP_ERROR)
            if user is _LOOKUP_ERROR:
                # Postgres hiccup — do not log anybody out over a blip
                return
            if not user:
                # User no longer exists — kill session
                session.clear()
                return jsonify({"error": "Session expired", "logout": True}), 401

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
                            invalidate_user_cache(user["id"])
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

            # Active ban punishment? (cached — this ran on every single poll)
            active_ban = active_ban_punishment(user["id"])
            if active_ban:
                session.clear()
                reason = active_ban.get("reason") or "no reason given"
                return jsonify({
                    "error": f"Your account is banned: {reason}",
                    "logout": True,
                    "banned": True
                }), 403
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
    return jsonify({"status": "ok", "time": now_iso(), "version": APP_VERSION})


# ============================================================
#   RESPONSE POST-PROCESSING
#   gzip + long-lived static caching + privacy headers.
#   The free Render container has very little CPU, so we only
#   compress payloads that are actually worth compressing.
# ============================================================
COMPRESSIBLE = ("json", "text", "javascript", "css", "html", "svg", "xml")


@app.after_request
def flush_cache_after_mutation(resp):
    """
    Safety net for the 2-second user cache: after ANY write request, this
    session's cached user row is dropped so the next read sees its own change.
    Without this, turning on 2FA and then reading /api/me returned the old row.
    """
    try:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            uid = session.get("user_id")
            if uid:
                _user_cache.pop(uid, None)
    except Exception:
        pass
    return resp


@app.after_request
def compress_and_harden(resp):
    try:
        # Privacy headers on everything we serve
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

        if request.path.startswith("/static"):
            # index.html references /static/app.js?v=1.2.0, so a long cache
            # lifetime is safe and a deploy busts it automatically.
            resp.headers.setdefault("Cache-Control", "public, max-age=86400")
            return resp

        if resp.status_code < 200 or resp.status_code >= 300:
            return resp
        if resp.direct_passthrough or resp.headers.get("Content-Encoding"):
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not any(t in ctype for t in COMPRESSIBLE):
            return resp
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        resp.set_data(gzip.compress(data, 6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception as e:
        print(f"[compress] {e}")
    return resp


@app.route("/api/file/<path:object_path>")
@login_required
def proxy_file(object_path):
    """
    Serve an encrypted attachment out of the private vault bucket.

    Same-origin (no CORS preflight from the browser), authorised (you have to
    be in a conversation that actually references the object) and opaque (the
    bytes are ciphertext, so even this endpoint never sees the image).
    """
    try:
        uid = session["user_id"]
        bucket = request.args.get("bucket") or CIPHER_VAULT_BUCKET
        clean = (object_path or "").lstrip("/")
        if not clean or ".." in clean:
            return jsonify({"error": "Bad path"}), 400

        # Your own uploads are always yours
        allowed = clean.startswith(f"{uid}/")
        if not allowed:
            try:
                mine = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
                conv_ids = [m["conversation_id"] for m in mine or []]
                if conv_ids:
                    hits = sb.table("messages").select("id").in_("conversation_id", conv_ids) \
                        .like("image_url", f"%{clean}%").limit(1).execute().data
                    allowed = bool(hits)
            except Exception:
                allowed = False
        if not allowed:
            return jsonify({"error": "Not allowed"}), 403

        data = sb.storage.from_(bucket).download(clean)
        if not data:
            return jsonify({"error": "Not found"}), 404
        return Response(data, mimetype="application/octet-stream",
                        headers={"Cache-Control": "private, max-age=604800"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

        # Bots live on the /api/bots/* token surface, never on a password form
        if user.get("is_bot"):
            return jsonify({"error": "Bot accounts cannot sign in here"}), 403

        # A login is the one moment we want the freshest possible picture
        invalidate_user_cache(user["id"])
        invalidate_punish_cache(user["id"])

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


def _sorry_uses_left(user):
    """How many 'Sorry, undo this' uses are left in the rolling 7-day window."""
    limit = 3 + (1 if "perk_extra_sorry" in active_perks(user.get("id")) else 0)
    used = user.get("sorry_uses_this_week") or 0
    week_start = user.get("sorry_week_start")
    if week_start:
        try:
            ws = datetime.fromisoformat(str(week_start).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ws).days >= 7:
                used = 0
        except Exception:
            pass
    return {"used": used, "limit": limit, "available": max(0, limit - used)}


def _has_identity_key(user_id):
    try:
        rows = sb.table("user_keys").select("user_id").eq("user_id", user_id).limit(1).execute().data
        return bool(rows)
    except Exception:
        return False


def _master_key_public():
    try:
        rows = sb.table("master_key_meta").select("public_key,curve,key_fingerprint") \
            .order("created_at", desc=True).limit(1).execute().data
        return rows[0] if rows else None
    except Exception:
        return None


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

    # Longevity bonuses are checked here (throttled to once a day per user)
    # so they arrive without needing a cron worker on the free tier.
    try:
        check_longevity_bonuses(u["id"])
        u = get_user_row(u["id"]) or u
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
            "is_bot": u.get("is_bot", False),
            "is_immune": is_immune(u),
            "keep_all_forever": u.get("keep_all_forever", False),
            "notify_before_delete": u.get("notify_before_delete", True),
            "nickname_color": u.get("nickname_color") or "#00d9ff",
            "theme_color": u.get("theme_color") or "#00d9ff",
            "anonymous_mode": u.get("anonymous_mode", False),
            "totp_enabled": u.get("totp_enabled", False),
            "shards": u.get("shards", 0) or 0,
            "cores": u.get("cores", 0) or 0,
            "leaderboard_opt_out": u.get("leaderboard_opt_out", False),
            "bubble_color": u.get("bubble_color"),
            "name_font": u.get("name_font"),
            "msg_animation": u.get("msg_animation"),
            "created_at": u.get("created_at"),
            "total_messages_sent": u.get("total_messages_sent", 0) or 0,
            "can_grant_cores": bool(u.get("can_grant_cores")),
            # v1.2 social + safety
            "friend_privacy": u.get("friend_privacy") or "approval",
            "streamer_mode": merge_streamer(u.get("streamer_mode"), get_settings().get("global_streamer_forces")),
            "tos_bonus_claimed": bool(u.get("tos_bonus_claimed")),
            "spam_warnings": u.get("spam_warnings", 0) or 0,
            "sorry_uses_this_week": _sorry_uses_left(u)["used"],
            "sorry_uses_available": _sorry_uses_left(u)["available"],
            # Profile data
            "bio": profile.get("bio", ""),
            "avatar_url": profile.get("avatar_url"),
            "banner_color": profile.get("banner_color"),
            "active_effects": profile.get("active_effects", []) or [],
            "active_badges": all_badges(profile.get("active_badges"), compute_system_badges(u)),
            "active_bubble_color": profile.get("active_bubble_color"),
            "active_nickname_font": profile.get("active_nickname_font"),
            "active_message_animation": profile.get("active_message_animation"),
            # Permissions
            "permissions": permissions,
            # Dynamic credits joke color name
            "theme_color_name": get_color_name(u.get("theme_color") or "#00d9ff")
        },
        "poll_config": POLL_CONFIG,
        "server_time": now_iso(),
        "encryption": {
            "prefix": CIPHER_ENVELOPE_PREFIX,
            "vault_bucket": CIPHER_VAULT_BUCKET,
            "has_identity_key": bool(_has_identity_key(u["id"])),
            "master_key": _master_key_public()
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
        # Fields on users table
        user_allowed = ["nickname_color", "theme_color", "anonymous_mode",
                        "notify_before_delete", "keep_all_forever",
                        "leaderboard_opt_out"]
        user_upd = {k: v for k, v in data.items() if k in user_allowed}

        # v1.2: friend privacy, validated (it is an enum in the database)
        if "friend_privacy" in data and data["friend_privacy"] in ("open", "approval", "closed"):
            user_upd["friend_privacy"] = data["friend_privacy"]

        if user_upd:
            safe_update("users", user_upd, ("id", session["user_id"]))
            invalidate_user_cache(session["user_id"])

        # Fields on user_profiles table
        profile_allowed = ["bio", "banner_color"]
        profile_upd = {k: v for k, v in data.items() if k in profile_allowed}
        if profile_upd:
            ensure_user_profile(session["user_id"])
            # Validate bio length
            if "bio" in profile_upd and profile_upd["bio"] and len(profile_upd["bio"]) > 160:
                profile_upd["bio"] = profile_upd["bio"][:160]
            safe_update("user_profiles", profile_upd, ("user_id", session["user_id"]))

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
def _member_projection(m):
    """Flatten a conversation_members row + embedded user into what the client needs."""
    u = m.get("users") or {}
    prof = u.get("user_profiles") or {}
    return {
        "id": m.get("user_id"),
        "username": u.get("username"),
        "nickname_color": u.get("nickname_color") or "#00d9ff",
        "is_group_admin": m.get("is_group_admin", False),
        # BUG 1: anonymous mode has to travel with the member so the sidebar
        # preview, the chat header and the typing indicator can hide the name.
        "anonymous": bool(u.get("anonymous_mode")),
        "name_font": u.get("name_font"),
        "avatar_url": prof.get("avatar_url"),
        "active_effects": prof.get("active_effects") or [],
        "active_badges": prof.get("active_badges") or [],
    }


MEMBER_EMBED_RICH = (
    "conversation_id,user_id,is_group_admin,"
    "users(id,username,nickname_color,anonymous_mode,name_font,"
    "user_profiles(avatar_url,active_effects,active_badges))"
)
MEMBER_EMBED_SIMPLE = (
    "conversation_id,user_id,is_group_admin,"
    "users(id,username,nickname_color,anonymous_mode,name_font)"
)


def fetch_members(conv_ids):
    """{conversation_id: [member, ...]} for many conversations in one query."""
    grouped = {cid: [] for cid in conv_ids}
    if not conv_ids:
        return grouped
    rows = None
    for embed in (MEMBER_EMBED_RICH, MEMBER_EMBED_SIMPLE):
        try:
            rows = sb.table("conversation_members").select(embed) \
                .in_("conversation_id", conv_ids).execute().data
            break
        except Exception as e:
            print(f"[fetch_members {embed[:24]}…] {e}")
    for m in rows or []:
        grouped.setdefault(m.get("conversation_id"), []).append(_member_projection(m))
    return grouped


def is_envelope(text):
    """True when a message body is Cipher HyperCrypt ciphertext, not plaintext."""
    return bool(text) and text.startswith(CIPHER_ENVELOPE_PREFIX)


@app.route("/api/conversations")
@login_required
def list_conversations():
    """
    Conversation list.

    v1.1 ran 1 + 2N queries here (members + last message per conversation) and
    the client polls it every 10s. This is 4 queries no matter how many chats
    you have, which is most of why the free Render tier felt slow.
    """
    try:
        uid = session["user_id"]
        mem = sb.table("conversation_members").select("conversation_id,muted,is_group_admin,last_read_at") \
            .eq("user_id", uid).execute().data
        if not mem:
            return jsonify({"conversations": []})

        conv_ids = [m["conversation_id"] for m in mem]
        muted_map = {m["conversation_id"]: m.get("muted", False) for m in mem}
        gadmin_map = {m["conversation_id"]: m.get("is_group_admin", False) for m in mem}
        read_map = {m["conversation_id"]: m.get("last_read_at") for m in mem}

        convs = sb.table("conversations").select(
            "id,name,is_group,created_at,updated_at,keep_forever,icon_url"
        ).in_("id", conv_ids).order("updated_at", desc=True).execute().data

        members_by_conv = fetch_members(conv_ids)

        # Last message per conversation, newest-first, in ONE query.
        last_by_conv = {}
        try:
            recent = sb.table("messages").select(
                "id,conversation_id,content,image_url,created_at,is_anonymous,sender_id"
            ).in_("conversation_id", conv_ids).eq("deleted", False) \
                .order("created_at", desc=True).limit(400).execute().data
            for row in recent or []:
                cid = row.get("conversation_id")
                if cid not in last_by_conv:
                    last_by_conv[cid] = row
        except Exception as e:
            print(f"[list_conv last] {e}")

        unread_by_conv = _unread_counts(conv_ids, read_map, uid)

        out = []
        for c in convs:
            last = last_by_conv.get(c["id"]) or {}
            content = last.get("content") or ""
            encrypted = is_envelope(content)
            if encrypted:
                # Never truncate ciphertext — the client decrypts it and then
                # builds the preview locally.
                preview = content
            else:
                preview = content or ("📷 Image" if last.get("image_url") else "")
                if len(preview) > 60:
                    preview = preview[:60] + "…"

            item = {
                "id": c["id"],
                "name": c.get("name"),
                "is_group": c.get("is_group", False),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
                "keep_forever": c.get("keep_forever", False),
                "icon_url": c.get("icon_url"),
                "members": members_by_conv.get(c["id"], []),
                "muted": muted_map.get(c["id"], False),
                "i_am_group_admin": gadmin_map.get(c["id"], False),
                "last_message": preview,
                "last_message_id": last.get("id"),
                "last_encrypted": encrypted,
                "last_has_image": bool(last.get("image_url")),
                "last_sender_id": last.get("sender_id"),
                "last_is_anonymous": bool(last.get("is_anonymous")),
                "last_time": last.get("created_at") or c.get("created_at", ""),
                "unread": unread_by_conv.get(c["id"], 0),
            }

            # BUG 1: an anonymous sender is anonymous in the preview too —
            # unless you are the one who sent it.
            if item["last_is_anonymous"] and item["last_sender_id"] != uid:
                item["last_sender_anonymous"] = True
                item["last_message"] = "🕶 Anonymous"
            else:
                item["last_sender_anonymous"] = False

            out.append(item)

        return jsonify({"conversations": out, "server_time": now_iso()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _unread_counts(conv_ids, read_map, uid):
    """
    Unread-per-conversation in one bounded query.
    Falls back to zeroes if conversation_members.last_read_at isn't there yet.
    """
    counts = {cid: 0 for cid in conv_ids}
    if not any(read_map.values()):
        # Nothing has ever been marked read on this account — don't pretend
        # the entire history is unread.
        if not read_map or all(v is None for v in read_map.values()):
            try:
                has_col = table_columns("conversation_members")
                if has_col and "last_read_at" not in has_col:
                    return counts
            except Exception:
                return counts
    floor = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    try:
        rows = sb.table("messages").select("conversation_id,created_at,sender_id") \
            .in_("conversation_id", conv_ids).eq("deleted", False) \
            .gt("created_at", floor).limit(1000).execute().data
    except Exception:
        return counts
    for r in rows or []:
        cid = r.get("conversation_id")
        if r.get("sender_id") == uid:
            continue
        threshold = read_map.get(cid) or floor
        if r.get("created_at") and r["created_at"] > threshold:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


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
MESSAGE_SENDER_EMBED_RICH = (
    "*,users:sender_id(username,nickname_color,name_font,"
    "user_profiles(avatar_url,active_effects,active_badges))"
)
MESSAGE_SENDER_EMBED_SIMPLE = "*,users:sender_id(username,nickname_color,name_font)"


def _sender_projection(msg):
    """Normalise the embedded sender into a flat `sender` object."""
    u = msg.get("users") or {}
    prof = u.get("user_profiles") or {}
    return {
        "username": u.get("username"),
        "nickname_color": u.get("nickname_color") or "#00d9ff",
        "name_font": u.get("name_font"),
        "avatar_url": prof.get("avatar_url"),
        # BUG 4: effects/badges have to reach OTHER people's screens, so they
        # ride along with the message instead of only living on /api/me.
        "active_effects": prof.get("active_effects") or [],
        "active_badges": prof.get("active_badges") or [],
    }


@app.route("/api/messages/<cid>")
@login_required
def get_messages(cid):
    """
    Message history for one conversation.

    `?since=<iso>` returns only messages newer than that timestamp so the
    poller stops re-downloading (and re-serialising) the whole thread every
    three seconds.
    """
    try:
        uid = session["user_id"]
        m = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not m:
            return jsonify({"error": "Not a member of this conversation"}), 403

        since = (request.args.get("since") or "").strip()
        incremental = bool(since)

        q = None
        rows = None
        for embed in (MESSAGE_SENDER_EMBED_RICH, MESSAGE_SENDER_EMBED_SIMPLE):
            try:
                q = sb.table("messages").select(embed) \
                    .eq("conversation_id", cid).eq("deleted", False)
                if incremental:
                    q = q.gt("created_at", since)
                    rows = q.order("created_at").limit(500).execute().data
                else:
                    rows = q.order("created_at").limit(200).execute().data
                break
            except Exception as e:
                print(f"[get_messages embed] {e}")
        msgs = rows or []

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

        if not incremental:
            # Only counted on a full load — the poller keeps its last value.
            try:
                soon_rows = sb.table("messages").select("id").eq("conversation_id", cid) \
                    .eq("deleted", False).gt("expires_at", now_iso()).lt("expires_at", soon) \
                    .limit(1000).execute().data
                expiring_soon = len(soon_rows or [])
            except Exception:
                expiring_soon = sum(1 for msg in msgs if msg.get("expires_at") and msg["expires_at"] < soon)

        # ============================================================
        # ANONYMOUS FIX (B1): If message is_anonymous, strip sender data
        # UNLESS the viewer is the sender themselves (they see their own msgs normally)
        # ============================================================
        to_mark_read = []
        for msg in msgs:
            msg["reactions"] = reactions_map.get(msg["id"], [])
            msg["read_by"] = reads_map.get(msg["id"], [])
            msg["sender"] = _sender_projection(msg)
            msg["encrypted"] = is_envelope(msg.get("content"))

            if msg.get("is_anonymous") and msg.get("sender_id") != uid:
                # Hide sender identity from recipients
                msg["users"] = {"username": "Anonymous", "nickname_color": "#8892a6"}
                msg["sender"] = {
                    "username": "Anonymous",
                    "nickname_color": "#8892a6",
                    "name_font": None,
                    "avatar_url": None,
                    "active_effects": [],
                    "active_badges": []
                }
                # Also strip the internal sender_id so client can't correlate
                # But we keep it internally for the sender's own view detection
                msg["sender_id_hidden"] = True
                msg["sender_id"] = None

            if msg.get("expires_at") and msg["expires_at"] < soon and incremental:
                expiring_soon += 1

            if msg.get("sender_id") and msg["sender_id"] != uid:
                to_mark_read.append({"message_id": msg["id"], "user_id": uid})

        # Earned badges ride along with the sender (BUG 3), one batched pass
        if msgs:
            attach_badges_to_messages(msgs)

        # Read receipts: one bulk upsert + one pointer update, instead of the
        # one-upsert-per-message the poller used to fire every cycle.
        if to_mark_read:
            try:
                sb.table("message_reads").upsert(to_mark_read, on_conflict="message_id,user_id").execute()
            except Exception as e:
                print(f"[read_receipts] {e}")
        try:
            safe_update("conversation_members", {"last_read_at": now_iso()},
                        ("conversation_id", cid), ("user_id", uid))
        except Exception:
            pass

        return jsonify({
            "messages": msgs,
            "expiring_soon": expiring_soon,
            "incremental": incremental,
            "server_time": now_iso()
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
        raw_content = (data.get("content") or "").strip()
        image_data = data.get("image_data")
        encrypted = is_envelope(raw_content)

        # Ciphertext is bigger than the plaintext it hides, so envelopes get a
        # larger budget than the 4000 chars of readable text.
        content = raw_content[:CIPHER_MAX_ENVELOPE] if encrypted else raw_content[:4000]

        if not content and not image_data:
            return jsonify({"error": "Empty message"}), 400
        if encrypted and len(raw_content) > CIPHER_MAX_ENVELOPE:
            return jsonify({"error": "Message is too large"}), 400

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
                perks = active_perks(uid)
                max_size = 5 * 1024 * 1024
                if "perk_upload_10mb_30_days" in perks:
                    max_size = 10 * 1024 * 1024

                if "," in image_data:
                    image_data = image_data.split(",", 1)[1]
                img_bytes = base64.b64decode(image_data)
                if len(img_bytes) > max_size:
                    mb = max_size // (1024 * 1024)
                    return jsonify({"error": f"Image too large (max {mb} MB)"}), 400

                if encrypted:
                    # These bytes are already ciphertext: the server stores
                    # them blind and never learns what the picture was.
                    filename = f"{uid}/{secrets.token_hex(12)}.cph"
                    try:
                        sb.storage.from_(CIPHER_VAULT_BUCKET).upload(
                            filename, img_bytes,
                            {"content-type": "application/octet-stream"}
                        )
                        image_url = sb.storage.from_(CIPHER_VAULT_BUCKET).get_public_url(filename)
                    except Exception:
                        sb.storage.from_("cipher-images").upload(
                            filename, img_bytes,
                            {"content-type": "application/octet-stream"}
                        )
                        image_url = sb.storage.from_("cipher-images").get_public_url(filename)
                else:
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
        settings = get_settings()
        keep = conv[0].get("keep_forever", False) if conv else False
        days = settings.get("default_retention_days", 30) or 30

        # perk_retention_30_days adds 30 days to retention
        if "perk_retention_30_days" in active_perks(uid):
            days += 30

        if keep or user.get("keep_all_forever", False):
            expires_at = None
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

        payload = {
            "conversation_id": cid,
            "sender_id": uid,
            "content": content if content else None,
            "image_url": image_url,
            "is_anonymous": is_anon,
            "expires_at": expires_at
        }
        if encrypted:
            payload["cipher_version"] = 1
        msg = safe_insert("messages", payload)
        if not msg:
            return jsonify({"error": "Message could not be saved"}), 500

        # Message milestones. Bots never count toward their owner's bonuses.
        if not user.get("is_bot"):
            try:
                register_message_sent(uid)
            except Exception as e:
                print(f"[milestone] {e}")

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
        typing = sb.table("typing_status").select(
            "user_id,users(username,anonymous_mode)"
        ).eq("conversation_id", cid).gt("started_at", cutoff).execute().data
        users = []
        for t in typing:
            if t["user_id"] == uid:
                continue
            u = t.get("users") or {}
            # BUG 1: typing indicators leaked real names past anonymous mode.
            if u.get("anonymous_mode"):
                users.append("Anonymous")
            elif u.get("username"):
                users.append(u["username"])
        # collapse duplicates ("Anonymous" x3 is noise)
        users = list(dict.fromkeys(users))
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
    """Markdown for the current modal, plus structured sections for the new one."""
    try:
        sections = tos_sections()
    except Exception:
        sections = []
    return jsonify({"tos": TOS_CONTENT, "sections": sections, "version": APP_VERSION})


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
        invalidate_punish_cache(uid)
        invalidate_user_cache(uid)
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
        invalidate_punish_cache(uid)
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
        rows = sb.table("user_punishments").select("user_id").eq("id", pid).execute().data
        sb.table("user_punishments").update({"active": False}).eq("id", pid).execute()
        if rows:
            invalidate_punish_cache(rows[0].get("user_id"))
        else:
            invalidate_punish_cache()
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
        invalidate_ban_cache(ip)
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
        invalidate_ban_cache(ip)
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
                   "shards_per_referral", "affiliate_mode",
                   # v1.2
                   "anti_cheat_enabled", "ask_nicely_enabled", "ask_nicely_chance",
                   "sorry_button_enabled", "admins_can_grant_shards",
                   "admins_can_grant_cores", "admin_core_grant_max",
                   "admins_can_approve_asks", "admin_ask_grant_amounts",
                   "admins_can_grant_custom_ask", "global_streamer_forces",
                   "bot_creation_policy"]
        upd = {k: v for k, v in data.items() if k in allowed}
        # keep the types honest — these land in typed columns
        for flag in ("anti_cheat_enabled", "ask_nicely_enabled", "sorry_button_enabled",
                     "admins_can_grant_shards", "admins_can_grant_cores",
                     "admins_can_approve_asks", "admins_can_grant_custom_ask",
                     "signups_enabled", "invites_enabled", "maintenance_mode"):
            if flag in upd:
                upd[flag] = bool(upd[flag])
        for num in ("admin_core_grant_max", "default_retention_days", "shards_per_referral"):
            if num in upd:
                try:
                    upd[num] = int(upd[num])
                except Exception:
                    upd.pop(num)
        if "ask_nicely_chance" in upd:
            try:
                upd["ask_nicely_chance"] = max(0.0, min(1.0, float(upd["ask_nicely_chance"])))
            except Exception:
                upd.pop("ask_nicely_chance")
        if "bot_creation_policy" in upd and upd["bot_creation_policy"] not in (
                "purchase_only", "anyone", "admin", "owner"):
            upd.pop("bot_creation_policy")

        if upd:
            safe_update("admin_settings", upd, ("id", 1))
            invalidate_settings_cache()
            audit("update_settings", "settings", 1, str(upd))
        return jsonify({"ok": True, "settings": get_settings(force=True)})
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
        admins = sb.table("users").select("id,username,is_owner,is_admin,nickname_color,created_at,admin_via_purchase") \
            .eq("is_admin", True).order("username").execute().data
        # BUG 2: the owner is the one person who must always show up here, even
        # when is_admin was never flipped on for the owner account.
        try:
            owners = sb.table("users").select("id,username,is_owner,is_admin,nickname_color,created_at,admin_via_purchase") \
                .eq("is_owner", True).execute().data
            seen = {a["id"] for a in admins}
            for o in owners or []:
                if o["id"] not in seen:
                    admins.append(o)
        except Exception:
            pass
        result = []
        for a in admins:
            perms = get_admin_permissions(a["id"])
            result.append({
                "id": a["id"],
                "username": a["username"],
                "is_owner": a.get("is_owner", False),
                "admin_via_purchase": a.get("admin_via_purchase", False),
                "nickname_color": a.get("nickname_color") or "#00d9ff",
                "created_at": a.get("created_at"),
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
        # Owner first, then purchased admins (the owner wants those visible),
        # then everyone else alphabetically.
        result.sort(key=lambda a: (not a["is_owner"], not a["admin_via_purchase"], a["username"]))
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
            item["currency"] = (item.get("currency") or "shards").lower()

        me = current_user() or {}
        return jsonify({
            "items": items,
            "balances": {
                "shards": me.get("shards", 0) or 0,
                "cores": me.get("cores", 0) or 0
            }
        })
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

        # Check balance (Shards or Cores, depending on the item)
        currency = (item.get("currency") or "shards").lower()
        price = item.get("price", 0) or 0
        balance = (u.get("cores", 0) or 0) if currency == "cores" else (u.get("shards", 0) or 0)
        label = "Cores" if currency == "cores" else "Shards"
        if balance < price:
            return jsonify({"error": f"Not enough {label} (need {price}, have {balance})"}), 400

        # Deduct
        if currency == "cores":
            new_balance = award_cores(
                uid, -price, "purchase", f"Bought: {item['name']}",
                related_type="shop_items", related_id=item["id"]
            )
        else:
            new_balance = award_shards(
                uid, -price, "purchase", f"Bought: {item['name']}",
                related_table="shop_items", related_id=item["id"]
            )
        invalidate_perks_cache(uid)

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

        # ---- Post-purchase wiring (CLEANUP 4) -------------------------
        # Every purchase answers "now go here and use it".
        next_action = None
        auto_equipped = False

        if item_key == "cores_admin_instant":
            safe_update("users", {
                "is_admin": True,
                "admin_via_purchase": True,
                "admin_purchased_at": now_iso()
            }, ("id", uid))
            invalidate_user_cache(uid)
            try:
                existing = sb.table("admin_permissions").select("user_id").eq("user_id", uid).execute().data
                if not existing:
                    sb.table("admin_permissions").insert({"user_id": uid, "granted_by": uid}).execute()
            except Exception:
                pass
            audit("admin_via_purchase", "user", uid, item["name"])
            next_action = {"action": "admin_panel", "label": "Open the admin panel"}

        elif item_key == "cores_add_by_owner":
            try:
                owners = sb.table("users").select("id,username").eq("is_owner", True).execute().data
                for o in owners or []:
                    push_notification(o["id"], "add_request", {
                        "username": u["username"], "item": item["name"]
                    })
            except Exception:
                pass
            next_action = {"action": "none", "label": "The owner has been asked"}

        elif item_key in ("cores_spotlight_basic", "cores_spotlight_custom"):
            next_action = {"action": "spotlight", "item_key": item_key,
                           "label": "Set up my spotlight"}

        elif item_key == "cores_bot_key":
            next_action = {"action": "bots", "label": "Create my bot"}

        elif item_key == "cores_theme_color":
            next_action = {"action": "settings", "focus": "theme", "label": "Pick my theme color"}

        elif item_key == "banner_color":
            next_action = {"action": "settings", "focus": "banner", "label": "Choose banner color"}

        elif item_key == "profile_bio":
            next_action = {"action": "settings", "focus": "bio", "label": "Set my bio now"}

        elif item_key == "profile_picture_upload":
            next_action = {"action": "avatar", "label": "Upload a picture"}

        elif item_key == "cores_group_room_icon":
            next_action = {"action": "group_icon", "label": "Change a group icon"}

        elif item.get("category") in ("avatar_effects", "badges"):
            # Buying a cosmetic equips it — nobody wants to buy a glow and
            # then have to go and find the switch.
            try:
                if shop_equip_item(uid, item, True):
                    auto_equipped = True
            except Exception as e:
                print(f"[auto_equip] {e}")
            next_action = {"action": "equipped", "label": "Equipped"}

        elif item.get("category") == "chat":
            next_action = {"action": "equip_chat", "item_key": item_key,
                           "label": "Pick your style"}

        return jsonify({
            "ok": True,
            "new_balance": new_balance,
            "currency": currency,
            "item_key": item_key,
            "auto_equipped": auto_equipped,
            "next_action": next_action
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def shop_equip_item(uid, item, equip, value=None):
    """
    Shared equip logic used by /api/shop/equip and by the auto-equip on
    purchase. Returns True when the profile actually changed.
    """
    item_id = item["id"]
    item_key = item.get("item_key", "")
    category = item.get("category", "")
    # BUG 3: older rows stored the visual key in css_class instead of
    # effect_key, so badges/effects equipped to nothing at all.
    effect_key = item.get("effect_key") or item.get("css_class") or item_key

    sb.table("user_purchases").update({"equipped": equip}).eq("user_id", uid).eq("item_id", item_id).execute()
    ensure_user_profile(uid)
    profile_res = sb.table("user_profiles").select("*").eq("user_id", uid).execute().data
    profile = profile_res[0] if profile_res else {}

    if category in ("avatar_effects", "effects"):
        active_effects = list(profile.get("active_effects", []) or [])
        try:
            all_effects = sb.table("shop_items").select("effect_key,css_class,id") \
                .in_("category", ["avatar_effects", "effects"]).execute().data
        except Exception:
            all_effects = []
        other_keys = [e.get("effect_key") or e.get("css_class") for e in all_effects
                      if (e.get("effect_key") or e.get("css_class")) and e["id"] != item_id]
        if equip:
            active_effects = [ek for ek in active_effects if ek not in other_keys]
            if effect_key and effect_key not in active_effects:
                active_effects.append(effect_key)
            for other in all_effects:
                if other["id"] != item_id:
                    try:
                        sb.table("user_purchases").update({"equipped": False}) \
                            .eq("user_id", uid).eq("item_id", other["id"]).execute()
                    except Exception:
                        pass
        else:
            active_effects = [ek for ek in active_effects if ek != effect_key]
        safe_update("user_profiles", {"active_effects": active_effects}, ("user_id", uid))
        return True

    if category == "badges":
        active_badges = list(profile.get("active_badges", []) or [])
        if equip:
            if effect_key and effect_key not in active_badges:
                active_badges.append(effect_key)
        else:
            active_badges = [b for b in active_badges if b != effect_key]
        safe_update("user_profiles", {"active_badges": active_badges}, ("user_id", uid))
        return True

    if category == "chat":
        if item_key == "chat_bubble_colors":
            new_val = value if (equip and value) else None
            safe_update("user_profiles", {"active_bubble_color": new_val}, ("user_id", uid))
            safe_update("users", {"bubble_color": new_val}, ("id", uid))
        elif item_key == "chat_nickname_font":
            new_val = (value or "italic") if equip else None
            safe_update("user_profiles", {"active_nickname_font": new_val}, ("user_id", uid))
            safe_update("users", {"name_font": new_val}, ("id", uid))
        elif item_key == "chat_send_animation":
            new_val = (value or "slide") if equip else None
            safe_update("user_profiles", {"active_message_animation": new_val}, ("user_id", uid))
            safe_update("users", {"msg_animation": new_val}, ("id", uid))
        invalidate_user_cache(uid)
        return True

    return False


@app.route("/api/shop/equip/<item_id>", methods=["POST"])
@login_required
def shop_equip(item_id):
    """Equip or unequip an owned item."""
    try:
        uid = session["user_id"]
        body = request.json or {}
        equip = bool(body.get("equip", True))

        purch = sb.table("user_purchases").select("id,equipped").eq("user_id", uid).eq("item_id", item_id).execute().data
        if not purch:
            return jsonify({"error": "You don't own this item"}), 400

        item_res = sb.table("shop_items").select("*").eq("id", item_id).execute().data
        if not item_res:
            return jsonify({"error": "Item not found"}), 404

        shop_equip_item(uid, item_res[0], equip, value=body.get("value"))
        invalidate_user_cache(uid)
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
        query = sb.table("users").select(
            "id,username,shards,nickname_color,created_at,is_owner,is_admin,"
            "total_messages_sent,shards_gifted_total,shards_earned_this_week"
        ).eq("leaderboard_opt_out", False)

        if sort == "newest":
            query = query.order("created_at", desc=True)
        elif sort == "oldest":
            query = query.order("created_at")
        else:
            query = query.order("shards", desc=True)

        users = query.limit(50).execute().data
        ids = [u["id"] for u in users]

        # Two batched lookups instead of 2 per user (this used to be ~100
        # queries for one leaderboard open).
        profiles = {}
        if ids:
            try:
                rows = sb.table("user_profiles").select("user_id,avatar_url,active_effects,active_badges") \
                    .in_("user_id", ids).execute().data
                profiles = {r["user_id"]: r for r in rows or []}
            except Exception:
                profiles = {}

        ref_counts = {}
        if ids:
            try:
                rows = sb.table("affiliate_uses").select("referrer_id").in_("referrer_id", ids) \
                    .limit(5000).execute().data
                for r in rows or []:
                    rid = r.get("referrer_id")
                    ref_counts[rid] = ref_counts.get(rid, 0) + 1
            except Exception:
                ref_counts = {}

        for u in users:
            prof = profiles.get(u["id"]) or {}
            u["avatar_url"] = prof.get("avatar_url")
            u["active_effects"] = prof.get("active_effects", []) or []
            u["referrals"] = ref_counts.get(u["id"], 0)
            u["active_badges"] = all_badges(prof.get("active_badges"), compute_system_badges(u))

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
        u = sb.table("users").select(
            "id,username,nickname_color,created_at,shards,leaderboard_opt_out,is_owner,"
            "is_admin,is_bot,friend_privacy,total_messages_sent,last_warning_at,name_font"
        ).eq("username", username).execute().data
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

        # Longest stretch without a warning, for the profile card stats
        streak = account_age_days(user)
        if user.get("last_warning_at"):
            try:
                lw = datetime.fromisoformat(str(user["last_warning_at"]).replace("Z", "+00:00"))
                streak = (datetime.now(timezone.utc) - lw).days
            except Exception:
                pass

        # Where do I stand with this person?
        friendship = {"state": "none", "friendship_id": None}
        try:
            viewer = session["user_id"]
            if viewer != user["id"]:
                a = sb.table("friendships").select("id,status").eq("requester_id", viewer).eq("addressee_id", user["id"]).execute().data
                b = sb.table("friendships").select("id,status").eq("requester_id", user["id"]).eq("addressee_id", viewer).execute().data
                for row in (a or []) + (b or []):
                    if row.get("status") == "accepted":
                        friendship = {"state": "friends", "friendship_id": row["id"]}
                        break
                    if row.get("status") == "pending":
                        friendship = {"state": "pending_out" if (a and row in a) else "pending_in",
                                      "friendship_id": row["id"]}
        except Exception:
            pass

        me = current_user() or {}
        return jsonify({
            "user": {
                "username": user["username"],
                "nickname_color": user.get("nickname_color") or "#00d9ff",
                "created_at": user.get("created_at"),
                "bio": profile.get("bio", ""),
                "avatar_url": profile.get("avatar_url"),
                "banner_color": profile.get("banner_color"),
                "name_font": user.get("name_font"),
                "active_effects": profile.get("active_effects", []) or [],
                "active_badges": all_badges(profile.get("active_badges"),
                                            compute_system_badges(user, referrals=referrals)),
                "shards": None if opted_out else (user.get("shards", 0) or 0),
                "referrals": None if opted_out else referrals,
                "total_messages_sent": user.get("total_messages_sent", 0) or 0,
                "no_warning_streak_days": streak,
                "hidden": opted_out,
                "is_bot": user.get("is_bot", False),
                "friend_privacy": user.get("friend_privacy") or "approval",
                "friendship": friendship,
                "is_self": session.get("user_id") == user["id"],
                "i_am_admin": bool(me.get("is_admin") or me.get("is_owner"))
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — SHARD ECONOMY (daily bonus, milestones, longevity,
#   ToS bonus, gifts, referrals) + CORES + NOTIFICATIONS
# ============================================================
MESSAGE_MILESTONES = [
    ("msg_200", 200, 2),
    ("msg_500", 500, 5),
    ("msg_1000", 1000, 10),
    ("msg_5000", 5000, 25),
    ("msg_10000", 10000, 50),
    ("msg_50000", 50000, 200),
]

LONGEVITY_BONUSES = [
    ("no_warn_7d", 7, 1),
    ("no_warn_30d", 30, 3),
    ("no_warn_90d", 90, 7),
    ("no_warn_180d", 180, 15),
    ("no_warn_1y", 365, 13),
    ("no_warn_2y", 730, 20),
]

WEEKLY_TIERS = [("weekly_diamond", 200), ("weekly_gold", 100), ("weekly_silver", 25), ("weekly_bronze", 5)]


def push_notification(user_id, kind, payload=None):
    """Queue an in-app notification. Silently no-ops if the table is missing."""
    if not user_id:
        return None
    return safe_insert("notifications", {
        "user_id": user_id,
        "kind": kind,
        "payload": payload or {},
        "read": False
    })


def bump_weekly_earned(user_id, amount):
    """Track shards earned this week (drives the Weekly Bronze→Diamond badges)."""
    try:
        row = sb.table("users").select("shards_earned_this_week,week_start").eq("id", user_id).execute().data
        if not row:
            return
        earned = row[0].get("shards_earned_this_week", 0) or 0
        week_start = row[0].get("week_start")
        reset = True
        if week_start:
            try:
                ws = datetime.fromisoformat(str(week_start).replace("Z", "+00:00"))
                reset = (datetime.now(timezone.utc) - ws).days >= 7
            except Exception:
                reset = True
        if reset:
            earned = 0
        safe_update("users", {
            "shards_earned_this_week": earned + amount,
            "week_start": now_iso()
        }, ("id", user_id))
        invalidate_user_cache(user_id)
    except Exception as e:
        print(f"[bump_weekly_earned] {e}")


def has_milestone(user_id, key):
    try:
        rows = sb.table("user_milestones").select("id").eq("user_id", user_id).eq("bonus_key", key).limit(1).execute().data
        return bool(rows)
    except Exception:
        return False


def claim_milestone(user_id, key, amount, description):
    """Award a one-time bonus exactly once. Returns True if it paid out."""
    if table_columns("user_milestones") is None:
        return False               # cannot record it, so cannot pay it
    if has_milestone(user_id, key):
        return False
    new_balance = award_shards(user_id, amount, "milestone", description)
    if new_balance is None:
        return False
    safe_insert("user_milestones", {
        "user_id": user_id,
        "bonus_key": key,
        "shards_awarded": amount
    })
    push_notification(user_id, "milestone", {"key": key, "shards": amount, "text": description})
    return True


def register_message_sent(user_id):
    """Bump the message counter and pay any milestone it just crossed."""
    try:
        if missing_columns("users", ["total_messages_sent"]):
            return None            # no counter, no payout — fail closed
        rows = sb.table("users").select("total_messages_sent").eq("id", user_id).execute().data
        if not rows:
            return None
        total = (rows[0].get("total_messages_sent", 0) or 0) + 1
        safe_update("users", {"total_messages_sent": total}, ("id", user_id))
        invalidate_user_cache(user_id)
        for key, threshold, reward in MESSAGE_MILESTONES:
            if total == threshold:
                claim_milestone(user_id, key, reward, f"{threshold:,} messages sent")
        return total
    except Exception as e:
        print(f"[register_message_sent] {e}")
        return None


def check_longevity_bonuses(user_id, force=False):
    """
    Pay the 'no warnings for X days' bonuses. Runs on login and from the
    background job; the last_no_warning_check column keeps it to once a day.
    """
    try:
        rows = sb.table("users").select("id,last_warning_at,created_at,last_no_warning_check").eq("id", user_id).execute().data
        if not rows:
            return []
        u = rows[0]
        if not force:
            last_check = u.get("last_no_warning_check")
            if last_check:
                try:
                    lc = datetime.fromisoformat(str(last_check).replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - lc).total_seconds() < 20 * 3600:
                        return []
                except Exception:
                    pass
        anchor = u.get("last_warning_at") or u.get("created_at")
        if not anchor:
            return []
        try:
            anchor_dt = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
        except Exception:
            return []
        days = (datetime.now(timezone.utc) - anchor_dt).days
        paid = []
        for key, need, reward in LONGEVITY_BONUSES:
            if days >= need and claim_milestone(user_id, key, reward, f"{need} days with no warnings"):
                paid.append(key)
        safe_update("users", {"last_no_warning_check": now_iso()}, ("id", user_id))
        invalidate_user_cache(user_id)
        return paid
    except Exception as e:
        print(f"[longevity] {e}")
        return []


@app.route("/api/shards/daily", methods=["POST"])
@login_required
def claim_daily_bonus():
    """1 Shard per 24h. Missed days do not stack."""
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401
        miss = missing_columns("users", ["last_daily_claim"])
        if miss:
            return migration_error(miss)
        last = user.get("last_daily_claim")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                elapsed = datetime.now(timezone.utc) - last_dt
                if elapsed < timedelta(hours=24):
                    remaining = timedelta(hours=24) - elapsed
                    hours = int(remaining.total_seconds() // 3600)
                    mins = int((remaining.total_seconds() % 3600) // 60)
                    return jsonify({
                        "error": f"Already claimed. Come back in {hours}h {mins}m.",
                        "retry_in_seconds": int(remaining.total_seconds())
                    }), 429
            except Exception:
                pass
        balance = award_shards(uid, 1, "daily", "Daily login bonus")
        if balance is None:
            return jsonify({"error": "Could not award the bonus"}), 500
        safe_update("users", {"last_daily_claim": now_iso()}, ("id", uid))
        invalidate_user_cache(uid)
        return jsonify({"ok": True, "shards": balance, "awarded": 1})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shards/daily")
@login_required
def daily_bonus_status():
    """Is the daily bonus available right now? Cheap enough to poll."""
    try:
        user = current_user()
        if not user:
            return jsonify({"available": False})
        last = user.get("last_daily_claim")
        if not last:
            return jsonify({"available": True})
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - last_dt
            available = delta >= timedelta(hours=24)
            return jsonify({
                "available": available,
                "retry_in_seconds": 0 if available else int((timedelta(hours=24) - delta).total_seconds())
            })
        except Exception:
            return jsonify({"available": True})
    except Exception:
        return jsonify({"available": False})


@app.route("/api/shards/tos_bonus", methods=["POST"])
@login_required
def claim_tos_bonus():
    """+5 Shards once per account for actually opening the Terms."""
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401
        miss = missing_columns("users", ["tos_bonus_claimed"])
        if miss:
            return migration_error(miss)
        if user.get("tos_bonus_claimed"):
            return jsonify({"error": "Already claimed"}), 400
        balance = award_shards(uid, 5, "bonus", "Read the Terms of Service")
        if balance is None:
            return jsonify({"error": "Could not award the bonus"}), 500
        safe_update("users", {"tos_bonus_claimed": True}, ("id", uid))
        invalidate_user_cache(uid)
        return jsonify({"ok": True, "shards": balance, "awarded": 5})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shards/gift", methods=["POST"])
@login_required
def gift_shards():
    """Send Shards to another user."""
    try:
        uid = session["user_id"]
        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        message = (data.get("message") or "").strip()[:280]
        try:
            amount = int(data.get("amount") or 0)
        except Exception:
            return jsonify({"error": "Amount must be a number"}), 400
        if amount <= 0:
            return jsonify({"error": "Amount must be at least 1"}), 400
        if amount > 100000:
            return jsonify({"error": "That is too many Shards to send at once"}), 400

        sender = current_user()
        if not sender:
            return jsonify({"error": "Session expired"}), 401
        miss = missing_columns("users", ["shards_gifted_total"])
        if miss:
            return migration_error(miss)
        if username == sender["username"]:
            return jsonify({"error": "You cannot gift Shards to yourself"}), 400
        if (sender.get("shards", 0) or 0) < amount:
            return jsonify({"error": f"You only have {sender.get('shards', 0) or 0} Shards"}), 400

        target = sb.table("users").select("id,username,is_bot").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "No user with that name"}), 404
        recipient = target[0]
        if recipient.get("is_bot"):
            return jsonify({"error": "Bots cannot hold Shards"}), 400

        new_balance = award_shards(uid, -amount, "gift", f"Gifted to @{recipient['username']}")
        if new_balance is None:
            return jsonify({"error": "Transfer failed"}), 500
        award_shards(recipient["id"], amount, "gift", f"Gift from @{sender['username']}")
        safe_insert("shard_gifts", {
            "sender_id": uid,
            "recipient_id": recipient["id"],
            "amount": amount,
            "message": message
        })
        safe_update("users", {
            "shards_gifted_total": (sender.get("shards_gifted_total", 0) or 0) + amount
        }, ("id", uid))
        invalidate_user_cache(uid)

        # One-time gift bonuses, both directions
        claim_milestone(uid, "first_gift_sent", 5, "First Shards gifted")
        claim_milestone(recipient["id"], "first_gift_received", 5, "First Shards received")

        push_notification(recipient["id"], "gift", {
            "from": sender["username"],
            "amount": amount,
            "message": message
        })
        audit("gift_shards", "user", recipient["id"], f"{amount} shards to @{recipient['username']}")
        return jsonify({
            "ok": True,
            "shards": new_balance,
            "to": recipient["username"],
            "amount": amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------ CORES
def can_grant_cores(user):
    if not user:
        return False, 0
    if user.get("is_owner"):
        return True, 1000000
    if user.get("can_grant_cores"):
        return True, 1000000
    settings = get_settings()
    if user.get("is_admin") and settings.get("admins_can_grant_cores"):
        return True, int(settings.get("admin_core_grant_max", 3) or 3)
    return False, 0


@app.route("/api/cores/grant", methods=["POST"])
@login_required
def grant_cores():
    """Cores are only ever handed out by a person. Never earned."""
    try:
        granter = current_user()
        if not granter:
            return jsonify({"error": "Session expired"}), 401
        miss = missing_columns("users", ["cores"])
        if miss:
            return migration_error(miss)
        allowed, cap = can_grant_cores(granter)
        if not allowed:
            return jsonify({"error": "You cannot grant Cores"}), 403

        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        reason = (data.get("reason") or "").strip()
        try:
            amount = int(data.get("amount") or 0)
        except Exception:
            return jsonify({"error": "Amount must be a number"}), 400
        if amount <= 0:
            return jsonify({"error": "Amount must be positive"}), 400
        if amount > cap:
            return jsonify({"error": f"You can grant at most {cap} Cores at a time"}), 400
        if len(reason) < 10:
            return jsonify({"error": "Please give a reason (at least 10 characters)"}), 400

        target = sb.table("users").select("id,username").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "No user with that name"}), 404

        balance = award_cores(target[0]["id"], amount, "grant", reason, granted_by=granter["id"])
        if balance is None:
            return jsonify({"error": "Grant failed"}), 500
        push_notification(target[0]["id"], "cores", {
            "amount": amount,
            "reason": reason,
            "from": granter["username"]
        })
        audit("grant_cores", "user", target[0]["id"], f"{amount} cores: {reason}")
        return jsonify({"ok": True, "cores": balance, "user": target[0]["username"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cores/history")
@login_required
def cores_history():
    try:
        uid = session["user_id"]
        tx = sb.table("core_transactions").select("*").eq("user_id", uid) \
            .order("created_at", desc=True).limit(100).execute().data
        return jsonify({"transactions": tx or []})
    except Exception:
        return jsonify({"transactions": []})


@app.route("/api/admin/cores")
@admin_required
def admin_cores_overview():
    """Total Cores in circulation + the most recent grants."""
    try:
        users = sb.table("users").select("id,username,cores").execute().data
        total = sum((u.get("cores") or 0) for u in users or [])
        holders = sorted([u for u in users or [] if (u.get("cores") or 0) > 0],
                         key=lambda u: u.get("cores") or 0, reverse=True)[:50]
        tx = []
        try:
            tx = sb.table("core_transactions").select("*,users(username)") \
                .order("created_at", desc=True).limit(60).execute().data
        except Exception:
            try:
                tx = sb.table("core_transactions").select("*") \
                    .order("created_at", desc=True).limit(60).execute().data
            except Exception:
                tx = []
        granter = current_user() or {}
        allowed, cap = can_grant_cores(granter)
        return jsonify({
            "total_in_circulation": total,
            "holders": holders,
            "transactions": tx or [],
            "can_grant": allowed,
            "grant_cap": cap,
            "settings": {
                "admins_can_grant_cores": get_settings().get("admins_can_grant_cores", False),
                "admin_core_grant_max": get_settings().get("admin_core_grant_max", 3)
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/user/<uid>/grant_shards", methods=["POST"])
@login_required
def admin_grant_shards(uid):
    """Owner always; admins only when admin_settings.admins_can_grant_shards."""
    try:
        me = current_user()
        if not me:
            return jsonify({"error": "Session expired"}), 401
        allowed = bool(me.get("is_owner")) or (
            bool(me.get("is_admin")) and bool(get_settings().get("admins_can_grant_shards"))
        )
        if not allowed:
            return jsonify({"error": "You cannot grant Shards"}), 403

        data = request.json or {}
        reason = (data.get("reason") or "").strip()
        try:
            amount = int(data.get("amount") or 0)
        except Exception:
            return jsonify({"error": "Amount must be a number"}), 400
        if amount == 0 or abs(amount) > 100000:
            return jsonify({"error": "Amount looks wrong"}), 400
        if len(reason) < 10:
            return jsonify({"error": "Please give a reason (at least 10 characters)"}), 400
        if not me.get("is_owner") and amount < 0:
            return jsonify({"error": "Only the owner can take Shards away"}), 403

        target = sb.table("users").select("id,username").eq("id", uid).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404

        balance = award_shards(uid, amount, "admin_grant", reason, created_by=me["id"])
        if balance is None:
            return jsonify({"error": "Grant failed"}), 500
        push_notification(uid, "shards", {
            "amount": amount,
            "reason": reason,
            "from": me["username"]
        })
        audit("grant_shards", "user", uid, f"{amount} shards: {reason}")
        return jsonify({"ok": True, "shards": balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------ NOTIFICATIONS
@app.route("/api/notifications")
@login_required
def list_notifications():
    try:
        uid = session["user_id"]
        rows = sb.table("notifications").select("*").eq("user_id", uid) \
            .order("created_at", desc=True).limit(40).execute().data
        rows = rows or []
        return jsonify({
            "notifications": rows,
            "unread": sum(1 for r in rows if not r.get("read"))
        })
    except Exception:
        # Table not migrated yet — notifications are a nicety, not a dependency
        return jsonify({"notifications": [], "unread": 0})


@app.route("/api/notifications/<nid>/read", methods=["POST"])
@login_required
def read_notification(nid):
    try:
        uid = session["user_id"]
        safe_update("notifications", {"read": True}, ("id", nid), ("user_id", uid))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/read_all", methods=["POST"])
@login_required
def read_all_notifications():
    try:
        uid = session["user_id"]
        try:
            sb.table("notifications").update({"read": True}).eq("user_id", uid).eq("read", False).execute()
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — BADGES
#   System badges are computed, never stored, so they can never
#   drift out of date. Purchased badges live in
#   user_profiles.active_badges and are merged in on the way out.
# ============================================================
BADGE_ORDER = [
    "owner", "admin", "founder", "cipher_og", "longtimer", "referral_king",
    "wordsmith", "generous", "rich", "weekly_diamond", "weekly_gold",
    "weekly_silver", "weekly_bronze", "goat", "vip", "trusted", "supporter",
    "custom",
]

BADGE_INFO = {
    "owner": {"label": "Owner", "icon": "👑"},
    "admin": {"label": "Admin", "icon": "🛡️"},
    "founder": {"label": "Founder", "icon": "🏛️"},
    "cipher_og": {"label": "Cipher OG", "icon": "💫"},
    "longtimer": {"label": "Longtimer", "icon": "🕰️"},
    "referral_king": {"label": "Referral King", "icon": "🎯"},
    "wordsmith": {"label": "Wordsmith", "icon": "✍️"},
    "generous": {"label": "Generous", "icon": "🎁"},
    "rich": {"label": "Rich", "icon": "💰"},
    "weekly_diamond": {"label": "Weekly Diamond", "icon": "💠"},
    "weekly_gold": {"label": "Weekly Gold", "icon": "🥇"},
    "weekly_silver": {"label": "Weekly Silver", "icon": "🥈"},
    "weekly_bronze": {"label": "Weekly Bronze", "icon": "🥉"},
    "goat": {"label": "GOAT", "icon": "🐐", "tip": "Good Guy That Does Good Stuff And Earns Well"},
    "vip": {"label": "VIP", "icon": "👑"},
    "trusted": {"label": "Trusted", "icon": "⭐"},
    "supporter": {"label": "Trusted", "icon": "⭐"},
}

_founder_cache = {}


def is_founder(user):
    """One of the first ten accounts. Computed once per worker, then cached."""
    uid = user.get("id")
    if uid in _founder_cache:
        return _founder_cache[uid]
    result = False
    try:
        created = user.get("created_at")
        if created:
            older = sb.table("users").select("id", count="exact").lt("created_at", created).execute()
            result = (older.count or 0) < 10
    except Exception:
        result = False
    _founder_cache[uid] = result
    return result


def set_flag(user_id, key):
    """Sticky boolean that costs nothing and pays nothing (e.g. 'was rich')."""
    try:
        existing = sb.table("user_milestones").select("id").eq("user_id", user_id).eq("bonus_key", key).limit(1).execute().data
        if existing:
            return False
        safe_insert("user_milestones", {"user_id": user_id, "bonus_key": key, "shards_awarded": 0})
        return True
    except Exception:
        return False


def has_flag(user_id, key):
    try:
        rows = sb.table("user_milestones").select("id").eq("user_id", user_id).eq("bonus_key", key).limit(1).execute().data
        return bool(rows)
    except Exception:
        return False


def account_age_days(user):
    created = user.get("created_at")
    if not created:
        return 0
    try:
        c = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - c).days
    except Exception:
        return 0


def compute_system_badges(user, referrals=None):
    """Badges the user has earned by doing things, not by buying things."""
    if not user:
        return []
    out = []
    if user.get("is_owner"):
        out.append("owner")
    if user.get("is_admin") and not user.get("is_owner"):
        out.append("admin")
    if is_founder(user):
        out.append("founder")

    age = account_age_days(user)
    if age >= 30:
        out.append("cipher_og")
    if age >= 365:
        out.append("longtimer")

    if referrals is None:
        try:
            ref = sb.table("affiliate_uses").select("id", count="exact").eq("referrer_id", user["id"]).execute()
            referrals = ref.count or 0
        except Exception:
            referrals = 0
    if referrals >= 10:
        out.append("referral_king")

    if (user.get("total_messages_sent") or 0) >= 10000:
        out.append("wordsmith")
    if (user.get("shards_gifted_total") or 0) >= 100:
        out.append("generous")

    shards = user.get("shards") or 0
    if shards >= 500:
        set_flag(user["id"], "rich_500")
        out.append("rich")
    elif shards >= 100 and has_flag(user["id"], "rich_500"):
        # Keeps the badge until the balance drops under 100, as specified.
        out.append("rich")

    earned = user.get("shards_earned_this_week") or 0
    for key, need in WEEKLY_TIERS:
        if earned >= need:
            out.append(key)   # only one weekly tier at a time
            break

    ordered = [b for b in BADGE_ORDER if b in out]
    return ordered + [b for b in out if b not in BADGE_ORDER]


def all_badges(profile_badges, system_badges):
    """Purchased badges first (they were chosen), then earned ones. De-duped."""
    seen = []
    for b in list(profile_badges or []) + list(system_badges or []):
        key = b.replace("badge-", "")
        if key == "supporter":
            key = "trusted"      # renamed: "Supporter" read too donation-y
        if key not in seen:
            seen.append(key)
    return seen


def attach_badges_to_messages(msgs):
    """One batched pass so senders' earned badges show up in chat."""
    try:
        senders = {m.get("sender_id") for m in msgs if m.get("sender_id")}
        if not senders:
            return
        rows = sb.table("users").select("id,is_owner,is_admin,created_at,shards,"
                                        "total_messages_sent,shards_gifted_total,"
                                        "shards_earned_this_week").in_("id", list(senders)).execute().data
        by_id = {r["id"]: r for r in rows or []}
        for m in msgs:
            sid = m.get("sender_id")
            row = by_id.get(sid)
            if not row:
                continue
            earned = compute_system_badges(row)
            existing = (m.get("sender") or {}).get("active_badges") or []
            m["sender"]["active_badges"] = all_badges(existing, earned)
    except Exception as e:
        print(f"[attach_badges] {e}")


@app.route("/api/badges/catalog")
@login_required
def badges_catalog():
    """Badge metadata so the client can render labels, icons and tooltips."""
    try:
        custom = []
        try:
            rows = sb.table("custom_badges").select("*").order("created_at").execute().data
            custom = rows or []
        except Exception:
            custom = []
        info = {k: dict(v) for k, v in BADGE_INFO.items()}
        for c in custom:
            info[c.get("name") or c.get("id")] = {
                "label": c.get("name"),
                "icon": c.get("icon") or "🏅",
                "tip": c.get("description") or "",
                "color": c.get("color")
            }
        return jsonify({"badges": info, "order": BADGE_ORDER, "custom": custom})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/badges", methods=["POST"])
@owner_required
def create_custom_badge():
    """Owner-made badge: assignable to users or purchasable in the shop."""
    try:
        data = request.json or {}
        name = (data.get("name") or "").strip()[:24]
        icon = (data.get("icon") or "🏅").strip()[:8]
        color = (data.get("color") or "#00d9ff").strip()[:9]
        description = (data.get("description") or "").strip()[:120]
        purchasable = bool(data.get("purchasable"))
        try:
            shop_price = int(data.get("shop_price") or 0)
        except Exception:
            shop_price = 0
        if not name:
            return jsonify({"error": "Name required"}), 400
        row = safe_insert("custom_badges", {
            "name": name, "icon": icon, "color": color, "description": description,
            "created_by": session["user_id"], "purchasable": purchasable,
            "shop_price": shop_price
        })
        audit("create_badge", "badge", None, name)
        return jsonify({"ok": True, "badge": row})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/badges/<username>/assign", methods=["POST"])
@owner_required
def assign_custom_badge(username):
    try:
        data = request.json or {}
        badge_key = (data.get("badge_key") or "").strip()
        remove = bool(data.get("remove"))
        if not badge_key:
            return jsonify({"error": "badge_key required"}), 400
        target = sb.table("users").select("id,username").eq("username", username.lower()).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404
        uid = target[0]["id"]
        ensure_user_profile(uid)
        prof = sb.table("user_profiles").select("active_badges").eq("user_id", uid).execute().data
        current = list((prof[0].get("active_badges") or []) if prof else [])
        if remove:
            current = [b for b in current if b != badge_key]
        elif badge_key not in current:
            current.append(badge_key)
        safe_update("user_profiles", {"active_badges": current}, ("user_id", uid))
        invalidate_user_cache(uid)
        audit("assign_badge", "user", uid, badge_key)
        return jsonify({"ok": True, "badges": current})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — FRIENDS
# ============================================================
@app.route("/api/friends")
@login_required
def list_friends():
    try:
        uid = session["user_id"]
        incoming, outgoing, friends = [], [], []
        try:
            rows = sb.table("friendships").select(
                "id,status,created_at,accepted_at,requester_id,addressee_id,"
                "requester:requester_id(username,nickname_color,avatar_url),"
                "addressee:addressee_id(username,nickname_color,avatar_url)"
            ).in_("requester_id", [uid]).execute().data
            rows2 = sb.table("friendships").select(
                "id,status,created_at,accepted_at,requester_id,addressee_id,"
                "requester:requester_id(username,nickname_color,avatar_url),"
                "addressee:addressee_id(username,nickname_color,avatar_url)"
            ).in_("addressee_id", [uid]).execute().data
        except Exception:
            rows, rows2 = [], []

        def person(row):
            other_id = row["addressee_id"] if row["requester_id"] == uid else row["requester_id"]
            side = "addressee" if row["requester_id"] == uid else "requester"
            u = row.get(side) or {}
            return {
                "friendship_id": row["id"],
                "status": row.get("status"),
                "since": row.get("accepted_at") or row.get("created_at"),
                "id": other_id,
                "username": u.get("username"),
                "nickname_color": u.get("nickname_color") or "#00d9ff",
                "avatar_url": u.get("avatar_url"),
            }

        for row in (rows or []) + (rows2 or []):
            status = row.get("status")
            if status == "accepted":
                friends.append(person(row))
            elif status == "pending":
                (outgoing if row["requester_id"] == uid else incoming).append(person(row))
        friends.sort(key=lambda f: (f.get("username") or "").lower())
        return jsonify({
            "friends": friends,
            "incoming": incoming,
            "outgoing": outgoing,
            "pending_count": len(incoming)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/request", methods=["POST"])
@login_required
def send_friend_request():
    try:
        uid = session["user_id"]
        username = ((request.json or {}).get("username") or "").strip().lower()
        me = current_user()
        if not me:
            return jsonify({"error": "Session expired"}), 401
        if username == me["username"]:
            return jsonify({"error": "That is you"}), 400
        target = sb.table("users").select("id,username,friend_privacy,is_bot").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "No user with that name"}), 404
        t = target[0]
        if t.get("is_bot"):
            return jsonify({"error": "Bots do not take friend requests"}), 400
        privacy = t.get("friend_privacy") or "approval"
        if privacy == "closed":
            return jsonify({"error": "This user is not accepting friend requests"}), 403

        existing = None
        try:
            a = sb.table("friendships").select("id,status,requester_id").eq("requester_id", uid).eq("addressee_id", t["id"]).execute().data
            b = sb.table("friendships").select("id,status,requester_id").eq("requester_id", t["id"]).eq("addressee_id", uid).execute().data
            existing = (a or b or [None])[0]
        except Exception:
            existing = None
        if existing:
            if existing.get("status") == "accepted":
                return jsonify({"error": "You are already friends"}), 400
            if existing.get("status") == "pending":
                return jsonify({"error": "A request is already pending"}), 400
            sb.table("friendships").delete().eq("id", existing["id"]).execute()

        row = safe_insert("friendships", {
            "requester_id": uid,
            "addressee_id": t["id"],
            "status": "pending"
        })
        if not row or not row.get("id"):
            return jsonify({"error": "Friends needs the v1.2 database migration"}), 503

        if privacy == "open":
            # 'open' means requests are accepted the moment they arrive.
            safe_update("friendships", {"status": "accepted", "accepted_at": now_iso()}, ("id", row["id"]))
            push_notification(uid, "friend", {"username": t["username"], "accepted": True})
            return jsonify({"ok": True, "auto_accepted": True, "friendship_id": row["id"]})

        push_notification(t["id"], "friend_request", {"username": me["username"]})
        return jsonify({"ok": True, "auto_accepted": False, "friendship_id": row["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<fid>/respond", methods=["POST"])
@login_required
def respond_friend_request(fid):
    try:
        uid = session["user_id"]
        accept = bool((request.json or {}).get("accept"))
        rows = sb.table("friendships").select("*").eq("id", fid).eq("addressee_id", uid).execute().data
        if not rows:
            return jsonify({"error": "Request not found"}), 404
        row = rows[0]
        if row.get("status") != "pending":
            return jsonify({"error": "That request is no longer pending"}), 400
        if accept:
            safe_update("friendships", {"status": "accepted", "accepted_at": now_iso()}, ("id", fid))
            push_notification(row["requester_id"], "friend", {
                "username": (current_user() or {}).get("username"),
                "accepted": True
            })
        else:
            # Rejection is silent by design — the sender is told nothing.
            safe_update("friendships", {"status": "rejected", "rejected_at": now_iso()}, ("id", fid))
        return jsonify({"ok": True, "status": "accepted" if accept else "rejected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<fid>", methods=["DELETE"])
@login_required
def remove_friend(fid):
    try:
        uid = session["user_id"]
        try:
            sb.table("friendships").delete().eq("id", fid) \
                .in_("requester_id", [uid]).execute()
            sb.table("friendships").delete().eq("id", fid) \
                .in_("addressee_id", [uid]).execute()
        except Exception:
            return jsonify({"error": "Could not remove"}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/privacy", methods=["POST"])
@login_required
def set_friend_privacy():
    try:
        mode = ((request.json or {}).get("mode") or "").strip()
        if mode not in ("open", "approval", "closed"):
            return jsonify({"error": "Mode must be open, approval or closed"}), 400
        safe_update("users", {"friend_privacy": mode}, ("id", session["user_id"]))
        invalidate_user_cache(session["user_id"])
        return jsonify({"ok": True, "friend_privacy": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — STREAMER MODE
#   Everything is stored in users.streamer_mode (JSONB) and
#   applied by the client. Owner-forced fields cannot be opted
#   out of.
# ============================================================
STREAMER_FIELDS = [
    "enabled", "hide_my_nickname", "hide_my_chats", "blur_all_chats",
    "blur_type", "blur_specific_chats", "hide_balance", "hide_notification_content"
]

DEFAULT_STREAMER = {
    "enabled": False,
    "hide_my_nickname": False,
    "hide_my_chats": False,
    "blur_all_chats": False,
    "blur_type": "blur",            # "blur" | "pixelate"
    "blur_specific_chats": [],
    "hide_balance": False,
    "hide_notification_content": False
}


def merge_streamer(user_mode, global_forces):
    """User settings + owner-forced overrides. Forced wins, always."""
    out = dict(DEFAULT_STREAMER)
    out.update(user_mode or {})
    forced = global_forces or {}
    out["forced"] = forced
    if forced.get("hide_balance") and out.get("enabled"):
        out["hide_balance"] = True
    if forced.get("hide_my_nickname") and out.get("enabled"):
        out["hide_my_nickname"] = True
    if forced.get("blur_all_chats") and out.get("enabled"):
        out["blur_all_chats"] = True
    return out


@app.route("/api/settings/streamer", methods=["POST"])
@login_required
def set_streamer_mode():
    try:
        uid = session["user_id"]
        data = request.json or {}
        current = {}
        try:
            row = sb.table("users").select("streamer_mode").eq("id", uid).execute().data
            current = (row[0].get("streamer_mode") or {}) if row else {}
        except Exception:
            current = {}
        merged = dict(DEFAULT_STREAMER)
        merged.update(current or {})
        for field in STREAMER_FIELDS:
            if field in data:
                merged[field] = data[field]
        merged["blur_type"] = "pixelate" if merged.get("blur_type") == "pixelate" else "blur"
        if isinstance(merged.get("blur_specific_chats"), list):
            merged["blur_specific_chats"] = [str(c) for c in merged["blur_specific_chats"]][:100]
        else:
            merged["blur_specific_chats"] = []
        miss = missing_columns("users", ["streamer_mode"])
        if miss:
            return migration_error(miss)
        if not safe_update("users", {"streamer_mode": merged}, ("id", uid)):
            return jsonify({"error": "Streamer mode could not be saved"}), 500
        invalidate_user_cache(uid)
        return jsonify({"ok": True, "streamer_mode": merged})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — NICKNAME CHANGE (5 per rolling hour)
# ============================================================
NICKNAME_MAX_PER_HOUR = 5


@app.route("/api/settings/username", methods=["POST"])
@login_required
def change_username():
    try:
        uid = session["user_id"]
        me = current_user()
        if not me:
            return jsonify({"error": "Session expired"}), 401
        new_name = ((request.json or {}).get("username") or "").strip().lower()
        miss = missing_columns("users", ["nickname_changes_this_hour",
                                         "nickname_change_window_start"])
        if miss:
            return migration_error(miss)

        if len(new_name) < 3 or len(new_name) > 20:
            return jsonify({"error": "Username must be 3-20 characters"}), 400
        if not new_name.replace("_", "").isalnum():
            return jsonify({"error": "Letters, numbers and underscores only"}), 400
        if new_name == me["username"]:
            return jsonify({"error": "That is already your name"}), 400
        if new_name.startswith("bot_") and not me.get("is_bot"):
            return jsonify({"error": "The bot_ prefix is reserved"}), 400
        if sb.table("users").select("id").eq("username", new_name).execute().data:
            return jsonify({"error": "That name is taken"}), 400

        # Rolling-hour rate limit
        used = me.get("nickname_changes_this_hour") or 0
        window_start = me.get("nickname_change_window_start")
        reset_window = True
        if window_start:
            try:
                ws = datetime.fromisoformat(str(window_start).replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - ws) < timedelta(hours=1):
                    reset_window = False
            except Exception:
                reset_window = True
        if reset_window:
            used = 0
        if used >= NICKNAME_MAX_PER_HOUR:
            return jsonify({
                "error": f"You have changed your name {NICKNAME_MAX_PER_HOUR} times this hour. Try again later.",
                "throttled": True
            }), 429

        old_name = me["username"]
        safe_update("users", {
            "username": new_name,
            "nickname_changes_this_hour": used + 1,
            "nickname_change_window_start": now_iso() if reset_window else window_start
        }, ("id", uid))
        invalidate_user_cache(uid)
        safe_insert("nickname_changes", {
            "user_id": uid,
            "old_username": old_name,
            "new_username": new_name,
            "ip_address": get_ip()
        })
        audit("username_change", "user", uid, f"{old_name} -> {new_name}")
        return jsonify({"ok": True, "username": new_name,
                        "changes_left": max(0, NICKNAME_MAX_PER_HOUR - (used + 1))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — BOTS
#   A bot is a real users row with is_bot=true. It authenticates
#   with a bearer token that we only ever store hashed, and it can
#   only ever talk through the /api/bots/* surface.
# ============================================================
BOT_MAX_PER_USER = 5
BOT_RATE_PER_MINUTE = 60


def hash_token(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def bot_from_request():
    """Resolve a bot from `Authorization: Bearer <token>`. Returns row or None."""
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        rows = sb.table("users").select("*").eq("bot_token_hash", hash_token(token)) \
            .eq("is_bot", True).execute().data
        return rows[0] if rows else None
    except Exception:
        return None


def bot_policy_allows(user):
    policy = get_settings().get("bot_creation_policy") or "purchase_only"
    if user.get("is_owner"):
        return True, "owner"
    if policy == "anyone":
        return True, "anyone"
    if policy == "admin":
        return bool(user.get("is_admin")), policy
    if policy == "owner":
        return False, policy
    # purchase_only: the cores_bot_key purchase is the ticket in
    if "cores_bot_key" in active_perks(user["id"]):
        return True, "purchase"
    return False, policy


@app.route("/api/bots/register", methods=["POST"])
@login_required
def register_bot():
    try:
        uid = session["user_id"]
        me = current_user()
        if not me:
            return jsonify({"error": "Session expired"}), 401
        miss = missing_columns("users", ["is_bot", "bot_token_hash", "bot_owner_id"])
        if miss:
            return migration_error(miss)
        allowed, why = bot_policy_allows(me)
        if not allowed:
            return jsonify({
                "error": "Bot creation is limited. Buy the Bot Key from the Cores shop, "
                         "or ask the owner to open it up."
            }), 403

        try:
            mine = sb.table("users").select("id").eq("bot_owner_id", uid).eq("is_bot", True).execute().data
            limit = BOT_MAX_PER_USER if not me.get("is_owner") else 100
            if len(mine or []) >= limit:
                return jsonify({"error": f"You can have at most {limit} bots"}), 400
        except Exception:
            pass

        username = ((request.json or {}).get("username") or "").strip().lower()
        if not username.startswith("bot_"):
            username = "bot_" + username
        if len(username) < 5 or len(username) > 24:
            return jsonify({"error": "Bot username must be 5-24 characters"}), 400
        if not username.replace("_", "").isalnum():
            return jsonify({"error": "Letters, numbers and underscores only"}), 400
        if sb.table("users").select("id").eq("username", username).execute().data:
            return jsonify({"error": "That bot name is taken"}), 400

        token = secrets.token_hex(32)     # 64 hex chars
        row = safe_insert("users", {
            "username": username,
            "password_hash": bcrypt.hashpw(secrets.token_hex(24).encode(), bcrypt.gensalt()).decode(),
            "is_bot": True,
            "bot_token_hash": hash_token(token),
            "bot_owner_id": uid,
            "bot_is_active": True,
            "can_send_messages": True
        })
        if not row:
            return jsonify({"error": "Could not create the bot"}), 500
        audit("create_bot", "user", row["id"], username)
        return jsonify({
            "ok": True,
            "bot_id": row["id"],
            "username": username,
            "token": token,
            "warning": "This token is shown exactly once. Store it somewhere safe."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bots/my_bots")
@login_required
def my_bots():
    try:
        uid = session["user_id"]
        rows = sb.table("users").select("id,username,bot_is_active,created_at") \
            .eq("bot_owner_id", uid).eq("is_bot", True).execute().data
        return jsonify({"bots": rows or []})
    except Exception:
        return jsonify({"bots": []})


@app.route("/api/bots/me")
def bot_me():
    try:
        bot = bot_from_request()
        if not bot:
            return jsonify({"error": "Invalid token"}), 401
        return jsonify({"bot": {
            "id": bot["id"],
            "username": bot["username"],
            "active": bot.get("bot_is_active", True),
            "owner_id": bot.get("bot_owner_id")
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def bot_rate_limited(bot_id):
    """60 messages/minute/bot. Returns (limited, remaining)."""
    bucket = int(time.time() // 60)
    try:
        existing = sb.table("bot_message_counters").select("*").eq("bot_id", bot_id) \
            .eq("minute_bucket", bucket).execute().data
        if existing:
            count = (existing[0].get("count") or 0) + 1
            safe_update("bot_message_counters", {"count": count},
                        ("bot_id", bot_id), ("minute_bucket", bucket))
            return count > BOT_RATE_PER_MINUTE, max(0, BOT_RATE_PER_MINUTE - count)
        safe_insert("bot_message_counters", {"bot_id": bot_id, "minute_bucket": bucket, "count": 1})
        return False, BOT_RATE_PER_MINUTE - 1
    except Exception:
        return False, BOT_RATE_PER_MINUTE


@app.route("/api/bots/send", methods=["POST"])
def bot_send():
    try:
        bot = bot_from_request()
        if not bot:
            return jsonify({"error": "Invalid token"}), 401
        if not bot.get("bot_is_active", True):
            return jsonify({"error": "This bot is deactivated"}), 403

        limited, remaining = bot_rate_limited(bot["id"])
        if limited:
            return jsonify({
                "error": f"Rate limit: {BOT_RATE_PER_MINUTE} messages per minute",
                "retry_after": 60
            }), 429

        data = request.json or {}
        username = (data.get("username") or "").strip().lower()
        content = (data.get("message") or "").strip()[:4000]
        if not username or not content:
            return jsonify({"error": "username and message are required"}), 400

        target = sb.table("users").select("id,username").eq("username", username).execute().data
        if not target:
            return jsonify({"error": "No user with that name"}), 404

        # Find or create the DM
        cid = None
        try:
            mine = sb.table("conversation_members").select("conversation_id").eq("user_id", bot["id"]).execute().data
            theirs = sb.table("conversation_members").select("conversation_id").eq("user_id", target[0]["id"]).execute().data
            shared = {m["conversation_id"] for m in mine or []} & {m["conversation_id"] for m in theirs or []}
            for shared_id in shared:
                conv = sb.table("conversations").select("id,is_group").eq("id", shared_id).execute().data
                if conv and not conv[0].get("is_group"):
                    cid = shared_id
                    break
        except Exception:
            cid = None
        if not cid:
            conv = sb.table("conversations").insert({
                "is_group": False,
                "created_by": bot["id"],
                "updated_at": now_iso()
            }).execute().data[0]
            cid = conv["id"]
            sb.table("conversation_members").insert([
                {"conversation_id": cid, "user_id": bot["id"]},
                {"conversation_id": cid, "user_id": target[0]["id"]}
            ]).execute()

        msg = safe_insert("messages", {
            "conversation_id": cid,
            "sender_id": bot["id"],
            "content": content,
            "is_anonymous": False,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        })
        sb.table("conversations").update({"updated_at": now_iso()}).eq("id", cid).execute()
        push_notification(target[0]["id"], "bot_message", {
            "from": bot["username"], "conversation_id": cid
        })
        return jsonify({
            "ok": True,
            "conversation_id": cid,
            "message_id": (msg or {}).get("id"),
            "rate_remaining": remaining
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _owned_bot(bot_id):
    """The bot row if the signed-in user owns it."""
    uid = session.get("user_id")
    try:
        rows = sb.table("users").select("*").eq("id", bot_id).eq("is_bot", True).execute().data
        if not rows:
            return None
        bot = rows[0]
        me = get_user_row(uid) or {}
        if me.get("is_owner") or bot.get("bot_owner_id") == uid:
            return bot
        return None
    except Exception:
        return None


@app.route("/api/bots/<bot_id>/regenerate_token", methods=["POST"])
@login_required
def regenerate_bot_token(bot_id):
    try:
        bot = _owned_bot(bot_id)
        if not bot:
            return jsonify({"error": "Bot not found"}), 404
        token = secrets.token_hex(32)
        safe_update("users", {"bot_token_hash": hash_token(token), "bot_is_active": True}, ("id", bot_id))
        audit("bot_token_reset", "user", bot_id, bot["username"])
        return jsonify({"ok": True, "token": token,
                        "warning": "Shown once. The old token no longer works."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bots/<bot_id>/deactivate", methods=["POST"])
@login_required
def deactivate_bot(bot_id):
    try:
        bot = _owned_bot(bot_id)
        if not bot:
            return jsonify({"error": "Bot not found"}), 404
        safe_update("users", {"bot_is_active": False}, ("id", bot_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bots/<bot_id>", methods=["DELETE"])
@login_required
def delete_bot(bot_id):
    try:
        bot = _owned_bot(bot_id)
        if not bot:
            return jsonify({"error": "Bot not found"}), 404
        try:
            sb.table("users").delete().eq("id", bot_id).execute()
        except Exception:
            safe_update("users", {"bot_is_active": False, "bot_token_hash": None}, ("id", bot_id))
        audit("delete_bot", "user", bot_id, bot["username"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/bots")
@admin_required
def admin_list_bots():
    try:
        rows = sb.table("users").select("id,username,bot_is_active,bot_owner_id,created_at,last_seen") \
            .eq("is_bot", True).order("created_at", desc=True).limit(200).execute().data
        owners = {}
        try:
            ids = list({r.get("bot_owner_id") for r in rows or [] if r.get("bot_owner_id")})
            if ids:
                users = sb.table("users").select("id,username").in_("id", ids).execute().data
                owners = {u["id"]: u["username"] for u in users or []}
        except Exception:
            owners = {}
        for r in rows or []:
            r["owner_username"] = owners.get(r.get("bot_owner_id"))
        return jsonify({"bots": rows or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/bots/<bot_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_bot(bot_id):
    try:
        active = bool((request.json or {}).get("active", False))
        safe_update("users", {"bot_is_active": active}, ("id", bot_id), ("is_bot", True))
        audit("bot_toggle", "user", bot_id, "active" if active else "disabled")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — ANTI-CHEAT
#   Client-side detection, server-side bookkeeping. The point is
#   to be funny about it, not to punish curious people: devtools,
#   adblockers and screen readers are explicitly not offences.
# ============================================================
CHEAT_QUIPS = [
    "You cannot hack this lol",
    "Nice try but no",
    "Don't try to hack me :)",
    "Bruh",
    "The devtools tab exists for a reason but this isn't it",
    "That number was never real anyway",
    "Bold of you to assume the important stuff runs in the browser",
    "The messages are encrypted. The Shards are in Postgres. Pick a different hobby.",
    "I built this alone. I know exactly what that button does.",
    "Refreshing the page does not mint Shards, unfortunately",
    "Somewhere, a database just ignored you",
    "You have modified a div. Congratulations. It is still a div.",
    "This is the digital equivalent of drawing a moustache on a poster",
    "Ask nicely instead, it has a better success rate",
    "The server said no, and the server is always right about numbers",
]

CHEAT_LEVELS = {
    1: {"title": "Warning 1 of 5", "body": "We noticed you messing with the app's internals. It does not do anything, but it is on record now."},
    2: {"title": "Warning 2 of 5", "body": "Stop tampering with the app. Nothing you change on your screen exists anywhere else."},
    3: {"title": "Warning 3 of 5", "body": "Continued attempts will result in a ban. This is the friendly part of the conversation."},
    4: {"title": "Warning 4 of 5", "body": "Your account is suspended for 24 hours. Come back tomorrow with better ideas."},
    5: {"title": "Warning 5 of 5", "body": "Goodbye."},
}

SHARDY_EVENTS = ("shards", "currency", "balance", "cores", "shards_display", "wallet")


@app.route("/api/anticheat/report", methods=["POST"])
@login_required
def anticheat_report():
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401

        data = request.json or {}
        event_type = (data.get("event_type") or "unknown").strip()[:60]
        details = str(data.get("details") or "")[:500]

        settings = get_settings()
        if not settings.get("anti_cheat_enabled", True):
            return jsonify({"ok": True, "ignored": True})
        if is_immune(user):
            return jsonify({"ok": True, "ignored": True})
        if missing_columns("users", ["cheat_warnings"]):
            # Cannot count strikes, so do not pretend to enforce anything.
            return jsonify({"ok": True, "ignored": True})

        safe_insert("anticheat_events", {
            "user_id": uid,
            "event_type": event_type,
            "details": details,
            "ip_address": get_ip()
        })

        # Ask-nicely tampering has its own ladder
        if event_type.startswith("ask_nicely"):
            return _ask_nicely_tamper(user)

        # The rare friendly path, for Shards-related tampering only
        shardy = any(s in event_type.lower() for s in SHARDY_EVENTS) or \
            any(s in details.lower() for s in SHARDY_EVENTS)
        chance = float(settings.get("ask_nicely_chance", 0.001) or 0)
        if (shardy and settings.get("ask_nicely_enabled", True)
                and not user.get("ask_nicely_banned") and secrets.randbelow(10 ** 6) < chance * 10 ** 6):
            return jsonify({
                "ok": True,
                "ask_nicely": True,
                "modal": {
                    "title": "You cannot hack Shards",
                    "body": "…but if you ask nicely, I might give you some 😊",
                    "input_label": "Say something nice",
                    "submit_label": "Send it over"
                }
            })

        warnings = (user.get("cheat_warnings") or 0) + 1
        updates = {"cheat_warnings": warnings}
        level = min(warnings, 5)

        if level == 4:
            updates["suspended"] = True
            updates["suspended_until"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            updates["suspension_reason"] = "Tampering with the app (anti-cheat 4/5)"
        if level >= 5:
            try:
                sb.table("bans").upsert({
                    "ip_address": get_ip(),
                    "reason": "Anti-cheat: 5 warnings",
                    "banned_by": None
                }, on_conflict="ip_address").execute()
                invalidate_ban_cache(get_ip())
            except Exception:
                pass

        safe_update("users", updates, ("id", uid))
        invalidate_user_cache(uid)
        invalidate_punish_cache(uid)

        info = CHEAT_LEVELS.get(level, CHEAT_LEVELS[5])
        return jsonify({
            "ok": True,
            "warning_number": warnings,
            "quip": CHEAT_QUIPS[(warnings - 1) % len(CHEAT_QUIPS)],
            "modal": {
                "title": info["title"],
                "body": info["body"],
                "action": "I understand"
            },
            "suspended": level == 4,
            "banned": level >= 5
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _ask_nicely_tamper(user):
    """Three strikes for poking at the ask-nicely odds themselves."""
    uid = user["id"]
    strikes = 0
    try:
        rows = sb.table("anticheat_events").select("id").eq("user_id", uid) \
            .ilike("event_type", "ask_nicely%").execute().data
        strikes = len(rows or [])
    except Exception:
        strikes = 1
    if strikes >= 3:
        try:
            sb.table("bans").upsert({
                "ip_address": get_ip(),
                "reason": "Tampering with the ask-nicely system",
                "banned_by": None
            }, on_conflict="ip_address").execute()
            invalidate_ban_cache(get_ip())
        except Exception:
            pass
        return jsonify({"ok": True, "quip": "That IP is done.", "modal": {
            "title": "Goodbye", "body": "This IP has been banned.", "action": "…"
        }})
    if strikes == 2:
        safe_update("users", {"ask_nicely_banned": True}, ("id", uid))
        invalidate_user_cache(uid)
    return jsonify({
        "ok": True,
        "quip": "Don't try to hack the ask nicely",
        "modal": {
            "title": "Don't try to hack the ask nicely",
            "body": "The odds are not on your machine. Strike "
                    f"{strikes} of 2 before this account loses access to it.",
            "action": "Fair enough"
        }
    })


@app.route("/api/anticheat/ask_nicely", methods=["POST"])
@login_required
def ask_nicely_submit():
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401
        if user.get("ask_nicely_banned"):
            return jsonify({"error": "Ask nicely is disabled for your account"}), 403
        message = ((request.json or {}).get("message") or "").strip()[:1000]
        if len(message) < 5:
            return jsonify({"error": "Say a little more than that"}), 400
        row = safe_insert("ask_nicely_requests", {
            "user_id": uid,
            "message": message,
            "ip_address": get_ip(),
            "status": "pending"
        })
        if row is None:
            return jsonify({"error": "Could not submit"}), 500
        return jsonify({"ok": True, "thanks": "Thanks! Your request has been sent for review."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/anticheat/ask_nicely")
@login_required
def ask_nicely_status():
    """Your requests, newest first. Drives the 'they replied!' modal."""
    try:
        uid = session["user_id"]
        rows = sb.table("ask_nicely_requests").select("*").eq("user_id", uid) \
            .order("created_at", desc=True).limit(10).execute().data
        return jsonify({"requests": rows or []})
    except Exception:
        return jsonify({"requests": []})


@app.route("/api/admin/ask_nicely")
@admin_required
def admin_ask_nicely_list():
    try:
        status = request.args.get("status") or "pending"
        q = sb.table("ask_nicely_requests").select("*,users(username,nickname_color,shards)")
        if status != "all":
            q = q.eq("status", status)
        rows = q.order("created_at", desc=True).limit(100).execute().data
        settings = get_settings()
        me = current_user() or {}
        amounts = [int(x) for x in str(settings.get("admin_ask_grant_amounts") or "20,50,100").split(",") if x.strip().isdigit()]
        return jsonify({
            "requests": rows or [],
            "can_approve": bool(me.get("is_owner")) or bool(settings.get("admins_can_approve_asks")),
            "can_custom_amount": bool(me.get("is_owner")) or bool(settings.get("admins_can_grant_custom_ask")),
            "preset_amounts": amounts,
            "ask_nicely_enabled": settings.get("ask_nicely_enabled", True),
            "ask_nicely_chance": settings.get("ask_nicely_chance", 0.001)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/ask_nicely/<rid>/approve", methods=["POST"])
@admin_required
def admin_ask_nicely_approve(rid):
    try:
        me = current_user()
        settings = get_settings()
        if not me.get("is_owner") and not settings.get("admins_can_approve_asks"):
            return jsonify({"error": "You cannot approve these"}), 403

        data = request.json or {}
        reply = (data.get("reply") or "").strip()[:500]
        try:
            amount = int(data.get("amount") or 0)
        except Exception:
            return jsonify({"error": "Amount must be a number"}), 400
        if amount <= 0:
            return jsonify({"error": "Amount must be positive"}), 400
        if not me.get("is_owner"):
            presets = [int(x) for x in str(settings.get("admin_ask_grant_amounts") or "20,50,100").split(",") if x.strip().isdigit()]
            if presets and amount not in presets and not settings.get("admins_can_grant_custom_ask"):
                return jsonify({"error": f"Admins can only grant {presets}"}), 400

        rows = sb.table("ask_nicely_requests").select("*").eq("id", rid).execute().data
        if not rows:
            return jsonify({"error": "Request not found"}), 404
        req = rows[0]
        if req.get("status") != "pending":
            return jsonify({"error": "Already reviewed"}), 400

        balance = award_shards(req["user_id"], amount, "ask_nicely", "Someone asked nicely", created_by=me["id"])
        safe_update("ask_nicely_requests", {
            "status": "approved",
            "reviewed_by": me["id"],
            "reviewed_at": now_iso(),
            "reply_message": reply,
            "shards_granted": amount
        }, ("id", rid))
        push_notification(req["user_id"], "ask_nicely_reply", {
            "approved": True, "amount": amount, "reply": reply
        })
        audit("ask_nicely_approve", "user", req["user_id"], f"{amount} shards")
        return jsonify({"ok": True, "shards": balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/ask_nicely/<rid>/reject", methods=["POST"])
@admin_required
def admin_ask_nicely_reject(rid):
    try:
        me = current_user()
        reply = ((request.json or {}).get("reply") or "").strip()[:500]
        rows = sb.table("ask_nicely_requests").select("*").eq("id", rid).execute().data
        if not rows:
            return jsonify({"error": "Request not found"}), 404
        req = rows[0]
        safe_update("ask_nicely_requests", {
            "status": "rejected",
            "reviewed_by": me["id"],
            "reviewed_at": now_iso(),
            "reply_message": reply
        }, ("id", rid))
        push_notification(req["user_id"], "ask_nicely_reply", {"approved": False, "reply": reply})
        audit("ask_nicely_reject", "user", req["user_id"], reply[:80])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/anticheat")
@admin_required
def admin_anticheat_events():
    try:
        rows = sb.table("anticheat_events").select("*,users(username)").order("created_at", desc=True) \
            .limit(150).execute().data
        return jsonify({"events": rows or []})
    except Exception:
        return jsonify({"events": []})


# ============================================================
#   v1.2 — ADMIN APPLICATIONS (the free path to admin)
# ============================================================
ADMIN_APP_MIN_AGE_DAYS = 180
ADMIN_APP_MAX_WARNINGS = 3


@app.route("/api/admin_applications", methods=["POST"])
@login_required
def submit_admin_application():
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401
        if user.get("is_admin") or user.get("is_owner"):
            return jsonify({"error": "You already have admin rights"}), 400

        age = account_age_days(user)
        if age < ADMIN_APP_MIN_AGE_DAYS:
            return jsonify({
                "error": f"Your account must be {ADMIN_APP_MIN_AGE_DAYS // 30}+ months old to apply "
                         f"(you are at {age} days)",
                "eligible": False
            }), 403
        total_warnings = (user.get("spam_warnings") or 0) + (user.get("cheat_warnings") or 0)
        if total_warnings >= ADMIN_APP_MAX_WARNINGS:
            return jsonify({"error": "You need fewer than 3 warnings to apply", "eligible": False}), 403

        try:
            pending = sb.table("admin_applications").select("id").eq("user_id", uid).eq("status", "pending").execute().data
            if pending:
                return jsonify({"error": "You already have an application waiting"}), 400
        except Exception:
            pass

        data = request.json or {}
        reason = (data.get("reason") or "").strip()
        availability = (data.get("availability") or "").strip()
        plan = (data.get("what_would_you_do") or "").strip()
        if len(reason) < 100:
            return jsonify({"error": "Your reason needs at least 100 characters"}), 400
        if availability not in ("A few hours/day", "Most days", "Every day"):
            return jsonify({"error": "Pick how often you would be around"}), 400
        if len(plan) < 50:
            return jsonify({"error": "'What would you do' needs at least 50 characters"}), 400

        row = safe_insert("admin_applications", {
            "user_id": uid, "reason": reason, "availability": availability,
            "what_would_you_do": plan, "status": "pending"
        })
        if row is None:
            return jsonify({"error": "Could not submit"}), 500
        return jsonify({"ok": True, "application_id": row.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin_applications/mine")
@login_required
def my_admin_application():
    try:
        uid = session["user_id"]
        user = current_user() or {}
        rows = sb.table("admin_applications").select("*").eq("user_id", uid) \
            .order("created_at", desc=True).limit(5).execute().data
        age = account_age_days(user)
        warnings = (user.get("spam_warnings") or 0) + (user.get("cheat_warnings") or 0)
        return jsonify({
            "applications": rows or [],
            "eligible": age >= ADMIN_APP_MIN_AGE_DAYS and warnings < ADMIN_APP_MAX_WARNINGS,
            "account_age_days": age,
            "warnings": warnings,
            "requirements": {"min_age_days": ADMIN_APP_MIN_AGE_DAYS, "max_warnings": ADMIN_APP_MAX_WARNINGS}
        })
    except Exception:
        return jsonify({"applications": [], "eligible": False})


@app.route("/api/admin/applications")
@admin_required
def admin_list_applications():
    try:
        status = request.args.get("status") or "pending"
        q = sb.table("admin_applications").select("*,users(username,nickname_color,created_at,spam_warnings,cheat_warnings,total_messages_sent,shards)")
        if status != "all":
            q = q.eq("status", status)
        rows = q.order("created_at", desc=True).limit(100).execute().data
        out = []
        for r in rows or []:
            u = r.get("users") or {}
            r["account_age_days"] = account_age_days(u)
            out.append(r)
        return jsonify({"applications": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/applications/<aid>/approve", methods=["POST"])
@owner_required
def admin_approve_application(aid):
    try:
        me = current_user()
        rows = sb.table("admin_applications").select("*").eq("id", aid).execute().data
        if not rows:
            return jsonify({"error": "Application not found"}), 404
        app_row = rows[0]
        safe_update("users", {"is_admin": True}, ("id", app_row["user_id"]))
        invalidate_user_cache(app_row["user_id"])
        safe_update("admin_applications", {
            "status": "approved", "reviewed_by": me["id"], "reviewed_at": now_iso()
        }, ("id", aid))
        # Make sure a permissions row exists so the admin panel can toggle them
        try:
            existing = sb.table("admin_permissions").select("user_id").eq("user_id", app_row["user_id"]).execute().data
            if not existing:
                sb.table("admin_permissions").insert({
                    "user_id": app_row["user_id"], "granted_by": me["id"]
                }).execute()
        except Exception:
            pass
        push_notification(app_row["user_id"], "admin", {"approved": True})
        audit("approve_admin_application", "user", app_row["user_id"], aid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/applications/<aid>/reject", methods=["POST"])
@owner_required
def admin_reject_application(aid):
    try:
        me = current_user()
        reason = ((request.json or {}).get("reason") or "").strip()[:500]
        rows = sb.table("admin_applications").select("*").eq("id", aid).execute().data
        if not rows:
            return jsonify({"error": "Application not found"}), 404
        safe_update("admin_applications", {
            "status": "rejected", "reviewed_by": me["id"], "reviewed_at": now_iso(),
            "rejection_reason": reason
        }, ("id", aid))
        push_notification(rows[0]["user_id"], "admin", {"approved": False, "reason": reason})
        audit("reject_admin_application", "user", rows[0]["user_id"], reason[:80])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/purchased_admins")
@owner_required
def purchased_admins():
    """Admins who bought the role. The owner can revoke any of them instantly."""
    try:
        rows = sb.table("users").select("id,username,nickname_color,admin_purchased_at,created_at,last_seen") \
            .eq("admin_via_purchase", True).execute().data
        return jsonify({"admins": rows or []})
    except Exception:
        return jsonify({"admins": []})


@app.route("/api/admin/revoke_admin/<uid>", methods=["POST"])
@owner_required
def revoke_admin(uid):
    try:
        target = sb.table("users").select("id,username,is_owner").eq("id", uid).execute().data
        if not target:
            return jsonify({"error": "User not found"}), 404
        if target[0].get("is_owner"):
            return jsonify({"error": "The owner cannot be revoked"}), 400
        safe_update("users", {"is_admin": False, "admin_via_purchase": False, "admin_purchased_at": None}, ("id", uid))
        invalidate_user_cache(uid)
        audit("revoke_admin", "user", uid, target[0]["username"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — SPOTLIGHTS
# ============================================================
SPOTLIGHT_BLOCKLIST = [
    "fuck", "shit", "bitch", "cunt", "nigger", "faggot", "retard", "kike",
    "tranny", "whore", "rape", "kill yourself", "kys"
]


def contains_blocked(text):
    low = (text or "").lower()
    return any(word in low for word in SPOTLIGHT_BLOCKLIST)


@app.route("/api/spotlight", methods=["POST"])
@login_required
def buy_spotlight():
    try:
        uid = session["user_id"]
        data = request.json or {}
        item_key = (data.get("item_key") or "cores_spotlight_basic").strip()
        if item_key not in ("cores_spotlight_basic", "cores_spotlight_custom"):
            return jsonify({"error": "Unknown spotlight tier"}), 400
        message = (data.get("message") or "").strip()[:100]

        # Must own the coupon; using it consumes it.
        try:
            owned = sb.table("user_purchases").select("id,shop_items!inner(item_key)") \
                .eq("user_id", uid).execute().data
        except Exception:
            owned = []
        purchase = None
        for p in owned or []:
            if (p.get("shop_items") or {}).get("item_key") == item_key:
                purchase = p
                break
        if not purchase:
            return jsonify({"error": "Buy the spotlight in the Cores shop first"}), 403

        if item_key == "cores_spotlight_custom":
            if len(message) < 3:
                return jsonify({"error": "Write a short message for the banner"}), 400
            if contains_blocked(message):
                return jsonify({"error": "That message is not allowed on the banner"}), 400
        else:
            message = ""

        row = safe_insert("spotlights", {
            "user_id": uid,
            "message": message,
            "tier": "custom" if item_key.endswith("custom") else "basic",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        })
        if row is None:
            return jsonify({"error": "Could not start the spotlight"}), 500
        try:
            sb.table("user_purchases").delete().eq("id", purchase["id"]).execute()
            invalidate_perks_cache(uid)
        except Exception:
            pass
        audit("spotlight", "user", uid, item_key)
        return jsonify({"ok": True, "expires_at": row.get("expires_at")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/spotlight")
@login_required
def current_spotlight():
    """The one active spotlight, most recent wins. Expired rows are cleaned up."""
    try:
        now = now_iso()
        try:
            sb.table("spotlights").delete().lt("expires_at", now).execute()
        except Exception:
            pass
        rows = sb.table("spotlights").select("*,users(username,nickname_color,avatar_url)") \
            .gt("expires_at", now).order("created_at", desc=True).limit(1).execute().data
        if not rows:
            return jsonify({"spotlight": None})
        row = rows[0]
        u = row.get("users") or {}
        return jsonify({"spotlight": {
            "id": row.get("id"),
            "username": u.get("username"),
            "nickname_color": u.get("nickname_color") or "#00d9ff",
            "message": row.get("message") or "",
            "tier": row.get("tier"),
            "expires_at": row.get("expires_at")
        }})
    except Exception:
        return jsonify({"spotlight": None})


# ============================================================
#   v1.2 — CIPHER HYPERCRYPT KEY MATERIAL
#
#   The server is a locker, not a reader. It stores:
#     • each user's PUBLIC identity key (plus a password-encrypted
#       backup of the private half, which the server cannot open)
#     • per-member wraps of each conversation key
#     • one wrap of each conversation key for the master key
#   Message plaintext is produced only inside a browser that holds
#   the matching private key. Render, Postgres and anything sitting
#   on the wire in between only ever see ciphertext.
# ============================================================
def _valid_b64(value, max_len=8192):
    if not isinstance(value, str) or not value or len(value) > max_len:
        return False
    try:
        base64.b64decode(value, validate=True)
        return True
    except Exception:
        return False


@app.route("/api/keys/register", methods=["POST"])
@login_required
def register_identity_key():
    """Publish this device's identity key + store its encrypted backup."""
    try:
        uid = session["user_id"]
        data = request.json or {}
        public_key = (data.get("public_key") or "").strip()
        curve = (data.get("curve") or "P-256").strip()[:16]
        backup = (data.get("encrypted_backup") or "").strip()
        salt = (data.get("backup_salt") or "").strip()
        iv = (data.get("backup_iv") or "").strip()
        try:
            iters = int(data.get("backup_iters") or 210000)
        except Exception:
            iters = 210000
        if not _valid_b64(public_key, 1024):
            return jsonify({"error": "public_key must be base64"}), 400
        if not _valid_b64(backup, 16384):
            return jsonify({"error": "encrypted_backup must be base64"}), 400
        if not _valid_b64(salt, 256):
            return jsonify({"error": "backup_salt must be base64"}), 400
        if not _valid_b64(iv, 256):
            return jsonify({"error": "backup_iv must be base64"}), 400
        if iters < 100000:
            return jsonify({"error": "backup_iters must be at least 100000"}), 400

        fingerprint = hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:16]
        payload = {
            "user_id": uid,
            "public_key": public_key,
            "curve": curve,
            "encrypted_backup": backup,
            "backup_salt": salt,
            "backup_iv": iv,
            "backup_iters": iters,
            "key_fingerprint": fingerprint,
            "updated_at": now_iso()
        }
        try:
            existing = sb.table("user_keys").select("user_id").eq("user_id", uid).execute().data
            if existing:
                if not safe_update("user_keys", payload, ("user_id", uid)):
                    raise RuntimeError("update failed")
            else:
                payload["created_at"] = now_iso()
                if safe_insert("user_keys", payload) is None:
                    raise RuntimeError("insert failed")
        except Exception as e:
            return jsonify({"error": f"Could not store the key: {e}"}), 500

        return jsonify({"ok": True, "key_fingerprint": fingerprint})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/mine")
@login_required
def my_identity_key():
    try:
        uid = session["user_id"]
        rows = sb.table("user_keys").select("public_key,curve,encrypted_backup,backup_salt,"
                                            "backup_iv,backup_iters,key_fingerprint,updated_at").eq("user_id", uid).execute().data
        return jsonify({"key": rows[0] if rows else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/backup", methods=["POST"])
@login_required
def update_key_backup():
    """Re-wrap the private key after a password change. Server sees only ciphertext."""
    try:
        uid = session["user_id"]
        data = request.json or {}
        backup = (data.get("encrypted_backup") or "").strip()
        salt = (data.get("backup_salt") or "").strip()
        iv = (data.get("backup_iv") or "").strip()
        try:
            iters = int(data.get("backup_iters") or 210000)
        except Exception:
            iters = 210000
        if not _valid_b64(backup, 16384) or not _valid_b64(salt, 256):
            return jsonify({"error": "Invalid backup payload"}), 400
        ok = safe_update("user_keys", {
            "encrypted_backup": backup,
            "backup_salt": salt,
            "backup_iv": iv,
            "backup_iters": iters,
            "updated_at": now_iso()
        }, ("user_id", uid))
        if not ok:
            return jsonify({"error": "No identity key to update — register one first"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/<username>")
@login_required
def public_identity_key(username):
    """Anyone can fetch a public key — that is the whole point of it being public."""
    try:
        rows = sb.table("users").select("id,username").eq("username", (username or "").strip().lower()).execute().data
        if not rows:
            return jsonify({"error": "User not found"}), 404
        keys = sb.table("user_keys").select("user_id,public_key,curve,key_fingerprint") \
            .eq("user_id", rows[0]["id"]).execute().data
        if not keys:
            return jsonify({"error": "That user has not set up encryption yet"}), 404
        return jsonify({"key": keys[0], "username": rows[0]["username"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/master")
@login_required
def master_public_key():
    """The master public key. Conversation keys get wrapped to it on creation."""
    try:
        rows = sb.table("master_key_meta").select("public_key,curve,key_fingerprint,created_at") \
            .order("created_at", desc=True).limit(1).execute().data
        return jsonify({"key": rows[0] if rows else None})
    except Exception:
        return jsonify({"key": None})


@app.route("/api/keys/master", methods=["POST"])
@owner_required
def register_master_key():
    """
    Publish the operator master public key. The encrypted copy of the private
    half rides along so the operator can recover it from any device with their
    password — the server still cannot open it.
    """
    try:
        data = request.json or {}
        public_key = (data.get("public_key") or "").strip()
        curve = (data.get("curve") or "P-256").strip()[:16]
        backup = (data.get("encrypted_backup") or "").strip()
        salt = (data.get("backup_salt") or "").strip()
        iv = (data.get("backup_iv") or "").strip()
        try:
            iters = int(data.get("backup_iters") or 210000)
        except Exception:
            iters = 210000
        if not _valid_b64(public_key, 1024):
            return jsonify({"error": "public_key must be base64"}), 400
        fingerprint = hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:16]
        payload = {
            "public_key": public_key,
            "curve": curve,
            "key_fingerprint": fingerprint,
            "created_at": now_iso()
        }
        if backup and _valid_b64(backup, 16384):
            payload["encrypted_backup"] = backup
            payload["backup_salt"] = salt
            payload["backup_iv"] = iv
            payload["backup_iters"] = iters
        safe_insert("master_key_meta", payload)
        audit("master_key_registered", "system", None, fingerprint)
        return jsonify({"ok": True, "key_fingerprint": fingerprint})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/master/mine")
@owner_required
def master_key_backup():
    """Owner-only: the encrypted master private key, for this device to open."""
    try:
        rows = sb.table("master_key_meta").select("public_key,curve,key_fingerprint,"
                                                  "encrypted_backup,backup_salt,backup_iv,backup_iters") \
            .order("created_at", desc=True).limit(1).execute().data
        return jsonify({"key": rows[0] if rows else None})
    except Exception:
        return jsonify({"key": None})


@app.route("/api/conversations/<cid>/keys", methods=["POST"])
@login_required
def store_conversation_key(cid):
    """
    Store the wrapped conversation key for one or more members, plus the
    master-key wrap. Called by whoever created the conversation (and by any
    member when someone new joins, since they can unwrap and re-wrap it).
    """
    try:
        uid = session["user_id"]
        member = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not member:
            return jsonify({"error": "Not a member of this conversation"}), 403

        data = request.json or {}
        wraps = data.get("wraps") or []
        master_wrap = (data.get("master_wrap") or "").strip()
        fingerprint = (data.get("key_fingerprint") or "").strip()[:32]
        if not wraps and not master_wrap:
            return jsonify({"error": "Nothing to store"}), 400

        member_ids = {m["user_id"] for m in sb.table("conversation_members")
                      .select("user_id").eq("conversation_id", cid).execute().data}
        stored = 0
        for w in wraps[:64]:
            target = w.get("user_id")
            blob = (w.get("wrapped_key") or "").strip()
            if target not in member_ids or not _valid_b64(blob, 8192):
                continue
            if safe_insert("conversation_keys", {
                "conversation_id": cid,
                "user_id": target,
                "wrapped_key": blob,
                "wrapped_by": uid,
                "key_fingerprint": fingerprint
            }):
                stored += 1

        if master_wrap and _valid_b64(master_wrap, 8192):
            safe_insert("conversation_master_keys", {
                "conversation_id": cid,
                "wrapped_key": master_wrap,
                "wrapped_for": "master",
                "created_at": now_iso()
            })
        return jsonify({"ok": True, "stored": stored})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<cid>/keys")
@login_required
def my_conversation_key(cid):
    """My wraps for this conversation (there can be more than one over time)."""
    try:
        uid = session["user_id"]
        member = sb.table("conversation_members").select("id").eq("conversation_id", cid).eq("user_id", uid).execute().data
        if not member:
            return jsonify({"error": "Not a member of this conversation"}), 403
        rows = sb.table("conversation_keys").select("wrapped_key,key_fingerprint,wrapped_by,created_at") \
            .eq("conversation_id", cid).eq("user_id", uid).order("created_at", desc=True).execute().data
        return jsonify({"wraps": rows or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/bulk")
@login_required
def bulk_conversation_keys():
    """
    Every conversation key this account can open, in one round trip.
    The client calls this once at boot instead of per conversation.
    """
    try:
        uid = session["user_id"]
        mem = sb.table("conversation_members").select("conversation_id").eq("user_id", uid).execute().data
        conv_ids = [m["conversation_id"] for m in mem or []]
        if not conv_ids:
            return jsonify({"keys": {}})
        rows = sb.table("conversation_keys").select("conversation_id,user_id,wrapped_key,key_fingerprint,created_at") \
            .eq("user_id", uid).in_("conversation_id", conv_ids).execute().data
        out = {}
        for r in rows or []:
            cid = r["conversation_id"]
            # newest wrap wins
            if cid not in out or (r.get("created_at") or "") > (out[cid].get("created_at") or ""):
                out[cid] = r
        return jsonify({"keys": out})
    except Exception:
        return jsonify({"keys": {}})


@app.route("/api/admin/keys/escrow/<cid>")
@owner_required
def escrow_key(cid):
    """Owner-only retrieval of the master-wrapped conversation key."""
    try:
        rows = sb.table("conversation_master_keys").select("wrapped_key,created_at") \
            .eq("conversation_id", cid).order("created_at", desc=True).limit(1).execute().data
        if not rows:
            return jsonify({"error": "No escrow wrap for this conversation"}), 404
        me = current_user() or {}
        audit("escrow_key_access", "conversation", cid, me.get("username"))
        return jsonify({"wrap": rows[0]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#   v1.2 — WHAT CIPHER ACTUALLY IS (the Features tab)
# ============================================================
@app.route("/api/features")
def features():
    """Static content for the "How Cipher works" screen. No auth needed."""
    return jsonify({
        "version": APP_VERSION,
        "encryption": {
            "name": "Cipher HyperCrypt",
            "by": "Step",
            "tagline": "Sender–receiver keys, sealed in your browser.",
            "points": [
                "Every account gets its own key pair. The private half never leaves your device in the clear.",
                "Each conversation has its own key, wrapped separately for every member — a self-enveloping seal.",
                "Messages, and the images inside them, are encrypted before they leave your browser.",
                "The hosting provider, the database and anything watching the connection see ciphertext only.",
                "Losing your password means losing your key. That is the trade-off, and it is the honest one."
            ]
        },
        "made_by": "Built purely by Step. And too much CYAN.",
        "notes": [
            "Free forever. No ads, no investors, no growth targets.",
            "Messages expire on their own. Nothing stays forever unless you ask it to.",
            "Read the Terms — they explain what is stored and for how long."
        ]
    })


def tos_sections():
    """Structured ToS, for clients that want to render it section by section."""
    settings = get_settings()
    return [
        {"title": "1. What this is",
         "body": "Cipher is a private messaging service built and run by one person. It is free. There is no advertising and no sale of data."},
        {"title": "2. Your account",
         "body": "You are responsible for your password and your recovery phrase. Cipher cannot reset your encryption keys, because it does not have them."},
        {"title": "3. What is stored",
         "body": "Account credentials (hashed), your public encryption key, an encrypted backup of your private key, message envelopes (ciphertext), timestamps, and the metadata needed to deliver messages: who is in a conversation, and when a message was sent."},
        {"title": "4. Message retention",
         "body": f"Messages are deleted automatically after {settings.get('default_retention_days', 30) or 30} days unless retention is extended. Deleted means deleted."},
        {"title": "5. Lawful access",
         "body": "Cipher operates under the laws of the jurisdiction it is hosted and operated in. Where those laws require it, the operator retains the technical ability to access stored communications in response to a lawful request from a competent authority. Conversation keys are additionally sealed to an operator-held master key for that purpose. Access is logged. If this is not acceptable to you, do not use this service."},
        {"title": "6. Conduct",
         "body": "No illegal content, no harassment, no spam. Warnings escalate from a nudge to a ban."},
        {"title": "7. No warranty",
         "body": "The service is provided as-is. One person runs it. Outages happen."},
        {"title": "8. Currency",
         "body": "Shards and Cores have no monetary value, cannot be exchanged for money, and are not a promise of anything."},
        {"title": "9. Changes",
         "body": "These terms can change. Big changes get an announcement banner."}
    ]


# ============================================================
#   v1.2 — THE SORRY BUTTON
#   Warnings 1-3 can be undone by apologising. Three apologies per
#   rolling week. Auto-bans (4+) cannot be apologised out of.
# ============================================================
@app.route("/api/sorry", methods=["POST"])
@login_required
def say_sorry():
    try:
        uid = session["user_id"]
        user = current_user()
        if not user:
            return jsonify({"error": "Session expired"}), 401
        if not get_settings().get("sorry_button_enabled", True):
            return jsonify({"error": "The Sorry button is turned off right now"}), 403
        miss = missing_columns("users", ["sorry_uses_this_week", "sorry_week_start"])
        if miss:
            return migration_error(miss)

        warnings = user.get("spam_warnings") or 0
        if warnings <= 0:
            return jsonify({"error": "You have no warning to undo"}), 400
        if warnings >= 4:
            return jsonify({"error": "That one is past apologising for"}), 403

        # Rolling weekly window
        used = user.get("sorry_uses_this_week") or 0
        week_start = user.get("sorry_week_start")
        reset = True
        if week_start:
            try:
                ws = datetime.fromisoformat(str(week_start).replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ws
                if delta < timedelta(days=7):
                    reset = False
                    if used >= _sorry_uses_left(user)["limit"]:
                        left = timedelta(days=7) - delta
                        return jsonify({
                            "error": f"You've used all {_sorry_uses_left(user)['limit']} Sorry uses this week. "
                                     f"Available again in {left.days} day(s).",
                            "retry_in_seconds": int(left.total_seconds())
                        }), 429
            except Exception:
                reset = True
        if reset:
            used = 0

        updates = {
            "spam_warnings": warnings - 1,
            "sorry_uses_this_week": used + 1
        }
        if reset or not week_start:
            updates["sorry_week_start"] = now_iso()
        # Lifting a throttle is the whole point of apologising
        if (user.get("throttle_level") or 0) in (2, 3):
            updates["throttle_level"] = 1
            updates["throttle_until"] = None
        safe_update("users", updates, ("id", uid))
        invalidate_user_cache(uid)
        safe_insert("sorry_uses", {"user_id": uid, "warning_reverted": warnings})
        remaining = _sorry_uses_left({**user, **updates})["available"]
        return jsonify({
            "ok": True,
            "spam_warnings": warnings - 1,
            "sorry_uses_available": max(0, remaining),
            "message": "Apology accepted. Try to keep it down."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sorry")
@login_required
def sorry_status():
    """Is there a warning to undo, and how many apologies are left?"""
    try:
        user = current_user()
        if not user:
            return jsonify({"can_use": False})
        left = _sorry_uses_left(user)
        warnings = user.get("spam_warnings") or 0
        return jsonify({
            "can_use": bool(0 < warnings < 4) and left["available"] > 0
                       and get_settings().get("sorry_button_enabled", True),
            "spam_warnings": warnings,
            "uses_available": left["available"],
            "uses_limit": left["limit"],
            "throttled": bool((user.get("throttle_level") or 0) in (2, 3))
        })
    except Exception:
        return jsonify({"can_use": False})


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
        invalidate_ban_cache()
        # Expire duration-based purchases (they stay in DB but frontend checks expires_at)

        # v1.2 housekeeping
        try:
            sb.table("spotlights").delete().lt("expires_at", now).execute()
        except Exception:
            pass
        try:
            old_bucket = int(time.time() // 60) - 5
            sb.table("bot_message_counters").delete().lt("minute_bucket", old_bucket).execute()
        except Exception:
            pass
        try:
            # Longevity bonuses for anyone who has not loaded the app recently.
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
            due = sb.table("users").select("id,last_no_warning_check").eq("is_bot", False) \
                .limit(500).execute().data
            checked = 0
            for row in due or []:
                last = row.get("last_no_warning_check")
                if last and str(last) > cutoff:
                    continue          # already checked in the last ~day
                check_longevity_bonuses(row["id"], force=True)
                checked += 1
                if checked >= 100:    # spread the work across runs
                    break
        except Exception as e:
            print(f"[cleanup longevity] {e}")
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
