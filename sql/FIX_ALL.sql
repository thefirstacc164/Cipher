-- ============================================================================
--   CIPHER — ONE-SHOT DATABASE REPAIR          (idempotent, safe to re-run)
--   Paste the whole thing into Supabase -> SQL Editor -> Run.
--   It only ADDS and REPAIRS. It never drops a table, a column, or a row.
--   It ends by printing a report that should say "ALL GOOD".
-- ============================================================================

BEGIN;
SET LOCAL statement_timeout = '600s';
SET LOCAL lock_timeout = '30s';

-- ----------------------------------------------------------------------------
--  1. Warn about any table that is missing entirely (nothing below can fix it)
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text; miss text := '';
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','admin_settings','conversations','conversation_members','messages',
    'message_reactions','message_reads','message_warnings','typing_status',
    'recent_messages','bans','user_punishments','spam_events','audit_log',
    'immunity_list','announcements','invite_links','invite_uses','recovery_keys',
    'terms_acceptance','admin_permissions','message_access_log','user_profiles',
    'affiliate_codes','affiliate_uses','shard_transactions','shard_gifts',
    'core_transactions','shop_items','user_purchases','user_milestones',
    'friendships','notifications','sorry_uses','spotlights','admin_applications',
    'ask_nicely_requests','anticheat_events','nickname_changes','custom_badges',
    'user_badges','bot_message_counters','user_keys','conversation_keys',
    'conversation_master_keys','master_key_meta']
  LOOP
    IF to_regclass('public.'||quote_ident(t)) IS NULL THEN miss := miss||' '||t; END IF;
  END LOOP;
  IF miss <> '' THEN
    RAISE WARNING 'cipher: these tables do not exist:%  -- run sql/02_fix_schema.sql from the repo instead', miss;
  END IF;
END $$;

-- ----------------------------------------------------------------------------
--  2. EVERY COLUMN app.py READS OR WRITES
--     Driven off a list so one bad row cannot abort the run. Missing tables
--     are skipped; existing columns are left exactly as they are.
--
--     >>> This is what fixes "I can't add people to chat".
--     conversations.icon_url is in an explicit PostgREST column list in
--     list_conversations(); while it is missing, GET /api/conversations
--     returns 500, so after new_dm / new_group / add_member the client's
--     refresh fails and the chat never shows up.
-- ----------------------------------------------------------------------------
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
  ('admin_applications','availability','TEXT'),
  ('admin_applications','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('admin_applications','id','UUID DEFAULT gen_random_uuid()'), ('admin_applications','reason','TEXT'),
  ('admin_applications','rejection_reason','TEXT'), ('admin_applications','reviewed_at','TIMESTAMPTZ'),
  ('admin_applications','reviewed_by','UUID'),
  ('admin_applications','status','TEXT NOT NULL DEFAULT ''pending'''),
  ('admin_applications','user_id','UUID'), ('admin_applications','what_would_you_do','TEXT'),
  ('admin_permissions','can_approve_affiliates','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_ban_ips','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_create_announcements','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_manage_admins','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_manage_shop_items','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_reset_passwords','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_suspend_ban_users','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','can_view_messages','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_permissions','granted_by','UUID'),
  ('admin_permissions','updated_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('admin_permissions','user_id','UUID'),
  ('admin_settings','admin_ask_grant_amounts','TEXT NOT NULL DEFAULT ''20,50,100'''),
  ('admin_settings','admin_core_grant_max','INTEGER NOT NULL DEFAULT 3'),
  ('admin_settings','admins_can_approve_asks','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_settings','admins_can_grant_cores','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_settings','admins_can_grant_custom_ask','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_settings','admins_can_grant_shards','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('admin_settings','affiliate_mode','TEXT NOT NULL DEFAULT ''everyone'''),
  ('admin_settings','anti_cheat_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('admin_settings','ask_nicely_chance','DOUBLE PRECISION NOT NULL DEFAULT 0.001'),
  ('admin_settings','ask_nicely_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('admin_settings','bot_creation_policy','TEXT NOT NULL DEFAULT ''purchase_only'''),
  ('admin_settings','default_retention_days','INTEGER NOT NULL DEFAULT 7'),
  ('admin_settings','global_streamer_forces','JSONB NOT NULL DEFAULT ''{}''::jsonb'),
  ('admin_settings','id','INTEGER DEFAULT 1'),
  ('admin_settings','invite_creation_mode','TEXT NOT NULL DEFAULT ''admin'''),
  ('admin_settings','invites_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('admin_settings','maintenance_mode','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('admin_settings','max_file_size_mb','INTEGER NOT NULL DEFAULT 5'),
  ('admin_settings','registration_message','TEXT'),
  ('admin_settings','shards_per_referral','INTEGER NOT NULL DEFAULT 10'),
  ('admin_settings','signups_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('admin_settings','site_name','TEXT NOT NULL DEFAULT ''Cipher'''),
  ('admin_settings','sorry_button_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('affiliate_codes','active','BOOLEAN DEFAULT TRUE'),
  ('affiliate_codes','approved','BOOLEAN DEFAULT FALSE'), ('affiliate_codes','approved_at','TIMESTAMPTZ'),
  ('affiliate_codes','approved_by','UUID'), ('affiliate_codes','code','TEXT'),
  ('affiliate_codes','created_at','TIMESTAMPTZ DEFAULT NOW()'),
  ('affiliate_codes','id','UUID DEFAULT gen_random_uuid()'),
  ('affiliate_codes','pending','BOOLEAN DEFAULT FALSE'), ('affiliate_codes','reason','TEXT'),
  ('affiliate_codes','rejected_at','TIMESTAMPTZ'), ('affiliate_codes','rejected_by','UUID'),
  ('affiliate_codes','rejection_reason','TEXT'),
  ('affiliate_codes','revoked','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('affiliate_codes','revoked_at','TIMESTAMPTZ'),
  ('affiliate_codes','total_earned','INTEGER NOT NULL DEFAULT 0'), ('affiliate_codes','user_id','UUID'),
  ('affiliate_codes','uses','INTEGER DEFAULT 0'), ('affiliate_uses','code_id','UUID'),
  ('affiliate_uses','created_at','TIMESTAMPTZ DEFAULT NOW()'),
  ('affiliate_uses','id','UUID DEFAULT gen_random_uuid()'), ('affiliate_uses','new_user_id','UUID'),
  ('affiliate_uses','referred_user_id','UUID'), ('affiliate_uses','referrer_id','UUID'),
  ('affiliate_uses','shards_awarded','INTEGER NOT NULL DEFAULT 0'), ('affiliate_uses','signup_ip','TEXT'),
  ('announcements','active','BOOLEAN NOT NULL DEFAULT TRUE'), ('announcements','content','TEXT'),
  ('announcements','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('announcements','created_by','UUID'),
  ('announcements','id','UUID DEFAULT gen_random_uuid()'),
  ('announcements','priority','TEXT DEFAULT ''normal'''), ('announcements','title','TEXT'),
  ('anticheat_events','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('anticheat_events','details','TEXT'), ('anticheat_events','event_type','TEXT'),
  ('anticheat_events','id','UUID DEFAULT gen_random_uuid()'), ('anticheat_events','ip_address','TEXT'),
  ('anticheat_events','user_id','UUID'),
  ('ask_nicely_requests','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('ask_nicely_requests','id','UUID DEFAULT gen_random_uuid()'), ('ask_nicely_requests','ip_address','TEXT'),
  ('ask_nicely_requests','message','TEXT'), ('ask_nicely_requests','reply_message','TEXT'),
  ('ask_nicely_requests','reviewed_at','TIMESTAMPTZ'), ('ask_nicely_requests','reviewed_by','UUID'),
  ('ask_nicely_requests','shards_granted','INTEGER'),
  ('ask_nicely_requests','status','TEXT NOT NULL DEFAULT ''pending'''),
  ('ask_nicely_requests','user_id','UUID'), ('audit_log','action','TEXT'), ('audit_log','admin_id','UUID'),
  ('audit_log','admin_username','TEXT'), ('audit_log','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('audit_log','details','TEXT'), ('audit_log','id','UUID DEFAULT gen_random_uuid()'),
  ('audit_log','ip_address','TEXT'), ('audit_log','target_id','TEXT'), ('audit_log','target_type','TEXT'),
  ('bans','banned_by','UUID'), ('bans','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('bans','expires_at','TIMESTAMPTZ'), ('bans','id','UUID DEFAULT gen_random_uuid()'),
  ('bans','ip_address','TEXT'), ('bans','reason','TEXT'), ('bot_message_counters','bot_id','UUID'),
  ('bot_message_counters','count','INTEGER NOT NULL DEFAULT 0'),
  ('bot_message_counters','id','UUID DEFAULT gen_random_uuid()'),
  ('bot_message_counters','minute_bucket','BIGINT'), ('conversation_keys','conversation_id','UUID'),
  ('conversation_keys','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('conversation_keys','id','UUID DEFAULT gen_random_uuid()'),
  ('conversation_keys','key_fingerprint','TEXT'), ('conversation_keys','user_id','UUID'),
  ('conversation_keys','wrapped_by','UUID'), ('conversation_keys','wrapped_key','TEXT'),
  ('conversation_master_keys','conversation_id','UUID'),
  ('conversation_master_keys','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('conversation_master_keys','id','UUID DEFAULT gen_random_uuid()'),
  ('conversation_master_keys','wrapped_for','TEXT NOT NULL DEFAULT ''master'''),
  ('conversation_master_keys','wrapped_key','TEXT'), ('conversation_members','conversation_id','UUID'),
  ('conversation_members','id','UUID DEFAULT gen_random_uuid()'),
  ('conversation_members','is_group_admin','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('conversation_members','joined_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('conversation_members','last_read_at','TIMESTAMPTZ'),
  ('conversation_members','muted','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('conversation_members','user_id','UUID'),
  ('conversations','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('conversations','created_by','UUID'),
  ('conversations','icon_url','TEXT'), ('conversations','id','UUID DEFAULT gen_random_uuid()'),
  ('conversations','is_group','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('conversations','keep_forever','BOOLEAN NOT NULL DEFAULT FALSE'), ('conversations','name','TEXT'),
  ('conversations','updated_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('core_transactions','amount','INTEGER'), ('core_transactions','balance_after','INTEGER'),
  ('core_transactions','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('core_transactions','description','TEXT'), ('core_transactions','granted_by','UUID'),
  ('core_transactions','id','UUID DEFAULT gen_random_uuid()'), ('core_transactions','metadata','JSONB'),
  ('core_transactions','related_id','TEXT'), ('core_transactions','related_type','TEXT'),
  ('core_transactions','transaction_type','TEXT'), ('core_transactions','user_id','UUID'),
  ('custom_badges','color','TEXT DEFAULT ''#00d9ff'''),
  ('custom_badges','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('custom_badges','created_by','UUID'),
  ('custom_badges','description','TEXT'), ('custom_badges','icon','TEXT DEFAULT ''🏅'''),
  ('custom_badges','id','UUID DEFAULT gen_random_uuid()'), ('custom_badges','name','TEXT'),
  ('custom_badges','purchasable','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('custom_badges','shop_price','INTEGER NOT NULL DEFAULT 0'), ('friendships','accepted_at','TIMESTAMPTZ'),
  ('friendships','addressee_id','UUID'), ('friendships','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('friendships','id','UUID DEFAULT gen_random_uuid()'), ('friendships','rejected_at','TIMESTAMPTZ'),
  ('friendships','requester_id','UUID'), ('friendships','status','TEXT NOT NULL DEFAULT ''accepted'''),
  ('immunity_list','added_by','UUID'), ('immunity_list','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('immunity_list','id','UUID DEFAULT gen_random_uuid()'), ('immunity_list','username','TEXT'),
  ('invite_links','active','BOOLEAN NOT NULL DEFAULT TRUE'), ('invite_links','code','TEXT'),
  ('invite_links','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('invite_links','created_by','UUID'),
  ('invite_links','expires_at','TIMESTAMPTZ'), ('invite_links','id','UUID DEFAULT gen_random_uuid()'),
  ('invite_links','max_uses','INTEGER'), ('invite_links','revoked','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('invite_links','uses','INTEGER NOT NULL DEFAULT 0'),
  ('invite_links','uses_count','INTEGER NOT NULL DEFAULT 0'),
  ('invite_uses','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('invite_uses','id','UUID DEFAULT gen_random_uuid()'), ('invite_uses','invite_id','UUID'),
  ('invite_uses','ip_address','TEXT'), ('invite_uses','user_id','UUID'),
  ('master_key_meta','backup_iters','INTEGER NOT NULL DEFAULT 210000'),
  ('master_key_meta','backup_iv','TEXT'), ('master_key_meta','backup_salt','TEXT'),
  ('master_key_meta','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('master_key_meta','curve','TEXT NOT NULL DEFAULT ''P-256'''),
  ('master_key_meta','encrypted_backup','TEXT'), ('master_key_meta','id','UUID DEFAULT gen_random_uuid()'),
  ('master_key_meta','key_fingerprint','TEXT'), ('master_key_meta','public_key','TEXT'),
  ('message_access_log','conversation_id','UUID'),
  ('message_access_log','created_at','TIMESTAMPTZ DEFAULT NOW()'),
  ('message_access_log','id','UUID DEFAULT gen_random_uuid()'), ('message_access_log','ip','TEXT'),
  ('message_access_log','ip_address','TEXT'), ('message_access_log','reason','TEXT'),
  ('message_access_log','target_user_id','UUID'), ('message_access_log','viewer_id','UUID'),
  ('message_reactions','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('message_reactions','emoji','TEXT'), ('message_reactions','id','UUID DEFAULT gen_random_uuid()'),
  ('message_reactions','message_id','UUID'), ('message_reactions','user_id','UUID'),
  ('message_reads','id','UUID DEFAULT gen_random_uuid()'), ('message_reads','message_id','UUID'),
  ('message_reads','read_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('message_reads','user_id','UUID'),
  ('message_warnings','conversation_id','UUID'),
  ('message_warnings','dismissed','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('message_warnings','id','UUID DEFAULT gen_random_uuid()'), ('message_warnings','user_id','UUID'),
  ('messages','cipher_version','INTEGER'), ('messages','content','TEXT'),
  ('messages','conversation_id','UUID'), ('messages','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('messages','deleted','BOOLEAN NOT NULL DEFAULT FALSE'), ('messages','edited_at','TIMESTAMPTZ'),
  ('messages','expires_at','TIMESTAMPTZ'), ('messages','id','UUID DEFAULT gen_random_uuid()'),
  ('messages','image_url','TEXT'), ('messages','is_anonymous','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('messages','sender_id','UUID'), ('messages','warning_sent','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('nickname_changes','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('nickname_changes','id','UUID DEFAULT gen_random_uuid()'), ('nickname_changes','ip_address','TEXT'),
  ('nickname_changes','new_username','TEXT'), ('nickname_changes','old_username','TEXT'),
  ('nickname_changes','user_id','UUID'), ('notifications','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('notifications','id','UUID DEFAULT gen_random_uuid()'), ('notifications','kind','TEXT'),
  ('notifications','payload','JSONB NOT NULL DEFAULT ''{}''::jsonb'),
  ('notifications','read','BOOLEAN NOT NULL DEFAULT FALSE'), ('notifications','user_id','UUID'),
  ('recent_messages','content_hash','TEXT'),
  ('recent_messages','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('recent_messages','id','UUID DEFAULT gen_random_uuid()'), ('recent_messages','user_id','UUID'),
  ('recovery_keys','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('recovery_keys','id','UUID DEFAULT gen_random_uuid()'), ('recovery_keys','key_hash','TEXT'),
  ('recovery_keys','method','TEXT'), ('recovery_keys','user_id','UUID'), ('shard_gifts','amount','INTEGER'),
  ('shard_gifts','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('shard_gifts','id','UUID DEFAULT gen_random_uuid()'), ('shard_gifts','message','TEXT'),
  ('shard_gifts','recipient_id','UUID'), ('shard_gifts','sender_id','UUID'),
  ('shard_transactions','amount','INTEGER'), ('shard_transactions','balance_after','INTEGER'),
  ('shard_transactions','created_at','TIMESTAMPTZ DEFAULT NOW()'),
  ('shard_transactions','created_by','UUID'), ('shard_transactions','description','TEXT'),
  ('shard_transactions','id','UUID DEFAULT gen_random_uuid()'), ('shard_transactions','related_id','TEXT'),
  ('shard_transactions','related_table','TEXT'), ('shard_transactions','transaction_type','TEXT'),
  ('shard_transactions','type','TEXT'), ('shard_transactions','user_id','UUID'),
  ('shop_items','active','BOOLEAN DEFAULT TRUE'), ('shop_items','category','TEXT'),
  ('shop_items','created_at','TIMESTAMPTZ DEFAULT NOW()'), ('shop_items','css_class','TEXT'),
  ('shop_items','currency','TEXT NOT NULL DEFAULT ''shards'''), ('shop_items','description','TEXT'),
  ('shop_items','duration_days','INTEGER'), ('shop_items','effect_key','TEXT'),
  ('shop_items','enabled','BOOLEAN NOT NULL DEFAULT TRUE'), ('shop_items','icon','TEXT'),
  ('shop_items','id','UUID DEFAULT gen_random_uuid()'), ('shop_items','item_key','TEXT'),
  ('shop_items','name','TEXT'), ('shop_items','one_time','BOOLEAN DEFAULT TRUE'),
  ('shop_items','price','INTEGER'), ('shop_items','sort_order','INTEGER NOT NULL DEFAULT 0'),
  ('sorry_uses','id','UUID DEFAULT gen_random_uuid()'),
  ('sorry_uses','used_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('sorry_uses','user_id','UUID'),
  ('sorry_uses','warning_reverted','INTEGER'),
  ('spam_events','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('spam_events','id','UUID DEFAULT gen_random_uuid()'), ('spam_events','message_count','INTEGER'),
  ('spam_events','trigger_reason','TEXT'), ('spam_events','user_id','UUID'),
  ('spam_events','warning_number','INTEGER'),
  ('spotlights','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('spotlights','expires_at','TIMESTAMPTZ'), ('spotlights','id','UUID DEFAULT gen_random_uuid()'),
  ('spotlights','message','TEXT'), ('spotlights','tier','TEXT NOT NULL DEFAULT ''basic'''),
  ('spotlights','user_id','UUID'), ('terms_acceptance','accepted_at','TIMESTAMPTZ DEFAULT NOW()'),
  ('terms_acceptance','accepted_version','TEXT'), ('terms_acceptance','ip','TEXT'),
  ('terms_acceptance','user_agent','TEXT'), ('terms_acceptance','user_id','UUID'),
  ('terms_acceptance','version','TEXT DEFAULT ''1.0'''), ('typing_status','conversation_id','UUID'),
  ('typing_status','id','UUID DEFAULT gen_random_uuid()'),
  ('typing_status','started_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('typing_status','user_id','UUID'),
  ('user_badges','badge_key','TEXT'), ('user_badges','granted_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('user_badges','granted_by','UUID'), ('user_badges','id','UUID DEFAULT gen_random_uuid()'),
  ('user_badges','user_id','UUID'), ('user_keys','backup_iters','INTEGER NOT NULL DEFAULT 210000'),
  ('user_keys','backup_iv','TEXT'), ('user_keys','backup_salt','TEXT'),
  ('user_keys','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('user_keys','curve','TEXT NOT NULL DEFAULT ''P-256'''), ('user_keys','encrypted_backup','TEXT'),
  ('user_keys','key_fingerprint','TEXT'), ('user_keys','public_key','TEXT'),
  ('user_keys','updated_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'), ('user_keys','user_id','UUID'),
  ('user_milestones','bonus_key','TEXT'),
  ('user_milestones','claimed_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('user_milestones','id','UUID DEFAULT gen_random_uuid()'),
  ('user_milestones','shards_awarded','INTEGER NOT NULL DEFAULT 0'), ('user_milestones','user_id','UUID'),
  ('user_profiles','active_badges','JSONB NOT NULL DEFAULT ''[]''::jsonb'),
  ('user_profiles','active_bubble_color','TEXT'),
  ('user_profiles','active_effects','JSONB NOT NULL DEFAULT ''[]''::jsonb'),
  ('user_profiles','active_message_animation','TEXT'), ('user_profiles','active_nickname_font','TEXT'),
  ('user_profiles','avatar_url','TEXT'), ('user_profiles','banner_color','TEXT'),
  ('user_profiles','bio','TEXT'), ('user_profiles','user_id','UUID'),
  ('user_punishments','active','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('user_punishments','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('user_punishments','expires_at','TIMESTAMPTZ'),
  ('user_punishments','id','UUID DEFAULT gen_random_uuid()'), ('user_punishments','punished_by','UUID'),
  ('user_punishments','reason','TEXT'), ('user_punishments','type','TEXT'),
  ('user_punishments','user_id','UUID'), ('user_purchases','equipped','BOOLEAN DEFAULT FALSE'),
  ('user_purchases','expires_at','TIMESTAMPTZ'), ('user_purchases','id','UUID DEFAULT gen_random_uuid()'),
  ('user_purchases','item_id','UUID'), ('user_purchases','price_paid','INTEGER'),
  ('user_purchases','purchased_at','TIMESTAMPTZ DEFAULT NOW()'), ('user_purchases','user_id','UUID'),
  ('users','admin_purchased_at','TIMESTAMPTZ'),
  ('users','admin_via_purchase','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','anonymous_mode','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','ask_nicely_banned','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','avatar_url','TEXT'),
  ('users','bot_is_active','BOOLEAN NOT NULL DEFAULT TRUE'), ('users','bot_owner_id','UUID'),
  ('users','bot_token_hash','TEXT'), ('users','bubble_color','TEXT'),
  ('users','can_create_invites','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','can_grant_cores','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','can_send_messages','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('users','cheat_warnings','INTEGER NOT NULL DEFAULT 0'), ('users','cores','INTEGER NOT NULL DEFAULT 0'),
  ('users','created_at','TIMESTAMPTZ NOT NULL DEFAULT NOW()'),
  ('users','friend_privacy','TEXT NOT NULL DEFAULT ''approval'''),
  ('users','id','UUID DEFAULT gen_random_uuid()'), ('users','is_admin','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','is_bot','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','is_owner','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','keep_all_forever','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','last_daily_claim','TIMESTAMPTZ'),
  ('users','last_ip','TEXT'), ('users','last_no_warning_check','TIMESTAMPTZ'),
  ('users','last_seen','TIMESTAMPTZ'), ('users','last_warning_at','TIMESTAMPTZ'),
  ('users','leaderboard_opt_out','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','msg_animation','TEXT'),
  ('users','name_font','TEXT'), ('users','nickname_change_window_start','TIMESTAMPTZ'),
  ('users','nickname_changes_this_hour','INTEGER NOT NULL DEFAULT 0'),
  ('users','nickname_color','TEXT DEFAULT ''#00d9ff'''), ('users','no_warnings_since','TIMESTAMPTZ'),
  ('users','notify_before_delete','BOOLEAN NOT NULL DEFAULT TRUE'), ('users','password_hash','TEXT'),
  ('users','recovery_phrase','TEXT'), ('users','shards','INTEGER NOT NULL DEFAULT 0'),
  ('users','shards_earned_this_week','INTEGER NOT NULL DEFAULT 0'),
  ('users','shards_gifted_total','INTEGER NOT NULL DEFAULT 0'),
  ('users','sorry_uses_this_week','INTEGER NOT NULL DEFAULT 0'), ('users','sorry_week_start','TIMESTAMPTZ'),
  ('users','spam_warnings','INTEGER NOT NULL DEFAULT 0'),
  ('users','streamer_mode','JSONB NOT NULL DEFAULT ''{}''::jsonb'),
  ('users','suspended','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','suspended_until','TIMESTAMPTZ'),
  ('users','suspension_reason','TEXT'), ('users','theme_color','TEXT DEFAULT ''#00d9ff'''),
  ('users','throttle_level','INTEGER NOT NULL DEFAULT 0'), ('users','throttle_until','TIMESTAMPTZ'),
  ('users','tos_bonus_claimed','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('users','total_messages_sent','INTEGER NOT NULL DEFAULT 0'),
  ('users','totp_enabled','BOOLEAN NOT NULL DEFAULT FALSE'), ('users','totp_secret','TEXT'),
  ('users','username','TEXT'), ('users','week_start','TIMESTAMPTZ')
  ) AS v(tbl, col, typ)
  LOOP
    IF to_regclass('public.'||quote_ident(r.tbl)) IS NOT NULL THEN
      BEGIN
        EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS %I %s',
                       r.tbl, r.col, r.typ);
      EXCEPTION WHEN others THEN
        RAISE NOTICE 'cipher: could not add %.% (%)', r.tbl, r.col, SQLERRM;
      END;
    END IF;
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
--  3. TYPE DRIFT
--     v1.1 created these as TEXT[]; patch2 "added" them as JSONB, which
--     ADD COLUMN IF NOT EXISTS can never do. app.py writes JSON into them.
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['active_effects','active_badges'] LOOP
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name='user_profiles'
                 AND column_name=t AND data_type='ARRAY') THEN
      EXECUTE format('ALTER TABLE user_profiles ALTER COLUMN %I DROP DEFAULT', t);
      EXECUTE format('ALTER TABLE user_profiles ALTER COLUMN %I TYPE jsonb USING COALESCE(to_jsonb(%I), ''[]''::jsonb)', t, t);
      EXECUTE format('UPDATE user_profiles SET %I = ''[]''::jsonb WHERE %I IS NULL', t, t);
      EXECUTE format('ALTER TABLE user_profiles ALTER COLUMN %I SET DEFAULT ''[]''::jsonb', t);
      RAISE NOTICE 'cipher: user_profiles.% converted text[] -> jsonb', t;
    END IF;
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
--  4. CARRY LEGACY VALUES INTO THE COLUMNS app.py ACTUALLY READS
--     Driven off a list and guarded per statement: a table that does not
--     exist, or one bad row, must not abort the whole migration.
-- ----------------------------------------------------------------------------
DO $$
DECLARE tbl text; stmt text;
BEGIN
  FOR tbl, stmt IN SELECT * FROM (VALUES
   ('shard_transactions','UPDATE shard_transactions SET transaction_type=type WHERE transaction_type IS NULL AND type IS NOT NULL'),
   ('message_access_log','UPDATE message_access_log SET ip=ip_address WHERE ip IS NULL AND ip_address IS NOT NULL'),
   ('message_access_log','UPDATE message_access_log SET ip_address=ip WHERE ip_address IS NULL AND ip IS NOT NULL'),
   ('terms_acceptance','UPDATE terms_acceptance SET accepted_version=version WHERE accepted_version IS NULL AND version IS NOT NULL'),
   ('terms_acceptance','UPDATE terms_acceptance SET version=accepted_version WHERE version IS NULL AND accepted_version IS NOT NULL'),
   ('invite_links','UPDATE invite_links SET uses_count=uses WHERE COALESCE(uses_count,0)=0 AND COALESCE(uses,0)>0'),
   ('invite_links','UPDATE invite_links SET uses=uses_count WHERE COALESCE(uses,0)=0 AND COALESCE(uses_count,0)>0'),
   ('invite_links','UPDATE invite_links SET active = NOT COALESCE(revoked,false) WHERE active IS NULL'),
   ('affiliate_uses','UPDATE affiliate_uses SET referred_user_id=new_user_id WHERE referred_user_id IS NULL AND new_user_id IS NOT NULL'),
   ('affiliate_uses','UPDATE affiliate_uses SET new_user_id=referred_user_id WHERE new_user_id IS NULL AND referred_user_id IS NOT NULL'),
   ('shop_items','UPDATE shop_items SET currency=''shards'' WHERE currency IS NULL'),
   ('friendships','UPDATE friendships SET status=''accepted'' WHERE status IS NULL'),
   ('users','UPDATE users SET friend_privacy=''approval'' WHERE friend_privacy IS NULL'),
   ('users','UPDATE users SET shards=0 WHERE shards IS NULL'),
   ('users','UPDATE users SET cores=0 WHERE cores IS NULL'),
   ('users','UPDATE users SET total_messages_sent=0 WHERE total_messages_sent IS NULL'),
   ('users','UPDATE users SET nickname_color=''#00d9ff'' WHERE nickname_color IS NULL'),
   ('messages','UPDATE messages SET deleted=false WHERE deleted IS NULL'),
   ('admin_permissions','UPDATE admin_permissions SET can_suspend_ban_users=TRUE WHERE can_suspend_users IS TRUE AND can_suspend_ban_users IS NOT TRUE'),
   ('admin_permissions','UPDATE admin_permissions SET can_manage_shop_items=TRUE WHERE can_manage_shop IS TRUE AND can_manage_shop_items IS NOT TRUE')
  ) AS v(t,q)
  LOOP
    IF to_regclass('public.'||quote_ident(tbl)) IS NOT NULL THEN
      BEGIN EXECUTE stmt; EXCEPTION WHEN others THEN NULL; END;
    END IF;
  END LOOP;
END $$;

--  5. FOREIGN KEYS
--     5a. add the ones PostgREST needs for the embeds app.py performs
--     5b. remove the DUPLICATE links to users that make a bare users(...)
--         embed ambiguous. PostgREST answers "Could not embed because more
--         than one relationship was found" -> that is the owner's affiliate
--         tab 500, the cores log, the ask-nicely queue and the admin
--         applications queue. Columns and data are kept; only the
--         constraint goes.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION cipher_ensure_fk(p_table text, p_column text,
  p_ref_table text, p_ref_col text, p_on_delete text DEFAULT 'CASCADE')
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE v_name text := left(p_table||'_'||p_column||'_fkey', 63);
BEGIN
  IF to_regclass('public.'||quote_ident(p_table)) IS NULL
     OR to_regclass('public.'||quote_ident(p_ref_table)) IS NULL THEN RETURN; END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                 AND table_name=p_table AND column_name=p_column) THEN RETURN; END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint c
             JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
             WHERE c.conrelid=('public.'||quote_ident(p_table))::regclass
               AND c.contype='f' AND array_length(c.conkey,1)=1 AND a.attname=p_column)
  THEN RETURN; END IF;
  BEGIN
    EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES public.%I(%I) ON DELETE %s',
                   p_table,v_name,p_column,p_ref_table,p_ref_col,p_on_delete);
  EXCEPTION WHEN others THEN
    BEGIN
      EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES public.%I(%I) ON DELETE %s NOT VALID',
                     p_table,v_name,p_column,p_ref_table,p_ref_col,p_on_delete);
      RAISE NOTICE 'cipher: %.% FK added NOT VALID (orphan rows)', p_table, p_column;
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'cipher: no FK on %.% (%)', p_table, p_column, SQLERRM;
    END;
  END;
END; $fn$;

CREATE OR REPLACE FUNCTION cipher_drop_fk(p_table text, p_column text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
  IF to_regclass('public.'||quote_ident(p_table)) IS NULL THEN RETURN; END IF;
  FOR r IN SELECT c.conname FROM pg_constraint c
           JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
           WHERE c.conrelid=('public.'||quote_ident(p_table))::regclass
             AND c.contype='f' AND array_length(c.conkey,1)=1 AND a.attname=p_column
  LOOP
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', p_table, r.conname);
    RAISE NOTICE 'cipher: dropped ambiguous FK %.%', p_table, p_column;
  END LOOP;
END; $fn$;

SELECT cipher_ensure_fk('messages','conversation_id','conversations','id','CASCADE');
SELECT cipher_ensure_fk('messages','sender_id','users','id','SET NULL');
SELECT cipher_ensure_fk('conversation_members','conversation_id','conversations','id','CASCADE');
SELECT cipher_ensure_fk('conversation_members','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('message_reactions','message_id','messages','id','CASCADE');
SELECT cipher_ensure_fk('message_reactions','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('message_reads','message_id','messages','id','CASCADE');
SELECT cipher_ensure_fk('message_reads','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('message_warnings','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('typing_status','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('typing_status','conversation_id','conversations','id','CASCADE');
SELECT cipher_ensure_fk('recent_messages','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_profiles','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_punishments','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('spam_events','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('admin_permissions','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_purchases','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_purchases','item_id','shop_items','id','CASCADE');
SELECT cipher_ensure_fk('shard_transactions','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('core_transactions','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('affiliate_codes','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('affiliate_uses','code_id','affiliate_codes','id','CASCADE');
SELECT cipher_ensure_fk('notifications','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('spotlights','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('admin_applications','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('ask_nicely_requests','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('anticheat_events','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('nickname_changes','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_milestones','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('sorry_uses','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_badges','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('bot_message_counters','bot_id','users','id','CASCADE');
SELECT cipher_ensure_fk('user_keys','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('conversation_keys','conversation_id','conversations','id','CASCADE');
SELECT cipher_ensure_fk('conversation_keys','user_id','users','id','CASCADE');
SELECT cipher_ensure_fk('conversation_master_keys','conversation_id','conversations','id','CASCADE');
SELECT cipher_ensure_fk('invite_uses','invite_id','invite_links','id','CASCADE');
SELECT cipher_ensure_fk('invite_uses','user_id','users','id','CASCADE');
-- these two are embedded BY COLUMN NAME in app.py, so two links are fine
SELECT cipher_ensure_fk('friendships','requester_id','users','id','CASCADE');
SELECT cipher_ensure_fk('friendships','addressee_id','users','id','CASCADE');
SELECT cipher_ensure_fk('message_access_log','viewer_id','users','id','SET NULL');
SELECT cipher_ensure_fk('message_access_log','target_user_id','users','id','SET NULL');

SELECT cipher_drop_fk('affiliate_codes','approved_by');
SELECT cipher_drop_fk('affiliate_codes','rejected_by');
SELECT cipher_drop_fk('core_transactions','granted_by');
SELECT cipher_drop_fk('ask_nicely_requests','reviewed_by');
SELECT cipher_drop_fk('admin_applications','reviewed_by');
SELECT cipher_drop_fk('affiliate_uses','new_user_id');
SELECT cipher_drop_fk('affiliate_uses','referrer_id');
SELECT cipher_drop_fk('affiliate_uses','referred_user_id');
SELECT cipher_drop_fk('shard_transactions','created_by');
SELECT cipher_drop_fk('admin_permissions','granted_by');
SELECT cipher_drop_fk('user_badges','granted_by');
SELECT cipher_drop_fk('custom_badges','created_by');
SELECT cipher_drop_fk('conversation_keys','wrapped_by');
SELECT cipher_drop_fk('users','bot_owner_id');

DROP FUNCTION IF EXISTS cipher_ensure_fk(text,text,text,text,text);
DROP FUNCTION IF EXISTS cipher_drop_fk(text,text);

-- ----------------------------------------------------------------------------
--  6. CHECK CONSTRAINTS + UNIQUENESS (each guarded on its own)
-- ----------------------------------------------------------------------------
DO $$ BEGIN ALTER TABLE users ADD CONSTRAINT users_friend_privacy_chk
  CHECK (friend_privacy IN ('open','approval','closed'));
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_affiliate_mode_chk
  CHECK (affiliate_mode IN ('everyone','requires_approval','owner_only'));
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE admin_settings ADD CONSTRAINT admin_settings_bot_policy_chk
  CHECK (bot_creation_policy IN ('purchase_only','anyone','admin','owner'));
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE shop_items ADD CONSTRAINT shop_items_currency_chk
  CHECK (currency IN ('shards','cores'));
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN CREATE UNIQUE INDEX IF NOT EXISTS shop_items_item_key_uniq
  ON shop_items(item_key) WHERE item_key IS NOT NULL;
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: duplicate shop_items.item_key rows (%)', SQLERRM; END $$;
-- one typing row per person per chat (app.py upserts into this table)
DO $$ BEGIN
  DELETE FROM typing_status a USING typing_status b
   WHERE a.ctid < b.ctid AND a.user_id = b.user_id
     AND a.conversation_id = b.conversation_id;
  CREATE UNIQUE INDEX IF NOT EXISTS typing_status_user_conv_uniq
    ON typing_status(user_id, conversation_id);
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN CREATE UNIQUE INDEX IF NOT EXISTS conversation_members_conv_user_uniq
  ON conversation_members(conversation_id, user_id);
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: duplicate conversation_members rows (%)', SQLERRM; END $$;

-- ----------------------------------------------------------------------------
--  7. INDEXES (these matter on the Supabase/Render free tier)
-- ----------------------------------------------------------------------------
DO $$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY[
   'CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages(conversation_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_messages_expires ON messages(expires_at) WHERE deleted = FALSE',
   'CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id)',
   'CREATE INDEX IF NOT EXISTS idx_conv_members_user ON conversation_members(user_id)',
   'CREATE INDEX IF NOT EXISTS idx_conv_members_conv ON conversation_members(conversation_id)',
   'CREATE INDEX IF NOT EXISTS idx_reactions_message ON message_reactions(message_id)',
   'CREATE INDEX IF NOT EXISTS idx_reads_message ON message_reads(message_id)',
   'CREATE INDEX IF NOT EXISTS idx_typing_conv ON typing_status(conversation_id, started_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_recent_messages_user ON recent_messages(user_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_bans_ip ON bans(ip_address)',
   'CREATE INDEX IF NOT EXISTS idx_punishments_user_active ON user_punishments(user_id, active)',
   'CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_conv_keys_conv_user ON conversation_keys(conversation_id, user_id)',
   'CREATE INDEX IF NOT EXISTS idx_master_keys_conv ON conversation_master_keys(conversation_id)',
   'CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_milestones_user ON user_milestones(user_id, bonus_key)',
   'CREATE INDEX IF NOT EXISTS idx_anticheat_user ON anticheat_events(user_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_asknicely_status ON ask_nicely_requests(status, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_admin_apps_status ON admin_applications(status, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_spotlights_expires ON spotlights(expires_at)',
   'CREATE INDEX IF NOT EXISTS idx_bot_token_hash ON users(bot_token_hash) WHERE bot_token_hash IS NOT NULL',
   'CREATE INDEX IF NOT EXISTS idx_users_is_bot ON users(is_bot) WHERE is_bot = TRUE',
   'CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users(lower(username))',
   'CREATE INDEX IF NOT EXISTS idx_friendships_requester ON friendships(requester_id)',
   'CREATE INDEX IF NOT EXISTS idx_friendships_addressee ON friendships(addressee_id)',
   'CREATE INDEX IF NOT EXISTS idx_core_tx_user ON core_transactions(user_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_shard_tx_user ON shard_transactions(user_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_shard_gifts_recipient ON shard_gifts(recipient_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_affiliate_code ON affiliate_codes(code)',
   'CREATE INDEX IF NOT EXISTS idx_affiliate_uses_referrer ON affiliate_uses(referrer_id, created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_purchases_user ON user_purchases(user_id)',
   'CREATE INDEX IF NOT EXISTS idx_access_log_time ON message_access_log(created_at DESC)',
   'CREATE INDEX IF NOT EXISTS idx_invite_links_code ON invite_links(code)']
  LOOP
    BEGIN EXECUTE s; EXCEPTION WHEN others THEN
      RAISE NOTICE 'cipher: index skipped (%)', SQLERRM; END;
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
--  8. SPOTLIGHT VIEW (drop+create: replacing fails if the shape changed)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  EXECUTE 'DROP VIEW IF EXISTS current_spotlight';
  EXECUTE 'CREATE VIEW current_spotlight AS
           SELECT s.*, u.username, u.nickname_color FROM spotlights s
           JOIN users u ON u.id = s.user_id
           WHERE s.expires_at > NOW() ORDER BY s.created_at DESC LIMIT 1';
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: current_spotlight skipped (%)', SQLERRM;
END $$;

-- ----------------------------------------------------------------------------
--  9. SEED + BACKFILL (each statement guarded on its own)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    INSERT INTO admin_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
  EXCEPTION WHEN others THEN RAISE NOTICE 'cipher: seed step skipped (%)', SQLERRM; END;
  BEGIN
    INSERT INTO user_profiles (user_id)
    SELECT u.id FROM users u
    WHERE NOT EXISTS (SELECT 1 FROM user_profiles p WHERE p.user_id = u.id);
  EXCEPTION WHEN others THEN RAISE NOTICE 'cipher: seed step skipped (%)', SQLERRM; END;
  BEGIN
    INSERT INTO admin_permissions (user_id)
    SELECT u.id FROM users u
    WHERE (u.is_admin IS TRUE OR u.is_owner IS TRUE)
      AND NOT EXISTS (SELECT 1 FROM admin_permissions a WHERE a.user_id = u.id);
  EXCEPTION WHEN others THEN RAISE NOTICE 'cipher: seed step skipped (%)', SQLERRM; END;
  BEGIN
    UPDATE admin_permissions ap SET
      can_view_messages=TRUE, can_approve_affiliates=TRUE, can_create_announcements=TRUE,
      can_ban_ips=TRUE, can_suspend_ban_users=TRUE, can_reset_passwords=TRUE,
      can_manage_shop_items=TRUE, can_manage_admins=TRUE, updated_at=NOW()
    FROM users u WHERE u.id = ap.user_id AND u.is_owner IS TRUE;
  EXCEPTION WHEN others THEN RAISE NOTICE 'cipher: seed step skipped (%)', SQLERRM; END;
  BEGIN
    WITH seed(name,item_key,category,price,description,icon,effect_key,sort_order,currency) AS (VALUES
     ('Profile Picture','profile_picture_upload','profile',20,'Upload a custom profile picture','PFP',NULL,10,'shards'),
     ('Profile Bio','profile_bio','profile',10,'Add a short bio to your profile','BIO',NULL,20,'shards'),
     ('Banner Color','profile_banner_color','profile',15,'Customize your profile banner colour','COL',NULL,30,'shards'),
     ('Glow Effect','effect_glow','effects',30,'Glowing aura around your avatar','GLW','effect-glow',40,'shards'),
     ('Sparkle Effect','effect_sparkle','effects',40,'Sparkling particles around your avatar','SPK','effect-sparkle',50,'shards'),
     ('Pulse Effect','effect_pulse','effects',30,'Breathing pulse animation on your avatar','PLS','effect-pulse',60,'shards'),
     ('Rainbow Border','effect_rainbow','effects',50,'Rainbow animated border on your avatar','RNB','effect-rainbow',70,'shards'),
     ('Custom Bubble Color','chat_bubble_color','chat',25,'Choose a custom colour for your bubbles','BUB',NULL,80,'shards'),
     ('Nickname Font','chat_nickname_font','chat',20,'Italic, bold or monospace for your name','FNT',NULL,90,'shards'),
     ('Send Animation','chat_send_animation','chat',30,'Slide, fade or bounce for sent messages','ANI',NULL,100,'shards'),
     ('VIP Badge','badge_vip','badges',100,'Exclusive VIP badge on your profile','VIP','badge-vip',110,'shards'),
     ('Supporter Badge','badge_supporter','badges',50,'Show your support with this badge','SUP','badge-supporter',120,'shards'),
     ('Extend Retention','perk_extend_retention','perks',50,'Extend all your message retention by 30 days','EXT',NULL,130,'shards'),
     ('Large Uploads','perk_large_uploads','perks',40,'Upload images up to 10MB for 30 days','UPL',NULL,140,'shards'),
     ('Group Icon','perk_group_icon','perks',60,'Upload a custom icon for a group chat','GRP',NULL,150,'shards'),
     ('Extra Sorry','perk_extra_sorry','perks',40,'+1 weekly Sorry undo use','SRY',NULL,160,'shards'),
     ('Custom Theme Color','cores_theme_color','cores_shop',15,'Make the whole app your colour','THM',NULL,10,'cores'),
     ('Animated Avatar','cores_gif_avatar','cores_shop',10,'An animated GIF profile picture, forever','GIF',NULL,20,'cores'),
     ('Spotlight (24h)','cores_spotlight_basic','cores_shop',20,'Your name on the banner for 24 hours','SPT',NULL,30,'cores'),
     ('Custom Spotlight','cores_spotlight_custom','cores_shop',100,'Your message on the banner for 24 hours','MSG',NULL,40,'cores'),
     ('GOAT Badge','cores_badge_goat','cores_shop',25,'Good Guy That Does Good Stuff And Earns Well','GOA','goat',50,'cores'),
     ('Instant Admin','cores_admin_instant','cores_shop',50,'Admin rights immediately. Monitored, revocable.','ADM',NULL,60,'cores'),
     ('Ask The Owner','cores_add_by_owner','cores_shop',5,'The owner gets a friend request from you','ASK',NULL,70,'cores'),
     ('Bot Key','cores_bot_key','cores_shop',30,'Create a bot account with an API token','BOT',NULL,80,'cores'),
     ('Group Icon Coupon','cores_group_room_icon','cores_shop',5,'Permanently change one group icon','ICO',NULL,90,'cores'))
    INSERT INTO shop_items (name,item_key,category,price,description,icon,effect_key,enabled,sort_order,currency)
    SELECT s.name,s.item_key,s.category,s.price,s.description,s.icon,s.effect_key,TRUE,s.sort_order,s.currency
    FROM seed s WHERE NOT EXISTS (SELECT 1 FROM shop_items si WHERE si.item_key = s.item_key);
  EXCEPTION WHEN others THEN RAISE NOTICE 'cipher: seed step skipped (%)', SQLERRM; END;
END $$;

-- 10. ROW LEVEL SECURITY (per-table guard: one unowned table must not abort)
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','conversations','conversation_members','messages','message_reactions',
    'message_reads','message_warnings','typing_status','recent_messages','bans',
    'user_punishments','spam_events','audit_log','immunity_list','announcements',
    'invite_links','invite_uses','recovery_keys','terms_acceptance','admin_settings',
    'admin_permissions','message_access_log','user_profiles','affiliate_codes',
    'affiliate_uses','shard_transactions','shop_items','user_purchases','user_keys',
    'conversation_keys','conversation_master_keys','master_key_meta','notifications',
    'user_milestones','sorry_uses','core_transactions','shard_gifts','spotlights',
    'admin_applications','ask_nicely_requests','anticheat_events','nickname_changes',
    'custom_badges','user_badges','bot_message_counters','friendships']
  LOOP
    IF to_regclass('public.'||quote_ident(t)) IS NOT NULL THEN
      BEGIN EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXCEPTION WHEN others THEN
        RAISE NOTICE 'cipher: RLS not enabled on % (%)', t, SQLERRM; END;
    END IF;
  END LOOP;
END $$;

COMMIT;
--  ^ everything above is now saved. Nothing below can undo it.

-- ----------------------------------------------------------------------------
-- 11. STORAGE BUCKETS - OUTSIDE the transaction ON PURPOSE.
--     On current Supabase projects storage.buckets is owned by
--     supabase_storage_admin and the SQL editor is often denied INSERT.
--     Inside a transaction that single "permission denied for table buckets"
--     rolls back the ENTIRE migration - which is why an earlier run can
--     appear to succeed and change nothing.
--     If it says SKIPPED, make the buckets by hand in Dashboard -> Storage:
--       cipher-avatars : public,  512000 byte limit, image/jpeg|png|webp|gif
--       cipher-vault   : private, no MIME restriction (it holds ciphertext)
-- ----------------------------------------------------------------------------
DO $$ BEGIN
  INSERT INTO storage.buckets (id,name,public,file_size_limit,allowed_mime_types)
  VALUES ('cipher-avatars','cipher-avatars',true,512000,
          ARRAY['image/jpeg','image/png','image/webp','image/gif'])
  ON CONFLICT (id) DO UPDATE SET public=true, file_size_limit=512000;
  RAISE NOTICE 'cipher: bucket cipher-avatars OK';
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: bucket cipher-avatars SKIPPED - create by hand (%)', SQLERRM;
END $$;

DO $$ BEGIN
  INSERT INTO storage.buckets (id,name,public)
  VALUES ('cipher-vault','cipher-vault',false)
  ON CONFLICT (id) DO UPDATE SET public=false;
  RAISE NOTICE 'cipher: bucket cipher-vault OK';
EXCEPTION WHEN others THEN
  RAISE NOTICE 'cipher: bucket cipher-vault SKIPPED - create by hand (%)', SQLERRM;
END $$;

-- ----------------------------------------------------------------------------
-- 12. Make the API pick the new schema up immediately
-- ----------------------------------------------------------------------------
NOTIFY pgrst, 'reload schema';
SELECT pg_notify('pgrst','reload schema');

-- ============================================================================
--  13. REPORT - this should print exactly one row: "ALL GOOD"
-- ============================================================================
WITH need(t,c) AS (VALUES
 ('users','avatar_url'),('users','shards'),('users','cores'),('users','friend_privacy'),
 ('users','streamer_mode'),('users','bot_token_hash'),('users','can_create_invites'),
 ('users','last_no_warning_check'),('users','no_warnings_since'),('users','week_start'),
 ('users','shards_earned_this_week'),('users','shards_gifted_total'),
 ('conversations','icon_url'),('conversation_members','last_read_at'),
 ('messages','cipher_version'),('messages','edited_at'),
 ('typing_status','id'),('invite_links','active'),('invite_links','uses'),
 ('invite_links','uses_count'),('invite_uses','created_at'),
 ('message_access_log','ip'),('message_access_log','ip_address'),
 ('message_access_log','target_user_id'),('terms_acceptance','version'),
 ('terms_acceptance','accepted_version'),('user_purchases','price_paid'),
 ('user_profiles','active_effects'),('user_profiles','active_badges'),
 ('shard_transactions','balance_after'),('shard_transactions','transaction_type'),
 ('shard_transactions','related_table'),('shard_transactions','related_id'),
 ('shard_transactions','created_by'),('core_transactions','metadata'),
 ('admin_permissions','can_suspend_ban_users'),('admin_permissions','can_manage_shop_items'),
 ('admin_permissions','can_manage_admins'),('admin_permissions','granted_by'),
 ('admin_permissions','updated_at'),('admin_settings','site_name'),
 ('admin_settings','max_file_size_mb'),('admin_settings','registration_message'),
 ('admin_settings','bot_creation_policy'),('affiliate_codes','revoked'),
 ('affiliate_codes','total_earned'),('affiliate_uses','referred_user_id'),
 ('shop_items','item_key'),('shop_items','currency'),('shop_items','enabled'),
 ('friendships','status'),('recovery_keys','method')),
have AS (
 SELECT c.relname t, a.attname c FROM pg_class c
 JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_attribute a ON a.attrelid=c.oid
 WHERE n.nspname='public' AND c.relkind IN ('r','p')
   AND a.attnum>0 AND NOT a.attisdropped),
miss AS (SELECT 'MISSING COLUMN' k, n.t, n.c FROM need n
         WHERE NOT EXISTS (SELECT 1 FROM have h WHERE h.t=n.t AND h.c=n.c)),
amb AS (
 SELECT 'AMBIGUOUS users EMBED' k, (con.conrelid::regclass)::text t, count(*)::text c
 FROM pg_constraint con JOIN pg_class cl ON cl.oid=con.conrelid
 JOIN pg_namespace n ON n.oid=cl.relnamespace
 WHERE n.nspname='public' AND con.contype='f'
   AND con.confrelid='public.users'::regclass
   AND (con.conrelid::regclass)::text IN
       ('affiliate_codes','core_transactions','ask_nicely_requests','admin_applications',
        'spotlights','anticheat_events','spam_events','conversation_members',
        'message_reactions','message_reads','typing_status','user_purchases',
        'shard_transactions','user_milestones','notifications','sorry_uses',
        'nickname_changes','user_badges','affiliate_uses')
 GROUP BY 2 HAVING count(*)>1),
bad AS (SELECT * FROM miss UNION ALL SELECT * FROM amb)
SELECT CASE WHEN (SELECT count(*) FROM bad)=0
            THEN 'ALL GOOD' ELSE 'NEEDS ATTENTION' END AS status,
       COALESCE(b.k,'-') AS problem,
       COALESCE(b.t,'-') AS table_name,
       COALESCE(b.c,'-') AS detail
FROM (SELECT 1) x LEFT JOIN bad b ON TRUE
ORDER BY 2,3,4;
