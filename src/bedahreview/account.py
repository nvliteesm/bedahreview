"""Delete a person's account and everything saved under it. Server side only: it needs the secret key.

The order matters. First the rows saved under the account (events, fixes, profile) are deleted one table at a time, so they are gone even if a
foreign key were ever missing; then the sign-in account itself is deleted through Supabase's admin side. If the first step fails nothing else
happens and the person can try again; if the last step fails the rows are already gone and a retry finishes the job. The secret key is only
ever sent to Supabase: never logged, never in a reply.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # used with fullmatch: "$" would let a trailing newline through
TABLES = ("events", "fixes", "profiles")  # every table that holds rows under a user_id
TIMEOUT_SECS = 10


class AccountError(Exception):
    """The deletion did not complete. The message is safe to log: it holds a status or a type name, never a key or an id."""


def _send(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECS) as reply:
            reply.read(1000)
            return reply.status
    except urllib.error.HTTPError as exc:
        return exc.code


class AccountAdmin:
    def __init__(self, project_url: str, key: str, request: Optional[Callable[[str, str, Dict[str, str], Optional[bytes]], int]] = None) -> None:
        self._base = project_url.rstrip("/")
        self._request = request or _send
        self._headers = {"apikey": key, "Content-Type": "application/json", "User-Agent": "BedahReview/0.1"}
        if key.startswith("eyJ"):  # an older service_role key is a token and goes in both places; the newer sb_secret_ key goes in apikey only
            self._headers["Authorization"] = "Bearer " + key

    def _call(self, method: str, path: str, body: Optional[dict] = None, ok_if_missing: bool = False) -> None:
        try:
            status = self._request(method, self._base + path, dict(self._headers), json.dumps(body).encode("utf-8") if body is not None else None)
        except Exception as exc:  # unreachable, slow, refused
            raise AccountError(type(exc).__name__)
        if not (200 <= status < 300 or (ok_if_missing and status == 404)):
            raise AccountError("HTTP %d" % status)

    def delete_account(self, user_id: str) -> None:
        """Remove the rows, then the account. Raises AccountError if any step fails."""
        if not isinstance(user_id, str) or not UUID_RE.fullmatch(user_id):  # the id goes into a web address: only a plain uuid is ever allowed there
            raise AccountError("bad id")
        for table in TABLES:
            self._call("DELETE", "/rest/v1/%s?user_id=eq.%s" % (table, user_id))
        self._call("DELETE", "/auth/v1/admin/users/" + user_id, {"should_soft_delete": False}, ok_if_missing=True)
