-- ============================================================
--   CIPHER v1.2.0 — PATCH 2  (exhaustive convergence)
--   Paste this whole block into the Supabase SQL editor and run it.
--   Safe to re-run: every statement is guarded. It cannot take the
--   site down — it only adds columns/tables/indexes that are missing.
--
--   What it fixes:
--   * "column last_no_warning_check does not exist" log spam
--   * affiliate tab 500s for the owner (revoked/total_earned/rejected_*)
--   * referral awards not recording (referred_user_id / signup_ip)
--   * invite codes (uses_count/revoked) and recovery/TLS drift columns
--   * friendships table (used by the app, never created by v1.1/v1.2)
--   * cores shop items, in case the original seed never ran
-- ============================================================

-- ------------------------------------------------------------
--   1. users
-- ------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_no_warning_check TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_warning_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS no_warnings_since TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bubble_color TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS name_font TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS msg_animation TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_create_invites BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_earned_this_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_gifted_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS week_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_claim TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_messages_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cores INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_grant_cores BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS friend_privacy TEXT NOT NULL DEFAULT 'approval';
ALTER TABLE users ADD COLUMN IF NOT EXISTS streamer_mode JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_bonus_claimed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_uses_this_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_week_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_changes_this_hour INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_change_window_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cheat_warnings INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ask_nicely_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_token_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_owner_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_via_purchase BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_purchased_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS leaderboard_opt_out BOOLEAN NOT NULL DEFAULT FALSE;

-- ------------------------------------------------------------
--   2. user_profiles
-- ------------------------------------------------------------
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS bio TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS banner_color TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_effects JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_badges JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_bubble_color TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_nickname_font TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_message_animation TEXT;

-- ------------------------------------------------------------
--   3. friendships (the app's friend system — create if missing)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS friendships (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  addressee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'accepted',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  accepted_at TIMESTAMPTZ,
  rejected_at TIMESTAMPTZ
);
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS requester_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS addressee_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'accepted';
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
UPDATE friendships SET status = 'accepted' WHERE status IS NULL;

-- ------------------------------------------------------------
--   4. invites (invite_codes UI removed in v1.2, backend kept)
-- ------------------------------------------------------------
ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS uses_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS invite_uses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invite_id UUID NOT NULL REFERENCES invite_links(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
--   5. affiliate system (owner tab 500 + referral awards)
-- ------------------------------------------------------------
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejected_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS total_earned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS referrer_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS referred_user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS shards_awarded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS signup_ip TEXT;
UPDATE affiliate_uses SET referred_user_id = new_user_id
  WHERE referred_user_id IS NULL AND new_user_id IS NOT NULL;

-- ------------------------------------------------------------
--   6. shop items
-- ------------------------------------------------------------
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS item_key TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS effect_key TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS icon TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS css_class TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'shards';
UPDATE shop_items SET currency = 'shards' WHERE currency IS NULL;

-- ------------------------------------------------------------
--   7. admin permissions + recovery / ToS drift
-- ------------------------------------------------------------
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_view_messages BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_approve_affiliates BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_create_announcements BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_ban_ips BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_suspend_ban_users BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_reset_passwords BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_manage_shop_items BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_manage_admins BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE recovery_keys ADD COLUMN IF NOT EXISTS method TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS accepted_version TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS ip TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS user_agent TEXT;

-- ------------------------------------------------------------
--   8. Re-seed shop items (each insert only if its item_key is absent)
-- ------------------------------------------------------------
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Custom Theme Color', 'cores_theme_color', 'cores_shop', 15, 'Make the whole app your colour', '🎨', NULL, TRUE, 10, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_theme_color');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Animated Avatar', 'cores_gif_avatar', 'cores_shop', 10, 'An animated GIF profile picture, forever', '🌀', NULL, TRUE, 20, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_gif_avatar');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Spotlight (24h)', 'cores_spotlight_basic', 'cores_shop', 20, 'Your name on the banner for 24 hours', '🔦', NULL, TRUE, 30, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_spotlight_basic');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Custom Spotlight', 'cores_spotlight_custom', 'cores_shop', 100, 'Your message on the banner for 24 hours', '📣', NULL, TRUE, 40, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_spotlight_custom');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'GOAT Badge', 'cores_badge_goat', 'cores_shop', 25, 'Good Guy That Does Good Stuff And Earns Well', '🐐', 'goat', TRUE, 50, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_badge_goat');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Instant Admin', 'cores_admin_instant', 'cores_shop', 50, 'Admin rights immediately. Monitored, revocable.', '🛡️', NULL, TRUE, 60, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_admin_instant');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Ask The Owner', 'cores_add_by_owner', 'cores_shop', 5, 'The owner gets a friend request from you', '📨', NULL, TRUE, 70, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_add_by_owner');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Bot Key', 'cores_bot_key', 'cores_shop', 30, 'Create a bot account with an API token', '🤖', NULL, TRUE, 80, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_bot_key');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Group Icon Coupon', 'cores_group_room_icon', 'cores_shop', 5, 'Permanently change one group icon', '🖼️', NULL, TRUE, 90, 'cores'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'cores_group_room_icon');
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT 'Extra Sorry', 'perk_extra_sorry', 'perks', 40, '+1 weekly "Sorry, undo this" use', '🙏', NULL, TRUE, 95, 'shards'
WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE item_key = 'perk_extra_sorry');

-- ------------------------------------------------------------
--   9. Encrypted attachment bucket + hot indexes
-- ------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public)
VALUES ('cipher-vault', 'cipher-vault', false)
ON CONFLICT (id) DO UPDATE SET public = false;

CREATE INDEX IF NOT EXISTS idx_affiliate_uses_referrer
  ON affiliate_uses(referrer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_friendships_pair
  ON friendships(requester_id, addressee_id);

SELECT 'patch2 done' AS status;
