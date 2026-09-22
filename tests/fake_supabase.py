"""
In-memory stand-in for the Supabase PostgREST client.

Cipher runs on Render + Supabase, and neither is reachable from a dev sandbox.
This module implements just enough of the query-builder surface that app.py
actually uses (table/select/eq/in_/gt/lt/ilike/order/limit/range/match,
insert/update/delete/upsert, nested embeds, storage) to let the real Flask
routes run end-to-end against a dict-backed database.

It is deliberately strict in two useful ways:

  * `select("id,username")` really does return only those keys, so code that
    reads a column it forgot to select fails here the same way it would
    against PostgREST.
  * Every reference to a column that is not in the declared schema is recorded
    in `SCHEMA_WARNINGS`. That is the exact class of bug behind
    "Could not find the 'name_font' column of 'users'", so the harness can
    print a report of columns the live database may be missing.

Swap the schema with `v11_schema()` / `v12_schema()` to test the app against a
database that has not been migrated yet.
"""

import copy
import re
import uuid
from datetime import datetime, timezone


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
#   SCHEMA
# ============================================================
# Columns that exist in the live database as of v1.1.0 (reconstructed from
# migration_v1.1.sql plus every column app.py reads/writes).
V11_SCHEMA = {
    "users": [
        "id", "username", "password_hash", "recovery_phrase", "is_owner", "is_admin",
        "created_at", "last_seen", "last_ip", "spam_warnings", "throttle_level",
        "throttle_until", "last_warning_at", "can_send_messages", "totp_enabled",
        "totp_secret", "keep_all_forever", "notify_before_delete", "nickname_color",
        "theme_color", "anonymous_mode", "suspended", "suspended_until",
        "suspension_reason", "bubble_color", "name_font", "msg_animation",
        "shards", "leaderboard_opt_out",
    ],
    "admin_settings": [
        "id", "signups_enabled", "invites_enabled", "invite_creation_mode",
        "maintenance_mode", "default_retention_days", "shards_per_referral",
        "affiliate_mode",
    ],
    "user_profiles": [
        "user_id", "bio", "avatar_url", "banner_color", "active_effects",
        "active_badges", "active_bubble_color", "active_nickname_font",
        "active_message_animation",
    ],
    "conversations": [
        "id", "name", "is_group", "created_at", "updated_at", "created_by",
        "keep_forever", "icon_url",
    ],
    "conversation_members": [
        "id", "conversation_id", "user_id", "is_group_admin", "muted", "joined_at",
    ],
    "messages": [
        "id", "conversation_id", "sender_id", "content", "image_url", "created_at",
        "expires_at", "deleted", "is_anonymous", "warning_sent", "edited_at",
    ],
    "message_reactions": ["id", "message_id", "user_id", "emoji", "created_at"],
    "message_reads": ["id", "message_id", "user_id", "read_at"],
    "message_warnings": ["id", "user_id", "conversation_id", "dismissed"],
    "typing_status": ["id", "user_id", "conversation_id", "started_at"],
    "recent_messages": ["id", "user_id", "content_hash", "created_at"],
    "bans": ["id", "ip_address", "reason", "banned_by", "created_at", "expires_at"],
    "user_punishments": [
        "id", "user_id", "punished_by", "type", "reason", "created_at",
        "expires_at", "active",
    ],
    "spam_events": [
        "id", "user_id", "message_count", "trigger_reason", "warning_number",
        "created_at",
    ],
    "audit_log": [
        "id", "admin_id", "admin_username", "action", "target_type", "target_id",
        "details", "ip_address", "created_at",
    ],
    "invite_links": [
        "id", "code", "created_by", "max_uses", "uses", "active", "created_at",
        "expires_at",
    ],
    "announcements": [
        "id", "title", "content", "priority", "created_by", "created_at", "active",
    ],
    "immunity_list": ["id", "username", "added_by", "created_at"],
    "recovery_keys": ["id", "user_id", "key_hash", "created_at"],
    "terms_acceptance": ["user_id", "accepted_at", "version"],
    "affiliate_codes": [
        "id", "user_id", "code", "approved", "pending", "reason", "approved_by",
        "uses", "active", "created_at",
    ],
    "affiliate_uses": [
        "id", "code_id", "new_user_id", "shards_awarded", "created_at", "referrer_id",
    ],
    "shard_transactions": [
        "id", "user_id", "amount", "balance_after", "transaction_type",
        "description", "related_table", "related_id", "created_by", "created_at",
    ],
    "shop_items": [
        "id", "name", "item_key", "category", "price", "description", "icon",
        "effect_key", "enabled", "sort_order",
    ],
    "user_purchases": [
        "id", "user_id", "item_id", "purchased_at", "expires_at", "price_paid",
        "equipped",
    ],
    "admin_permissions": [
        "user_id", "can_view_messages", "can_approve_affiliates",
        "can_create_announcements", "can_ban_ips", "can_suspend_ban_users",
        "can_reset_passwords", "can_manage_shop_items", "can_manage_admins",
        "granted_by", "updated_at",
    ],
    "message_access_log": [
        "id", "viewer_id", "conversation_id", "reason", "ip_address", "created_at",
    ],
}

# Columns/tables added by the v1.2.0 migration.
V12_EXTRA_COLUMNS = {
    "conversation_members": ["last_read_at"],
    "users": [
        "cores", "can_grant_cores", "total_messages_sent", "last_daily_claim",
        "sorry_uses_this_week", "sorry_week_start", "friend_privacy",
        "streamer_mode", "nickname_changes_this_hour", "nickname_change_window_start",
        "tos_bonus_claimed", "cheat_warnings", "ask_nicely_banned", "is_bot",
        "bot_token_hash", "bot_owner_id", "bot_is_active", "admin_via_purchase",
        "admin_purchased_at", "shards_earned_this_week", "week_start",
        "shards_gifted_total", "last_no_warning_check", "no_warnings_since",
    ],
    "admin_settings": [
        "anti_cheat_enabled", "ask_nicely_enabled", "ask_nicely_chance",
        "sorry_button_enabled", "admins_can_grant_shards", "admins_can_grant_cores",
        "admin_core_grant_max", "admins_can_approve_asks", "admin_ask_grant_amounts",
        "admins_can_grant_custom_ask", "global_streamer_forces", "bot_creation_policy",
    ],
    "messages": ["cipher_version"],
    "shop_items": ["currency"],
    "friendships": ["accepted_at", "rejected_at"],
}

V12_EXTRA_TABLES = {
    "user_milestones": ["id", "user_id", "bonus_key", "shards_awarded", "claimed_at"],
    "sorry_uses": ["id", "user_id", "warning_reverted", "used_at"],
    "core_transactions": [
        "id", "user_id", "amount", "balance_after", "transaction_type",
        "description", "granted_by", "related_type", "related_id", "created_at",
        "metadata",
    ],
    "shard_gifts": ["id", "sender_id", "recipient_id", "amount", "message", "created_at"],
    "bot_message_counters": ["id", "bot_id", "minute_bucket", "count"],
    "spotlights": ["id", "user_id", "message", "tier", "expires_at", "created_at"],
    "admin_applications": [
        "id", "user_id", "reason", "availability", "what_would_you_do", "status",
        "reviewed_by", "reviewed_at", "rejection_reason", "created_at",
    ],
    "ask_nicely_requests": [
        "id", "user_id", "message", "ip_address", "status", "reviewed_by",
        "reviewed_at", "reply_message", "shards_granted", "created_at",
    ],
    "anticheat_events": ["id", "user_id", "event_type", "details", "ip_address", "created_at"],
    "nickname_changes": ["id", "user_id", "old_username", "new_username", "ip_address", "created_at"],
    "friendships": [
        "id", "requester_id", "addressee_id", "status", "created_at",
        "accepted_at", "rejected_at",
    ],
    "custom_badges": [
        "id", "name", "icon", "color", "description", "created_by", "created_at",
        "shop_price", "purchasable",
    ],
    "user_badges": ["id", "user_id", "badge_key", "granted_by", "granted_at"],
    "notifications": ["id", "user_id", "kind", "payload", "read", "created_at"],
    # Cipher HyperCrypt (v1.2 E2EE)
    "user_keys": [
        "user_id", "public_key", "curve", "encrypted_backup", "backup_salt",
        "backup_iv", "backup_iters", "key_fingerprint", "created_at", "updated_at",
    ],
    "conversation_keys": [
        "id", "conversation_id", "user_id", "wrapped_key", "wrapped_by",
        "key_fingerprint", "created_at",
    ],
    "conversation_master_keys": [
        "id", "conversation_id", "wrapped_key", "wrapped_for", "created_at",
    ],
    "master_key_meta": [
        "id", "public_key", "curve", "key_fingerprint", "encrypted_backup",
        "backup_salt", "backup_iv", "backup_iters", "created_at",
    ],
}


def v11_schema():
    return {k: list(v) for k, v in V11_SCHEMA.items()}


def v12_schema():
    schema = v11_schema()
    for table, cols in V12_EXTRA_COLUMNS.items():
        schema.setdefault(table, [])
        for c in cols:
            if c not in schema[table]:
                schema[table].append(c)
    for table, cols in V12_EXTRA_TABLES.items():
        schema[table] = list(cols)
    return schema


# Foreign keys used to resolve nested embeds like `users(username)` or
# `users:sender_id(username,nickname_color)`.
FOREIGN_KEYS = {
    ("messages", "users"): ("sender_id", "users", "id"),
    ("conversation_members", "users"): ("user_id", "users", "id"),
    ("conversation_members", "conversations"): ("conversation_id", "conversations", "id"),
    ("message_reactions", "users"): ("user_id", "users", "id"),
    ("message_reads", "users"): ("user_id", "users", "id"),
    ("typing_status", "users"): ("user_id", "users", "id"),
    ("user_purchases", "shop_items"): ("item_id", "shop_items", "id"),
    ("shard_transactions", "users"): ("user_id", "users", "id"),
    ("spam_events", "users"): ("user_id", "users", "id"),
    ("affiliate_codes", "users"): ("user_id", "users", "id"),
    ("affiliate_uses", "users"): ("new_user_id", "users", "id"),
    ("user_punishments", "users"): ("user_id", "users", "id"),
    ("admin_permissions", "users"): ("user_id", "users", "id"),
    ("message_access_log", "viewer"): ("viewer_id", "users", "id"),
    ("message_access_log", "target"): ("target_user_id", "users", "id"),
    ("core_transactions", "users"): ("user_id", "users", "id"),
    ("shard_gifts", "users"): ("sender_id", "users", "id"),
    ("notifications", "users"): ("user_id", "users", "id"),
    ("spotlights", "users"): ("user_id", "users", "id"),
    ("admin_applications", "users"): ("user_id", "users", "id"),
    ("ask_nicely_requests", "users"): ("user_id", "users", "id"),
    ("anticheat_events", "users"): ("user_id", "users", "id"),
    ("user_milestones", "users"): ("user_id", "users", "id"),
    ("user_keys", "users"): ("user_id", "users", "id"),
    ("conversation_keys", "conversations"): ("conversation_id", "conversations", "id"),
    ("users", "user_profiles"): ("id", "user_profiles", "user_id"),
    ("friendships", "requester"): ("requester_id", "users", "id"),
    ("friendships", "addressee"): ("addressee_id", "users", "id"),
    ("user_badges", "users"): ("user_id", "users", "id"),
    ("custom_badges", "users"): ("created_by", "users", "id"),
    ("sorry_uses", "users"): ("user_id", "users", "id"),
    ("nickname_changes", "users"): ("user_id", "users", "id"),
    ("admin_applications", "users"): ("user_id", "users", "id"),
}


class SchemaWarning(Exception):
    pass


class FakeResponse:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count if count is not None else len(data)


class FakeQuery:
    """Chainable builder mirroring supabase-py's SyncRequestBuilder."""

    def __init__(self, db, table, op="select", payload=None, on_conflict=None):
        self.db = db
        self.table = table
        self.op = op
        self.payload = payload
        self.on_conflict = on_conflict
        self.cols = "*"
        self.want_count = False
        self.filters = []
        self.orders = []
        self.limit_n = None
        self.range_slice = None

    # ---- projections -------------------------------------------------
    def select(self, cols="*", count=None):
        self.cols = cols
        if count:
            self.want_count = True
        return self

    # ---- filters -----------------------------------------------------
    def eq(self, col, val):
        return self._f(col, "eq", val)

    def neq(self, col, val):
        return self._f(col, "neq", val)

    def in_(self, col, vals):
        return self._f(col, "in", list(vals or []))

    def gt(self, col, val):
        return self._f(col, "gt", val)

    def gte(self, col, val):
        return self._f(col, "gte", val)

    def lt(self, col, val):
        return self._f(col, "lt", val)

    def lte(self, col, val):
        return self._f(col, "lte", val)

    def ilike(self, col, pattern):
        return self._f(col, "ilike", pattern)

    def like(self, col, pattern):
        return self._f(col, "ilike", pattern)

    def is_(self, col, val):
        return self._f(col, "eq", val)

    def match(self, mapping):
        for k, v in (mapping or {}).items():
            self._f(k, "eq", v)
        return self

    def contains(self, col, val):
        return self._f(col, "contains", val)

    def _f(self, col, op, val):
        self.db.note_column(self.table, col)
        self.filters.append((col, op, val))
        return self

    # ---- shape -------------------------------------------------------
    def order(self, col, desc=False, **kw):
        self.orders.append((col, bool(desc)))
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def range(self, start, end):
        self.range_slice = (start, end)
        return self

    def single(self):
        res = self.execute()
        res.data = res.data[0] if res.data else None
        return res

    def maybe_single(self):
        return self.single()

    # ---- execution ---------------------------------------------------
    def execute(self):
        rows = self.db.rows(self.table)

        if self.op == "insert":
            inserted = self.db.do_insert(self.table, self.payload)
            return FakeResponse(inserted)

        if self.op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                self.db.do_update(self.table, r, self.payload)
            return FakeResponse([copy.deepcopy(r) for r in matched])

        if self.op == "delete":
            matched = [r for r in rows if self._matches(r)]
            for r in list(matched):
                self.db.data[self.table].remove(r)
            return FakeResponse([copy.deepcopy(r) for r in matched])

        if self.op == "upsert":
            return FakeResponse(self.db.do_upsert(self.table, self.payload, self.on_conflict))

        matched = [r for r in rows if self._matches(r)]

        for col, desc in reversed(self.orders):
            matched.sort(key=lambda r: _sort_key(r.get(col)), reverse=desc)

        total = len(matched)
        if self.range_slice:
            start, end = self.range_slice
            matched = matched[start:end + 1]
        if self.limit_n is not None:
            matched = matched[:self.limit_n]

        out = [self._project(copy.deepcopy(r)) for r in matched]
        return FakeResponse(out, count=total if self.want_count else None)

    # ---- internals ---------------------------------------------------
    def _matches(self, row):
        for col, op, val in self.filters:
            actual = row.get(col)
            if op == "eq":
                if isinstance(val, bool) or isinstance(actual, bool):
                    ok = bool(actual) == bool(val)
                else:
                    ok = str(actual) == str(val) if actual is not None else val is None
                if not ok:
                    return False
            elif op == "neq":
                if str(actual) == str(val):
                    return False
            elif op == "in":
                if not val:
                    return False
                if str(actual) not in [str(v) for v in val]:
                    return False
            elif op in ("gt", "gte", "lt", "lte"):
                if actual is None:
                    return False
                a, b = _comparable(actual), _comparable(val)
                if a is None or b is None:
                    return False
                if op == "gt" and not a > b:
                    return False
                if op == "gte" and not a >= b:
                    return False
                if op == "lt" and not a < b:
                    return False
                if op == "lte" and not a <= b:
                    return False
            elif op == "ilike":
                pattern = re.escape(str(val)).replace("\\%", ".*").replace("\\_", ".")
                if not re.search(pattern, str(actual or ""), re.IGNORECASE):
                    return False
            elif op == "contains":
                if val not in (actual or []):
                    return False
        return True

    def _project(self, row):
        was_counting = self.db._counting
        self.db._counting = False
        try:
            return self._project_inner(row)
        finally:
            self.db._counting = was_counting

    def _project_inner(self, row):
        spec = self.cols.strip()
        if spec == "*":
            keep = dict(row)
            embeds = []
        else:
            keep = {}
            embeds = []
            depth = 0
            buf = ""
            for ch in spec:
                if ch == "(":
                    depth += 1
                    buf += ch
                elif ch == ")":
                    depth -= 1
                    buf += ch
                elif ch == "," and depth == 0:
                    embeds.append(buf.strip())
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                embeds.append(buf.strip())

            for token in embeds:
                if "(" in token:
                    continue  # nested relation, resolved below
                if token == "*":
                    keep.update(row)
                    continue
                self.db.note_column(self.table, token)
                if token in row:
                    keep[token] = row[token]
                else:
                    keep[token] = None

        # Resolve `rel(cols)`, `alias:rel(cols)`, `rel!inner(cols)`
        for token in embeds:
            if "(" not in token:
                continue
            head, rest = token.split("(", 1)
            inner = rest.rstrip(")")
            head = head.split("!")[0].strip()
            if ":" in head:
                alias, rel = head.split(":", 1)
            else:
                alias, rel = head, head
            rel_cols = [c.strip() for c in inner.split(",") if c.strip()]
            fk = FOREIGN_KEYS.get((self.table, rel)) or FOREIGN_KEYS.get((self.table, alias))
            if not fk:
                keep[alias] = None
                continue
            local, target_table, target_col = fk
            target = self.db.table_obj(target_table)
            key = row.get(local)
            found = None
            if key is not None:
                for r in target.rows(target_table):
                    if str(r.get(target_col)) == str(key):
                        found = {}
                        for c in rel_cols:
                            if "(" in c:
                                # nested embed one level deeper
                                nhead, nrest = c.split("(", 1)
                                nhead = nhead.split("!")[0].strip()
                                nfk = FOREIGN_KEYS.get((target_table, nhead))
                                if not nfk:
                                    found[nhead] = None
                                    continue
                                nlocal, ntable, ncol = nfk
                                nkey = r.get(nlocal)
                                nested = None
                                if nkey is not None:
                                    for nr in self.db.rows(ntable):
                                        if str(nr.get(ncol)) == str(nkey):
                                            nested = {x.strip(): nr.get(x.strip())
                                                      for x in nrest.rstrip(")").split(",")}
                                            break
                                found[nhead] = nested
                            else:
                                found[c] = r.get(c)
                        break
            keep[alias] = found
        return keep


def _sort_key(v):
    """Sortable key that tolerates NULLs mixed with numbers and strings."""
    c = _comparable(v)
    if c is None:
        return (0, 0.0, "")
    if isinstance(c, (int, float)):
        return (1, float(c), "")
    return (2, 0.0, c)


# Columns whose live Postgres definition carries a DEFAULT. The fake starts
# every row with None for every declared column, so without these the app sees
# NULL where production would see the default.
COLUMN_DEFAULTS = {
    "can_send_messages": True,
    "notify_before_delete": True,
    "keep_all_forever": False,
    "anonymous_mode": False,
    "leaderboard_opt_out": False,
    "totp_enabled": False,
    "suspended": False,
    "is_owner": False,
    "is_admin": False,
    "is_bot": False,
    "bot_is_active": False,
    "deleted": False,
    "is_anonymous": False,
    "warning_sent": False,
    "muted": False,
    "is_group": False,
    "is_group_admin": False,
    "keep_forever": False,
    "enabled": True,
    "active": True,
    "approved": False,
    "pending": False,
    "equipped": False,
    "read": False,
    "signups_enabled": True,
    "invites_enabled": True,
    "maintenance_mode": False,
    "anti_cheat_enabled": True,
    "ask_nicely_enabled": True,
    "sorry_button_enabled": True,
    "tos_bonus_claimed": False,
    "admin_via_purchase": False,
    "can_grant_cores": False,
    "ask_nicely_banned": False,
    "shards": 0,
    "cores": 0,
    "total_messages_sent": 0,
    "spam_warnings": 0,
    "cheat_warnings": 0,
    "throttle_level": 0,
    "sorry_uses_this_week": 0,
    "nickname_changes_this_hour": 0,
    "shards_earned_this_week": 0,
    "shards_gifted_total": 0,
    "uses": 0,
    "count": 0,
    "sort_order": 0,
    "price": 0,
    "active_effects": list,
    "active_badges": list,
    "streamer_mode": dict,
    "global_streamer_forces": dict,
    "friend_privacy": "approval",
    "bot_creation_policy": "purchase_only",
    "admin_ask_grant_amounts": "20,50,100",
    "admin_core_grant_max": 3,
    "ask_nicely_chance": 0.001,
    "default_retention_days": 30,
    "shards_per_referral": 10,
}


def _comparable(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    return str(v)


class FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def select(self, cols="*", count=None):
        return FakeQuery(self.db, self.name).select(cols, count=count)

    def insert(self, payload):
        return FakeQuery(self.db, self.name, op="insert", payload=payload)

    def update(self, payload):
        return FakeQuery(self.db, self.name, op="update", payload=payload)

    def delete(self):
        return FakeQuery(self.db, self.name, op="delete")

    def upsert(self, payload, on_conflict=None, **kw):
        return FakeQuery(self.db, self.name, op="upsert", payload=payload, on_conflict=on_conflict)

    # convenience used by the harness itself
    def rows(self, _name=None):
        return self.db.rows(self.name)


class FakeBucket:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        db.storage_files.setdefault(name, {})

    def upload(self, path, data, options=None):
        self.db.storage_files[self.name][path] = data
        return {"Key": f"{self.name}/{path}"}

    def get_public_url(self, path):
        return f"https://fake.supabase.co/storage/v1/object/public/{self.name}/{path}"

    def download(self, path):
        return self.db.storage_files[self.name].get(path)


class FakeStorage:
    def __init__(self, db):
        self.db = db

    def from_(self, bucket):
        return FakeBucket(self.db, bucket)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class FakePostgREST:
    """Serves the OpenAPI document, like the real PostgREST root endpoint."""

    def __init__(self, db):
        self.db = db

    def get(self, path, headers=None, **kw):
        if (headers or {}).get("Accept") == "application/openapi+json":
            return _FakeResponse({
                "definitions": {t: {c: {} for c in cols} for t, cols in self.db.schema.items()}
            })
        return _FakeResponse({})


class FakeClient:
    def __init__(self, schema=None):
        self.schema = schema if schema is not None else v12_schema()
        self.data = {t: [] for t in self.schema}
        self.storage = FakeStorage(self)
        self.postgrest = FakePostgREST(self)
        self.schema_warnings = []
        self.query_count = 0
        self._counting = True

    # ---- helpers -----------------------------------------------------
    def note_column(self, table, col):
        cols = self.schema.get(table)
        if cols is None:
            self._warn(f"table '{table}' is not in the declared schema")
            self.schema[table] = []
            return
        if col not in cols:
            self._warn(f"column '{col}' not found on '{table}'")

    def _warn(self, msg):
        if msg not in self.schema_warnings:
            self.schema_warnings.append(msg)

    def rows(self, table):
        # Nested-embed resolution walks tables row by row in here; real
        # PostgREST answers the whole embed in one round trip, so those
        # lookups must not be counted as application queries.
        if self._counting:
            self.query_count += 1
        if table not in self.schema:
            self._warn(f"table '{table}' is not in the declared schema")
            self.schema[table] = []
        self.data.setdefault(table, [])
        return self.data[table]

    def table_obj(self, table):
        return FakeTable(self, table)

    def table(self, name):
        return FakeTable(self, name)

    def reset_counts(self):
        self.query_count = 0

    # ---- mutations ---------------------------------------------------
    def do_insert(self, table, payload):
        rows = self.rows(table)
        items = payload if isinstance(payload, list) else [payload]
        out = []
        for item in items:
            row = {c: None for c in self.schema.get(table, [])}
            for k, v in (item or {}).items():
                self.note_column(table, k)
                row[k] = v
            for col, default in COLUMN_DEFAULTS.items():
                if col in self.schema.get(table, []) and row.get(col) is None:
                    row[col] = default() if default in (list, dict) else default
            if not row.get("id") and "id" in self.schema.get(table, []):
                row["id"] = _uuid()
            if "created_at" in self.schema.get(table, []) and row.get("created_at") is None:
                row["created_at"] = _now()
            rows.append(row)
            out.append(copy.deepcopy(row))
        return out

    def do_update(self, table, row, payload):
        for k, v in (payload or {}).items():
            self.note_column(table, k)
            row[k] = v

    def do_upsert(self, table, payload, on_conflict):
        rows = self.rows(table)
        items = payload if isinstance(payload, list) else [payload]
        conflict_cols = [c.strip() for c in (on_conflict or "id").split(",")]
        out = []
        for item in items:
            existing = None
            for r in rows:
                if all(str(r.get(c)) == str(item.get(c)) for c in conflict_cols):
                    existing = r
                    break
            if existing is not None:
                self.do_update(table, existing, item)
                out.append(copy.deepcopy(existing))
            else:
                out.extend(self.do_insert(table, item))
        return out


def seed_defaults(client):
    """Insert the rows app.py assumes always exist (admin_settings id=1, shop)."""
    client.data["admin_settings"].append({
        "id": 1,
        "signups_enabled": True,
        "invites_enabled": True,
        "invite_creation_mode": "everyone",
        "maintenance_mode": False,
        "default_retention_days": 30,
        "shards_per_referral": 10,
        "affiliate_mode": "everyone",
        "anti_cheat_enabled": True,
        "ask_nicely_enabled": True,
        "ask_nicely_chance": 0.001,
        "sorry_button_enabled": True,
        "admins_can_grant_shards": True,
        "admins_can_grant_cores": True,
        "admin_core_grant_max": 3,
        "admins_can_approve_asks": True,
        "admin_ask_grant_amounts": "20,50,100",
        "admins_can_grant_custom_ask": False,
        "global_streamer_forces": {},
        "bot_creation_policy": "purchase_only",
    })
    items = [
        ("profile_picture_upload", "Profile Picture", "profile", 20, "🖼️", None),
        ("profile_bio", "Profile Bio", "profile", 10, "📝", None),
        ("banner_color", "Banner Color", "profile", 15, "🎨", None),
        ("effect_glow", "Glow Effect", "avatar_effects", 30, "✨", "effect-glow"),
        ("effect_sparkle", "Sparkle Effect", "avatar_effects", 40, "🌟", "effect-sparkle"),
        ("effect_pulse", "Pulse Effect", "avatar_effects", 30, "💓", "effect-pulse"),
        ("effect_rainbow", "Rainbow Border", "avatar_effects", 50, "🌈", "effect-rainbow"),
        ("chat_bubble_colors", "Custom Bubble Color", "chat", 25, "🫧", None),
        ("chat_nickname_font", "Nickname Font", "chat", 20, "🔤", None),
        ("chat_send_animation", "Send Animation", "chat", 30, "🎬", None),
        ("badge_vip", "VIP Badge", "badges", 100, "👑", "vip"),
        ("badge_supporter", "Supporter Badge", "badges", 50, "⭐", "supporter"),
        ("perk_retention_30_days", "Extend Retention", "perks", 50, "⏳", None),
        ("perk_upload_10mb_30_days", "Large Uploads", "perks", 40, "📦", None),
        ("perk_extra_sorry", "Extra Sorry", "perks", 40, "🙏", None),
    ]
    for key, name, cat, price, icon, effect in items:
        client.data["shop_items"].append({
            "id": _uuid(), "item_key": key, "name": name, "category": cat,
            "price": price, "description": f"{name} for your profile", "icon": icon,
            "effect_key": effect, "enabled": True, "sort_order": 0, "currency": "shards",
        })
    return client
