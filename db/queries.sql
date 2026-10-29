-- BedahReview: questions to ask the events table. Paste one at a time into the Supabase SQL editor. Read-only, except the last block.
-- Rows are one per event; user_id is the account. Nothing here shows an email, a review or a product.

-- 1. How many different people used it, per week
select date_trunc('week', ts) as week, count(distinct user_id) as people, count(*) as events
from public.events group by 1 order by 1 desc;

-- 2. Accounts that came back on a different day (real repeat use)
select count(*) as people_who_returned from (
  select user_id from public.events group by user_id having count(distinct (ts at time zone 'Asia/Jakarta')::date) >= 2
) t;

-- 3. Analyses per account (most active first)
select user_id, count(*) filter (where event = 'analysed') as analyses, count(*) filter (where event = 'draft_copied') as drafts_copied, min(ts) as first_seen, max(ts) as last_seen
from public.events group by user_id order by analyses desc;

-- 4. The funnel: how many accounts got from opening the app to copying a draft
select
  count(distinct user_id) filter (where event = 'opened') as opened,
  count(distinct user_id) filter (where event = 'analysed') as analysed,
  count(distinct user_id) filter (where event = 'tab_viewed') as looked_at_results,
  count(distinct user_id) filter (where event = 'draft_copied') as copied_a_draft,
  count(distinct user_id) filter (where event = 'fix_marked') as marked_a_fix
from public.events;

-- 5. Was it useful? (the feedback question)
select props->>'answer' as answer, count(*) from public.events where event = 'feedback' group by 1 order by 2 desc;

-- 6. Keep events for 90 days: run by hand when you want to tidy up (this deletes rows)
-- delete from public.events where ts < now() - interval '90 days';
