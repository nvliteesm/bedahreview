-- BedahReview: tables for accounts, usage events, each seller's fixes list and a 60-day history of analyses. Paste this whole file into the Supabase SQL editor and run it.
-- Safe to run twice. It only creates things in the "public" schema and never touches auth.users.
--
-- What is NOT stored here, on purpose: review text, reviewer names, IP addresses.
-- The email address lives only in Supabase Auth.
-- The one place a product name, product link and the seller's own short note ARE stored is `fixes` (the seller's own "mark as fixed" list),
-- so it can be opened from another phone. That is a deliberate product choice; the privacy page says so.
-- `history` (Riwayat) also holds the product name the seller typed, next to counts only, for 60 days.
--
-- Access model: the browser (role "authenticated") may read its own rows and edit its own profile.
-- Events are written only by our server with the secret key (role "service_role"), never by the browser.
-- "Automatically expose new tables" is switched off in this project, so every grant below is explicit.

-- ---------- profiles: one optional row per account, filled in on the intro screen ----------
create table if not exists public.profiles (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  platform   text check (platform in ('tokopedia', 'lazada', 'tiktokshop', 'shopee', 'other')),
  shop_url   text check (char_length(shop_url) <= 300),
  category   text check (char_length(category) <= 80),
  lang       text check (lang in ('id', 'en'))
);

-- ---------- events: the fixed list in src/bedahreview/events.py, one row per event ----------
-- Keep the event names in sync with EVENTS and SERVER_EVENTS in events.py (a test compares them). The last two are written by the server when an analysis fails or hits a limit.
create table if not exists public.events (
  id      bigint generated always as identity primary key,
  ts      timestamptz not null default now(),
  user_id uuid not null references auth.users (id) on delete cascade,
  event   text not null check (event in ('opened', 'analysed', 'tab_viewed', 'draft_copied', 'fix_marked', 'rivals_started', 'feedback', 'free_unlocked', 'history_opened', 'analysis_failed', 'limit_hit')),
  props   jsonb not null default '{}'::jsonb check (jsonb_typeof(props) = 'object' and octet_length(props::text) <= 2000)
);
-- A project made before analysis_failed and limit_hit existed has the old list: replace it (safe to repeat).
alter table public.events drop constraint if exists events_event_check;
alter table public.events add constraint events_event_check check (event in ('opened', 'analysed', 'tab_viewed', 'draft_copied', 'fix_marked', 'rivals_started', 'feedback', 'free_unlocked', 'history_opened', 'analysis_failed', 'limit_hit'));
create index if not exists events_user_ts_idx on public.events (user_id, ts desc);
create index if not exists events_ts_idx on public.events (ts desc);

-- ---------- fixes: the seller's own "mark as fixed" list, so it follows them to another phone ----------
-- Same shape as the record kept in the browser. "product" is the link (or lower-cased title) used to match later analyses of the same product.
create table if not exists public.fixes (
  user_id       uuid not null references auth.users (id) on delete cascade,
  id            text not null check (char_length(id) between 1 and 40),
  updated_at    timestamptz not null default now(),
  theme         text not null check (char_length(theme) between 1 and 80),
  variant       text not null default '' check (char_length(variant) <= 60),
  product       text not null default '' check (char_length(product) <= 300),
  product_label text not null default '' check (char_length(product_label) <= 150),
  note          text not null default '' check (char_length(note) <= 200),
  fixed_on      date not null,
  before_count  integer not null check (before_count between 0 and 100000),
  before_total  integer not null check (before_total between 0 and 100000),
  before_sig    text not null default '' check (char_length(before_sig) <= 40),
  after_count   integer check (after_count between 0 and 100000),
  after_total   integer check (after_total between 0 and 100000),
  after_sig     text check (char_length(after_sig) <= 40),
  after_on      date,
  check_on      date,
  remind        text not null default 'none' check (remind in ('whatsapp', 'calendar', 'none')),
  primary key (user_id, id),
  check (before_count <= before_total),
  check ((after_count is null and after_total is null) or (after_count is not null and after_total is not null and after_count <= after_total))
);
-- Added with today's plan (2026-11): when the seller wants to look again, and how they asked to be reminded. A project made before has the table without them.
alter table public.fixes add column if not exists check_on date;
alter table public.fixes add column if not exists remind text not null default 'none';
alter table public.fixes drop constraint if exists fixes_remind_check;
alter table public.fixes add constraint fixes_remind_check check (remind in ('whatsapp', 'calendar', 'none'));

-- ---------- history: a summary of each analysis (Riwayat), kept 60 days ----------
-- One row per analysis by a signed-in seller, written by the browser with the seller's own sign-in. It holds the date, the product name the seller
-- typed (optional), the number of reviews, the four group counts and the theme name of the top complaint: never review text, quotes, drafts or
-- AI suggestions. The page deletes the seller's rows older than 60 days whenever they open Riwayat or a row is saved. Keep the theme names in sync
-- with prompts/output_schema.json (a test compares them).
create table if not exists public.history (
  id         bigint generated always as identity primary key,
  user_id    uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  product    text not null default '' check (char_length(product) <= 150),
  reviews    integer not null check (reviews between 0 and 1000),
  fix_n      integer not null check (fix_n between 0 and 1000),
  fight_n    integer not null check (fight_n between 0 and 1000),
  ignore_n   integer not null check (ignore_n between 0 and 1000),
  keep_n     integer not null check (keep_n between 0 and 1000),
  top_theme  text check (top_theme in ('Not as described / claim vs reality', 'Colour vs photo', 'Sizing & fit', 'Fabric & material quality', 'Defect / not working', 'Wrong variant / item sent', 'Quantity shortage / missing item', 'Packaging / damaged on arrival', 'Spoilage / freshness', 'Portion / low meat or filler', 'Taste / subjective', 'Fake / authenticity doubt', 'Praise: quality / material', 'Praise: value for money', 'Praise: seller service', 'Praise: delivery / packing', 'Praise: taste', 'Praise: works as described', 'No text', 'Other'))
);
create index if not exists history_user_created_idx on public.history (user_id, created_at desc);
-- Optional, NOT verified on this project: a nightly clean-up inside Supabase, so rows of sellers who never come back also go after 60 days.
-- To use it, remove the two leading dashes from both lines below and run them once (Database > Extensions must allow pg_cron).
-- create extension if not exists pg_cron;
-- select cron.schedule('bedahreview-history-60d', '0 3 * * *', $$delete from public.history where created_at < now() - interval '60 days'$$);

-- ---------- per-account ceilings, counted outside the policies ----------
-- A policy may not read its own table: Postgres stops every insert with "infinite recursion detected in policy" (seen live on 2026-11-02).
-- So the ceilings below count through these functions. They run as the table owner, count only the caller's own rows, return a number and
-- nothing else, and sit in a schema the API does not expose.
create schema if not exists private;
revoke all on schema private from public, anon;
grant usage on schema private to authenticated;
create or replace function private.my_fix_count() returns bigint language sql stable security definer set search_path = '' as $$
  select count(*) from public.fixes where user_id = (select auth.uid())
$$;
create or replace function private.my_history_count() returns bigint language sql stable security definer set search_path = '' as $$
  select count(*) from public.history where user_id = (select auth.uid())
$$;
revoke all on function private.my_fix_count() from public, anon;
revoke all on function private.my_history_count() from public, anon;
grant execute on function private.my_fix_count() to authenticated;
grant execute on function private.my_history_count() to authenticated;

-- ---------- row level security: on, and each person sees only their own rows ----------
alter table public.profiles enable row level security;
alter table public.events   enable row level security;
alter table public.fixes    enable row level security;
alter table public.history  enable row level security;

drop policy if exists profiles_select_own on public.profiles;
drop policy if exists profiles_insert_own on public.profiles;
drop policy if exists profiles_update_own on public.profiles;
create policy profiles_select_own on public.profiles for select to authenticated using (user_id = (select auth.uid()));
create policy profiles_insert_own on public.profiles for insert to authenticated with check (user_id = (select auth.uid()));
create policy profiles_update_own on public.profiles for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

drop policy if exists events_select_own on public.events;
create policy events_select_own on public.events for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists fixes_select_own on public.fixes;
drop policy if exists fixes_insert_own on public.fixes;
drop policy if exists fixes_update_own on public.fixes;
drop policy if exists fixes_delete_own on public.fixes;
create policy fixes_select_own on public.fixes for select to authenticated using (user_id = (select auth.uid()));
-- a ceiling per account, a little above the page's 200 so that updating a full list still works (an upsert is checked as an insert first)
create policy fixes_insert_own on public.fixes for insert to authenticated
  with check (user_id = (select auth.uid()) and (select private.my_fix_count()) < 250);
create policy fixes_update_own on public.fixes for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy fixes_delete_own on public.fixes for delete to authenticated using (user_id = (select auth.uid()));

drop policy if exists history_select_own on public.history;
drop policy if exists history_insert_own on public.history;
drop policy if exists history_delete_own on public.history;
create policy history_select_own on public.history for select to authenticated using (user_id = (select auth.uid()));
-- a ceiling per account: far above 60 days of daily limits, so a broken page cannot fill the table
create policy history_insert_own on public.history for insert to authenticated
  with check (user_id = (select auth.uid()) and (select private.my_history_count()) < 500);
create policy history_delete_own on public.history for delete to authenticated using (user_id = (select auth.uid()));

-- ---------- grants: start from nothing, then give only what is needed ----------
revoke all on public.profiles from anon, authenticated;
revoke all on public.events   from anon, authenticated;
revoke all on public.fixes    from anon, authenticated;
revoke all on public.history  from anon, authenticated;

grant select, insert, update on public.profiles to authenticated;
grant select                 on public.events   to authenticated;
grant select, insert, update, delete on public.fixes to authenticated;
grant select, insert, delete         on public.history to authenticated;

grant select, insert, update, delete on public.profiles to service_role;
grant select, insert, delete         on public.events   to service_role;
grant select, delete                 on public.fixes    to service_role;
grant select, delete                 on public.history  to service_role;
