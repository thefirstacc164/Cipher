-- CIPHER v1.1.0 Migration (idempotent)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- New columns on users
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS leaderboard_opt_out BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS anonymous_mode BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspension_reason TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bubble_color TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS name_font TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS msg_animation TEXT;

-- New columns on admin_settings
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS shards_per_referral INTEGER NOT NULL DEFAULT 10;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS affiliate_mode TEXT NOT NULL DEFAULT 'everyone';

-- Constraints
DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_shards_per_referral_nonnegative_chk CHECK (shards_per_referral >= 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_affiliate_mode_chk CHECK (affiliate_mode IN ('everyone','requires_approval','owner_only'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- New tables
CREATE TABLE IF NOT EXISTS terms_acceptance (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  accepted_at TIMESTAMPTZ DEFAULT NOW(),
  version TEXT DEFAULT '1.0'
);

CREATE TABLE IF NOT EXISTS affiliate_codes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  code TEXT UNIQUE NOT NULL,
  approved BOOLEAN DEFAULT FALSE,
  pending BOOLEAN DEFAULT FALSE,
  reason TEXT,
  approved_by UUID REFERENCES users(id),
  uses INT DEFAULT 0,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS affiliate_uses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code_id UUID REFERENCES affiliate_codes(id) ON DELETE CASCADE,
  new_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  shards_awarded INT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shard_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  amount INT NOT NULL,
  type TEXT,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  category TEXT,
  price INT NOT NULL,
  css_class TEXT,
  description TEXT,
  active BOOLEAN DEFAULT TRUE,
  one_time BOOLEAN DEFAULT TRUE,
  duration_days INT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_purchases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  item_id UUID REFERENCES shop_items(id) ON DELETE CASCADE,
  purchased_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  equipped BOOLEAN DEFAULT FALSE,
  UNIQUE(user_id, item_id)
);

CREATE TABLE IF NOT EXISTS user_profiles (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  bio TEXT,
  avatar_url TEXT,
  banner_color TEXT,
  active_effects TEXT[] DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS message_access_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  viewer_id UUID REFERENCES users(id),
  conversation_id UUID REFERENCES conversations(id),
  reason TEXT,
  ip_address TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_permissions (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  can_view_messages BOOLEAN DEFAULT FALSE,
  can_approve_affiliates BOOLEAN DEFAULT FALSE,
  can_create_announcements BOOLEAN DEFAULT FALSE,
  can_ban_ips BOOLEAN DEFAULT FALSE,
  can_suspend_users BOOLEAN DEFAULT FALSE,
  can_reset_passwords BOOLEAN DEFAULT FALSE,
  can_manage_shop BOOLEAN DEFAULT FALSE
);

-- Storage bucket
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('cipher-avatars', 'cipher-avatars', true, 512000, ARRAY['image/jpeg','image/png','image/webp'])
ON CONFLICT (id) DO UPDATE SET public = true, file_size_limit = 512000;

-- Seed shop items
INSERT INTO shop_items (name, category, price, css_class, description, one_time, duration_days) VALUES
('Profile Picture', 'profile', 20, NULL, 'Upload a custom profile picture', true, NULL),
('Profile Bio', 'profile', 10, NULL, 'Add a short bio to your profile', true, NULL),
('Banner Color', 'profile', 15, NULL, 'Customize your profile banner color', true, NULL),
('Glow Effect', 'effects', 30, 'effect-glow', 'Glowing aura around your avatar', true, NULL),
('Sparkle Effect', 'effects', 40, 'effect-sparkle', 'Sparkling particles around your avatar', true, NULL),
('Pulse Effect', 'effects', 30, 'effect-pulse', 'Breathing pulse animation on your avatar', true, NULL),
('Rainbow Border', 'effects', 50, 'effect-rainbow', 'Rainbow animated border on your avatar', true, NULL),
('Custom Bubble Color', 'chat', 25, NULL, 'Choose a custom color for your message bubbles', true, NULL),
('Nickname Font', 'chat', 20, NULL, 'Choose italic, bold, or monospace for your name', true, NULL),
('Send Animation', 'chat', 30, NULL, 'Choose slide, fade, or bounce for sent messages', true, NULL),
('VIP Badge', 'badges', 100, 'badge-vip', 'Exclusive VIP badge on your profile', true, NULL),
('Supporter Badge', 'badges', 50, 'badge-supporter', 'Show your support with this badge', true, NULL),
('Extend Retention', 'perks', 50, NULL, 'Extend all your message retention by 30 days', false, 30),
('Large Uploads', 'perks', 40, NULL, 'Upload images up to 10MB for 30 days', false, 30),
('Group Icon', 'perks', 60, NULL, 'Upload a custom icon for a group chat', true, NULL)
ON CONFLICT DO NOTHING;

-- Backfill profiles & permissions
INSERT INTO user_profiles (user_id)
SELECT id FROM users
ON CONFLICT DO NOTHING;

INSERT INTO admin_permissions (user_id)
SELECT id FROM users WHERE is_admin = true OR is_owner = true
ON CONFLICT DO NOTHING;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_affiliate_code ON affiliate_codes(code);
CREATE INDEX IF NOT EXISTS idx_affiliate_user ON affiliate_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_shard_tx_user ON shard_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_purchases_user ON user_purchases(user_id);
CREATE INDEX IF NOT EXISTS idx_access_log_time ON message_access_log(created_at DESC);