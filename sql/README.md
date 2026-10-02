# Cipher — database audit & repair

Two files. Run them in the **Supabase Dashboard → SQL Editor**.

| File | What it does | Writes? |
|---|---|---|
| `01_schema_audit.sql` | Dumps your entire `public` schema — every table, column, type, default, constraint, index, RLS policy, trigger, view, row count — plus a diff against what `app.py` actually needs. | **No.** Read-only. |
| `02_fix_schema.sql` | Makes the database match `app.py` exactly. Idempotent, additive, safe to re-run. | Yes, but only `CREATE`/`ADD`/`UPDATE`-to-sane-defaults. Never drops a table or a data column. |

## Order of operations

1. Run **QUERY 1** of `01_schema_audit.sql` on its own and keep the JSON. That is your "before" snapshot.
2. Run **QUERY 9** of the same file. Every row it prints is something `app.py` reads or writes that your database does not have.
3. Paste the whole of `02_fix_schema.sql` and run it. It ends by printing a verification table that should be **empty**.
4. Re-run QUERY 9. It should return 0 rows.
5. Redeploy / restart the Render service so the in-process schema cache (`load_schema_map`) is rebuilt. The script already sends `NOTIFY pgrst, 'reload schema'` for Supabase's own API layer.

## What `02_fix_schema.sql` repairs

These were found by reading `app.py` against `migration_v1.1.sql`,
`migration_v1.2.sql` and `migration_v1.2_patch2.sql`:

1. **`users.avatar_url` does not exist.** `/api/friends` and `/api/spotlight`
   ask PostgREST for `users(username,nickname_color,avatar_url)`. The friends
   endpoint catches the resulting error and returns `[]`, so the friends list
   is permanently, silently empty.
2. **PostgREST embed ambiguity.** `affiliate_codes` (3 FKs to `users`),
   `core_transactions`, `ask_nicely_requests` and `admin_applications`
   (2 each) are queried with a bare `users(...)` embed. PostgREST answers
   *"Could not embed because more than one relationship was found"* → 300/500.
   This is the "affiliate tab 500s for the owner" symptom `patch2` tried and
   failed to fix (it actually made it worse by adding `rejected_by`).
   The fix drops the **secondary** FK only; the columns and their data stay.
3. **`message_access_log`** — the app writes `ip` and `target_user_id`;
   v1.1 only created `ip_address` and no target column.
4. **`user_purchases.price_paid`** — written on every purchase, never created.
5. **`shard_transactions`** — the app writes `balance_after`,
   `transaction_type`, `related_table`, `related_id`, `created_by`;
   v1.1 created `type`/`description`. Legacy `type` values are copied over.
6. **`admin_permissions`** — `granted_by` / `updated_at` missing, and v1.1's
   `can_suspend_users` / `can_manage_shop` were never carried over to the
   names the app reads (`can_suspend_ban_users` / `can_manage_shop_items`).
7. **`user_profiles.active_effects`** was created `TEXT[]` by v1.1 and
   declared `JSONB` by patch2 — `ADD COLUMN IF NOT EXISTS` could never change
   the type, so it stayed `TEXT[]` while the app writes JSON. Converted.
8. **`admin_settings`** — missing `site_name`, `max_file_size_mb`,
   `registration_message`, and sometimes missing the `id = 1` row itself.
9. **Base (v1.0) tables** are created `IF NOT EXISTS`, so this one file can
   stand the whole database up from nothing.
10. **Missing foreign keys** that PostgREST needs for the embeds `app.py`
    performs (`messages:sender_id`, `user_purchases→shop_items`,
    `friendships` requester/addressee, `message_access_log` viewer/target, …).
11. **`NOTIFY pgrst, 'reload schema'`** at the end, so the API stops serving
    the stale schema cache the moment the script finishes.

## Verified

Both files were executed against a real PostgreSQL 16 server in three states:

* a completely empty database,
* `v1.0 → migration_v1.1.sql`,
* `v1.0 → v1.1 → v1.2 → v1.2_patch2` (including v1.2's real failures on the
  not-yet-existing `friendships` and `shop_items.item_key`),

and then re-run a second time on each. Result every time: no errors, and the
verification query returns zero rows. On the realistic
`v1.0+v1.1+v1.2+patch2` database the audit's QUERY 9 reported exactly these
14 gaps before the fix and none after:

```
admin_permissions.granted_by          admin_permissions.updated_at
admin_settings.max_file_size_mb       admin_settings.registration_message
admin_settings.site_name              message_access_log.ip
message_access_log.target_user_id     shard_transactions.balance_after
shard_transactions.created_by         shard_transactions.related_id
shard_transactions.related_table      shard_transactions.transaction_type
user_purchases.price_paid             users.avatar_url
```

## Note on `migration_v1.2_wipe_messages.sql`

That file is unrelated to this repair and is **destructive** — it deletes every
message. `02_fix_schema.sql` never touches message data.
