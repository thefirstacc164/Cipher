-- ============================================================================
--   CIPHER — FULL SCHEMA CONVERGENCE / REPAIR
--   Target: v1.2.x (app.py @ this commit)
-- ============================================================================
--   WHAT THIS IS
--   ------------
--   One idempotent script that makes your Supabase database match exactly
--   what app.py expects. Safe to run on a fresh database, on a v1.0 database,
--   on a half-migrated one, and safe to run twice. It only ADDS and REPAIRS:
--   it never drops a table, never drops a data column, never deletes a row.
--
--   HOW TO RUN
--   ----------
--   Supabase Dashboard -> SQL Editor -> New query -> paste this whole file
--   -> Run. It ends by printing a verification table.
--
--   WHAT IT FIXES (the real bugs found by reading app.py against the
--   existing migrations):
--
--   1. users.avatar_url does not exist, but app.py embeds it in
--      /api/friends and /api/spotlight. /api/friends swallows the error,
--      so the friends list silently comes back EMPTY forever.
--   2. PostgREST embed ambiguity. affiliate_codes, core_transactions,
--      ask_nicely_requests and admin_applications each have 2-3 foreign
--      keys to users, while app.py asks for a bare `users(...)` embed.
--      PostgREST answers 300 "more than one relationship was found" ->
--      the owner's affiliate tab, the cores log, the ask-nicely queue and
--      the admin-applications queue all break. Fixed by removing the
--      *secondary* FK (approved_by / rejected_by / granted_by / reviewed_by)
--      so the `users` relationship resolves uniquely through user_id.
--      The columns and their data are kept.
--   3. message_access_log: app.py writes `ip` and `target_user_id`;
--      migration v1.1 only created `ip_address` and no target column.
--   4. user_purchases.price_paid is written on every purchase, never created.
--   5. shard_transactions: app.py writes balance_after / transaction_type /
--      related_table / related_id / created_by; v1.1 created type/description.
--   6. admin_permissions.granted_by + updated_at missing; v1.1's
--      can_suspend_users / can_manage_shop never renamed to the names
--      app.py reads (can_suspend_ban_users / can_manage_shop_items).
--   7. user_profiles.active_effects was created TEXT[] in v1.1 but is
--      written as JSON by app.py -> converted to JSONB.
--   8. admin_settings missing site_name / max_file_size_mb /
--      registration_message, and sometimes missing its id=1 row entirely.
--   9. Every base (v1.0) table is created IF NOT EXISTS, so this file alone
--      can stand up the whole database from nothing.
--  10. Missing foreign keys that PostgREST needs for the embeds app.py uses
--      (messages:sender_id, conversation_members, reactions, reads, typing,
--      user_purchases->shop_items, friendships requester/addressee, ...).
--  11. Reloads the PostgREST schema cache at the end, so the fix takes
--      effect immediately instead of after the next restart.
-- ============================================================================

BEGIN;

SET LOCAL statement_timeout = '600s';
SET LOCAL lock_timeout = '30s';

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ============================================================================
--   0. HELPERS (temporary — dropped at the bottom of this file)
-- ============================================================================

-- Add a single-column FK only if that column has no FK yet. Never duplicates
-- a relationship (a duplicate would re-create the PostgREST ambiguity).
CREATE OR REPLACE FUNCTION cipher_ensure_fk(
  p_table text, p_column text, p_ref_table text, p_ref_col text,
  p_on_delete text DEFAULT 'CASCADE'
) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
  v_name text := left(p_table || '_' || p_column || '_fkey', 63);
BEGIN
  IF to_regclass('public.' || quote_ident(p_table)) IS NULL
     OR to_regclass('public.' || quote_ident(p_ref_table)) IS NULL THEN
    RETURN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='public' AND table_name=p_table
                   AND column_name=p_column) THEN
    RETURN;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.conrelid = ('public.' || quote_ident(p_table))::regclass
      AND c.contype = 'f'
      AND array_length(c.conkey, 1) = 1
      AND a.attname = p_column
  ) THEN
    RETURN;                                  -- already linked, leave it alone
  END IF;

  BEGIN
    EXECUTE format(
      'ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES public.%I(%I) ON DELETE %s',
      p_table, v_name, p_column, p_ref_table, p_ref_col, p_on_delete);
  EXCEPTION WHEN others THEN
    -- Legacy orphan rows would block a validated FK. Add it NOT VALID:
    -- PostgREST still sees the relationship, existing rows are left alone.
    BEGIN
      EXECUTE format(
        'ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES public.%I(%I) ON DELETE %s NOT VALID',
        p_table, v_name, p_column, p_ref_table, p_ref_col, p_on_delete);
      RAISE NOTICE 'cipher: % .% FK added NOT VALID (orphan rows present)', p_table, p_column;
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'cipher: could not add FK on %.% (%)', p_table, p_column, SQLERRM;
    END;
  END;
END;
$fn$;

-- Remove every single-column FK on a column, keeping the column and its data.
-- Used to kill PostgREST embed ambiguity.
CREATE OR REPLACE FUNCTION cipher_drop_fk(p_table text, p_column text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
  IF to_regclass('public.' || quote_ident(p_table)) IS NULL THEN RETURN; END IF;
  FOR r IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.conrelid = ('public.' || quote_ident(p_table))::regclass
      AND c.contype = 'f'
      AND array_length(c.conkey, 1) = 1
      AND a.attname = p_column
  LOOP
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', p_table, r.conname);
    RAISE NOTICE 'cipher: dropped ambiguous FK %.% (%)', p_table, p_column, r.conname;
  END LOOP;
END;
$fn$;


-- ============================================================================
--   1. BASE TABLES (v1.0). CREATE IF NOT EXISTS — no-ops on a live database.
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username             TEXT UNIQUE NOT NULL,
  password_hash        TEXT NOT NULL,
  recovery_phrase      TEXT,
  is_owner             BOOLEAN NOT NULL DEFAULT FALSE,
  is_admin             BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen            TIMESTAMPTZ,
  last_ip              TEXT,
  spam_warnings        INTEGER NOT NULL DEFAULT 0,
  throttle_level       INTEGER NOT NULL DEFAULT 0,
  throttle_until       TIMESTAMPTZ,
  can_send_messages    BOOLEAN NOT NULL DEFAULT TRUE,
  totp_enabled         BOOLEAN NOT NULL DEFAULT FALSE,
  totp_secret          TEXT,
  keep_all_forever     BOOLEAN NOT NULL DEFAULT FALSE,
  notify_before_delete BOOLEAN NOT NULL DEFAULT TRUE,
  nickname_color       TEXT DEFAULT '#00d9ff',
  theme_color          TEXT DEFAULT '#00d9ff'
);

CREATE TABLE IF NOT EXISTS admin_settings (
  id                     INTEGER PRIMARY KEY DEFAULT 1,
  site_name              TEXT NOT NULL DEFAULT 'Cipher',
  max_file_size_mb       INTEGER NOT NULL DEFAULT 5,
  signups_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
  invites_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
  invite_creation_mode   TEXT NOT NULL DEFAULT 'admin',
  maintenance_mode       BOOLEAN NOT NULL DEFAULT FALSE,
  registration_message   TEXT,
  default_retention_days INTEGER NOT NULL DEFAULT 7
);

CREATE TABLE IF NOT EXISTS conversations (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT,
  is_group     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
  keep_forever BOOLEAN NOT NULL DEFAULT FALSE,
  icon_url     TEXT
);

CREATE TABLE IF NOT EXISTS conversation_members (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_group_admin  BOOLEAN NOT NULL DEFAULT FALSE,
  muted           BOOLEAN NOT NULL DEFAULT FALSE,
  joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_read_at    TIMESTAMPTZ,
  UNIQUE (conversation_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_id       UUID REFERENCES users(id) ON DELETE SET NULL,
  content         TEXT,
  image_url       TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ,
  deleted         BOOLEAN NOT NULL DEFAULT FALSE,
  is_anonymous    BOOLEAN NOT NULL DEFAULT FALSE,
  warning_sent    BOOLEAN NOT NULL DEFAULT FALSE,
  edited_at       TIMESTAMPTZ,
  cipher_version  INTEGER
);

CREATE TABLE IF NOT EXISTS message_reactions (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  emoji      TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (message_id, user_id, emoji)
);

CREATE TABLE IF NOT EXISTS message_reads (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  read_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS message_warnings (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  dismissed       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS typing_status (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS recent_messages (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content_hash TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bans (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ip_address TEXT NOT NULL,
  reason     TEXT,
  banned_by  UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_punishments (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  punished_by UUID REFERENCES users(id) ON DELETE SET NULL,
  type        TEXT NOT NULL,
  reason      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at  TIMESTAMPTZ,
  active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS spam_events (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_count  INTEGER,
  trigger_reason TEXT,
  warning_number INTEGER,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  admin_id       UUID REFERENCES users(id) ON DELETE SET NULL,
  admin_username TEXT,
  action         TEXT NOT NULL,
  target_type    TEXT,
  target_id      TEXT,
  details        TEXT,
  ip_address     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS immunity_list (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username   TEXT UNIQUE NOT NULL,
  added_by   UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS announcements (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title      TEXT,
  content    TEXT,
  priority   TEXT DEFAULT 'normal',
  created_by UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS invite_links (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code       TEXT UNIQUE NOT NULL,
  created_by UUID REFERENCES users(id) ON DELETE SET NULL,
  max_uses   INTEGER,
  uses       INTEGER NOT NULL DEFAULT 0,
  uses_count INTEGER NOT NULL DEFAULT 0,
  active     BOOLEAN NOT NULL DEFAULT TRUE,
  revoked    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS recovery_keys (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_hash   TEXT NOT NULL,
  method     TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================================
--   2. v1.1 TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS terms_acceptance (
  user_id          UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  accepted_at      TIMESTAMPTZ DEFAULT NOW(),
  version          TEXT DEFAULT '1.0',
  accepted_version TEXT,
  ip               TEXT,
  user_agent       TEXT
);

CREATE TABLE IF NOT EXISTS affiliate_codes (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID REFERENCES users(id) ON DELETE CASCADE,
  code             TEXT UNIQUE NOT NULL,
  approved         BOOLEAN DEFAULT FALSE,
  pending          BOOLEAN DEFAULT FALSE,
  reason           TEXT,
  approved_by      UUID,
  approved_at      TIMESTAMPTZ,
  rejected_by      UUID,
  rejected_at      TIMESTAMPTZ,
  rejection_reason TEXT,
  uses             INTEGER DEFAULT 0,
  total_earned     INTEGER NOT NULL DEFAULT 0,
  active           BOOLEAN DEFAULT TRUE,
  revoked          BOOLEAN NOT NULL DEFAULT FALSE,
  revoked_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS affiliate_uses (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code_id          UUID REFERENCES affiliate_codes(id) ON DELETE CASCADE,
  new_user_id      UUID,
  referrer_id      UUID,
  referred_user_id UUID,
  shards_awarded   INTEGER NOT NULL DEFAULT 0,
  signup_ip        TEXT,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shard_transactions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID REFERENCES users(id) ON DELETE CASCADE,
  amount           INTEGER NOT NULL,
  balance_after    INTEGER,
  type             TEXT,
  transaction_type TEXT,
  description      TEXT,
  related_table    TEXT,
  related_id       TEXT,
  created_by       UUID,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shop_items (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  item_key      TEXT,
  category      TEXT,
  price         INTEGER NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'shards',
  css_class     TEXT,
  icon          TEXT,
  effect_key    TEXT,
  description   TEXT,
  active        BOOLEAN DEFAULT TRUE,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  one_time      BOOLEAN DEFAULT TRUE,
  duration_days INTEGER,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_purchases (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID REFERENCES users(id) ON DELETE CASCADE,
  item_id      UUID REFERENCES shop_items(id) ON DELETE CASCADE,
  purchased_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at   TIMESTAMPTZ,
  price_paid   INTEGER,
  equipped     BOOLEAN DEFAULT FALSE,
  UNIQUE (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS user_profiles (
  user_id                  UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  bio                      TEXT,
  avatar_url               TEXT,
  banner_color             TEXT,
  active_effects           JSONB NOT NULL DEFAULT '[]'::jsonb,
  active_badges            JSONB NOT NULL DEFAULT '[]'::jsonb,
  active_bubble_color      TEXT,
  active_nickname_font     TEXT,
  active_message_animation TEXT
);

CREATE TABLE IF NOT EXISTS message_access_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  viewer_id       UUID,
  target_user_id  UUID,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  reason          TEXT,
  ip              TEXT,
  ip_address      TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_permissions (
  user_id                  UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  can_view_messages        BOOLEAN NOT NULL DEFAULT FALSE,
  can_approve_affiliates   BOOLEAN NOT NULL DEFAULT FALSE,
  can_create_announcements BOOLEAN NOT NULL DEFAULT FALSE,
  can_ban_ips              BOOLEAN NOT NULL DEFAULT FALSE,
  can_suspend_ban_users    BOOLEAN NOT NULL DEFAULT FALSE,
  can_reset_passwords      BOOLEAN NOT NULL DEFAULT FALSE,
  can_manage_shop_items    BOOLEAN NOT NULL DEFAULT FALSE,
  can_manage_admins        BOOLEAN NOT NULL DEFAULT FALSE,
  granted_by               UUID,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================================
--   3. v1.2 TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS friendships (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  addressee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status       TEXT NOT NULL DEFAULT 'accepted',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  accepted_at  TIMESTAMPTZ,
  rejected_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS invite_uses (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invite_id  UUID REFERENCES invite_links(id) ON DELETE CASCADE,
  user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_milestones (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bonus_key      TEXT NOT NULL,
  shards_awarded INTEGER NOT NULL DEFAULT 0,
  claimed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, bonus_key)
);

CREATE TABLE IF NOT EXISTS sorry_uses (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  warning_reverted INTEGER,
  used_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS core_transactions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount           INTEGER NOT NULL,
  balance_after    INTEGER,
  transaction_type TEXT,
  description      TEXT,
  granted_by       UUID,
  related_type     TEXT,
  related_id       TEXT,
  metadata         JSONB,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shard_gifts (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sender_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recipient_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount       INTEGER NOT NULL,
  message      TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_message_counters (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  minute_bucket BIGINT NOT NULL,
  count         INTEGER NOT NULL DEFAULT 0,
  UNIQUE (bot_id, minute_bucket)
);

CREATE TABLE IF NOT EXISTS spotlights (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message    TEXT,
  tier       TEXT NOT NULL DEFAULT 'basic',
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_applications (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason            TEXT NOT NULL,
  availability      TEXT,
  what_would_you_do TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',
  reviewed_by       UUID,
  reviewed_at       TIMESTAMPTZ,
  rejection_reason  TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ask_nicely_requests (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message        TEXT NOT NULL,
  ip_address     TEXT,
  status         TEXT NOT NULL DEFAULT 'pending',
  reviewed_by    UUID,
  reviewed_at    TIMESTAMPTZ,
  reply_message  TEXT,
  shards_granted INTEGER,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS anticheat_events (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_type TEXT,
  details    TEXT,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nickname_changes (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  old_username TEXT,
  new_username TEXT,
  ip_address   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS custom_badges (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  icon        TEXT DEFAULT '🏅',
  color       TEXT DEFAULT '#00d9ff',
  description TEXT,
  purchasable BOOLEAN NOT NULL DEFAULT FALSE,
  shop_price  INTEGER NOT NULL DEFAULT 0,
  created_by  UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_badges (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  badge_key  TEXT NOT NULL,
  granted_by UUID,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, badge_key)
);

CREATE TABLE IF NOT EXISTS notifications (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  read       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- Cipher HyperCrypt: public keys + wrapped key material only -------------
CREATE TABLE IF NOT EXISTS user_keys (
  user_id          UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  public_key       TEXT NOT NULL,
  curve            TEXT NOT NULL DEFAULT 'P-256',
  encrypted_backup TEXT NOT NULL,
  backup_salt      TEXT NOT NULL,
  backup_iv        TEXT,
  backup_iters     INTEGER NOT NULL DEFAULT 210000,
  key_fingerprint  TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_keys (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  wrapped_key     TEXT NOT NULL,
  wrapped_by      UUID,
  key_fingerprint TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_master_keys (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  wrapped_key     TEXT NOT NULL,
  wrapped_for     TEXT NOT NULL DEFAULT 'master',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS master_key_meta (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  public_key       TEXT NOT NULL,
  curve            TEXT NOT NULL DEFAULT 'P-256',
  key_fingerprint  TEXT,
  encrypted_backup TEXT,
  backup_salt      TEXT,
  backup_iv        TEXT,
  backup_iters     INTEGER NOT NULL DEFAULT 210000,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================================
--   4. EVERY COLUMN app.py TOUCHES — added if missing
--      (this is what repairs a database created before any of the above)
-- ============================================================================

-- ---- users -----------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_phrase              TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen                    TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ip                      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS spam_warnings                INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS throttle_level               INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS throttle_until               TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_send_messages            BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled                 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret                  TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS keep_all_forever             BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_before_delete         BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_color               TEXT DEFAULT '#00d9ff';
ALTER TABLE users ADD COLUMN IF NOT EXISTS theme_color                  TEXT DEFAULT '#00d9ff';
-- >>> FIX #1: /api/friends and /api/spotlight embed users(...,avatar_url).
--     Without this column the friends list silently returns empty.
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url                   TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS anonymous_mode               BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended                    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_until              TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspension_reason            TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bubble_color                 TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS name_font                    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS msg_animation                TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards                       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS leaderboard_opt_out          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_create_invites           BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cores                        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_grant_cores              BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_messages_sent          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_claim             TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_uses_this_week         INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sorry_week_start             TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS friend_privacy               TEXT NOT NULL DEFAULT 'approval';
ALTER TABLE users ADD COLUMN IF NOT EXISTS streamer_mode                JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_changes_this_hour   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname_change_window_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_bonus_claimed            BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cheat_warnings               INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ask_nicely_banned            BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot                       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_token_hash               TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_owner_id                 UUID;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_is_active                BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_via_purchase           BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_purchased_at           TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_earned_this_week      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS week_start                   TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shards_gifted_total          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_warning_at              TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_no_warning_check        TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS no_warnings_since            TIMESTAMPTZ;

-- ---- admin_settings --------------------------------------------------------
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS site_name                   TEXT NOT NULL DEFAULT 'Cipher';
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS max_file_size_mb            INTEGER NOT NULL DEFAULT 5;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS signups_enabled             BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS invites_enabled             BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS invite_creation_mode        TEXT NOT NULL DEFAULT 'admin';
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS maintenance_mode            BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS registration_message        TEXT;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS default_retention_days      INTEGER NOT NULL DEFAULT 7;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS shards_per_referral         INTEGER NOT NULL DEFAULT 10;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS affiliate_mode              TEXT NOT NULL DEFAULT 'everyone';
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS anti_cheat_enabled          BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS ask_nicely_enabled          BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS ask_nicely_chance           DOUBLE PRECISION NOT NULL DEFAULT 0.001;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS sorry_button_enabled        BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_shards     BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_cores      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admin_core_grant_max        INTEGER NOT NULL DEFAULT 3;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_approve_asks     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admin_ask_grant_amounts     TEXT NOT NULL DEFAULT '20,50,100';
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS admins_can_grant_custom_ask BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS global_streamer_forces      JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS bot_creation_policy         TEXT NOT NULL DEFAULT 'purchase_only';

-- ---- conversations / members / messages ------------------------------------
ALTER TABLE conversations        ADD COLUMN IF NOT EXISTS icon_url       TEXT;
ALTER TABLE conversations        ADD COLUMN IF NOT EXISTS keep_forever   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE conversation_members ADD COLUMN IF NOT EXISTS last_read_at   TIMESTAMPTZ;
ALTER TABLE conversation_members ADD COLUMN IF NOT EXISTS muted          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE conversation_members ADD COLUMN IF NOT EXISTS is_group_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages             ADD COLUMN IF NOT EXISTS cipher_version INTEGER;
ALTER TABLE messages             ADD COLUMN IF NOT EXISTS edited_at      TIMESTAMPTZ;
ALTER TABLE messages             ADD COLUMN IF NOT EXISTS warning_sent   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages             ADD COLUMN IF NOT EXISTS is_anonymous   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages             ADD COLUMN IF NOT EXISTS image_url      TEXT;

-- ---- user_profiles ---------------------------------------------------------
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS bio                      TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avatar_url               TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS banner_color             TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_effects           JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_badges            JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_bubble_color      TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_nickname_font     TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS active_message_animation TEXT;

-- >>> FIX #7: v1.1 created active_effects as TEXT[]; app.py writes JSON.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['active_effects','active_badges'] LOOP
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name='user_profiles'
                 AND column_name=t AND data_type='ARRAY') THEN
      EXECUTE format(
        'ALTER TABLE user_profiles ALTER COLUMN %I DROP DEFAULT,
         ALTER COLUMN %I TYPE jsonb USING COALESCE(to_jsonb(%I), ''[]''::jsonb),
         ALTER COLUMN %I SET DEFAULT ''[]''::jsonb', t, t, t, t);
      EXECUTE format('UPDATE user_profiles SET %I = ''[]''::jsonb WHERE %I IS NULL', t, t);
      EXECUTE format('ALTER TABLE user_profiles ALTER COLUMN %I SET NOT NULL', t);
      RAISE NOTICE 'cipher: converted user_profiles.% from text[] to jsonb', t;
    END IF;
  END LOOP;
END $$;

-- ---- shop / purchases / currencies ----------------------------------------
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS item_key      TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS effect_key    TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS icon          TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS css_class     TEXT;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS enabled       BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS sort_order    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS currency      TEXT NOT NULL DEFAULT 'shards';
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS active        BOOLEAN DEFAULT TRUE;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS one_time      BOOLEAN DEFAULT TRUE;
ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS duration_days INTEGER;
UPDATE shop_items SET currency = 'shards' WHERE currency IS NULL;

-- >>> FIX #4: every purchase writes price_paid.
ALTER TABLE user_purchases ADD COLUMN IF NOT EXISTS price_paid INTEGER;
ALTER TABLE user_purchases ADD COLUMN IF NOT EXISTS equipped   BOOLEAN DEFAULT FALSE;
ALTER TABLE user_purchases ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- >>> FIX #5: shard ledger columns app.py actually writes.
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS balance_after    INTEGER;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS related_table    TEXT;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS related_id       TEXT;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS created_by       UUID;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS type             TEXT;
ALTER TABLE shard_transactions ADD COLUMN IF NOT EXISTS description      TEXT;
-- carry the legacy `type` values into `transaction_type` once
UPDATE shard_transactions SET transaction_type = type
  WHERE transaction_type IS NULL AND type IS NOT NULL;

ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS balance_after    INTEGER;
ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT;
ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS related_type     TEXT;
ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS related_id       TEXT;
ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS metadata         JSONB;
ALTER TABLE core_transactions ADD COLUMN IF NOT EXISTS granted_by       UUID;

-- ---- affiliates ------------------------------------------------------------
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS revoked          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS revoked_at       TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS approved_at      TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS approved_by      UUID;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejected_at      TIMESTAMPTZ;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejected_by      UUID;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS total_earned     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS uses             INTEGER DEFAULT 0;
ALTER TABLE affiliate_codes ADD COLUMN IF NOT EXISTS active           BOOLEAN DEFAULT TRUE;

ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS referrer_id      UUID;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS referred_user_id UUID;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS new_user_id      UUID;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS shards_awarded   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE affiliate_uses ADD COLUMN IF NOT EXISTS signup_ip        TEXT;
UPDATE affiliate_uses SET referred_user_id = new_user_id
  WHERE referred_user_id IS NULL AND new_user_id IS NOT NULL;
UPDATE affiliate_uses SET new_user_id = referred_user_id
  WHERE new_user_id IS NULL AND referred_user_id IS NOT NULL;

-- ---- invites / recovery / tos ---------------------------------------------
ALTER TABLE invite_links     ADD COLUMN IF NOT EXISTS uses_count       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invite_links     ADD COLUMN IF NOT EXISTS revoked          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE invite_links     ADD COLUMN IF NOT EXISTS uses             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invite_links     ADD COLUMN IF NOT EXISTS active           BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE recovery_keys    ADD COLUMN IF NOT EXISTS method           TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS accepted_version TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS ip               TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS user_agent       TEXT;
ALTER TABLE terms_acceptance ADD COLUMN IF NOT EXISTS version          TEXT DEFAULT '1.0';

-- ---- friendships -----------------------------------------------------------
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS status      TEXT NOT NULL DEFAULT 'accepted';
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
ALTER TABLE friendships ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW();
UPDATE friendships SET status = 'accepted' WHERE status IS NULL;

-- >>> FIX #3: message_access_log — app.py writes `ip` and `target_user_id`.
ALTER TABLE message_access_log ADD COLUMN IF NOT EXISTS ip             TEXT;
ALTER TABLE message_access_log ADD COLUMN IF NOT EXISTS ip_address     TEXT;
ALTER TABLE message_access_log ADD COLUMN IF NOT EXISTS target_user_id UUID;
ALTER TABLE message_access_log ADD COLUMN IF NOT EXISTS viewer_id      UUID;
UPDATE message_access_log SET ip = ip_address WHERE ip IS NULL AND ip_address IS NOT NULL;

-- >>> FIX #6: admin_permissions — the names app.py reads, plus audit columns.
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_view_messages        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_approve_affiliates   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_create_announcements BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_ban_ips              BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_suspend_ban_users    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_reset_passwords      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_manage_shop_items    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS can_manage_admins        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS granted_by               UUID;
ALTER TABLE admin_permissions ADD COLUMN IF NOT EXISTS updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- carry v1.1's differently-named permission flags across, once
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='admin_permissions'
               AND column_name='can_suspend_users') THEN
    EXECUTE 'UPDATE admin_permissions SET can_suspend_ban_users = TRUE
             WHERE can_suspend_users IS TRUE AND can_suspend_ban_users IS NOT TRUE';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='admin_permissions'
               AND column_name='can_manage_shop') THEN
    EXECUTE 'UPDATE admin_permissions SET can_manage_shop_items = TRUE
             WHERE can_manage_shop IS TRUE AND can_manage_shop_items IS NOT TRUE';
  END IF;
END $$;


-- ============================================================================
--   5. CONSTRAINTS (value checks app.py relies on)
-- ============================================================================
DO $$ BEGIN
  ALTER TABLE users ADD CONSTRAINT users_friend_privacy_chk
    CHECK (friend_privacy IN ('open','approval','closed'));
EXCEPTION WHEN duplicate_object THEN NULL; WHEN others THEN
  RAISE NOTICE 'cipher: users_friend_privacy_chk skipped (%)', SQLERRM; END $$;

DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_affiliate_mode_chk
    CHECK (affiliate_mode IN ('everyone','requires_approval','owner_only'));
EXCEPTION WHEN duplicate_object THEN NULL; WHEN others THEN
  RAISE NOTICE 'cipher: affiliate_mode chk skipped (%)', SQLERRM; END $$;

DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_shards_per_referral_nonnegative_chk
    CHECK (shards_per_referral >= 0);
EXCEPTION WHEN duplicate_object THEN NULL; WHEN others THEN
  RAISE NOTICE 'cipher: shards_per_referral chk skipped (%)', SQLERRM; END $$;

DO $$ BEGIN
  ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_bot_policy_chk
    CHECK (bot_creation_policy IN ('purchase_only','anyone','admin','owner'));
EXCEPTION WHEN duplicate_object THEN NULL; WHEN others THEN
  RAISE NOTICE 'cipher: bot_policy chk skipped (%)', SQLERRM; END $$;

DO $$ BEGIN
  ALTER TABLE shop_items ADD CONSTRAINT shop_items_currency_chk
    CHECK (currency IN ('shards','cores'));
EXCEPTION WHEN duplicate_object THEN NULL; WHEN others THEN
  RAISE NOTICE 'cipher: currency chk skipped (%)', SQLERRM; END $$;

-- shop_items.item_key is the key the backend switches on; it must be unique.
DO $$ BEGIN
  CREATE UNIQUE INDEX IF NOT EXISTS shop_items_item_key_uniq
    ON shop_items(item_key) WHERE item_key IS NOT NULL;
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: duplicate shop_items.item_key values present (%)', SQLERRM; END $$;


-- ============================================================================
--   6. FOREIGN KEYS
--   6a. The ones PostgREST needs for the embeds app.py performs.
-- ============================================================================
SELECT cipher_ensure_fk('messages',                 'conversation_id', 'conversations', 'id', 'CASCADE');
SELECT cipher_ensure_fk('messages',                 'sender_id',       'users',         'id', 'SET NULL');
SELECT cipher_ensure_fk('conversation_members',     'conversation_id', 'conversations', 'id', 'CASCADE');
SELECT cipher_ensure_fk('conversation_members',     'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('message_reactions',        'message_id',      'messages',      'id', 'CASCADE');
SELECT cipher_ensure_fk('message_reactions',        'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('message_reads',            'message_id',      'messages',      'id', 'CASCADE');
SELECT cipher_ensure_fk('message_reads',            'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('message_warnings',         'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('typing_status',            'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('typing_status',            'conversation_id', 'conversations', 'id', 'CASCADE');
SELECT cipher_ensure_fk('recent_messages',          'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_profiles',            'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_punishments',         'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('spam_events',              'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('admin_permissions',        'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_purchases',           'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_purchases',           'item_id',         'shop_items',    'id', 'CASCADE');
SELECT cipher_ensure_fk('shard_transactions',       'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('core_transactions',        'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('affiliate_codes',          'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('affiliate_uses',           'code_id',         'affiliate_codes','id','CASCADE');
SELECT cipher_ensure_fk('notifications',            'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('spotlights',               'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('admin_applications',       'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('ask_nicely_requests',      'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('anticheat_events',         'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('nickname_changes',         'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_milestones',          'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('sorry_uses',               'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_badges',              'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('bot_message_counters',     'bot_id',          'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('user_keys',                'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('conversation_keys',        'conversation_id', 'conversations', 'id', 'CASCADE');
SELECT cipher_ensure_fk('conversation_keys',        'user_id',         'users',         'id', 'CASCADE');
SELECT cipher_ensure_fk('conversation_master_keys', 'conversation_id', 'conversations', 'id', 'CASCADE');
SELECT cipher_ensure_fk('invite_uses',              'invite_id',       'invite_links',  'id', 'CASCADE');
SELECT cipher_ensure_fk('invite_uses',              'user_id',         'users',         'id', 'CASCADE');
-- aliased embeds (`requester:requester_id(...)`, `viewer:viewer_id(...)`)
-- need an FK on the aliasing column — two FKs here are fine because app.py
-- always names the column.
SELECT cipher_ensure_fk('friendships',        'requester_id',   'users', 'id', 'CASCADE');
SELECT cipher_ensure_fk('friendships',        'addressee_id',   'users', 'id', 'CASCADE');
SELECT cipher_ensure_fk('message_access_log', 'viewer_id',      'users', 'id', 'SET NULL');
SELECT cipher_ensure_fk('message_access_log', 'target_user_id', 'users', 'id', 'SET NULL');

-- ----------------------------------------------------------------------------
--   6b. >>> FIX #2: remove PostgREST embed AMBIGUITY.
--   app.py asks for a bare `users(...)` embed on these four tables. A second
--   FK to users makes PostgREST refuse with
--     "Could not embed because more than one relationship was found"
--   which is exactly the "affiliate tab 500s for the owner" symptom.
--   The columns stay, only the referential constraint goes.
-- ----------------------------------------------------------------------------
SELECT cipher_drop_fk('affiliate_codes',     'approved_by');
SELECT cipher_drop_fk('affiliate_codes',     'rejected_by');
SELECT cipher_drop_fk('core_transactions',   'granted_by');
SELECT cipher_drop_fk('ask_nicely_requests', 'reviewed_by');
SELECT cipher_drop_fk('admin_applications',  'reviewed_by');
-- affiliate_uses has three candidate user columns and no bare embed in app.py,
-- but keeping them unlinked keeps the relationship graph unambiguous too.
SELECT cipher_drop_fk('affiliate_uses', 'new_user_id');
SELECT cipher_drop_fk('affiliate_uses', 'referrer_id');
SELECT cipher_drop_fk('affiliate_uses', 'referred_user_id');
-- same reasoning for the audit-style "who did it" columns
SELECT cipher_drop_fk('shard_transactions',  'created_by');
SELECT cipher_drop_fk('admin_permissions',   'granted_by');
SELECT cipher_drop_fk('user_badges',         'granted_by');
SELECT cipher_drop_fk('custom_badges',       'created_by');
SELECT cipher_drop_fk('conversation_keys',   'wrapped_by');
SELECT cipher_drop_fk('users',               'bot_owner_id');


-- ============================================================================
--   7. INDEXES
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_messages_conv_created     ON messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_expires          ON messages(expires_at) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_messages_sender           ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_conv_members_user         ON conversation_members(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_members_conv         ON conversation_members(conversation_id);
CREATE INDEX IF NOT EXISTS idx_reactions_message         ON message_reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_reads_message             ON message_reads(message_id);
CREATE INDEX IF NOT EXISTS idx_typing_conv               ON typing_status(conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recent_messages_user      ON recent_messages(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bans_ip                   ON bans(ip_address);
CREATE INDEX IF NOT EXISTS idx_punishments_user_active   ON user_punishments(user_id, active);
CREATE INDEX IF NOT EXISTS idx_audit_created             ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_keys_conv_user       ON conversation_keys(conversation_id, user_id);
CREATE INDEX IF NOT EXISTS idx_master_keys_conv          ON conversation_master_keys(conversation_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user        ON notifications(user_id, read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_milestones_user           ON user_milestones(user_id, bonus_key);
CREATE INDEX IF NOT EXISTS idx_anticheat_user            ON anticheat_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_asknicely_status          ON ask_nicely_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_apps_status         ON admin_applications(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_spotlights_expires        ON spotlights(expires_at);
CREATE INDEX IF NOT EXISTS idx_bot_token_hash            ON users(bot_token_hash) WHERE bot_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_is_bot              ON users(is_bot) WHERE is_bot = TRUE;
CREATE INDEX IF NOT EXISTS idx_users_username_lower      ON users(lower(username));
CREATE INDEX IF NOT EXISTS idx_friendships_requester     ON friendships(requester_id);
CREATE INDEX IF NOT EXISTS idx_friendships_addressee     ON friendships(addressee_id);
CREATE INDEX IF NOT EXISTS idx_friendships_pair          ON friendships(requester_id, addressee_id);
CREATE INDEX IF NOT EXISTS idx_core_tx_user              ON core_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shard_tx_user             ON shard_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shard_gifts_recipient     ON shard_gifts(recipient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_affiliate_code            ON affiliate_codes(code);
CREATE INDEX IF NOT EXISTS idx_affiliate_user            ON affiliate_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_uses_referrer   ON affiliate_uses(referrer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_user            ON user_purchases(user_id);
CREATE INDEX IF NOT EXISTS idx_access_log_time           ON message_access_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invite_links_code         ON invite_links(code);


-- ============================================================================
--   8. VIEW
-- ============================================================================
CREATE OR REPLACE VIEW current_spotlight AS
SELECT s.*, u.username, u.nickname_color
FROM spotlights s
JOIN users u ON u.id = s.user_id
WHERE s.expires_at > NOW()
ORDER BY s.created_at DESC
LIMIT 1;


-- ============================================================================
--   9. SEED + BACKFILL
-- ============================================================================

-- >>> FIX #8: admin_settings must have the row the app reads (id = 1).
INSERT INTO admin_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Every user needs a profile row and every staff member a permission row.
INSERT INTO user_profiles (user_id)
SELECT u.id FROM users u
WHERE NOT EXISTS (SELECT 1 FROM user_profiles p WHERE p.user_id = u.id);

INSERT INTO admin_permissions (user_id)
SELECT u.id FROM users u
WHERE (u.is_admin IS TRUE OR u.is_owner IS TRUE)
  AND NOT EXISTS (SELECT 1 FROM admin_permissions a WHERE a.user_id = u.id);

-- The owner gets every permission.
UPDATE admin_permissions ap
SET can_view_messages = TRUE, can_approve_affiliates = TRUE,
    can_create_announcements = TRUE, can_ban_ips = TRUE,
    can_suspend_ban_users = TRUE, can_reset_passwords = TRUE,
    can_manage_shop_items = TRUE, can_manage_admins = TRUE,
    updated_at = NOW()
FROM users u
WHERE u.id = ap.user_id AND u.is_owner IS TRUE;

-- Sane values for anything that slipped through NULL.
UPDATE users SET friend_privacy      = 'approval' WHERE friend_privacy IS NULL;
UPDATE users SET total_messages_sent = 0          WHERE total_messages_sent IS NULL;
UPDATE users SET shards              = 0          WHERE shards IS NULL;
UPDATE users SET cores               = 0          WHERE cores IS NULL;
UPDATE users SET nickname_color      = '#00d9ff'  WHERE nickname_color IS NULL;

-- ---- shop catalogue --------------------------------------------------------
-- Step 1: adopt rows created before item_key existed (v1.1 seeded by name
-- only). This MUST happen before the seed insert below, otherwise the two
-- collide on shop_items_item_key_uniq. One row per name, and never a key
-- that is already taken.
UPDATE shop_items si
SET item_key = m.item_key
FROM (VALUES
  ('Profile Picture','profile_picture_upload'), ('Profile Bio','profile_bio'),
  ('Banner Color','profile_banner_color'),      ('Glow Effect','effect_glow'),
  ('Sparkle Effect','effect_sparkle'),          ('Pulse Effect','effect_pulse'),
  ('Rainbow Border','effect_rainbow'),          ('Custom Bubble Color','chat_bubble_color'),
  ('Nickname Font','chat_nickname_font'),       ('Send Animation','chat_send_animation'),
  ('VIP Badge','badge_vip'),                    ('Supporter Badge','badge_supporter'),
  ('Extend Retention','perk_extend_retention'), ('Large Uploads','perk_large_uploads'),
  ('Group Icon','perk_group_icon')
) AS m(name, item_key)
WHERE si.item_key IS NULL
  AND si.name = m.name
  AND si.id = (SELECT x.id FROM shop_items x
               WHERE x.name = m.name AND x.item_key IS NULL
               ORDER BY x.created_at NULLS LAST, x.id
               LIMIT 1)
  AND NOT EXISTS (SELECT 1 FROM shop_items y WHERE y.item_key = m.item_key);

-- Step 2: insert only what is still absent, keyed by item_key.
WITH seed(name, item_key, category, price, description, icon, effect_key, sort_order, currency) AS (
  VALUES
    ('Profile Picture',     'profile_picture_upload', 'profile',    20,  'Upload a custom profile picture',              '🖼️', NULL,          10,  'shards'),
    ('Profile Bio',         'profile_bio',            'profile',    10,  'Add a short bio to your profile',              '📝', NULL,          20,  'shards'),
    ('Banner Color',        'profile_banner_color',   'profile',    15,  'Customize your profile banner color',          '🎨', NULL,          30,  'shards'),
    ('Glow Effect',         'effect_glow',            'effects',    30,  'Glowing aura around your avatar',              '✨', 'effect-glow', 40,  'shards'),
    ('Sparkle Effect',      'effect_sparkle',         'effects',    40,  'Sparkling particles around your avatar',       '🌟', 'effect-sparkle', 50, 'shards'),
    ('Pulse Effect',        'effect_pulse',           'effects',    30,  'Breathing pulse animation on your avatar',     '💓', 'effect-pulse',  60, 'shards'),
    ('Rainbow Border',      'effect_rainbow',         'effects',    50,  'Rainbow animated border on your avatar',       '🌈', 'effect-rainbow',70, 'shards'),
    ('Custom Bubble Color', 'chat_bubble_color',      'chat',       25,  'Choose a custom color for your bubbles',       '🫧', NULL,          80,  'shards'),
    ('Nickname Font',       'chat_nickname_font',     'chat',       20,  'Italic, bold or monospace for your name',      '🔤', NULL,          90,  'shards'),
    ('Send Animation',      'chat_send_animation',    'chat',       30,  'Slide, fade or bounce for sent messages',      '🎬', NULL,          100, 'shards'),
    ('VIP Badge',           'badge_vip',              'badges',     100, 'Exclusive VIP badge on your profile',          '👑', 'badge-vip',   110, 'shards'),
    ('Supporter Badge',     'badge_supporter',        'badges',     50,  'Show your support with this badge',            '💎', 'badge-supporter', 120, 'shards'),
    ('Extend Retention',    'perk_extend_retention',  'perks',      50,  'Extend all your message retention by 30 days', '⏳', NULL,          130, 'shards'),
    ('Large Uploads',       'perk_large_uploads',     'perks',      40,  'Upload images up to 10MB for 30 days',         '📦', NULL,          140, 'shards'),
    ('Group Icon',          'perk_group_icon',        'perks',      60,  'Upload a custom icon for a group chat',        '🖼️', NULL,          150, 'shards'),
    ('Extra Sorry',         'perk_extra_sorry',       'perks',      40,  '+1 weekly "Sorry, undo this" use',             '🙏', NULL,          160, 'shards'),
    ('Custom Theme Color',  'cores_theme_color',      'cores_shop', 15,  'Make the whole app your colour',               '🎨', NULL,          10,  'cores'),
    ('Animated Avatar',     'cores_gif_avatar',       'cores_shop', 10,  'An animated GIF profile picture, forever',     '🌀', NULL,          20,  'cores'),
    ('Spotlight (24h)',     'cores_spotlight_basic',  'cores_shop', 20,  'Your name on the banner for 24 hours',         '🔦', NULL,          30,  'cores'),
    ('Custom Spotlight',    'cores_spotlight_custom', 'cores_shop', 100, 'Your message on the banner for 24 hours',      '📣', NULL,          40,  'cores'),
    ('GOAT Badge',          'cores_badge_goat',       'cores_shop', 25,  'Good Guy That Does Good Stuff And Earns Well', '🐐', 'goat',        50,  'cores'),
    ('Instant Admin',       'cores_admin_instant',    'cores_shop', 50,  'Admin rights immediately. Monitored, revocable.','🛡️', NULL,        60,  'cores'),
    ('Ask The Owner',       'cores_add_by_owner',     'cores_shop', 5,   'The owner gets a friend request from you',     '📨', NULL,          70,  'cores'),
    ('Bot Key',             'cores_bot_key',          'cores_shop', 30,  'Create a bot account with an API token',       '🤖', NULL,          80,  'cores'),
    ('Group Icon Coupon',   'cores_group_room_icon',  'cores_shop', 5,   'Permanently change one group icon',            '🖼️', NULL,          90,  'cores')
)
INSERT INTO shop_items (name, item_key, category, price, description, icon, effect_key, enabled, sort_order, currency)
SELECT s.name, s.item_key, s.category, s.price, s.description, s.icon, s.effect_key, TRUE, s.sort_order, s.currency
FROM seed s
WHERE NOT EXISTS (SELECT 1 FROM shop_items si WHERE si.item_key = s.item_key);


-- ============================================================================
--  10. ROW LEVEL SECURITY
--   The app connects with the service role key, which bypasses RLS. These
--   policies exist so that a leaked anon key reads nothing.
-- ============================================================================
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','conversations','conversation_members','messages','message_reactions',
    'message_reads','message_warnings','typing_status','recent_messages','bans',
    'user_punishments','spam_events','audit_log','immunity_list','announcements',
    'invite_links','invite_uses','recovery_keys','terms_acceptance','admin_settings',
    'admin_permissions','message_access_log','user_profiles','affiliate_codes',
    'affiliate_uses','shard_transactions','shop_items','user_purchases',
    'user_keys','conversation_keys','conversation_master_keys','master_key_meta',
    'notifications','user_milestones','sorry_uses','core_transactions','shard_gifts',
    'spotlights','admin_applications','ask_nicely_requests','anticheat_events',
    'nickname_changes','custom_badges','user_badges','bot_message_counters','friendships'
  ]
  LOOP
    IF to_regclass('public.' || quote_ident(t)) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    END IF;
  END LOOP;
END $$;


-- ============================================================================
--  11. STORAGE BUCKETS
-- ============================================================================
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('cipher-avatars', 'cipher-avatars', true, 512000,
        ARRAY['image/jpeg','image/png','image/webp','image/gif'])
ON CONFLICT (id) DO UPDATE
  SET public = true, file_size_limit = 512000;

INSERT INTO storage.buckets (id, name, public)
VALUES ('cipher-vault', 'cipher-vault', false)
ON CONFLICT (id) DO UPDATE SET public = false;


-- ============================================================================
--  12. CLEAN UP THE HELPERS
--   They live in `public`, which PostgREST exposes as RPC. Remove them.
-- ============================================================================
DROP FUNCTION IF EXISTS cipher_ensure_fk(text, text, text, text, text);
DROP FUNCTION IF EXISTS cipher_drop_fk(text, text);

COMMIT;


-- ============================================================================
--  13. TELL POSTGREST TO RELOAD ITS SCHEMA CACHE
--   Without this, Supabase's API keeps serving the OLD schema for a while and
--   you will still see "Could not find the 'x' column" after a successful
--   migration. Must run OUTSIDE the transaction.
-- ============================================================================
NOTIFY pgrst, 'reload schema';
SELECT pg_notify('pgrst', 'reload schema');


-- ============================================================================
--  14. VERIFY — should return ZERO rows
--   (Same diff as QUERY 9 in 01_schema_audit.sql, trimmed to the columns this
--    script is responsible for.)
-- ============================================================================
WITH expected(table_name, column_name) AS (
  VALUES
  ('users','avatar_url'),('users','shards'),('users','cores'),
  ('users','last_no_warning_check'),('users','no_warnings_since'),
  ('users','shards_earned_this_week'),('users','week_start'),
  ('users','shards_gifted_total'),('users','can_create_invites'),
  ('users','bot_token_hash'),('users','streamer_mode'),('users','friend_privacy'),
  ('admin_settings','site_name'),('admin_settings','max_file_size_mb'),
  ('admin_settings','registration_message'),('admin_settings','bot_creation_policy'),
  ('admin_settings','global_streamer_forces'),
  ('user_profiles','active_effects'),('user_profiles','active_badges'),
  ('conversation_members','last_read_at'),
  ('messages','cipher_version'),('messages','edited_at'),
  ('message_access_log','ip'),('message_access_log','target_user_id'),
  ('user_purchases','price_paid'),
  ('shard_transactions','balance_after'),('shard_transactions','transaction_type'),
  ('shard_transactions','related_table'),('shard_transactions','related_id'),
  ('shard_transactions','created_by'),
  ('core_transactions','metadata'),('core_transactions','balance_after'),
  ('admin_permissions','can_suspend_ban_users'),('admin_permissions','can_manage_shop_items'),
  ('admin_permissions','can_manage_admins'),('admin_permissions','granted_by'),
  ('admin_permissions','updated_at'),
  ('affiliate_codes','revoked'),('affiliate_codes','total_earned'),
  ('affiliate_codes','rejected_by'),('affiliate_codes','rejection_reason'),
  ('affiliate_uses','referred_user_id'),('affiliate_uses','signup_ip'),
  ('invite_links','uses_count'),('invite_links','revoked'),
  ('recovery_keys','method'),
  ('terms_acceptance','accepted_version'),('terms_acceptance','ip'),
  ('terms_acceptance','user_agent'),
  ('shop_items','item_key'),('shop_items','currency'),('shop_items','enabled'),
  ('shop_items','sort_order'),('shop_items','icon'),('shop_items','css_class'),
  ('friendships','status'),('friendships','accepted_at'),('friendships','rejected_at'),
  ('notifications','payload'),('user_keys','encrypted_backup'),
  ('conversation_keys','wrapped_key'),('conversation_master_keys','wrapped_for'),
  ('master_key_meta','encrypted_backup'),('user_milestones','bonus_key'),
  ('sorry_uses','warning_reverted'),('spotlights','tier'),
  ('admin_applications','what_would_you_do'),('ask_nicely_requests','reply_message'),
  ('anticheat_events','event_type'),('nickname_changes','new_username'),
  ('custom_badges','purchasable'),('user_badges','badge_key'),
  ('bot_message_counters','minute_bucket'),('shard_gifts','recipient_id'),
  ('invite_uses','invite_id')
),
actual AS (
  SELECT c.relname AS table_name, a.attname AS column_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
  WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
    AND a.attnum > 0 AND NOT a.attisdropped
)
SELECT 'STILL MISSING' AS problem, e.table_name, e.column_name
FROM expected e
WHERE NOT EXISTS (SELECT 1 FROM actual a
                  WHERE a.table_name = e.table_name AND a.column_name = e.column_name)
UNION ALL
SELECT 'STILL AMBIGUOUS (users embed)', (con.conrelid::regclass)::text, 'n_fks=' || count(*)::text
FROM pg_constraint con
JOIN pg_class c     ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND con.contype = 'f' AND con.confrelid = 'public.users'::regclass
  AND (con.conrelid::regclass)::text IN
      ('affiliate_codes','core_transactions','ask_nicely_requests','admin_applications',
       'spotlights','anticheat_events','spam_events','conversation_members',
       'message_reactions','message_reads','typing_status','user_purchases',
       'shard_transactions','user_milestones')
GROUP BY 2
HAVING count(*) > 1
ORDER BY 1, 2, 3;
