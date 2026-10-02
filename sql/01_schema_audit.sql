-- ============================================================================
--   CIPHER — SCHEMA AUDIT / FULL DATABASE DUMP  (read-only, 100% safe)
-- ============================================================================
--   This file NEVER writes. Run it in the Supabase SQL editor
--   (Dashboard -> SQL Editor -> New query) and paste the result back.
--
--   HOW TO USE
--   ----------
--   Run QUERY 1 on its own. It returns ONE row with ONE column containing a
--   single pretty-printed JSON document describing the entire `public`
--   schema: every table, every column (type / nullability / default /
--   identity / comment), every primary key, foreign key, unique and check
--   constraint, every index, row counts, RLS state, views, triggers,
--   functions, sequences and extensions.
--   Click the cell, copy it, and that is the whole answer.
--
--   QUERIES 2..9 are the same information in small human-readable tables,
--   plus a few Cipher-specific checks. Run them individually if the JSON
--   blob is too big to copy out of the editor in one go.
--
--   QUERY 9 is the important one: it diffs your live database against the
--   exact schema app.py expects and prints every missing table/column.
-- ============================================================================


-- ============================================================================
--   QUERY 1 — EVERYTHING, AS ONE JSON DOCUMENT
--   Run this alone. Copy the single cell it returns.
-- ============================================================================
SELECT jsonb_pretty(jsonb_build_object(

  'generated_at',     now(),
  'database',         current_database(),
  'postgres_version', version(),
  'search_path',      current_setting('search_path'),

  ----------------------------------------------------------------------------
  -- Installed extensions
  ----------------------------------------------------------------------------
  'extensions', COALESCE((
    SELECT jsonb_agg(jsonb_build_object('name', e.extname, 'version', e.extversion)
                     ORDER BY e.extname)
    FROM pg_extension e
  ), '[]'::jsonb),

  ----------------------------------------------------------------------------
  -- Schemas that are visible (so we can see if anything lives outside public)
  ----------------------------------------------------------------------------
  'schemas', COALESCE((
    SELECT jsonb_agg(n.nspname ORDER BY n.nspname)
    FROM pg_namespace n
    WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  ), '[]'::jsonb),

  ----------------------------------------------------------------------------
  -- Every table / partitioned table / foreign table in public, in full detail
  ----------------------------------------------------------------------------
  'tables', COALESCE((
    SELECT jsonb_agg(t ORDER BY t->>'table')
    FROM (
      SELECT jsonb_build_object(
        'table',            c.relname,
        'kind',             CASE c.relkind WHEN 'r' THEN 'table'
                                           WHEN 'p' THEN 'partitioned table'
                                           WHEN 'f' THEN 'foreign table'
                                           ELSE c.relkind::text END,
        'owner',            pg_get_userbyid(c.relowner),
        'comment',          obj_description(c.oid, 'pg_class'),
        'rls_enabled',      c.relrowsecurity,
        'rls_forced',       c.relforcerowsecurity,
        'has_triggers',     c.relhastriggers,
        'estimated_rows',   c.reltuples::bigint,   -- exact counts: see QUERY 2b
        'total_size',       pg_size_pretty(pg_total_relation_size(c.oid)),

        -- ---------------- columns ----------------
        'columns', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
                   'n',            a.attnum,
                   'column',       a.attname,
                   'type',         format_type(a.atttypid, a.atttypmod),
                   'nullable',     NOT a.attnotnull,
                   'default',      pg_get_expr(ad.adbin, ad.adrelid),
                   'identity',     CASE a.attidentity WHEN 'a' THEN 'always'
                                                      WHEN 'd' THEN 'by default'
                                                      ELSE NULL END,
                   'generated',    CASE a.attgenerated WHEN 's' THEN 'stored' ELSE NULL END,
                   'collation',    co.collname,
                   'comment',      col_description(a.attrelid, a.attnum)
                 ) ORDER BY a.attnum)
          FROM pg_attribute a
          LEFT JOIN pg_attrdef  ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
          LEFT JOIN pg_collation co ON co.oid = a.attcollation
                                   AND co.collname <> 'default'
          WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        ), '[]'::jsonb),

        -- ---------------- constraints ----------------
        'primary_key', (
          SELECT jsonb_build_object('name', con.conname,
                                    'definition', pg_get_constraintdef(con.oid))
          FROM pg_constraint con
          WHERE con.conrelid = c.oid AND con.contype = 'p'
          LIMIT 1
        ),
        'foreign_keys', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
                   'name',       con.conname,
                   'definition', pg_get_constraintdef(con.oid),
                   'references', (con.confrelid::regclass)::text
                 ) ORDER BY con.conname)
          FROM pg_constraint con
          WHERE con.conrelid = c.oid AND con.contype = 'f'
        ), '[]'::jsonb),
        'unique_constraints', COALESCE((
          SELECT jsonb_agg(jsonb_build_object('name', con.conname,
                                              'definition', pg_get_constraintdef(con.oid))
                           ORDER BY con.conname)
          FROM pg_constraint con
          WHERE con.conrelid = c.oid AND con.contype = 'u'
        ), '[]'::jsonb),
        'check_constraints', COALESCE((
          SELECT jsonb_agg(jsonb_build_object('name', con.conname,
                                              'definition', pg_get_constraintdef(con.oid))
                           ORDER BY con.conname)
          FROM pg_constraint con
          WHERE con.conrelid = c.oid AND con.contype = 'c'
        ), '[]'::jsonb),

        -- ---------------- indexes ----------------
        'indexes', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
                   'name',       i.indexname,
                   'definition', i.indexdef,
                   'size',       pg_size_pretty(pg_relation_size((quote_ident(i.schemaname)
                                                                 ||'.'||quote_ident(i.indexname))::regclass))
                 ) ORDER BY i.indexname)
          FROM pg_indexes i
          WHERE i.schemaname = n.nspname AND i.tablename = c.relname
        ), '[]'::jsonb),

        -- ---------------- RLS policies ----------------
        'policies', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
                   'name',       p.polname,
                   'command',    CASE p.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                                               WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE'
                                               ELSE 'ALL' END,
                   'permissive', p.polpermissive,
                   'roles',      (SELECT jsonb_agg(pg_get_userbyid(r))
                                  FROM unnest(COALESCE(p.polroles, '{}')) AS r),
                   'using',      pg_get_expr(p.polqual, p.polrelid),
                   'with_check', pg_get_expr(p.polwithcheck, p.polrelid)
                 ) ORDER BY p.polname)
          FROM pg_policy p WHERE p.polrelid = c.oid
        ), '[]'::jsonb),

        -- ---------------- triggers ----------------
        'triggers', COALESCE((
          SELECT jsonb_agg(jsonb_build_object('name', tg.tgname,
                                              'definition', pg_get_triggerdef(tg.oid))
                           ORDER BY tg.tgname)
          FROM pg_trigger tg
          WHERE tg.tgrelid = c.oid AND NOT tg.tgisinternal
        ), '[]'::jsonb)

      ) AS t
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r','p','f')
    ) s
  ), '[]'::jsonb),

  ----------------------------------------------------------------------------
  -- Views and materialized views
  ----------------------------------------------------------------------------
  'views', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
             'name',       c.relname,
             'kind',       CASE c.relkind WHEN 'v' THEN 'view' ELSE 'materialized view' END,
             'columns',    (SELECT jsonb_agg(a.attname ORDER BY a.attnum)
                            FROM pg_attribute a
                            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
             'definition', pg_get_viewdef(c.oid, true)
           ) ORDER BY c.relname)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('v','m')
  ), '[]'::jsonb),

  ----------------------------------------------------------------------------
  -- Sequences, functions, enums
  ----------------------------------------------------------------------------
  'sequences', COALESCE((
    SELECT jsonb_agg(jsonb_build_object('name', c.relname,
                                        'owned_by', (SELECT (d.refobjid::regclass)::text
                                                     FROM pg_depend d
                                                     WHERE d.objid = c.oid AND d.deptype = 'a'
                                                     LIMIT 1))
                     ORDER BY c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'S'
  ), '[]'::jsonb),

  'functions', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
             'name',      p.proname,
             'args',      pg_get_function_identity_arguments(p.oid),
             'returns',   pg_get_function_result(p.oid),
             'language',  l.lanname,
             'security_definer', p.prosecdef
           ) ORDER BY p.proname)
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language  l ON l.oid = p.prolang
    WHERE n.nspname = 'public'
  ), '[]'::jsonb),

  'enum_types', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
             'name',   t.typname,
             'values', (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                        FROM pg_enum e WHERE e.enumtypid = t.oid)
           ) ORDER BY t.typname)
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype = 'e'
  ), '[]'::jsonb)

)) AS cipher_schema_report;


-- ============================================================================
--   QUERY 2 — TABLE INVENTORY (one row per table)
-- ============================================================================
SELECT
  c.relname                                            AS table_name,
  c.reltuples::bigint                                  AS estimated_rows,
  pg_size_pretty(pg_total_relation_size(c.oid))        AS total_size,
  c.relrowsecurity                                     AS rls_enabled,
  (SELECT count(*) FROM pg_attribute a
   WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS n_columns,
  (SELECT count(*) FROM pg_constraint k
   WHERE k.conrelid = c.oid AND k.contype = 'f')       AS n_foreign_keys,
  (SELECT count(*) FROM pg_indexes i
   WHERE i.schemaname = 'public' AND i.tablename = c.relname) AS n_indexes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
ORDER BY c.relname;


-- ============================================================================
--   QUERY 2b — EXACT ROW COUNTS
--   Run this; it prints a second query. Copy that output and run it.
--   (Two steps because counting every table needs generated SQL.)
-- ============================================================================
SELECT string_agg(
         format('SELECT %L::text AS table_name, count(*) AS exact_rows FROM public.%I',
                c.relname, c.relname),
         E'\nUNION ALL ' ORDER BY c.relname) || E'\nORDER BY 1;' AS run_this_next
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p');


-- ============================================================================
--   QUERY 3 — EVERY COLUMN OF EVERY TABLE
-- ============================================================================
SELECT
  c.relname                                   AS table_name,
  a.attnum                                    AS pos,
  a.attname                                   AS column_name,
  format_type(a.atttypid, a.atttypmod)        AS data_type,
  NOT a.attnotnull                            AS is_nullable,
  pg_get_expr(ad.adbin, ad.adrelid)           AS column_default,
  CASE a.attidentity WHEN 'a' THEN 'always' WHEN 'd' THEN 'by default' END AS identity,
  CASE a.attgenerated WHEN 's' THEN 'stored' END                          AS generated,
  col_description(a.attrelid, a.attnum)       AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum;


-- ============================================================================
--   QUERY 4 — EVERY CONSTRAINT (PK / FK / UNIQUE / CHECK / EXCLUDE)
-- ============================================================================
SELECT
  (con.conrelid::regclass)::text AS table_name,
  con.conname                    AS constraint_name,
  CASE con.contype WHEN 'p' THEN 'PRIMARY KEY' WHEN 'f' THEN 'FOREIGN KEY'
                   WHEN 'u' THEN 'UNIQUE'      WHEN 'c' THEN 'CHECK'
                   WHEN 'x' THEN 'EXCLUDE'     ELSE con.contype::text END AS constraint_type,
  pg_get_constraintdef(con.oid)  AS definition,
  con.convalidated               AS validated
FROM pg_constraint con
JOIN pg_class c      ON c.oid = con.conrelid
JOIN pg_namespace n  ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
ORDER BY table_name, constraint_type, constraint_name;


-- ============================================================================
--   QUERY 5 — EVERY INDEX
-- ============================================================================
SELECT schemaname, tablename, indexname,
       pg_size_pretty(pg_relation_size((quote_ident(schemaname)||'.'||quote_ident(indexname))::regclass)) AS size,
       indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;


-- ============================================================================
--   QUERY 6 — RLS STATE + POLICIES
-- ============================================================================
SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled,
       c.relforcerowsecurity AS rls_forced,
       COALESCE(p.polname, '(no policies)') AS policy_name,
       CASE p.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                     WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' ELSE 'ALL' END AS command,
       pg_get_expr(p.polqual, p.polrelid)      AS using_expr,
       pg_get_expr(p.polwithcheck, p.polrelid) AS with_check_expr
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid
WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
ORDER BY c.relname, policy_name;


-- ============================================================================
--   QUERY 7 — STORAGE BUCKETS  (Cipher needs cipher-avatars + cipher-vault)
-- ============================================================================
SELECT id, name, public, file_size_limit, allowed_mime_types, created_at
FROM storage.buckets
ORDER BY id;

-- Object counts per bucket (can be slow on big buckets):
-- SELECT bucket_id, count(*) FROM storage.objects GROUP BY bucket_id;


-- ============================================================================
--   QUERY 8 — POSTGREST EMBED AMBIGUITY DETECTOR
--   app.py does embeds like  .select("*,users(username)")
--   If a table has MORE THAN ONE foreign key pointing at the same table,
--   PostgREST cannot resolve a bare `users(...)` embed and returns
--   "Could not embed because more than one relationship was found"
--   -> the endpoint 500s or silently returns an empty list.
--   Every row this query returns is a bug unless app.py disambiguates
--   with an alias (e.g. `viewer:viewer_id(...)`).
-- ============================================================================
SELECT
  (con.conrelid::regclass)::text  AS child_table,
  (con.confrelid::regclass)::text AS parent_table,
  count(*)                        AS n_relationships,
  string_agg(con.conname || ' (' || pg_get_constraintdef(con.oid) || ')', E'\n'
             ORDER BY con.conname) AS relationships
FROM pg_constraint con
JOIN pg_class c     ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND con.contype = 'f'
GROUP BY 1, 2
HAVING count(*) > 1
ORDER BY 1, 2;


-- ============================================================================
--   QUERY 9 — THE DIFF: WHAT app.py NEEDS vs WHAT YOU HAVE
--   This is the query that actually answers "what is broken".
--   It returns one row per missing table and one row per missing column.
--   If it returns zero rows, your schema is complete.
-- ============================================================================
WITH expected(table_name, column_name) AS (
  VALUES
  -- ---------- core ----------
  ('users','id'),('users','username'),('users','password_hash'),('users','recovery_phrase'),
  ('users','is_owner'),('users','is_admin'),('users','created_at'),('users','last_seen'),
  ('users','last_ip'),('users','spam_warnings'),('users','throttle_level'),('users','throttle_until'),
  ('users','last_warning_at'),('users','can_send_messages'),('users','totp_enabled'),('users','totp_secret'),
  ('users','keep_all_forever'),('users','notify_before_delete'),('users','nickname_color'),
  ('users','theme_color'),('users','avatar_url'),('users','anonymous_mode'),('users','suspended'),
  ('users','suspended_until'),('users','suspension_reason'),('users','bubble_color'),
  ('users','name_font'),('users','msg_animation'),('users','shards'),('users','leaderboard_opt_out'),
  ('users','can_create_invites'),('users','cores'),('users','can_grant_cores'),
  ('users','total_messages_sent'),('users','last_daily_claim'),('users','sorry_uses_this_week'),
  ('users','sorry_week_start'),('users','friend_privacy'),('users','streamer_mode'),
  ('users','nickname_changes_this_hour'),('users','nickname_change_window_start'),
  ('users','tos_bonus_claimed'),('users','cheat_warnings'),('users','ask_nicely_banned'),
  ('users','is_bot'),('users','bot_token_hash'),('users','bot_owner_id'),('users','bot_is_active'),
  ('users','admin_via_purchase'),('users','admin_purchased_at'),('users','shards_earned_this_week'),
  ('users','week_start'),('users','shards_gifted_total'),('users','last_no_warning_check'),
  ('users','no_warnings_since'),
  ('admin_settings','id'),('admin_settings','site_name'),('admin_settings','max_file_size_mb'),
  ('admin_settings','signups_enabled'),('admin_settings','invites_enabled'),
  ('admin_settings','invite_creation_mode'),('admin_settings','maintenance_mode'),
  ('admin_settings','registration_message'),('admin_settings','default_retention_days'),
  ('admin_settings','shards_per_referral'),('admin_settings','affiliate_mode'),
  ('admin_settings','anti_cheat_enabled'),('admin_settings','ask_nicely_enabled'),
  ('admin_settings','ask_nicely_chance'),('admin_settings','sorry_button_enabled'),
  ('admin_settings','admins_can_grant_shards'),('admin_settings','admins_can_grant_cores'),
  ('admin_settings','admin_core_grant_max'),('admin_settings','admins_can_approve_asks'),
  ('admin_settings','admin_ask_grant_amounts'),('admin_settings','admins_can_grant_custom_ask'),
  ('admin_settings','global_streamer_forces'),('admin_settings','bot_creation_policy'),
  ('user_profiles','user_id'),('user_profiles','bio'),('user_profiles','avatar_url'),
  ('user_profiles','banner_color'),('user_profiles','active_effects'),('user_profiles','active_badges'),
  ('user_profiles','active_bubble_color'),('user_profiles','active_nickname_font'),
  ('user_profiles','active_message_animation'),
  ('conversations','id'),('conversations','name'),('conversations','is_group'),
  ('conversations','created_at'),('conversations','updated_at'),('conversations','created_by'),
  ('conversations','keep_forever'),('conversations','icon_url'),
  ('conversation_members','id'),('conversation_members','conversation_id'),
  ('conversation_members','user_id'),('conversation_members','is_group_admin'),
  ('conversation_members','muted'),('conversation_members','joined_at'),
  ('conversation_members','last_read_at'),
  ('messages','id'),('messages','conversation_id'),('messages','sender_id'),('messages','content'),
  ('messages','image_url'),('messages','created_at'),('messages','expires_at'),('messages','deleted'),
  ('messages','is_anonymous'),('messages','warning_sent'),('messages','edited_at'),
  ('messages','cipher_version'),
  ('message_reactions','id'),('message_reactions','message_id'),('message_reactions','user_id'),
  ('message_reactions','emoji'),('message_reactions','created_at'),
  ('message_reads','id'),('message_reads','message_id'),('message_reads','user_id'),('message_reads','read_at'),
  ('message_warnings','id'),('message_warnings','user_id'),('message_warnings','conversation_id'),
  ('message_warnings','dismissed'),
  ('typing_status','id'),('typing_status','user_id'),('typing_status','conversation_id'),
  ('typing_status','started_at'),
  ('recent_messages','id'),('recent_messages','user_id'),('recent_messages','content_hash'),
  ('recent_messages','created_at'),
  -- ---------- moderation ----------
  ('bans','id'),('bans','ip_address'),('bans','reason'),('bans','banned_by'),('bans','created_at'),('bans','expires_at'),
  ('user_punishments','id'),('user_punishments','user_id'),('user_punishments','punished_by'),
  ('user_punishments','type'),('user_punishments','reason'),('user_punishments','created_at'),
  ('user_punishments','expires_at'),('user_punishments','active'),
  ('spam_events','id'),('spam_events','user_id'),('spam_events','message_count'),
  ('spam_events','trigger_reason'),('spam_events','warning_number'),('spam_events','created_at'),
  ('audit_log','id'),('audit_log','admin_id'),('audit_log','admin_username'),('audit_log','action'),
  ('audit_log','target_type'),('audit_log','target_id'),('audit_log','details'),
  ('audit_log','ip_address'),('audit_log','created_at'),
  ('immunity_list','id'),('immunity_list','username'),('immunity_list','added_by'),('immunity_list','created_at'),
  ('announcements','id'),('announcements','title'),('announcements','content'),('announcements','priority'),
  ('announcements','created_by'),('announcements','created_at'),('announcements','active'),
  ('message_access_log','id'),('message_access_log','viewer_id'),('message_access_log','target_user_id'),
  ('message_access_log','conversation_id'),('message_access_log','reason'),('message_access_log','ip'),
  ('message_access_log','ip_address'),('message_access_log','created_at'),
  ('admin_permissions','user_id'),('admin_permissions','can_view_messages'),
  ('admin_permissions','can_approve_affiliates'),('admin_permissions','can_create_announcements'),
  ('admin_permissions','can_ban_ips'),('admin_permissions','can_suspend_ban_users'),
  ('admin_permissions','can_reset_passwords'),('admin_permissions','can_manage_shop_items'),
  ('admin_permissions','can_manage_admins'),('admin_permissions','granted_by'),
  ('admin_permissions','updated_at'),
  -- ---------- invites / recovery / tos ----------
  ('invite_links','id'),('invite_links','code'),('invite_links','created_by'),('invite_links','max_uses'),
  ('invite_links','uses'),('invite_links','uses_count'),('invite_links','active'),('invite_links','revoked'),
  ('invite_links','created_at'),('invite_links','expires_at'),
  ('invite_uses','id'),('invite_uses','invite_id'),('invite_uses','user_id'),('invite_uses','ip_address'),
  ('invite_uses','created_at'),
  ('recovery_keys','id'),('recovery_keys','user_id'),('recovery_keys','key_hash'),
  ('recovery_keys','method'),('recovery_keys','created_at'),
  ('terms_acceptance','user_id'),('terms_acceptance','accepted_at'),('terms_acceptance','version'),
  ('terms_acceptance','accepted_version'),('terms_acceptance','ip'),('terms_acceptance','user_agent'),
  -- ---------- economy ----------
  ('affiliate_codes','id'),('affiliate_codes','user_id'),('affiliate_codes','code'),
  ('affiliate_codes','approved'),('affiliate_codes','pending'),('affiliate_codes','reason'),
  ('affiliate_codes','approved_by'),('affiliate_codes','approved_at'),('affiliate_codes','rejected_at'),
  ('affiliate_codes','rejected_by'),('affiliate_codes','rejection_reason'),('affiliate_codes','uses'),
  ('affiliate_codes','total_earned'),('affiliate_codes','active'),('affiliate_codes','revoked'),
  ('affiliate_codes','revoked_at'),('affiliate_codes','created_at'),
  ('affiliate_uses','id'),('affiliate_uses','code_id'),('affiliate_uses','new_user_id'),
  ('affiliate_uses','referrer_id'),('affiliate_uses','referred_user_id'),
  ('affiliate_uses','shards_awarded'),('affiliate_uses','signup_ip'),('affiliate_uses','created_at'),
  ('shard_transactions','id'),('shard_transactions','user_id'),('shard_transactions','amount'),
  ('shard_transactions','balance_after'),('shard_transactions','transaction_type'),
  ('shard_transactions','description'),('shard_transactions','related_table'),
  ('shard_transactions','related_id'),('shard_transactions','created_by'),('shard_transactions','created_at'),
  ('shard_gifts','id'),('shard_gifts','sender_id'),('shard_gifts','recipient_id'),
  ('shard_gifts','amount'),('shard_gifts','message'),('shard_gifts','created_at'),
  ('core_transactions','id'),('core_transactions','user_id'),('core_transactions','amount'),
  ('core_transactions','balance_after'),('core_transactions','transaction_type'),
  ('core_transactions','description'),('core_transactions','granted_by'),
  ('core_transactions','related_type'),('core_transactions','related_id'),
  ('core_transactions','metadata'),('core_transactions','created_at'),
  ('shop_items','id'),('shop_items','name'),('shop_items','item_key'),('shop_items','category'),
  ('shop_items','price'),('shop_items','currency'),('shop_items','description'),('shop_items','icon'),
  ('shop_items','css_class'),('shop_items','effect_key'),('shop_items','enabled'),
  ('shop_items','sort_order'),('shop_items','active'),('shop_items','one_time'),
  ('shop_items','duration_days'),('shop_items','created_at'),
  ('user_purchases','id'),('user_purchases','user_id'),('user_purchases','item_id'),
  ('user_purchases','purchased_at'),('user_purchases','expires_at'),('user_purchases','price_paid'),
  ('user_purchases','equipped'),
  ('user_milestones','id'),('user_milestones','user_id'),('user_milestones','bonus_key'),
  ('user_milestones','shards_awarded'),('user_milestones','claimed_at'),
  -- ---------- v1.2 social / moderation ----------
  ('friendships','id'),('friendships','requester_id'),('friendships','addressee_id'),
  ('friendships','status'),('friendships','created_at'),('friendships','accepted_at'),
  ('friendships','rejected_at'),
  ('notifications','id'),('notifications','user_id'),('notifications','kind'),
  ('notifications','payload'),('notifications','read'),('notifications','created_at'),
  ('sorry_uses','id'),('sorry_uses','user_id'),('sorry_uses','warning_reverted'),('sorry_uses','used_at'),
  ('spotlights','id'),('spotlights','user_id'),('spotlights','message'),('spotlights','tier'),
  ('spotlights','expires_at'),('spotlights','created_at'),
  ('admin_applications','id'),('admin_applications','user_id'),('admin_applications','reason'),
  ('admin_applications','availability'),('admin_applications','what_would_you_do'),
  ('admin_applications','status'),('admin_applications','reviewed_by'),('admin_applications','reviewed_at'),
  ('admin_applications','rejection_reason'),('admin_applications','created_at'),
  ('ask_nicely_requests','id'),('ask_nicely_requests','user_id'),('ask_nicely_requests','message'),
  ('ask_nicely_requests','ip_address'),('ask_nicely_requests','status'),
  ('ask_nicely_requests','reviewed_by'),('ask_nicely_requests','reviewed_at'),
  ('ask_nicely_requests','reply_message'),('ask_nicely_requests','shards_granted'),
  ('ask_nicely_requests','created_at'),
  ('anticheat_events','id'),('anticheat_events','user_id'),('anticheat_events','event_type'),
  ('anticheat_events','details'),('anticheat_events','ip_address'),('anticheat_events','created_at'),
  ('nickname_changes','id'),('nickname_changes','user_id'),('nickname_changes','old_username'),
  ('nickname_changes','new_username'),('nickname_changes','ip_address'),('nickname_changes','created_at'),
  ('custom_badges','id'),('custom_badges','name'),('custom_badges','icon'),('custom_badges','color'),
  ('custom_badges','description'),('custom_badges','purchasable'),('custom_badges','shop_price'),
  ('custom_badges','created_by'),('custom_badges','created_at'),
  ('user_badges','id'),('user_badges','user_id'),('user_badges','badge_key'),
  ('user_badges','granted_by'),('user_badges','granted_at'),
  ('bot_message_counters','id'),('bot_message_counters','bot_id'),
  ('bot_message_counters','minute_bucket'),('bot_message_counters','count'),
  -- ---------- hypercrypt (E2EE key material) ----------
  ('user_keys','user_id'),('user_keys','public_key'),('user_keys','curve'),
  ('user_keys','encrypted_backup'),('user_keys','backup_salt'),('user_keys','backup_iv'),
  ('user_keys','backup_iters'),('user_keys','key_fingerprint'),('user_keys','created_at'),
  ('user_keys','updated_at'),
  ('conversation_keys','id'),('conversation_keys','conversation_id'),('conversation_keys','user_id'),
  ('conversation_keys','wrapped_key'),('conversation_keys','wrapped_by'),
  ('conversation_keys','key_fingerprint'),('conversation_keys','created_at'),
  ('conversation_master_keys','id'),('conversation_master_keys','conversation_id'),
  ('conversation_master_keys','wrapped_key'),('conversation_master_keys','wrapped_for'),
  ('conversation_master_keys','created_at'),
  ('master_key_meta','id'),('master_key_meta','public_key'),('master_key_meta','curve'),
  ('master_key_meta','key_fingerprint'),('master_key_meta','encrypted_backup'),
  ('master_key_meta','backup_salt'),('master_key_meta','backup_iv'),('master_key_meta','backup_iters'),
  ('master_key_meta','created_at')
),
actual AS (
  SELECT c.relname AS table_name, a.attname AS column_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
  WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v')
    AND a.attnum > 0 AND NOT a.attisdropped
),
present_tables AS (SELECT DISTINCT table_name FROM actual)
SELECT 'MISSING TABLE'  AS problem, e.table_name, NULL::text AS column_name
FROM (SELECT DISTINCT table_name FROM expected) e
WHERE e.table_name NOT IN (SELECT table_name FROM present_tables)
UNION ALL
SELECT 'MISSING COLUMN' AS problem, e.table_name, e.column_name
FROM expected e
WHERE e.table_name IN (SELECT table_name FROM present_tables)
  AND NOT EXISTS (SELECT 1 FROM actual a
                  WHERE a.table_name = e.table_name AND a.column_name = e.column_name)
ORDER BY 1, 2, 3;
