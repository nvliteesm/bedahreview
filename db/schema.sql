-- BedahReview: tables for accounts and usage events. Paste this whole file into the Supabase SQL editor and run it.
-- Safe to run twice. It only creates things in the "public" schema and never touches auth.users.
--
-- What is NOT stored here, on purpose: review text, product names, reviewer names, IP addresses.
-- The email address lives only in Supabase Auth.
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
-- Keep the event names in sync with events.py (a test compares them).
create table if not exists public.events (
  id      bigint generated always as identity primary key,
  ts      timestamptz not null default now(),
  user_id uuid not null references auth.users (id) on delete cascade,
  event   text not null check (event in ('opened', 'analysed', 'tab_viewed', 'draft_copied', 'fix_marked', 'rivals_started', 'feedback')),
  props   jsonb not null default '{}'::jsonb check (jsonb_typeof(props) = 'object' and octet_length(props::text) <= 2000)
);
create index if not exists events_user_ts_idx on public.events (user_id, ts desc);
create index if not exists events_ts_idx on public.events (ts desc);

-- ---------- row level security: on, and each person sees only their own rows ----------
alter table public.profiles enable row level security;
alter table public.events   enable row level security;

drop policy if exists profiles_select_own on public.profiles;
drop policy if exists profiles_insert_own on public.profiles;
drop policy if exists profiles_update_own on public.profiles;
create policy profiles_select_own on public.profiles for select to authenticated using (user_id = (select auth.uid()));
create policy profiles_insert_own on public.profiles for insert to authenticated with check (user_id = (select auth.uid()));
create policy profiles_update_own on public.profiles for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

drop policy if exists events_select_own on public.events;
create policy events_select_own on public.events for select to authenticated using (user_id = (select auth.uid()));

-- ---------- grants: start from nothing, then give only what is needed ----------
revoke all on public.profiles from anon, authenticated;
revoke all on public.events   from anon, authenticated;

grant select, insert, update on public.profiles to authenticated;
grant select                 on public.events   to authenticated;

grant select, insert, update, delete on public.profiles to service_role;
grant select, insert, delete         on public.events   to service_role;
