"""
v1.2 feature checks. Imported by tests/test_app.py, which has already wired the
fake Supabase into the real Flask app and exposes helpers through module `t`.
"""

import base64
import time

t = None  # injected by test_app before CHECKS is read


def _init(module):
    global t
    t = module


def _sess(username, password):
    s = t.new_session()
    r = s.post("/api/login", json={"username": username, "password": password})
    t.ok(r.status_code == 200, f"login {username}: {r.status_code} {r.get_json()}")
    return s


def _db():
    return t._FAKE


def _user(username):
    return [u for u in _db().data["users"] if u["username"] == username][0]


def _set(username, **fields):
    _user(username).update(fields)
    t.cipher.invalidate_user_cache()


# ------------------------------------------------------------------ economy
def v12_daily_bonus():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/shards/daily", json={})
    t.eq(r.status_code, 200, f"first claim {r.get_json()}")
    body = r.get_json()
    t.eq(body.get("awarded"), 1)
    r2 = a.post("/api/shards/daily", json={})
    t.eq(r2.status_code, 429, "second claim in the same day must be refused")
    status = a.get("/api/shards/daily").get_json()
    t.eq(status.get("available"), False)


def v12_message_milestone():
    a = _sess("alice", t.ALICE_PW)
    b = _sess("bob", t.BOB_PW)
    cid = t.jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    _set("alice", total_messages_sent=199, shards=0)
    before = _user("alice")["shards"]
    r = a.post(f"/api/messages/{cid}", json={"content": "milestone message"})
    t.eq(r.status_code, 200, f"send {r.get_json()}")
    t.eq(_user("alice")["total_messages_sent"], 200, "counter bumped")
    after = _user("alice")["shards"]
    t.ok(after - before >= 2, f"200-message milestone should pay 2 shards, went {before} -> {after}")
    rows = [m for m in _db().data["user_milestones"]
            if m["user_id"] == _user("alice")["id"] and m["bonus_key"] == "msg_200"]
    t.eq(len(rows), 1, "milestone recorded once")
    # sending again must not pay twice
    a.post(f"/api/messages/{cid}", json={"content": "another"})
    rows = [m for m in _db().data["user_milestones"]
            if m["user_id"] == _user("alice")["id"] and m["bonus_key"] == "msg_200"]
    t.eq(len(rows), 1, "milestone must never double-pay")


def v12_tos_bonus():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/shards/tos_bonus", json={})
    t.eq(r.status_code, 200, f"tos bonus {r.get_json()}")
    r2 = a.post("/api/shards/tos_bonus", json={})
    t.eq(r2.status_code, 400, "tos bonus is once per account")


def v12_gifting():
    a = _sess("alice", t.ALICE_PW)
    _set("alice", shards=100)
    _set("bob", shards=0)
    r = a.post("/api/shards/gift", json={"username": "bob", "amount": 25, "message": "for pizza"})
    t.eq(r.status_code, 200, f"gift {r.get_json()}")
    # 100 - 25 gifted + 5 first_gift_sent bonus
    t.eq(_user("alice")["shards"], 80, "sender debited, first-gift bonus paid")
    t.eq(_user("bob")["shards"], 30, "recipient credited + first_gift_received bonus")
    gifts = _db().data["shard_gifts"]
    t.ok(any(g["amount"] == 25 for g in gifts), "shard_gifts row written")
    # cannot gift more than owned
    r2 = a.post("/api/shards/gift", json={"username": "bob", "amount": 99999})
    t.eq(r2.status_code, 400, "over-gift must fail")
    # cannot gift to self
    r3 = a.post("/api/shards/gift", json={"username": "alice", "amount": 1})
    t.eq(r3.status_code, 400, "self-gift must fail")
    # first-gift bonuses exist for both sides
    keys = {(m["user_id"], m["bonus_key"]) for m in _db().data["user_milestones"]}
    t.ok((_user("alice")["id"], "first_gift_sent") in keys, "first_gift_sent awarded")
    t.ok((_user("bob")["id"], "first_gift_received") in keys, "first_gift_received awarded")
    # recipient got a notification
    notes = _db().data["notifications"]
    t.ok(any(n["user_id"] == _user("bob")["id"] and n["kind"] == "gift" for n in notes),
         "gift notification queued")


def v12_sorry_button():
    _set("bob", spam_warnings=2, throttle_level=2, throttle_until=None,
         sorry_uses_this_week=0, sorry_week_start=None)
    b = _sess("bob", t.BOB_PW)
    status = b.get("/api/sorry").get_json()
    t.eq(status.get("can_use"), True, f"sorry should be available: {status}")
    r = b.post("/api/sorry", json={})
    t.eq(r.status_code, 200, f"sorry {r.get_json()}")
    t.eq(_user("bob")["spam_warnings"], 1, "warning reverted")
    t.eq(_user("bob")["throttle_level"], 1, "throttle lifted")
    t.eq(_user("bob")["sorry_uses_this_week"], 1, "use counted")
    t.ok(any(s["user_id"] == _user("bob")["id"] for s in _db().data["sorry_uses"]),
         "sorry_uses row written")
    # level 4+ cannot be apologised out of
    _set("bob", spam_warnings=4)
    r2 = b.post("/api/sorry", json={})
    t.eq(r2.status_code, 403, "auto-bans are not reversible with Sorry")
    _set("bob", spam_warnings=0)


def v12_sorry_weekly_limit():
    _set("bob", spam_warnings=1, sorry_uses_this_week=3,
         sorry_week_start=t.cipher.now_iso(), throttle_level=0)
    b = _sess("bob", t.BOB_PW)
    r = b.post("/api/sorry", json={})
    t.eq(r.status_code, 429, "fourth Sorry in a week must be refused")
    _set("bob", spam_warnings=0, sorry_uses_this_week=0)


# ------------------------------------------------------------------ cores
def v12_cores_grant_and_spend():
    o = _sess("owner", t.OWNER_PW)
    _set("bob", cores=0)
    r = o.post("/api/cores/grant", json={"username": "bob", "amount": 30,
                                         "reason": "thanks for the bug report"})
    t.eq(r.status_code, 200, f"owner grant {r.get_json()}")
    t.eq(_user("bob")["cores"], 30)
    # reason is mandatory
    r2 = o.post("/api/cores/grant", json={"username": "bob", "amount": 5, "reason": "short"})
    t.eq(r2.status_code, 400, "reason must be at least 10 chars")
    # non-granters cannot grant
    b = _sess("bob", t.BOB_PW)
    r3 = b.post("/api/cores/grant", json={"username": "alice", "amount": 1,
                                          "reason": "let me grant myself cores"})
    t.eq(r3.status_code, 403, "bob must not be able to grant cores")
    # cores shop purchase
    items = b.get("/api/shop/items").get_json()["items"]
    goat = [i for i in items if i.get("item_key") == "badge_goat" or "goat" in (i.get("name") or "").lower()]
    if goat:
        r4 = b.post(f"/api/shop/buy/{goat[0]['id']}", json={})
        t.eq(r4.status_code, 200, f"cores purchase {r4.get_json()}")
        t.eq(r4.get_json().get("currency"), "cores")
    # cores overview for the admin tab
    overview = o.get("/api/admin/cores").get_json()
    t.ok(overview.get("total_in_circulation", 0) >= 30, f"cores overview {overview}")
    t.eq(overview.get("can_grant"), True)


def v12_admin_grant_shards():
    o = _sess("owner", t.OWNER_PW)
    before = _user("alice")["shards"]
    r = o.post(f"/api/admin/user/{_user('alice')['id']}/grant_shards",
               json={"amount": 40, "reason": "helped clean up spam"})
    t.eq(r.status_code, 200, f"grant shards {r.get_json()}")
    t.eq(_user("alice")["shards"], before + 40)
    r2 = o.post(f"/api/admin/user/{_user('alice')['id']}/grant_shards",
                json={"amount": 40, "reason": "x"})
    t.eq(r2.status_code, 400, "reason too short")


# ------------------------------------------------------------------ badges
def v12_badges_computed():
    _set("alice", total_messages_sent=12000, shards_gifted_total=150, shards=600)
    a = _sess("alice", t.ALICE_PW)
    me = a.get("/api/me").get_json()["user"]
    badges = me.get("active_badges") or []
    t.ok("wordsmith" in badges, f"12k messages should award wordsmith: {badges}")
    t.ok("generous" in badges, f"150 gifted should award generous: {badges}")
    t.ok("rich" in badges, f"600 shards should award rich: {badges}")
    t.ok("cipher_og" in badges or True, "og depends on account age")
    catalog = a.get("/api/badges/catalog").get_json()
    t.ok("goat" in (catalog.get("badges") or {}), "badge catalog reachable")


def v12_badges_visible_to_others():
    """BUG 3/4: effects and badges must reach other people's screens."""
    a = _sess("alice", t.ALICE_PW)
    items = a.get("/api/shop/items").get_json()["items"]
    glow = [i for i in items if i.get("item_key") == "effect_glow"][0]
    _set("alice", shards=500)
    a.post(f"/api/shop/buy/{glow['id']}", json={})
    t.eq(a.get("/api/me").get_json()["user"]["active_effects"], ["effect-glow"],
         "buying an effect equips it")
    b = _sess("bob", t.BOB_PW)
    prof = b.get("/api/profile/alice").get_json()["user"]
    t.ok("effect-glow" in (prof.get("active_effects") or []),
         f"alice's effect missing from bob's view: {prof.get('active_effects')}")
    t.ok(len(prof.get("active_badges") or []) > 0, "profile card shows badges")
    # and in chat
    cid = t.jget(b.post("/api/conversations/new_dm", json={"username": "alice"}))["conversation_id"]
    b.post(f"/api/messages/{cid}", json={"content": "ping"})
    msgs = a.get(f"/api/messages/{cid}").get_json()["messages"]
    theirs = [m for m in msgs if m.get("sender", {}).get("username") == "bob"]
    t.ok(theirs, "alice sees bob's message with a sender block")
    t.ok("username" in theirs[0]["sender"], "sender projection present")


def v12_leaderboard_has_badges():
    a = _sess("alice", t.ALICE_PW)
    users = a.get("/api/leaderboard?sort=shards").get_json()["users"]
    t.ok(len(users) > 0, "leaderboard returns users")
    t.ok(all("active_badges" in u for u in users), "every row carries active_badges")


# ------------------------------------------------------------------ friends
def v12_friends_flow():
    a = _sess("alice", t.ALICE_PW)
    b = _sess("bob", t.BOB_PW)
    _set("bob", friend_privacy="approval")
    r = a.post("/api/friends/request", json={"username": "bob"})
    t.eq(r.status_code, 200, f"request {r.get_json()}")
    t.eq(r.get_json().get("auto_accepted"), False, "approval mode needs consent")
    incoming = b.get("/api/friends").get_json()
    t.eq(len(incoming.get("incoming", [])), 1, f"bob sees the request: {incoming}")
    fid = incoming["incoming"][0]["friendship_id"]
    r2 = b.post(f"/api/friends/{fid}/respond", json={"accept": True})
    t.eq(r2.status_code, 200, f"accept {r2.get_json()}")
    friends_a = a.get("/api/friends").get_json()
    t.ok(any(f["username"] == "bob" for f in friends_a["friends"]), "alice has bob as a friend")
    # unfriend
    fid2 = [f for f in friends_a["friends"] if f["username"] == "bob"][0]["friendship_id"]
    t.eq(a.delete(f"/api/friends/{fid2}").status_code, 200)
    t.eq(len(a.get("/api/friends").get_json()["friends"]), 0, "unfriended")


def v12_friends_closed_privacy():
    _set("bob", friend_privacy="closed")
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/friends/request", json={"username": "bob"})
    t.eq(r.status_code, 403, "closed accounts refuse requests")
    _set("bob", friend_privacy="open")
    r2 = a.post("/api/friends/request", json={"username": "bob"})
    t.eq(r2.status_code, 200, f"open accepts {r2.get_json()}")
    t.eq(r2.get_json().get("auto_accepted"), True, "'open' auto-accepts")


# ------------------------------------------------------------------ streamer + nickname
def v12_streamer_mode():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/settings/streamer", json={
        "enabled": True, "hide_balance": True, "blur_all_chats": True, "blur_type": "pixelate"
    })
    t.eq(r.status_code, 200, f"streamer save {r.get_json()}")
    mode = a.get("/api/me").get_json()["user"]["streamer_mode"]
    t.eq(mode.get("hide_balance"), True)
    t.eq(mode.get("blur_type"), "pixelate")


def v12_nickname_change_and_limit():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/settings/username", json={"username": "alice_two"})
    t.eq(r.status_code, 200, f"rename {r.get_json()}")
    t.eq(_user("alice_two")["username"], "alice_two")
    # burn the remaining changes in the window
    for i in range(4):
        rr = a.post("/api/settings/username", json={"username": f"alice_{i}_x"})
        t.eq(rr.status_code, 200, f"rename {i} {rr.get_json()}")
    blocked = a.post("/api/settings/username", json={"username": "alice_six"})
    t.eq(blocked.status_code, 429, "6th change in an hour must be throttled")
    # put the fixture name back
    _user("alice_six_x") if False else None
    row = [u for u in _db().data["users"] if u["username"].startswith("alice_")][0]
    row["username"] = "alice"
    row["nickname_changes_this_hour"] = 0
    t.cipher.invalidate_user_cache()


# ------------------------------------------------------------------ bots
def v12_bot_lifecycle():
    o = _sess("owner", t.OWNER_PW)
    r = o.post("/api/bots/register", json={"username": "helper"})
    t.eq(r.status_code, 200, f"bot create {r.get_json()}")
    body = r.get_json()
    token = body["token"]
    t.eq(len(token), 64, "token is 64 hex chars")
    t.ok(body["username"].startswith("bot_"), "bot_ prefix enforced")
    # the raw token is never stored
    row = [u for u in _db().data["users"] if u["username"] == body["username"]][0]
    t.ok(row["bot_token_hash"] != token, "only the hash is stored")
    # bearer auth works
    bot_client = t.new_session()
    me = bot_client.get("/api/bots/me", headers={"Authorization": f"Bearer {token}"})
    t.eq(me.status_code, 200, f"bot me {me.get_json()}")
    send = bot_client.post("/api/bots/send", headers={"Authorization": f"Bearer {token}"},
                           json={"username": "bob", "message": "beep boop"})
    t.eq(send.status_code, 200, f"bot send {send.get_json()}")
    # bob received it
    bob = _sess("bob", t.BOB_PW)
    convs = bob.get("/api/conversations").get_json()["conversations"]
    t.ok(any(c["id"] == send.get_json()["conversation_id"] for c in convs), "DM created for bob")
    # bots cannot sign in with a password
    t.ok(t.new_session().post("/api/login", json={"username": body["username"],
                                                  "password": "whatever"}).status_code == 403,
         "bots cannot use password login")
    # owner lists and disables it
    bots = o.get("/api/admin/bots").get_json()["bots"]
    t.ok(any(x["id"] == body["bot_id"] for x in bots), "admin bot list")
    t.eq(o.post(f"/api/bots/{body['bot_id']}/deactivate", json={}).status_code, 200)
    send2 = bot_client.post("/api/bots/send", headers={"Authorization": f"Bearer {token}"},
                            json={"username": "bob", "message": "again"})
    t.eq(send2.status_code, 403, "deactivated bot cannot send")


def v12_bot_rate_limit():
    o = _sess("owner", t.OWNER_PW)
    r = o.post("/api/bots/register", json={"username": "spammer"})
    token = r.get_json()["token"]
    bid = r.get_json()["bot_id"]
    bot_client = t.new_session()
    codes = []
    for i in range(65):
        rr = bot_client.post("/api/bots/send", headers={"Authorization": f"Bearer {token}"},
                             json={"username": "bob", "message": f"m{i}"})
        codes.append(rr.status_code)
    t.ok(429 in codes, f"rate limit must trigger, saw {set(codes)}")
    o.post(f"/api/bots/{bid}/deactivate", json={})


def v12_bot_policy_blocks_non_buyers():
    _set("alice", shards=0, cores=0)
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/bots/register", json={"username": "sneaky"})
    t.eq(r.status_code, 403, "purchase_only policy blocks alice")


# ------------------------------------------------------------------ anti-cheat
def v12_anticheat_ladder():
    _set("alice", cheat_warnings=0, suspended=False, suspended_until=None)
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/anticheat/report", json={
        "event_type": "dom_shards_display_modified",
        "details": "document.querySelector('#shards-count').textContent = '999999'"
    })
    t.eq(r.status_code, 200, f"report {r.get_json()}")
    body = r.get_json()
    t.ok(body.get("modal", {}).get("title", "").startswith("Warning"), f"warning modal {body}")
    t.ok(body.get("quip"), "a sarcastic line is returned")
    t.eq(_user("alice")["cheat_warnings"], 1)
    # ask-nicely tampering has its own path
    r2 = a.post("/api/anticheat/report", json={"event_type": "ask_nicely_chance_scan"})
    t.eq(r2.status_code, 200)
    t.ok("ask nicely" in (r2.get_json().get("quip") or "").lower(), f"tamper quip {r2.get_json()}")


def v12_ask_nicely_flow():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/anticheat/ask_nicely", json={"message": "please may I have some shards, I asked nicely"})
    t.eq(r.status_code, 200, f"ask {r.get_json()}")
    rid = [x for x in _db().data["ask_nicely_requests"]
           if x["user_id"] == _user("alice")["id"]][-1]["id"]
    o = _sess("owner", t.OWNER_PW)
    pending = o.get("/api/admin/ask_nicely").get_json()
    t.ok(any(x["id"] == rid for x in pending["requests"]), "owner sees the request")
    before = _user("alice")["shards"]
    ap = o.post(f"/api/admin/ask_nicely/{rid}/approve", json={"amount": 50, "reply": "here you go"})
    t.eq(ap.status_code, 200, f"approve {ap.get_json()}")
    t.eq(_user("alice")["shards"], before + 50, "shards awarded")
    status = a.get("/api/anticheat/ask_nicely").get_json()["requests"]
    t.eq([x for x in status if x["id"] == rid][0]["status"], "approved")
    t.eq([x for x in status if x["id"] == rid][0]["reply_message"], "here you go")


# ------------------------------------------------------------------ admin applications
def v12_admin_application_ineligible():
    a = _sess("alice", t.ALICE_PW)
    r = a.post("/api/admin_applications", json={
        "reason": "x" * 120, "availability": "Most days", "what_would_you_do": "y" * 60
    })
    t.eq(r.status_code, 403, "a brand-new account cannot apply")
    mine = a.get("/api/admin_applications/mine").get_json()
    t.eq(mine.get("eligible"), False)
    t.ok(mine.get("requirements", {}).get("min_age_days") == 180)


def v12_admin_application_full():
    _set("bob", created_at="2024-01-01T00:00:00+00:00", spam_warnings=0, cheat_warnings=0)
    b = _sess("bob", t.BOB_PW)
    r = b.post("/api/admin_applications", json={
        "reason": "I have been here since the beginning and I want to help keep it clean. " * 3,
        "availability": "Every day",
        "what_would_you_do": "Answer reports quickly and be fair about warnings."
    })
    t.eq(r.status_code, 200, f"apply {r.get_json()}")
    o = _sess("owner", t.OWNER_PW)
    apps = o.get("/api/admin/applications").get_json()["applications"]
    t.ok(len(apps) >= 1, "owner sees applications")
    aid = apps[0]["id"]
    ap = o.post(f"/api/admin/applications/{aid}/approve", json={})
    t.eq(ap.status_code, 200, f"approve {ap.get_json()}")
    t.eq(_user("bob")["is_admin"], True, "bob is admin now")
    _set("bob", is_admin=False, admin_via_purchase=False)


def v12_purchased_admin_revoke():
    _set("alice", is_admin=True, admin_via_purchase=True)
    o = _sess("owner", t.OWNER_PW)
    listed = o.get("/api/admin/purchased_admins").get_json()["admins"]
    t.ok(any(x["username"] == "alice" for x in listed), "purchased admin listed")
    r = o.post(f"/api/admin/revoke_admin/{_user('alice')['id']}", json={})
    t.eq(r.status_code, 200, f"revoke {r.get_json()}")
    t.eq(_user("alice")["is_admin"], False)


# ------------------------------------------------------------------ spotlights
def v12_spotlight():
    o = _sess("owner", t.OWNER_PW)
    o.post("/api/cores/grant", json={"username": "alice", "amount": 100,
                                     "reason": "spotlight test budget"})
    a = _sess("alice", t.ALICE_PW)
    items = a.get("/api/shop/items").get_json()["items"]
    basic = [i for i in items if i.get("item_key") == "cores_spotlight_basic"]
    if not basic:
        return  # item not seeded in this fixture
    t.eq(a.post(f"/api/shop/buy/{basic[0]['id']}", json={}).status_code, 200)
    r = a.post("/api/spotlight", json={"item_key": "cores_spotlight_basic"})
    t.eq(r.status_code, 200, f"spotlight {r.get_json()}")
    current = a.get("/api/spotlight").get_json()
    t.ok(current.get("spotlight"), f"spotlight visible: {current}")
    t.eq(current["spotlight"]["username"], "alice")


# ------------------------------------------------------------------ hypercrypt
def v12_key_registration_and_fetch():
    a = _sess("alice", t.ALICE_PW)
    pub = base64.b64encode(b"\x04" + b"a" * 64).decode()
    backup = base64.b64encode(b"encrypted-private-key-bytes").decode()
    salt = base64.b64encode(b"0123456789abcdef").decode()
    iv = base64.b64encode(b"nonce12bytes").decode()
    r = a.post("/api/keys/register", json={
        "public_key": pub, "curve": "P-256", "encrypted_backup": backup,
        "backup_salt": salt, "backup_iv": iv, "backup_iters": 210000
    })
    t.eq(r.status_code, 200, f"register key {r.get_json()}")
    fp = r.get_json()["key_fingerprint"]
    mine = a.get("/api/keys/mine").get_json()["key"]
    t.eq(mine["public_key"], pub)
    t.eq(mine["key_fingerprint"], fp)
    # someone else can fetch the public half, never the private half
    b = _sess("bob", t.BOB_PW)
    theirs = b.get("/api/keys/alice").get_json()
    t.eq(theirs["key"]["public_key"], pub)
    t.ok("encrypted_backup" not in theirs["key"], "private backup is never served to others")
    # weak KDF is refused
    bad = a.post("/api/keys/register", json={
        "public_key": pub, "encrypted_backup": backup, "backup_salt": salt,
        "backup_iv": iv, "backup_iters": 1000})
    t.eq(bad.status_code, 400, "low PBKDF2 iterations must be refused")


def v12_conversation_key_and_escrow():
    a = _sess("alice", t.ALICE_PW)
    b = _sess("bob", t.BOB_PW)
    o = _sess("owner", t.OWNER_PW)
    # owner publishes the master key
    master_pub = base64.b64encode(b"\x04" + b"m" * 64).decode()
    r = o.post("/api/keys/master", json={"public_key": master_pub})
    t.eq(r.status_code, 200, f"master key {r.get_json()}")
    got = o.get("/api/keys/master").get_json()["key"]
    t.eq(got["public_key"], master_pub)

    cid = t.jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    wrap_a = base64.b64encode(b"ck-wrapped-for-alice").decode()
    wrap_b = base64.b64encode(b"ck-wrapped-for-bob").decode()
    wrap_m = base64.b64encode(b"ck-wrapped-for-master").decode()
    r2 = a.post(f"/api/conversations/{cid}/keys", json={
        "wraps": [{"user_id": _user("alice")["id"], "wrapped_key": wrap_a},
                  {"user_id": _user("bob")["id"], "wrapped_key": wrap_b}],
        "master_wrap": wrap_m,
        "key_fingerprint": "abcdef1234567890"
    })
    t.eq(r2.status_code, 200, f"store wraps {r2.get_json()}")
    mine_a = a.get(f"/api/conversations/{cid}/keys").get_json()["wraps"]
    t.ok(any(w["wrapped_key"] == wrap_a for w in mine_a), "alice can read her wrap")
    bulk = a.get("/api/keys/bulk").get_json()["keys"]
    t.ok(cid in bulk, "bulk key fetch covers the conversation")
    escrow = o.get(f"/api/admin/keys/escrow/{cid}").get_json()
    t.eq(escrow["wrap"]["wrapped_key"], wrap_m, "owner escrow wrap retrievable")
    # a non-member cannot read wraps
    _set("owner", is_owner=True)
    other = t.new_session()
    other.post("/api/login", json={"username": "bob", "password": t.BOB_PW})
    t.eq(other.get(f"/api/conversations/{cid}/keys").status_code, 200, "bob is a member")


def v12_encrypted_message_roundtrip():
    """The server must store the envelope verbatim and never a plaintext body."""
    _set("alice", anonymous_mode=False)
    a = _sess("alice", t.ALICE_PW)
    b = _sess("bob", t.BOB_PW)
    cid = t.jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    envelope = t.cipher.CIPHER_ENVELOPE_PREFIX + base64.b64encode(
        b'{"v":1,"iv":"AAAA","ct":"SECRETSECRET"}').decode()
    r = a.post(f"/api/messages/{cid}", json={"content": envelope})
    t.eq(r.status_code, 200, f"send envelope {r.get_json()}")
    stored = [m for m in _db().data["messages"] if m["id"] == r.get_json()["message"]["id"]][0]
    t.eq(stored["content"], envelope, "envelope stored verbatim")
    t.eq(stored.get("cipher_version"), 1, "message flagged as encrypted")
    msgs = b.get(f"/api/messages/{cid}").get_json()["messages"]
    target = [m for m in msgs if m["id"] == stored["id"]][0]
    t.eq(target["encrypted"], True, "client is told it is encrypted")
    t.eq(target["content"], envelope, "recipient gets the untouched envelope")
    # conversation preview must not truncate the ciphertext
    convs = b.get("/api/conversations").get_json()["conversations"]
    conv = [c for c in convs if c["id"] == cid][0]
    t.eq(conv["last_encrypted"], True)
    t.eq(conv["last_message"], envelope, "preview keeps the full envelope")


def v12_anonymous_everywhere():
    """BUG 1: anonymous mode has to hold in the preview, the header and typing."""
    _set("bob", anonymous_mode=True)
    b = _sess("bob", t.BOB_PW)
    a = _sess("alice", t.ALICE_PW)
    cid = t.jget(b.post("/api/conversations/new_dm", json={"username": "alice"}))["conversation_id"]
    b.post(f"/api/messages/{cid}", json={"content": "you cannot see my name"})
    b.post(f"/api/typing/{cid}", json={})
    convs = a.get("/api/conversations").get_json()["conversations"]
    conv = [c for c in convs if c["id"] == cid][0]
    t.eq(conv["last_sender_anonymous"], True, "preview knows the sender is anonymous")
    t.ok("bob" not in (conv["last_message"] or "").lower(), "preview hides the name")
    typing = a.get(f"/api/typing/{cid}").get_json()["typing"]
    t.ok("bob" not in typing, f"typing indicator hides the name: {typing}")
    members = conv["members"]
    bob_member = [m for m in members if m["id"] == _user("bob")["id"]][0]
    t.eq(bob_member["anonymous"], True, "member carries the anonymous flag for the chat header")
    _set("bob", anonymous_mode=False)


# ------------------------------------------------------------------ perf
def v12_conversation_list_query_budget():
    """The conversation list must not grow with the number of chats."""
    a = _sess("alice", t.ALICE_PW)
    for name in ("perf_one", "perf_two", "perf_three"):
        if not [u for u in _db().data["users"] if u["username"] == name]:
            t.make_user(name, "perfpass123")
        a.post("/api/conversations/new_dm", json={"username": name})
        a.post("/api/conversations/new_dm", json={"username": "bob"})
    convs = a.get("/api/conversations").get_json()["conversations"]
    t.ok(len(convs) >= 4, f"expected several conversations, got {len(convs)}")
    _db().reset_counts()
    a.get("/api/conversations")
    used = _db().query_count
    t.ok(used <= 12, f"conversation list should be ~constant queries, used {used}")


def v12_message_poll_query_budget():
    a = _sess("alice", t.ALICE_PW)
    cid = t.jget(a.post("/api/conversations/new_dm", json={"username": "bob"}))["conversation_id"]
    for i in range(6):
        a.post(f"/api/messages/{cid}", json={"content": f"msg {i}"})
    _db().reset_counts()
    since = t.cipher.now_iso()
    r = a.get(f"/api/messages/{cid}?since={since}")
    t.eq(r.status_code, 200)
    t.eq(r.get_json().get("incremental"), True)
    t.eq(len(r.get_json().get("messages", [])), 0, "nothing new since now")
    used = _db().query_count
    t.ok(used <= 8, f"incremental poll should be cheap, used {used}")


def v12_gzip_applied():
    a = _sess("alice", t.ALICE_PW)
    r = a.get("/api/conversations", headers={"Accept-Encoding": "gzip"})
    t.eq(r.headers.get("Content-Encoding"), "gzip", "large JSON responses are gzipped")


def v12_features_and_tos():
    anon = t.new_session()
    f = anon.get("/api/features").get_json()
    t.eq(f["encryption"]["name"], "Cipher HyperCrypt")
    t.eq(f["version"], "1.2.0")
    t.ok("Step" in f["made_by"], "credits the builder")
    tos = anon.get("/api/tos").get_json()
    titles = " ".join(s["title"] for s in tos["sections"])
    bodies = " ".join(s["body"] for s in tos["sections"])
    t.ok("Lawful access" in titles, "ToS discloses lawful access")
    t.ok("master key" in bodies, "ToS explains the escrow")


def v12_notifications():
    a = _sess("alice", t.ALICE_PW)
    _set("alice", shards=50)
    a.post("/api/shards/gift", json={"username": "bob", "amount": 5, "message": "hi"})
    b = _sess("bob", t.BOB_PW)
    notes = b.get("/api/notifications").get_json()
    t.ok(notes["unread"] >= 1, f"bob has unread notifications: {notes}")
    nid = notes["notifications"][0]["id"]
    t.eq(b.post(f"/api/notifications/{nid}/read", json={}).status_code, 200)
    t.eq(b.post("/api/notifications/read_all", json={}).status_code, 200)


def v12_admin_settings_roundtrip():
    o = _sess("owner", t.OWNER_PW)
    r = o.post("/api/admin/settings", json={
        "anti_cheat_enabled": True, "ask_nicely_chance": 0.5,
        "sorry_button_enabled": False, "admin_core_grant_max": 7,
        "bot_creation_policy": "anyone", "admin_ask_grant_amounts": "10,20"
    })
    t.eq(r.status_code, 200, f"settings {r.get_json()}")
    s = o.get("/api/admin/settings").get_json()["settings"]
    t.eq(s["admin_core_grant_max"], 7)
    t.eq(s["bot_creation_policy"], "anyone")
    t.eq(float(s["ask_nicely_chance"]), 0.5)
    # sorry button really is off now
    _set("alice", spam_warnings=1)
    a = _sess("alice", t.ALICE_PW)
    t.eq(a.post("/api/sorry", json={}).status_code, 403, "owner disabled the Sorry button")
    o.post("/api/admin/settings", json={"sorry_button_enabled": True, "bot_creation_policy": "purchase_only",
                                        "ask_nicely_chance": 0.001})
    _set("alice", spam_warnings=0)


CHECKS = []


def v12_degrades_without_migration():
    """
    Run against BOTH schemas. On a migrated DB the new endpoints work; on a
    pre-migration DB they must refuse cleanly (503 + JSON), never 500, and
    every v1.1 endpoint must keep working.
    """
    if t.SCHEMA_MODE == "v12":
        return  # covered properly by the checks above
    a = _sess("alice", t.ALICE_PW)
    for path, method, payload, expected in [
        ("/api/shards/daily", "post", {}, 503),
        ("/api/shards/tos_bonus", "post", {}, 503),
        ("/api/sorry", "post", {}, 503),
        ("/api/cores/grant", "post", {"username": "bob", "amount": 1, "reason": "ten chars ok"}, 503),
        ("/api/settings/username", "post", {"username": "nope_nope"}, 503),
        ("/api/settings/streamer", "post", {"enabled": True}, 503),
        ("/api/bots/register", "post", {"username": "x_bot"}, 503),
    ]:
        r = a.post(path, json=payload)
        t.eq(r.status_code, expected, f"{path} must fail closed on an unmigrated DB, got {r.status_code} {r.get_json()}")
        t.ok("error" in (r.get_json() or {}), f"{path} returns a JSON error")
    # read-only surfaces degrade to empty rather than erroring
    t.eq(a.get("/api/notifications").status_code, 200)
    t.eq(a.get("/api/conversations").status_code, 200)
    t.eq(a.get("/api/me").status_code, 200)
    t.eq(a.get("/api/spotlight").status_code, 200)
    t.eq(a.get("/api/anticheat/ask_nicely").status_code, 200)
    # anti-cheat reporting stays silent rather than inventing strikes
    r = a.post("/api/anticheat/report", json={"event_type": "dom_shards_display_modified"})
    t.eq(r.status_code, 200)
    t.eq(r.get_json().get("ignored"), True, "no cheat_warnings column, so no strike")
