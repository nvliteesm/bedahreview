"""The owner's dashboard numbers (/admin). Server side only: it reads Supabase with the secret key and returns totals.

What it reads: the sign-in accounts (id, sign-up date, sign-in method, last sign-in; the email in Supabase's reply is dropped at once and
never kept, logged or returned), the events table, the profiles table (platform, category, language; not the shop link) and the fixes table
(dates only; not the product or the note). What it returns: counts per day, per event and per field, a short anonymous id per account,
and the comments people typed under "Was this useful?". Never an email, a review, a product or a note.

The owner's own accounts (BEDAHREVIEW_ADMIN_USER_IDS) are left out of every number, so testing the app does not count as a user.
Results are kept for a minute, so reloading the page does not hit Supabase each time.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

JAKARTA = timezone(timedelta(hours=7))  # days end at midnight in Jakarta, like the daily limit
DAY_CHOICES = (7, 30, 90)  # events are kept for 90 days
DEFAULT_DAYS = 30
PAGE_ROWS = 1000  # Supabase returns at most this many rows per request
MAX_EVENT_ROWS = 50_000  # more than this in one window is not expected for a test; the page says when the numbers were cut
MAX_ACCOUNT_PAGES = 20
CACHE_SECS = 60
MAX_COMMENTS = 30
MAX_PEOPLE = 50
SHORT_ID = 8  # the first characters of an account id: enough to find it under Authentication > Users, not an email
TIMEOUT_SECS = 10
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$")


class StatsError(Exception):
    """Supabase could not be read. The message is safe to log: a status or a type name, never a key."""


def parse_ts(value: Any) -> Optional[datetime]:
    """A Supabase time ("2026-10-27T09:15:02.12345+00:00" or "...Z") as an aware datetime, or None. Python 3.9's fromisoformat is too strict for these."""
    match = _TS_RE.match(value or "") if isinstance(value, str) else None
    if not match:
        return None
    day, clock, frac, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else (zone if ":" in zone else zone[:3] + ":" + (zone[3:] or "00"))
    try:
        return datetime.fromisoformat("%sT%s.%s%s" % (day, clock, (frac or "0")[:6].ljust(6, "0"), zone))
    except ValueError:
        return None


def _jakarta_day(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(JAKARTA).strftime("%Y-%m-%d") if moment else None


def _get(url: str, headers: Dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECS) as reply:
        return json.loads(reply.read(20_000_000).decode("utf-8"))


class AdminStats:
    def __init__(self, project_url: str, key: str, exclude: frozenset = frozenset(), get: Optional[Callable[[str, Dict[str, str]], Any]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._base = project_url.rstrip("/")
        self._get = get or _get
        self._clock = clock
        self.exclude = frozenset(exclude)
        self._headers = {"apikey": key, "User-Agent": "BedahReview/0.1"}
        if key.startswith("eyJ"):  # an older service_role key is a token and goes in both places; the newer sb_secret_ key goes in apikey only
            self._headers["Authorization"] = "Bearer " + key
        self._cache: Dict[int, tuple] = {}
        self._lock = threading.Lock()

    # -- reading Supabase ------------------------------------------------
    def _read(self, path: str) -> Any:
        try:
            return self._get(self._base + path, dict(self._headers))
        except urllib.error.HTTPError as exc:
            raise StatsError("HTTP %d" % exc.code)
        except Exception as exc:  # unreachable, paused, slow, bad reply
            raise StatsError(type(exc).__name__)

    def _rows(self, table: str, query: str, cap: int) -> tuple:
        """(rows, cut): every row of a table query, page by page, up to `cap`. cut is True when there were more."""
        rows: List[Dict[str, Any]] = []
        while True:
            page = self._read("/rest/v1/%s?%s&limit=%d&offset=%d" % (table, query, PAGE_ROWS, len(rows)))
            if not isinstance(page, list):
                raise StatsError("bad reply")
            rows += [r for r in page if isinstance(r, dict)]
            if len(page) < PAGE_ROWS:
                return rows, False
            if len(rows) >= cap:
                return rows[:cap], True

    def _accounts(self) -> List[Dict[str, Any]]:
        """Every sign-in account, keeping only the id, the dates and the sign-in method."""
        kept: List[Dict[str, Any]] = []
        for page in range(1, MAX_ACCOUNT_PAGES + 1):
            reply = self._read("/auth/v1/admin/users?page=%d&per_page=%d" % (page, PAGE_ROWS))
            users = reply.get("users") if isinstance(reply, dict) else None
            if not isinstance(users, list):
                raise StatsError("bad reply")
            for user in users:
                if isinstance(user, dict) and isinstance(user.get("id"), str):
                    meta = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
                    provider = meta.get("provider") if meta.get("provider") in ("google", "email") else "other"
                    kept.append({"id": user["id"], "created": parse_ts(user.get("created_at")), "last": parse_ts(user.get("last_sign_in_at")), "provider": provider})
            full = len(users) >= PAGE_ROWS
            del users, reply  # the email addresses in Supabase's reply go no further than this loop
            if not full:
                break
        return kept

    # -- the numbers -----------------------------------------------------
    def summary(self, days: int = DEFAULT_DAYS) -> Dict[str, Any]:
        """The dashboard for the last `days` days (7, 30 or 90), from cache when it is less than a minute old. Raises StatsError."""
        days = days if days in DAY_CHOICES else DEFAULT_DAYS
        with self._lock:
            now = self._clock()
            hit = self._cache.get(days)
            if hit and now - hit[0] < CACHE_SECS:
                return hit[1]
            result = self._build(days, now)
            self._cache[days] = (now, result)
            return result

    def _build(self, days: int, now: float) -> Dict[str, Any]:
        today = datetime.fromtimestamp(now, JAKARTA).date()
        first_day = today - timedelta(days=days - 1)
        start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=JAKARTA).astimezone(timezone.utc)
        day_list = [(first_day + timedelta(days=i)).isoformat() for i in range(days)]
        week_start = (today - timedelta(days=6)).isoformat()

        accounts = [a for a in self._accounts() if a["id"] not in self.exclude]
        since = urllib.parse.quote(start.strftime("%Y-%m-%dT%H:%M:%SZ"))
        events, cut = self._rows("events", "select=ts,user_id,event,props&ts=gte.%s&order=ts.asc" % since, MAX_EVENT_ROWS)
        profiles, _ = self._rows("profiles", "select=user_id,platform,category,lang", MAX_EVENT_ROWS)
        fixes, _ = self._rows("fixes", "select=user_id,fixed_on,after_on", MAX_EVENT_ROWS)
        profiles = [p for p in profiles if p.get("user_id") not in self.exclude]
        fixes = [f for f in fixes if f.get("user_id") not in self.exclude]

        evs = []
        for row in events:
            day = _jakarta_day(parse_ts(row.get("ts")))
            if day and day >= day_list[0] and row.get("user_id") not in self.exclude and isinstance(row.get("event"), str):
                evs.append((day, row["user_id"], row["event"], row.get("props") if isinstance(row.get("props"), dict) else {}))

        # people: accounts and who was active when
        joined = {a["id"]: _jakarta_day(a["created"]) for a in accounts}
        signups = Counter(d for d in joined.values() if d and d >= day_list[0])
        active_days: Dict[str, set] = defaultdict(set)
        analysed_days: Dict[str, set] = defaultdict(set)
        per_user: Dict[str, Counter] = defaultdict(Counter)
        first_seen: Dict[str, str] = {}
        last_seen: Dict[str, str] = {}
        day_people: Dict[str, set] = defaultdict(set)
        day_analyses: Counter = Counter()
        day_failures: Counter = Counter()
        for day, uid, name, props in evs:
            if name not in ("analysis_failed", "limit_hit"):  # a failure is not the person using the app
                active_days[uid].add(day)
                day_people[day].add(uid)
                first_seen.setdefault(uid, day)
                last_seen[uid] = day
            per_user[uid][name] += 1
            if name == "analysed":
                analysed_days[uid].add(day)
                day_analyses[day] += 1
            if name == "analysis_failed":
                day_failures[day] += 1

        def count(name: str) -> int:
            return sum(1 for e in evs if e[2] == name)

        def split(name: str, field: str) -> Dict[str, int]:
            return dict(Counter(str(e[3].get(field)) for e in evs if e[2] == name and e[3].get(field) is not None).most_common())

        def reached(name: str) -> int:
            return len({e[1] for e in evs if e[2] == name})

        analysed = [e for e in evs if e[2] == "analysed"]
        reviews = [e[3].get("reviews") for e in analysed if isinstance(e[3].get("reviews"), int)]
        profile_by_user = {p.get("user_id"): p for p in profiles}
        intro_done = sum(1 for p in profiles if p.get("platform") or p.get("category"))
        fixes_by_user = Counter(f.get("user_id") for f in fixes)

        people = []
        for uid in set(active_days) | set(per_user):
            counts = per_user[uid]
            people.append({
                "id": uid[:SHORT_ID], "joined": joined.get(uid), "first": first_seen.get(uid), "last": last_seen.get(uid), "days": len(active_days[uid]),
                "analyses": counts["analysed"], "drafts": counts["draft_copied"], "fixes": counts["fix_marked"], "failed": counts["analysis_failed"],
                "platform": (profile_by_user.get(uid) or {}).get("platform") or None,
            })
        people.sort(key=lambda p: (-p["days"], -p["analyses"], p["id"]))

        comments = [{"day": e[0], "answer": e[3].get("answer"), "comment": e[3]["comment"]} for e in evs if e[2] == "feedback" and isinstance(e[3].get("comment"), str)]
        active_ids = set(active_days)
        return {
            "days": days,
            "from": day_list[0],
            "to": day_list[-1],
            "generated": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cut": cut,
            "excluded": len(self.exclude),
            "accounts": {
                "total": len(accounts),
                "new": sum(signups.values()),
                "by_method": dict(Counter(a["provider"] for a in accounts).most_common()),
                "ever_analysed_in_window": len(analysed_days),
            },
            "active": {
                "window": len(active_ids),
                "week": len({uid for uid, ds in active_days.items() if any(d >= week_start for d in ds)}),
                "today": len(day_people.get(day_list[-1], ())),
                "returned": sum(1 for ds in active_days.values() if len(ds) >= 2),
                "analysed_on_two_days": sum(1 for ds in analysed_days.values() if len(ds) >= 2),
            },
            "daily": [{"day": d, "signups": signups.get(d, 0), "people": len(day_people.get(d, ())), "analyses": day_analyses.get(d, 0), "failed": day_failures.get(d, 0)} for d in day_list],
            "analyses": {
                "total": len(analysed),
                "by_source": split("analysed", "source"),
                "by_lang": split("analysed", "lang"),
                "avg_reviews": round(sum(reviews) / len(reviews), 1) if reviews else None,
                "per_person": round(len(analysed) / len(analysed_days), 1) if analysed_days else None,
            },
            "funnel": [{"step": s, "people": reached(s)} for s in ("opened", "analysed", "tab_viewed", "draft_copied", "fix_marked")],
            "features": {
                "tabs": split("tab_viewed", "tab"),
                "drafts": split("draft_copied", "kind"),
                "fix_marked": count("fix_marked"),
                "rivals_started": count("rivals_started"),
            },
            "profiles": {
                "intro_done": intro_done,
                "platform": dict(Counter(p.get("platform") or "not given" for p in profiles).most_common()),
                "category": dict(Counter((p.get("category") or "not given").strip().lower() for p in profiles).most_common(10)),
                "lang": dict(Counter(p.get("lang") or "not given" for p in profiles).most_common()),
            },
            "fixes": {"total": len(fixes), "accounts": len(fixes_by_user), "rechecked": sum(1 for f in fixes if f.get("after_on"))},
            "feedback": {"answers": split("feedback", "answer"), "comments": comments[::-1][:MAX_COMMENTS]},
            "health": {
                "failed": count("analysis_failed"),
                "failed_by_code": split("analysis_failed", "code"),
                "failed_by_step": split("analysis_failed", "step"),
                "limits": count("limit_hit"),
                "limits_by_kind": split("limit_hit", "limit"),
                "people_hit_limit": reached("limit_hit"),
            },
            "people": people[:MAX_PEOPLE],
            "people_total": len(people),
        }
