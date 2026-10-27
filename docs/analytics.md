# What BedahReview logs, and what it does not

The goal is to say what testers actually did, not what we hoped. So the app logs a small, fixed list of anonymous events.
Nothing else is ever logged. The list lives in one place, `src/bedahreview/events.py`, and every event is checked against it on the server.

## What is never logged

- Review text, product names, product links, shop names, reviewer names.
- IP addresses and user agents. (The request log prints the path only.)
- Anything from the Fixes list. It stays in the tester's browser.

## The device id

A random id (24 hex characters) is made in the tester's browser and kept in `localStorage` under `bedahreview-device`. It tells browsers apart, nothing more. It is not linked to a name, account or address. Clearing browser data makes a new id, and the same person on two phones counts as two devices.

If the browser sends "Do Not Track", the page sends no events at all, except a feedback answer the tester presses Send on themselves.

## The events

Every event carries `event`, `device` and a server timestamp `ts`, plus the fields below. Anything outside this table is rejected with a 400.

| Event | When | Extra fields |
|---|---|---|
| `opened` | the page loads | none |
| `analysed` | an analysis finishes | `reviews` (a number, 0 to 50), `source` (`paste` or `link`), `lang` (`id` or `en`) |
| `tab_viewed` | the tester opens a results tab | `tab` (`fix`, `fight`, `ignore`, `keep` or `rivals`) |
| `draft_copied` | the tester taps Copy | `kind` (`fix`, `side`, `chat`, `public` or `report`). The draft text is not sent. |
| `fix_marked` | "Mark as fixed" is saved | none. The theme, note and counts stay in the browser. |
| `rivals_started` | a competitor search starts | none |
| `feedback` | the tester sends "Was this useful?" | `answer` (`yes`, `partly` or `no`), and `comment` only if they typed one (300 characters at most) |

### The one free-text field

`comment` is the only place a tester can type. The page says that a comment is saved only if they write one and asks them not to write names or paste review text. As a second guard, the page refuses to send a comment that repeats 20 or more characters of any review in the current analysis. This is a guard, not a guarantee: a comment is still a person's own words.

## Where the lines go

The server writes one JSON line per event to standard output, for example:

```
{"severity":"INFO","message":"bedahreview_event","ts":"2026-10-27T09:15:02Z","event":"analysed","device":"3fa9c2d41b7e8a06c5d1e2f3","reviews":10,"source":"paste","lang":"id"}
```

On Cloud Run these lines appear in Cloud Logging as `jsonPayload`. Locally they print in the terminal. The endpoint is `POST /api/event`, behind the access code, with a limit of 600 events per visitor per hour in public mode.

## Reading the numbers

```
gcloud logging read 'jsonPayload.message="bedahreview_event"' --format=json --freshness=30d --limit=5000 > events.json
python -m bedahreview.events_report events.json
```

It prints counts per event, per device, the analyses by source and language, tabs viewed, drafts copied, the Yes / Partly / No split, and any comments. A saved copy of terminal lines works too (one JSON line per event).

## Honest limits

- A device is a browser, not a person. With a handful of testers these are stories, not statistics, and the report says so.
- Cloud Logging keeps logs for 30 days by default. Export or note the numbers you want to keep.
- Events can be lost if the browser is closed at the wrong moment, or if someone blocks the request.
