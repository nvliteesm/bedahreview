"""Save usage events to the Supabase `events` table from a background thread, so a visitor's request never waits for Supabase.

The server hands over rows and carries on. If Supabase is slow, paused or down, the rows are dropped and one short warning is
printed (again only after a success in between): losing a few counts is better than slowing the app. The stdout line the server
prints for every event stays as the backup. The secret key is only ever sent to Supabase, never logged, never put in a response.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

POST_TIMEOUT_SECS = 5
MAX_PENDING = 200  # requests waiting to be saved; more are dropped
MAX_ROWS_PER_POST = 200


def post_rows(project_url: str, key: str, rows: List[Dict[str, Any]], timeout: float = POST_TIMEOUT_SECS) -> None:
    """Insert rows into public.events with the secret key. Raises on any failure."""
    headers = {"apikey": key, "Content-Type": "application/json", "Prefer": "return=minimal", "User-Agent": "BedahReview/0.1"}
    if key.startswith("eyJ"):  # an older service_role key is a token and goes in both places; the newer sb_secret_ key goes in apikey only
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(project_url.rstrip("/") + "/rest/v1/events", data=json.dumps(rows).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        reply.read(1000)


class EventSink:
    def __init__(self, project_url: str, key: str, post: Optional[Callable[[List[Dict[str, Any]]], None]] = None) -> None:
        self._post = post or (lambda rows: post_rows(project_url, key, rows))
        self._queue: "queue.Queue[List[Dict[str, Any]]]" = queue.Queue(maxsize=MAX_PENDING)
        self._warned = False
        self._forgotten: set = set()  # accounts that were deleted: their rows would be refused (the account is gone), so they are never sent
        threading.Thread(target=self._run, name="event-sink", daemon=True).start()

    def submit(self, rows: List[Dict[str, Any]]) -> bool:
        """Hand over rows to be saved. False (and the rows are dropped) when too many are already waiting."""
        rows = [r for r in rows if r.get("user_id") not in self._forgotten]
        if not rows:
            return True
        try:
            self._queue.put_nowait(list(rows))
            return True
        except queue.Full:
            self._warn("queue full")
            return False

    def forget(self, user_id: str) -> None:
        """Drop anything still waiting for this account, and anything that arrives later."""
        self._forgotten.add(user_id)

    def wait_idle(self) -> None:
        """Block until everything handed over has been tried. For tests."""
        self._queue.join()

    def _warn(self, why: str) -> None:
        if not self._warned:
            sys.stderr.write("events not saved to Supabase: %s\n" % why)
            self._warned = True

    def _run(self) -> None:
        while True:
            rows = self._queue.get()
            taken = 1
            try:
                while len(rows) < MAX_ROWS_PER_POST:  # several requests that queued up are saved in one go
                    try:
                        rows += self._queue.get_nowait()
                        taken += 1
                    except queue.Empty:
                        break
                rows = [r for r in rows if r.get("user_id") not in self._forgotten]
                if rows:
                    self._post(rows)
                self._warned = False
            except urllib.error.HTTPError as exc:
                self._warn("HTTP %d" % exc.code)
            except Exception as exc:  # down, paused, slow, refused: drop quietly
                self._warn(type(exc).__name__)
            finally:
                for _ in range(taken):
                    self._queue.task_done()
