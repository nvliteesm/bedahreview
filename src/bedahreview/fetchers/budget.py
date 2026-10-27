"""A daily budget for every paid fetch, on top of the per-run cap (APIFY_MAX_COST_USD)."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .base import FetchError

DEFAULT_DAILY_BUDGET_USD = 1.00
DAILY_BUDGET_CEILING_USD = 5.00  # refuse an APIFY_DAILY_BUDGET_USD typo like 50


class BudgetError(FetchError):
    """Today's budget for paid fetches is used up. The message is safe to show the seller."""


class DailyBudget:
    """Counts each paid run at its worst-case cost (the per-run cap) the moment it starts, and never refunds.

    Resets at midnight UTC. In memory, so it assumes one server instance and starts again at zero after a restart:
    set a monthly limit inside the Apify account too.
    """

    def __init__(self, limit_usd: float, clock: Callable[[], float] = time.time) -> None:
        self.limit = Decimal(str(limit_usd))
        self._clock = clock
        self._day = self._today()
        self._spent = Decimal(0)
        self._lock = threading.Lock()

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent = today, Decimal(0)

    @property
    def spent(self) -> Decimal:
        with self._lock:
            self._roll()
            return self._spent

    def reserve(self, worst_case_usd: float) -> None:
        """Take the cost of one run, or raise BudgetError without taking anything."""
        cost = Decimal(str(worst_case_usd))
        with self._lock:
            self._roll()
            if self._spent + cost > self.limit:
                sys.stderr.write("daily paid-fetch budget reached\n")
                raise BudgetError("Demo limit reached today.")
            self._spent += cost
