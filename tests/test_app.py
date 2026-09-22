"""
End-to-end checks for Cipher, run against the real Flask routes.

Usage:
    .venv/bin/python tests/test_app.py            # v1.2 schema (migrated DB)
    .venv/bin/python tests/test_app.py --v11      # pre-migration schema
    .venv/bin/python tests/test_app.py -k shop    # only checks matching "shop"

Supabase is replaced by tests/fake_supabase.py, so no network is needed, but
every line of app.py that these checks touch is the real production code.
"""

import os
import sys
import json
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
os.environ.setdefault("FLASK_SECRET", "test-secret")

import supabase as _supabase_mod  # noqa: E402
from fake_supabase import FakeClient, seed_defaults, v11_schema, v12_schema  # noqa: E402

SCHEMA_MODE = "v11" if "--v11" in sys.argv else "v12"
FILTER = None
if "-k" in sys.argv:
    FILTER = sys.argv[sys.argv.index("-k") + 1]

_FAKE = FakeClient(schema=v11_schema() if SCHEMA_MODE == "v11" else v12_schema())
seed_defaults(_FAKE)
_supabase_mod.create_client = lambda url, key: _FAKE

import app as cipher  # noqa: E402

cipher.sb = _FAKE
try:
    cipher.scheduler.shutdown(wait=False)
except Exception:
    pass

client = cipher.app.test_client()

PASS, FAIL = [], []
WARNINGS_SEEN = set()


def check(name, fn):
    if FILTER and FILTER.lower() not in name.lower():
        return
    before = len(_FAKE.schema_warnings)
    try:
        fn()
        new = [w for w in _FAKE.schema_warnings[before:] if w not in WARNINGS_SEEN]
        WARNINGS_SEEN.update(new)
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m {name}")
        if new:
            for w in new:
                print(f"       \033[33mschema note:\033[0m {w}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  \033[31mFAIL\033[0m {name}: {e}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  \033[31mERROR\033[0m {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def ok(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


# ---------------------------------------------------------------- session
def login(username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    return r


def jget(r):
    try:
        return r.get_json() or {}
    except Exception:
        return {}


def new_session():
    """A fresh browser session (its own cookie jar)."""
    return cipher.app.test_client()


# ---------------------------------------------------------------- fixtures
OWNER_PW = "ownerpass123"
ALICE_PW = "alicepass123"
BOB_PW = "bobpass123"


def make_user(username, password, owner=False):
    r = client.post("/api/signup", json={
        "username": username,
        "password": password,
        "accepted_tos": True,
    })
    data = jget(r)
    ok(r.status_code == 200, f"signup {username} -> {r.status_code} {data}")
    if owner:
        row = [u for u in _FAKE.data["users"] if u["username"] == username][0]
        row["is_owner"] = True
        row["is_admin"] = True
    return data


def setup_users():
    if any(u["username"] == "owner" for u in _FAKE.data["users"]):
        return
    make_user("owner", OWNER_PW, owner=True)
    make_user("alice", ALICE_PW)
    make_user("bob", BOB_PW)


# ================================================================ CHECKS
def t_health():
    r = client.get("/health")
    eq(r.status_code, 200)
    eq(jget(r).get("status"), "ok")


def t_signup_login_me():
    setup_users()
    s = new_session()
    r = s.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    eq(r.status_code, 200, "login alice")
    me = jget(s.get("/api/me")).get("user")
    ok(me, "/api/me returned no user")
    eq(me["username"], "alice")
    ok("poll_config" in jget(s.get("/api/me")))


def t_login_rejects_bots_and_bad_pw():
    setup_users()
    s = new_session()
    r = s.post("/api/login", json={"username": "alice", "password": "wrong"})
    ok(r.status_code != 200, "wrong password should not log in")


def t_dm_send_receive():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    b = new_session()
    b.post("/api/login", json={"username": "bob", "password": BOB_PW})

    r = a.post("/api/conversations/new_dm", json={"username": "bob"})
    cid = jget(r).get("conversation_id")
    ok(cid, f"new_dm failed: {jget(r)}")

    r = a.post(f"/api/messages/{cid}", json={"content": "hello bob"})
    eq(r.status_code, 200, f"send failed {jget(r)}")

    msgs = jget(b.get(f"/api/messages/{cid}")).get("messages", [])
    ok(len(msgs) >= 1, "bob sees no messages")
    found = any(m.get("content") == "hello bob" for m in msgs)
    ok(found, f"bob did not see the message: {msgs}")

    convs = jget(b.get("/api/conversations")).get("conversations", [])
    ok(any(c["id"] == cid for c in convs), "conversation missing from bob's list")


def t_reactions_reads_typing():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    cid = jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    sent = jget(a.post(f"/api/messages/{cid}", json={"content": "react to me"}))
    mid = sent["message"]["id"]
    eq(jget(a.post(f"/api/messages/{mid}/react", json={"emoji": "🔥"})).get("ok"), True)
    msgs = jget(a.get(f"/api/messages/{cid}"))["messages"]
    target = [m for m in msgs if m["id"] == mid][0]
    ok(any(x["emoji"] == "🔥" for x in target.get("reactions", [])), "reaction missing")
    eq(jget(a.post(f"/api/typing/{cid}")).get("ok"), True)
    ok("typing" in jget(a.get(f"/api/typing/{cid}")))


def t_search_and_export():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    cid = jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    a.post(f"/api/messages/{cid}", json={"content": "zebrafish unique token"})
    res = jget(a.get("/api/search?q=zebrafish"))
    ok("results" in res, "search shape changed")
    r = a.get(f"/api/conversations/{cid}/export")
    eq(r.status_code, 200)


def t_profile_update():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    r = a.post("/api/profile", json={
        "nickname_color": "#ff00aa", "theme_color": "#00ff88", "bio": "hi there",
        "anonymous_mode": True, "banner_color": "#123456",
    })
    eq(r.status_code, 200, f"profile update failed {jget(r)}")
    me = jget(a.get("/api/me"))["user"]
    eq(me["nickname_color"], "#ff00aa")
    eq(me["bio"], "hi there")


def t_shop_flow():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    row = [u for u in _FAKE.data["users"] if u["username"] == "alice"][0]
    row["shards"] = 500
    items = jget(a.get("/api/shop/items")).get("items", [])
    ok(len(items) > 0, "no shop items")
    glow = [i for i in items if i.get("item_key") == "effect_glow"]
    ok(glow, "effect_glow item missing")
    r = a.post(f"/api/shop/buy/{glow[0]['id']}", json={})
    eq(r.status_code, 200, f"buy failed {jget(r)}")
    r = a.post(f"/api/shop/equip/{glow[0]['id']}", json={"equip": True})
    eq(r.status_code, 200, f"equip failed {jget(r)}")
    me = jget(a.get("/api/me"))["user"]
    ok("effect-glow" in (me.get("active_effects") or []), f"effect not active: {me.get('active_effects')}")


def t_leaderboard_and_public_profile():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    users = jget(a.get("/api/leaderboard?sort=shards")).get("users")
    ok(isinstance(users, list), "leaderboard shape")
    p = jget(a.get("/api/profile/bob"))
    eq(p.get("user", {}).get("username"), "bob", f"public profile {p}")


def t_admin_panel():
    setup_users()
    o = new_session()
    o.post("/api/login", json={"username": "owner", "password": OWNER_PW})
    eq(jget(o.get("/api/admin/stats")).get("users", 0) >= 3, True, "admin stats users")
    ok("users" in jget(o.get("/api/admin/users")), "admin users list")
    ok("admins" in jget(o.get("/api/admin/admin_rights")), "admin rights list")
    ok("settings" in jget(o.get("/api/admin/settings")), "admin settings")
    ok("logs" in jget(o.get("/api/admin/audit")), "audit log")
    ok("events" in jget(o.get("/api/admin/spam_events")), "spam events")
    ok("bans" in jget(o.get("/api/admin/bans")), "bans")
    r = o.post("/api/admin/settings", json={"default_retention_days": 45})
    eq(r.status_code, 200, f"settings save {jget(r)}")


def t_admin_rights_visible_to_owner():
    setup_users()
    o = new_session()
    o.post("/api/login", json={"username": "owner", "password": OWNER_PW})
    admins = jget(o.get("/api/admin/admin_rights")).get("admins", [])
    ok(any(a.get("is_owner") for a in admins), "owner missing from admin rights list")


def t_announcements():
    setup_users()
    o = new_session()
    o.post("/api/login", json={"username": "owner", "password": OWNER_PW})
    r = o.post("/api/announcements", json={"title": "T", "content": "C", "priority": "info"})
    eq(r.status_code, 200, f"announcement create {jget(r)}")
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    ok("announcements" in jget(a.get("/api/announcements")))


def t_affiliate_and_shards_history():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    ok("codes" in jget(a.get("/api/affiliate/my_codes")), "affiliate codes shape")
    ok("transactions" in jget(a.get("/api/shards/history")), "shards history shape")


def t_two_factor_roundtrip():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    r = jget(a.post("/api/2fa/setup", json={}))
    ok(r.get("secret"), f"2fa setup {r}")
    import pyotp
    code = pyotp.TOTP(r["secret"]).now()
    r2 = a.post("/api/2fa/enable", json={"code": code})
    eq(r2.status_code, 200, f"2fa enable {jget(r2)}")
    me = jget(a.get("/api/me"))["user"]
    eq(me.get("totp_enabled"), True)
    r3 = a.post("/api/2fa/disable", json={"password": ALICE_PW})
    eq(r3.status_code, 200, f"2fa disable {jget(r3)}")


def t_change_password():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    r = a.post("/api/change_password", json={"old_password": ALICE_PW, "new_password": "newpass123"})
    eq(r.status_code, 200, f"change password {jget(r)}")
    b = new_session()
    eq(b.post("/api/login", json={"username": "alice", "password": "newpass123"}).status_code, 200)
    # restore
    a.post("/api/change_password", json={"old_password": "newpass123", "new_password": ALICE_PW})


def t_group_chat():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    r = jget(a.post("/api/conversations/new_group", json={"name": "Squad", "usernames": ["bob"]}))
    cid = r.get("conversation_id")
    ok(cid, f"new_group {r}")
    convs = jget(a.get("/api/conversations"))["conversations"]
    grp = [c for c in convs if c["id"] == cid][0]
    eq(grp["is_group"], True)
    ok(len(grp["members"]) == 2, f"group members {grp['members']}")
    eq(jget(a.post(f"/api/conversations/{cid}/rename", json={"name": "Squad2"})).get("ok"), True)


def t_mute_leave_extend():
    setup_users()
    a = new_session()
    a.post("/api/login", json={"username": "alice", "password": ALICE_PW})
    cid = jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    eq(jget(a.post(f"/api/conversations/{cid}/mute", json={"muted": True})).get("ok"), True)
    a.post(f"/api/messages/{cid}", json={"content": "keep me"})
    eq(jget(a.post(f"/api/conversations/{cid}/extend_all", json={})).get("ok"), True)


def t_unauthenticated_blocked():
    anon = new_session()
    eq(anon.get("/api/me").status_code, 200)  # /api/me returns user None
    ok(anon.get("/api/conversations").status_code == 401)
    ok(anon.get("/api/admin/stats").status_code in (401, 403))


def t_version_strings():
    r = jget(client.get("/health"))
    eq(r.get("version"), "1.2.0", "health version")


# ================================================================ RUNNER
CHECKS = [
    ("health", t_health),
    ("signup/login/me", t_signup_login_me),
    ("login rejects bad password", t_login_rejects_bots_and_bad_pw),
    ("unauthenticated blocked", t_unauthenticated_blocked),
    ("dm send + receive", t_dm_send_receive),
    ("reactions/reads/typing", t_reactions_reads_typing),
    ("search + export", t_search_and_export),
    ("profile update", t_profile_update),
    ("shop buy + equip effect", t_shop_flow),
    ("leaderboard + public profile", t_leaderboard_and_public_profile),
    ("admin panel endpoints", t_admin_panel),
    ("admin rights includes owner", t_admin_rights_visible_to_owner),
    ("announcements", t_announcements),
    ("affiliate + shards history", t_affiliate_and_shards_history),
    ("2FA setup/enable/disable", t_two_factor_roundtrip),
    ("change password", t_change_password),
    ("group chat create/rename", t_group_chat),
    ("mute + extend_all", t_mute_leave_extend),
    ("version bumped to 1.2.0", t_version_strings),
]

# Extra checks live in tests/test_v12.py and register themselves here.
try:
    import test_v12  # noqa: E402
    test_v12._init(sys.modules[__name__])
    _v12 = [(getattr(getattr(test_v12, _n), "__code__").co_firstlineno, _n)
            for _n in dir(test_v12)
            if _n.startswith("v12_") and callable(getattr(test_v12, _n))]
    for _, _name in sorted(_v12):        # declaration order, not alphabetical
        # Against a pre-migration database, the only v1.2 claim worth testing
        # is that the app degrades cleanly instead of breaking.
        if SCHEMA_MODE == "v11" and not _name.startswith("v12_degrades"):
            continue
        CHECKS.append((_name[4:].replace("_", " "), getattr(test_v12, _name)))
except ImportError as e:
    print(f"(test_v12 not loaded: {e})")


def main():
    print(f"\nCipher checks — schema mode: {SCHEMA_MODE}"
          + (f" — filter: {FILTER}" if FILTER else ""))
    print("-" * 62)
    for name, fn in CHECKS:
        check(name, fn)
    print("-" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, err in FAIL:
            print(f"  • {name}: {err}")
    if WARNINGS_SEEN:
        print("\nSchema notes (columns the live DB may be missing):")
        for w in sorted(WARNINGS_SEEN):
            print(f"  • {w}")
    print(f"\n(fake supabase served {_FAKE.query_count} table queries)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
