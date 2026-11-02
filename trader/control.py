"""Authenticated operator controls: kill switch, resume, risk-limit changes. Every call is audited.

Auth: TRADER_ADMIN_TOKEN_SHA256 env var holds sha256(token). The token itself is never stored or logged.
5 consecutive failures -> locked for 60 s.

Kill levels (each includes the ones below):
  1 HALT_ENTRIES  : risk rejects every exposure-increasing order. Working orders and positions untouched.
  2 CANCEL_ORDERS : + cancel requests for every working order. Fills racing the cancel are kept and
                    recorded; the order ends CANCELLED (with filled qty) or FILLED per broker confirmation.
  3 FLATTEN       : + reduce-only marketable limit sells (at bid) for every long, every book. Positions
                    without a fresh bid are reported "NOT submitted - manual action needed".
Outcomes reported are what the broker confirmed at call time; run reconcile() to refresh.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from .engine import Engine
from .risk import Limits

LEVELS = {1: "HALT_ENTRIES", 2: "CANCEL_ORDERS", 3: "FLATTEN"}


class Controller:
    def __init__(self, engine: Engine, token_sha256: str | None = None) -> None:
        self.e = engine
        self.expected = token_sha256 or os.environ.get("TRADER_ADMIN_TOKEN_SHA256", "")
        self.fails, self.locked_until = 0, 0.0

    def _auth(self, token: str, actor: str, action: str, safety: bool = False) -> None:
        """safety=True (kill): a valid token works even during lockout, so bad guesses can't block a halt."""
        now = time.monotonic()
        ok = ((safety or now >= self.locked_until) and bool(self.expected)
              and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), self.expected))
        if not ok:
            if now < self.locked_until and not safety:
                self.e.store.audit(actor, f"DENIED (locked) {action}", {})
                raise PermissionError("locked out")
            self.fails += 1
            if self.fails >= 5:
                self.locked_until, self.fails = now + 60, 0
                self.e.store.audit(actor, "LOCKOUT 60s", {"action": action})
            self.e.store.audit(actor, f"DENIED {action}", {})
            raise PermissionError("not authorized")
        self.fails = 0

    def kill(self, token: str, level: int, actor: str) -> dict:
        self._auth(token, actor, f"kill {level}", safety=True)
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self.e.kill_level = max(self.e.kill_level, level)
        report: dict = {"level": LEVELS[level]}
        if level >= 2:
            report["cancel_requested"] = self.e.oms.cancel_all()
        if level >= 3:
            report["flatten"] = self.e.flatten(actor)
        self.e.oms.pump()
        report["open_orders"] = [f"{o.client_id} {o.state.value} {o.filled_qty}/{o.qty}"
                                 for o in self.e.store.orders() if o.state.value not in
                                 ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")]
        self.e.store.audit(actor, "kill", report)
        return report

    def resume(self, token: str, actor: str) -> list[str]:
        """Clears the kill switch only if reconciliation finds no issues."""
        self._auth(token, actor, "resume")
        issues = self.e.oms.reconcile()
        if not issues:
            self.e.kill_level = 0
        self.e.store.audit(actor, "resume", {"issues": issues, "kill_level": self.e.kill_level})
        return issues

    def set_limits(self, token: str, path: str, actor: str) -> str:
        self._auth(token, actor, "set_limits")
        try:
            limits, h = Limits.from_file(path)       # validates; raises before anything changes
        except (ValueError, OSError, TypeError) as e:
            self.e.store.audit(actor, "set_limits REJECTED", {"error": str(e)})
            raise
        old = self.e.risk.hash
        self.e.risk.limits, self.e.risk.hash = limits, h
        self.e.store.audit(actor, "set_limits", {"from": old, "to": h, "version": limits.version})
        return h
