-- ============================================================
--   CIPHER v1.2.0 — MIGRATION  (idempotent, safe to re-run)
-- ============================================================
--   Paste this into the Supabase SQL editor and run it.
--   Every statement is guarded, so running it twice is harmless,
--   and running it against a database that is already partly
--   migrated just fills in the gaps.
--
--   Order matters: run this BEFORE deploying v1.2.0. The app
--   degrades gracefully if you don't (v1.1 features keep working,
--   v1.2 features refuse with a clean 503), but nothing new will
--   light up until this has run.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
--   1. NEW COLUMNS ON users
-- ------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS cores INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_grant_cores BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_messages_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_claim TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_uses_this_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_week_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS friend_privacy TEXT NOT NULL DEFAULT 'approval';
ALTER TABLE users ADD COLUMN IF NOT EXISTS streamer_mode JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_changes_this_hour INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_change_window_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_bonus_claimed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cheat_warnings INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ask_nicely_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_token_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_owner_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_via_purchase BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_purchased_at TIMESTAMPTZ;

-- Added by this build (not in the original v1.2 spec, needed by the code):
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_earned_this_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS week_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_gifted_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_no_warning_check TIMESTAMPTZ;

DO $$ BEGIN
  ALTER TABLE users ADD CONSTRAINT users_friend_privacy_chk
    CHECK (friend_privacy IN ('open','approval','closed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------
--   2. NEW COLUMNS ON admin_settings
-- ------------------------------------------------------------
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS anti_cheat_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS ask_nicely_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS ask_nicely_chance DOUBLE PRECISION NOT NULL DEFAULT 0.001;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS sorry_button_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_shards BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_cores BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admin_core_grant_max INTEGER NOT NULL DEFAULT 3;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_approve_asks BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admin_ask_grant_amounts TEXT NOT NULL DEFAULT '20,50,100';
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_custom_ask BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS global_streamer_forces JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS bot_creation_policy TEXT NOT NULL DEFAULT 'purchase_only';

DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_bot_policy_chk
    CHECK (bot_creation_policy IN ('purchase_only','anyone','admin','owner'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------
--   3. OTHER EXISTING TABLES
-- ------------------------------------------------------------
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
ALTER TABLE shop_items  ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'shards';
ALTER TABLE messages    ADD COLUMN IF NOT EXISTS cipher_version INTEGER;

-- Unread counts per conversation. Without this the app still works,
-- it just cannot show badge counts in the sidebar.
ALTER TABLE conversation_members ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMPTZ;

DO $$ BEGIN
  ALTER TABLE shop_items ADD CONSTRAINT shop_items_currency_chk
    CHECK (currency IN ('shards','cores'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------
--   4. NEW TABLES
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_milestones (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bonus_key TEXT NOT NULL,
  shards_awarded INTEGER NOT NULL DEFAULT 0,
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, bonus_key)
);

CREATE TABLE IF NOT EXISTS sorry_uses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  warning_reverted INTEGER,
  used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS core_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount INTEGER NOT NULL,
  balance_after INTEGER,
  transaction_type TEXT,
  description TEXT,
  granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
  related_type TEXT,
  related_id TEXT,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shard_gifts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sender_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recipient_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount INTEGER NOT NULL,
  message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_message_counters (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  minute_bucket BIGINT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(bot_id, minute_bucket)
);

CREATE TABLE IF NOT EXISTS spotlights (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message TEXT,
  tier TEXT NOT NULL DEFAULT 'basic',
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_applications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason TEXT NOT NULL,
  availability TEXT,
  what_would_you_do TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ,
  rejection_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ask_nicely_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  ip_address TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ,
  reply_message TEXT,
  shards_granted INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS anticheat_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_type TEXT,
  details TEXT,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nickname_changes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  old_username TEXT,
  new_username TEXT,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS custom_badges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  icon TEXT DEFAULT '🏅',
  color TEXT DEFAULT '#00d9ff',
  description TEXT,
  purchasable BOOLEAN NOT NULL DEFAULT FALSE,
  shop_price INTEGER NOT NULL DEFAULT 0,
  created_by UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_badges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  badge_key TEXT NOT NULL,
  granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, badge_key)
);

-- In-app notifications (gifts, friend requests, ask-nicely replies, cores)
CREATE TABLE IF NOT EXISTS notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  read BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
--   5. CIPHER HYPERCRYPT — KEY MATERIAL
--   Only public keys and wrapped (encrypted) key material ever
--   lands here. There is no column anywhere that can hold a
--   plaintext message or a plaintext private key.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_keys (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  public_key TEXT NOT NULL,
  curve TEXT NOT NULL DEFAULT 'P-256',
  encrypted_backup TEXT NOT NULL,
  backup_salt TEXT NOT NULL,
  backup_iv TEXT,
  backup_iters INTEGER NOT NULL DEFAULT 210000,
  key_fingerprint TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  wrapped_key TEXT NOT NULL,
  wrapped_by UUID REFERENCES users(id) ON DELETE SET NULL,
  key_fingerprint TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_master_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  wrapped_key TEXT NOT NULL,
  wrapped_for TEXT NOT NULL DEFAULT 'master',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS master_key_meta (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  public_key TEXT NOT NULL,
  curve TEXT NOT NULL DEFAULT 'P-256',
  key_fingerprint TEXT,
  -- The private half, sealed with a key derived from the operator's
  -- password. The database holds ciphertext it cannot open.
  encrypted_backup TEXT,
  backup_salt TEXT,
  backup_iv TEXT,
  backup_iters INTEGER NOT NULL DEFAULT 210000,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The current spotlight, exposed as a view for convenience.
CREATE OR REPLACE VIEW current_spotlight AS
SELECT s.*, u.username, u.nickname_color
FROM spotlights s
JOIN users u ON u.id = s.user_id
WHERE s.expires_at > NOW()
ORDER BY s.created_at DESC
LIMIT 1;

-- ------------------------------------------------------------
--   6. INDEXES (these matter on the free tier)
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_messages_conv_created
  ON messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_expires
  ON messages(expires_at) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_conv_members_user
  ON conversation_members(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_members_conv
  ON conversation_members(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conv_keys_conv_user
  ON conversation_keys(conversation_id, user_id);
CREATE INDEX IF NOT EXISTS idx_master_keys_conv
  ON conversation_master_keys(conversation_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user
  ON notifications(user_id, read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_milestones_user
  ON user_milestones(user_id, bonus_key);
CREATE INDEX IF NOT EXISTS idx_anticheat_user
  ON anticheat_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_asknicely_status
  ON ask_nicely_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_spotlights_expires
  ON spotlights(expires_at);
CREATE INDEX IF NOT EXISTS idx_bot_token_hash
  ON users(bot_token_hash) WHERE bot_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_is_bot
  ON users(is_bot) WHERE is_bot = TRUE;
CREATE INDEX IF NOT EXISTS idx_friendships_requester
  ON friendships(requester_id);
CREATE INDEX IF NOT EXISTS idx_friendships_addressee
  ON friendships(addressee_id);
CREATE INDEX IF NOT EXISTS idx_core_tx_user
  ON core_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shard_gifts_recipient
  ON shard_gifts(recipient_id, created_at DESC);

-- ------------------------------------------------------------
--   7. ENCRYPTED ATTACHMENT BUCKET
--   Private, no MIME restriction: the objects in here are
--   ciphertext and are served through /api/file/<path>.
-- ------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public)
VALUES ('cipher-vault', 'cipher-vault', false)
ON CONFLICT (id) DO UPDATE SET public = false;

-- ------------------------------------------------------------
--   8. SEED SHOP ITEMS
--   item_key is what the backend switches on. Re-running is safe.
-- ------------------------------------------------------------
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
VALUES
  -- Cores shop
  ('Custom Theme Color', 'cores_theme_color',      'cores_shop', 15,  'Make the whole app your colour', '🎨', NULL, TRUE, 10, 'cores'),
  ('Animated Avatar',    'cores_gif_avatar',       'cores_shop', 10,  'An animated GIF profile picture, forever', '🌀', NULL, TRUE, 20, 'cores'),
  ('Spotlight (24h)',    'cores_spotlight_basic',  'cores_shop', 20,  'Your name on the banner for 24 hours', '🔦', NULL, TRUE, 30, 'cores'),
  ('Custom Spotlight',   'cores_spotlight_custom', 'cores_shop', 100, 'Your message on the banner for 24 hours', '📣', NULL, TRUE, 40, 'cores'),
  ('GOAT Badge',         'cores_badge_goat',       'cores_shop', 25,  'Good Guy That Does Good Stuff And Earns Well', '🐐', 'goat', TRUE, 50, 'cores'),
  ('Instant Admin',      'cores_admin_instant',    'cores_shop', 50,  'Admin rights immediately. Monitored, revocable.', '🛡️', NULL, TRUE, 60, 'cores'),
  ('Ask The Owner',      'cores_add_by_owner',     'cores_shop', 5,   'The owner gets a friend request from you', '📨', NULL, TRUE, 70, 'cores'),
  ('Bot Key',            'cores_bot_key',          'cores_shop', 30,  'Create a bot account with an API token', '🤖', NULL, TRUE, 80, 'cores'),
  ('Group Icon Coupon',  'cores_group_room_icon',  'cores_shop', 5,   'Permanently change one group icon', '🖼️', NULL, TRUE, 90, 'cores'),
  -- Shards shop additions
  ('Extra Sorry',        'perk_extra_sorry',       'perks',      40,  '+1 weekly "Sorry, undo this" use', '🙏', NULL, TRUE, 95, 'shards')
ON CONFLICT DO NOTHING;

-- Mark any pre-existing items as Shards-priced (the column default already
-- does this, but be explicit for rows created before the column existed).
UPDATE shop_items SET currency = 'shards' WHERE currency IS NULL;

-- ------------------------------------------------------------
--   9. BACKFILL
-- ------------------------------------------------------------
UPDATE users SET friend_privacy = 'approval' WHERE friend_privacy IS NULL;
UPDATE users SET total_messages_sent = 0 WHERE total_messages_sent IS NULL;

-- ------------------------------------------------------------
--  10. ROW LEVEL SECURITY
--   The app talks to Postgres with the service role key, so these
--   policies are a second line of defence: if an anon key ever
--   leaked, no row in these tables would be readable.
-- ------------------------------------------------------------
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'user_keys','conversation_keys','conversation_master_keys','master_key_meta',
    'notifications','user_milestones','sorry_uses','core_transactions','shard_gifts',
    'spotlights','admin_applications','ask_nicely_requests','anticheat_events',
    'nickname_changes','custom_badges','user_badges','bot_message_counters'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
