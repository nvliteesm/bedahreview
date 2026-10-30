# What BedahReview logs, and what it does not

The goal is to say what testers actually did, not what we hoped. So the app logs a small, fixed list of usage events, tied to an account but never to a name, an email, a review or a product.
Nothing else is ever logged. The list lives in one place, `src/bedahreview/events.py`, and every event is checked against it on the server.

## What is never logged

- Review text and reviewer names, anywhere.
- Product names, product links, shop names and notes in the events or the log. (A seller's own fixes list does hold a product name, link and note: see "The fixes list" below. It is a separate table, never an event.)
- IP addresses and user agents. (The request log prints the path only.)

## Accounts and the device id

With Supabase sign-in on, each event is saved against the account (a random user id; the email stays in Supabase Auth and is never in the events table). That is how "unique people" are counted: one account, however many taps or phones.

Events also carry a random device id made in the browser (`localStorage`, `bedahreview-device`). It only appears in the log line below, as a backup for people who signed in with an access code or a local run. It is not saved in Supabase.

If the browser sends "Do Not Track" (or Global Privacy Control), the page sends no events at all, except a feedback answer the tester presses Send on themselves, and the server writes no failure or limit event for that request.

## The fixes list (a separate table, on purpose)

Marking a complaint as fixed keeps a small record: the theme, the variant, the date, the complaint counts before and after, the **product link or name** (used to match later analyses of the same product), and the seller's own short **note**. It is kept in the browser and, when signed in, in the account's `fixes` table in Supabase so it opens on another phone. Only that account can read it from the app; whoever runs the Supabase project can read it in the dashboard. That is a product choice, and the privacy page and the fixes screen say so.

- The note is the seller's own words, up to 200 characters. The page refuses a note that repeats 20 or more characters of a review in the current analysis (a guard, not a guarantee), and the fixes screen asks people not to write names.
- Nothing from this table is ever sent in an event or printed in the log.
- Signing out saves what is pending, then clears the copy kept in that browser. The account's copy stays.
- "Hapus akun dan data saya" deletes the account's events, fixes and profile, then the account, and clears this browser's copy (see the README).
- Honest limit: a fix deleted on one phone can come back from another phone that was offline with an old copy.

## The events

Every event carries `event` and a server timestamp `ts`, plus the fields below (and `device` in the log line). Anything outside this table is rejected with a 400.

| Event | When | Extra fields |
|---|---|---|
| `opened` | the page loads | none |
| `analysed` | an analysis finishes | `reviews` (a number, 0 to 50), `source` (`paste` or `link`), `lang` (`id` or `en`) |
| `tab_viewed` | the tester opens a results tab, or the Competitors section (`rivals`) | `tab` (`fix`, `fight`, `ignore`, `keep` or `rivals`) |
| `draft_copied` | the tester taps Copy | `kind` (`fix`, `side`, `chat`, `public` or `report`). The draft text is not sent. |
| `fix_marked` | "Mark as fixed" is saved | none. The fix itself goes to the fixes table, not into the event. |
| `rivals_started` | a competitor search starts | none |
| `feedback` | the tester sends "Was this useful?" | `answer` (`yes`, `partly` or `no`), and `comment` only if they typed one (300 characters at most) |
| `free_unlocked` | someone signs in to see the details of the free analysis they made before having an account | none. Sent once, right after the sign-in, together with an `analysed` event for that analysis. |
| `analysis_failed` | **written by the server**: an analysis, competitor search or compare failed (5xx) | `step` (`analyse`, `suggest` or `compare`), `code` (the server's own error code, such as `model_quota`; never the message) |
| `limit_hit` | **written by the server**: one of those was refused by a limit | `step`, `limit` (`daily`, `hour`, `budget` or `busy`) |

A free analysis made without an account is not an event (there is no account to save it under). The server prints one `bedahreview_free_analysis` log line for it, with nothing else in it, and counts it in memory for `/admin` ("since the last restart").

The last two are `SERVER_EVENTS` in `events.py`: the page can never send them, and their log line has `"device":"server"`. A person's own slip (nothing pasted, a bad link) is not an event; only the app failing or turning someone away is.

### The one free-text field

`comment` is the only place a tester can type. The page says that a comment is saved only if they write one and asks them not to write names or paste review text. As a second guard, the page refuses to send a comment that repeats 20 or more characters of any review in the current analysis. This is a guard, not a guarantee: a comment is still a person's own words.

## Where the events go

1. **Batches from the page.** Events wait in a small queue and are sent together every 10 seconds while there is something to send, and once more when the page is hidden or closed (`POST /api/events`, up to 50 events, signed in). A failed send is dropped, never retried.
2. **A log line, always.** The server prints one JSON line per event to standard output, for example:

```
{"severity":"INFO","message":"bedahreview_event","ts":"2026-10-27T09:15:02Z","event":"analysed","device":"3fa9c2d41b7e8a06c5d1e2f3","reviews":10,"source":"paste","lang":"id"}
```

   On Cloud Run these lines appear in Cloud Logging as `jsonPayload`; locally they print in the terminal. This is the backup that needs nothing else.
3. **Supabase, for signed-in accounts.** When `SUPABASE_SERVICE_KEY` is set, the server also saves one row per event in the `events` table (`user_id`, `event`, `props`) from a background thread, so a visitor never waits for it. If Supabase is slow, paused or down, the rows are dropped and one short warning is printed. Access-code testers and local runs have no account, so they only get the log line.

Limits: 600 requests per visitor per hour in public mode.

## Reading the numbers

**The owner dashboard** (`/admin`): set `BEDAHREVIEW_ADMIN_USER_IDS` to your own account id (Supabase, Authentication > Users), with `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` set, and restart. Sign in on the app with that account, then open `/admin`. Anyone else, signed in or not, gets "not found". It shows, for the last 7, 30 or 90 days (Jakarta days): accounts and sign-ups, people active per day, who came back on a second day, analyses per day, the funnel from opening to marking a fix, tabs and drafts used, platform and category from the intro, failures and limits, a per-account table, and the "Was this useful?" answers with comments.

- The server reads Supabase with the secret key (`src/bedahreview/admin_stats.py`) and sends the page totals only. From the sign-in accounts it keeps the id, sign-up date, last sign-in and method (Google or email); the email in Supabase's reply is dropped at once. It does not read the shop link, the fixed product or the note. Accounts appear as the first 8 characters of their id.
- Your own admin accounts are left out of every number, so your testing does not count as a user.
- Access-code testers have no account, so they are not on the dashboard (their events are log lines only).
- Numbers are kept for a minute; "Refresh" after that reads Supabase again. If Supabase is paused the page says so.


**From Supabase** (the source for unique people): open the SQL editor and run the queries in `db/queries.sql`: accounts per week, analyses per account, and the funnel from opened to analysed to copied a draft.

**From the log** (the backup):

```
gcloud logging read 'jsonPayload.message="bedahreview_event"' --format=json --freshness=30d --limit=5000 > events.json
python -m bedahreview.events_report events.json
```

It prints counts per event, per device, the analyses by source and language, tabs viewed, drafts copied, the Yes / Partly / No split, and any comments. A saved copy of terminal lines works too (one JSON line per event).

## Honest limits

- In the log, a device is a browser, not a person. In Supabase an account is a person, but one person can make two accounts (two emails). With a handful of testers these are stories, not statistics.
- Cloud Logging keeps logs for 30 days by default. Export or note the numbers you want to keep.
- Events can be lost if the browser is closed at the wrong moment, if someone blocks the request, if the sign-in token has run out while the page was hidden, or if Supabase is down.
- Supabase's free plan pauses a project after a week with no use. Events sent while it is paused are lost; the log lines remain.
- Keep events for 90 days (delete older rows by hand, see the end of `db/queries.sql`) and delete an account's rows with the account.
