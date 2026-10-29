"""Check a Supabase sign-in token on this server, without calling Supabase for each request.

Supabase signs every user's access token (a JWT) with a private key and publishes the matching public keys at
<project>/auth/v1/.well-known/jwks.json. We fetch those keys once, keep them in memory, and verify the signature,
the expiry, the audience and the issuer ourselves. That keeps us clear of Supabase's per-address limit on
/auth/v1/user, and it means a signed-in person can still analyse reviews while Supabase is paused or down
(only a server that has never reached Supabase cannot check tokens).

Only the user id is kept from a token. The email in it is never read, logged or stored here.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

import jwt

ALGORITHMS = ["ES256", "RS256"]  # asymmetric only: a token signed with a shared secret (HS256) or "none" is refused
KEYS_TTL_SECS = 3600  # how long fetched keys are trusted before a fresh fetch
REFETCH_COOLDOWN_SECS = 60  # an unknown key id triggers one fresh fetch, but not more often than this
FETCH_TIMEOUT_SECS = 5
MAX_TOKEN_CHARS = 4096
REVOKE_SECS = 2 * 3600  # longer than a token lives (an hour by default), so a deleted account cannot keep using its old token


def fetch_jwks(url: str) -> Dict[str, Any]:
    """Download the public keys. Raises OSError / ValueError when Supabase cannot be reached or answers badly."""
    request = urllib.request.Request(url, headers={"User-Agent": "BedahReview/0.1"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECS) as reply:
        data = json.loads(reply.read(200_000).decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise ValueError("no keys in reply")
    return data


class SupabaseAuth:
    """verify(token) -> the Supabase user id, or None. Never raises for a bad token."""

    def __init__(
        self,
        project_url: str,
        fetch: Optional[Callable[[str], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        base = project_url.rstrip("/")
        self.project_url = base
        self.issuer = base + "/auth/v1"
        self.jwks_url = self.issuer + "/.well-known/jwks.json"
        self._fetch = fetch or fetch_jwks
        self._clock = clock
        self._keys: Dict[str, Any] = {}  # kid -> public key
        self._fetched_at = 0.0
        self._warned = False
        self._revoked: Dict[str, float] = {}  # user id -> until when it is refused. In memory: a restart forgets it, and the token ends within the hour anyway
        self._lock = threading.Lock()

    def _load(self, force: bool = False) -> None:
        """Refresh the key set when it is empty, old, or (force) when a token names a key we do not have."""
        now = self._clock()
        if self._keys and not force and now - self._fetched_at < KEYS_TTL_SECS:
            return
        if self._fetched_at and now - self._fetched_at < REFETCH_COOLDOWN_SECS:
            return  # tried a moment ago; do not hammer Supabase
        self._fetched_at = now  # set before the call: a failure also waits out the cooldown
        try:
            data = self._fetch(self.jwks_url)
            keys = {}
            for entry in data["keys"]:
                if isinstance(entry, dict) and entry.get("kid") and entry.get("kty") in ("EC", "RSA"):
                    keys[entry["kid"]] = jwt.PyJWK(entry).key
            if keys:
                self._keys, self._warned = keys, False
        except Exception as exc:  # network, bad JSON, bad key: keep whatever keys we already have
            if not self._warned:
                sys.stderr.write("supabase keys not loaded: %s\n" % type(exc).__name__)
                self._warned = True

    def revoke(self, user_id: str) -> None:
        """Refuse this account's tokens from now on (its account has been deleted, but the token itself is still signed and unexpired)."""
        with self._lock:
            now = self._clock()
            self._revoked = {u: until for u, until in self._revoked.items() if until > now}
            self._revoked[user_id] = now + REVOKE_SECS

    def verify(self, token: str) -> Optional[str]:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid, alg = header.get("kid"), header.get("alg")
        except jwt.PyJWTError:
            return None
        if alg not in ALGORITHMS or not isinstance(kid, str):
            return None
        with self._lock:
            self._load()
            if kid not in self._keys:
                self._load(force=True)
            key = self._keys.get(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token, key, algorithms=ALGORITHMS, audience="authenticated", issuer=self.issuer,
                leeway=10, options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            return None
        sub = claims.get("sub")
        if not (isinstance(sub, str) and 8 <= len(sub) <= 64):
            return None
        with self._lock:
            if self._revoked.get(sub, 0) > self._clock():
                return None
        return sub
