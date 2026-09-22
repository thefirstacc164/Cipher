-- ============================================================
--   CIPHER v1.2.0 — ONE-WAY MESSAGE WIPE
-- ============================================================
--   SEPARATE FILE ON PURPOSE. Do not run this by accident.
--
--   v1.2.0 seals messages in the browser, so anything already in
--   the database is old plaintext. This deletes it. There is no
--   undo: run it once, right after you run migration_v1.2.sql and
--   before (or immediately after) you deploy v1.2.0.
--
--   What it removes:
--     • every message body and every stored image reference
--     • the reaction / read-receipt / typing rows attached to them
--     • the objects in the old plaintext image bucket
--
--   What it keeps:
--     • accounts, conversations and memberships (nobody has to
--       re-add their friends or rebuild their chats)
--     • audit log, bans, punishments
-- ============================================================

BEGIN;

-- Reactions, read receipts and warnings point at messages.
DELETE FROM message_reactions;
DELETE FROM message_reads;
DELETE FROM message_warnings;
DELETE FROM typing_status;
DELETE FROM recent_messages;

-- The messages themselves.
DELETE FROM messages;

-- Nothing is left to expire or preview.
UPDATE conversations SET updated_at = NOW();

COMMIT;

-- Storage objects have to go through the Supabase API, not SQL. Either:
--   • Storage → cipher-images → delete all objects, or
--   • leave them: with the rows gone nothing links to them, and they are
--     unreachable from the app. They still sit in the bucket, so deleting
--     them properly is the honest finish.

-- Verify:
--   SELECT count(*) FROM messages;            -- 0
--   SELECT count(*) FROM message_reactions;   -- 0
