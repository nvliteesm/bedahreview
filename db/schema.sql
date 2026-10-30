-- BedahReview: tables for accounts, usage events and each seller's fixes list. Paste this whole file into the Supabase SQL editor and run it.
-- Safe to run twice. It only creates things in the "public" schema and never touches auth.users.
--
-- What is NOT stored here, on purpose: review text, reviewer names, IP addresses.
-- The email address lives only in Supabase Auth.
-- The one place a product name, product link and the seller's own short note ARE stored is `fixes` (the seller's own "mark as fixed" list),
-- so it can be opened from another phone. That is a deliberate product choice; the privacy page says so.
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
  event   text not null check (event in ('opened', 'analysed', 'tab_viewed', 'draft_copied', 'fix_marked', 'rivals_started', 'feedback', 'free_unlocked', 'analysis_failed', 'limit_hit')),
  props   jsonb not null default '{}'::jsonb check (jsonb_typeof(props) = 'object' and octet_length(props::text) <= 2000)
);
-- A project made before analysis_failed and limit_hit existed has the old list: replace it (safe to repeat).
alter table public.events drop constraint if exists events_event_check;
alter table public.events add constraint events_event_check check (event in ('opened', 'analysed', 'tab_viewed', 'draft_copied', 'fix_marked', 'rivals_started', 'feedback', 'free_unlocked', 'analysis_failed', 'limit_hit'));
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
  primary key (user_id, id),
  check (before_count <= before_total),
  check ((after_count is null and after_total is null) or (after_count is not null and after_total is not null and after_count <= after_total))
);

-- ---------- row level security: on, and each person sees only their own rows ----------
alter table public.profiles enable row level security;
alter table public.events   enable row level security;
alter table public.fixes    enable row level security;

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
  with check (user_id = (select auth.uid()) and (select count(*) from public.fixes f where f.user_id = (select auth.uid())) < 250);
create policy fixes_update_own on public.fixes for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy fixes_delete_own on public.fixes for delete to authenticated using (user_id = (select auth.uid()));

-- ---------- grants: start from nothing, then give only what is needed ----------
revoke all on public.profiles from anon, authenticated;
revoke all on public.events   from anon, authenticated;
revoke all on public.fixes    from anon, authenticated;

grant select, insert, update on public.profiles to authenticated;
grant select                 on public.events   to authenticated;
grant select, insert, update, delete on public.fixes to authenticated;

grant select, insert, update, delete on public.profiles to service_role;
grant select, insert, delete         on public.events   to service_role;
grant select, delete                 on public.fixes    to service_role;
