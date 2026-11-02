"""News / ownership / policy intelligence: evidence-backed, point-in-time, auditable.

Trust boundary: everything here is built from UNTRUSTED text (news, filings, translations, model output).
It can only emit an Assessment. It has no access to risk limits, credentials or order placement; the strategy
reads Assessment.status and the risk engine still checks every order.

Key rules implemented:
- Entities merge only on a shared registry identifier (LEI/CIN/CIK/ISIN...), never on similar names.
- Relationships carry type, %, effective dates, DISCLOSURE time, source, confidence, inferred/verified flags.
  Graph queries are point-in-time: a link disclosed after the knowledge time does not exist yet.
- Path tracing handles cycles, multiplies ownership along chains, dedupes paths; inferred/unverified links
  are reported but never make an assessment ELIGIBLE.
- Business impact, predicted direction and trade eligibility are separate fields. Classification confidence
  is separate from trade probability. Few independent comparables -> INSUFFICIENT_EVIDENCE.
- Repeated articles collapse into one underlying event (cluster) for scoring.
- Forecasts are appended to a hash-chained ledger at issuance (abstentions included) before outcomes exist.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum

from .store import Store

# ---------------- vocabularies ----------------
REL_KINDS = frozenset({"legal_owner", "beneficial_owner", "control", "board_member", "subsidiary", "jv",
                       "supplier", "customer", "lender", "guarantee", "pledge", "regulator", "contract_award",
                       "listed_as"})
# src OWNS dst: value shock on dst reaches src scaled by pct (impact flows owned -> owner, never to siblings)
OWNERSHIP_KINDS = frozenset({"legal_owner", "beneficial_owner", "subsidiary"})
# business links transmit both ways, no % pass-through; control/board/regulator do not transmit value
COMMERCIAL_KINDS = frozenset({"supplier", "customer", "lender", "guarantee", "pledge", "jv", "contract_award"})
MATERIAL_PCT = 5.0
EVENT_KINDS = frozenset({"earnings", "guidance", "financing", "m_and_a", "management_change", "holding_disclosure",
                         "insider_transaction", "ownership_change", "policy", "tariff", "subsidy", "contract_award",
                         "investigation", "litigation", "sanction", "supply_chain", "commodity", "macro",
                         "index_rebalance", "halt", "rumour"})
POLICY_STAGES = ("rumour", "consultation", "proposal", "draft", "enacted", "effective", "amended", "stayed",
                 "repealed")
FACTUALITY = frozenset({"confirmed", "estimate", "allegation", "rumour", "unknown"})


class Status(str, Enum):
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    ELIGIBLE = "ELIGIBLE"
    NO_ACTION = "NO_ACTION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Impact(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    NONE_IDENTIFIED = "NO_MATERIAL_IMPACT_IDENTIFIED"
    UNKNOWN = "UNKNOWN"


# ---------------- entities & relationships ----------------
@dataclass(frozen=True)
class Entity:
    id: str                     # internal stable id
    kind: str                   # person | company | fund | government | security
    name: str
    jurisdiction: str
    registry_ids: tuple = ()    # (("LEI", "..."), ("ISIN", "...")) - the ONLY basis for merging


@dataclass(frozen=True)
class Relationship:
    src: str
    dst: str
    kind: str
    source: str                 # URL / filing id
    disclosed_ns: int           # when it became public (point-in-time key)
    effective_from: str         # ISO date
    effective_to: str | None = None
    pct: float | None = None    # ownership / voting %, if disclosed
    confidence: float = 1.0
    inferred: bool = False
    verified: bool = False

    def __post_init__(self) -> None:
        if self.kind not in REL_KINDS:
            raise ValueError(f"unknown relationship kind {self.kind}")
        if self.pct is not None and not 0 <= self.pct <= 100:
            raise ValueError(f"pct out of range {self.pct}")


@dataclass(frozen=True)
class Path:
    security: str
    hops: tuple                 # ((src, kind, dst, pct, source), ...)
    effective_pct: float | None
    verified: bool              # every hop verified and not inferred
    material: bool = True       # ownership chain >= MATERIAL_PCT (commercial links: materiality from caller)


class Graph:
    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.rels: list[Relationship] = []

    def add_entity(self, e: Entity) -> str:
        """Returns the canonical id. Merges ONLY on a shared registry id; same name never merges."""
        for other in self.entities.values():
            if set(e.registry_ids) & set(other.registry_ids):
                return other.id
        if e.id in self.entities and self.entities[e.id] != e:
            raise ValueError(f"id collision {e.id} with different entity")
        self.entities[e.id] = e
        return e.id

    def add(self, r: Relationship) -> None:
        for end in (r.src, r.dst):
            if end not in self.entities:
                raise KeyError(f"unknown entity {end}")
        self.rels.append(r)

    def as_of(self, knowledge_ns: int, date: str) -> list[Relationship]:
        """Point-in-time: disclosed by knowledge_ns and effective on date."""
        return [r for r in self.rels if r.disclosed_ns <= knowledge_ns and r.effective_from <= date
                and (r.effective_to is None or date < r.effective_to)]

    def paths(self, start: str, knowledge_ns: int, date: str, max_depth: int = 4) -> list[Path]:
        """Impact-transmission paths from an affected entity to listed securities.
        Ownership edges are walked owned -> owner; listed_as company -> security; commercial both ways.
        Cycles cut; duplicate paths removed."""
        edges: dict[str, list[tuple[str, Relationship]]] = {}
        for r in self.as_of(knowledge_ns, date):
            if r.kind in OWNERSHIP_KINDS:
                edges.setdefault(r.dst, []).append((r.src, r))
            elif r.kind == "listed_as":
                edges.setdefault(r.src, []).append((r.dst, r))
            elif r.kind in COMMERCIAL_KINDS:
                edges.setdefault(r.src, []).append((r.dst, r))
                edges.setdefault(r.dst, []).append((r.src, r))
        out: dict[tuple, Path] = {}

        def walk(node: str, hops: tuple, seen: frozenset, pct: float | None, ok: bool) -> None:
            if self.entities[node].kind == "security" and hops:
                key = (node, tuple((h[0], h[1], h[2]) for h in hops))
                out.setdefault(key, Path(node, hops, pct, ok, pct is None or pct >= MATERIAL_PCT))
                return
            if len(hops) >= max_depth:
                return
            for nxt, r in sorted(edges.get(node, []), key=lambda x: (x[0], x[1].kind, x[1].source)):
                if nxt in seen:
                    continue                                    # circular ownership
                chain = pct
                if r.kind in OWNERSHIP_KINDS:
                    chain = None if r.pct is None else (100.0 if pct is None else pct) * r.pct / 100
                elif r.kind in COMMERCIAL_KINDS:
                    chain = None                                # business link: no % pass-through
                walk(nxt, hops + ((node, r.kind, nxt, r.pct, r.source),), seen | {nxt}, chain,
                     ok and r.verified and not r.inferred)

        walk(start, (), frozenset({start}), None, True)
        return list(out.values())


# ---------------- events ----------------
@dataclass(frozen=True)
class Event:
    kind: str
    jurisdiction: str
    source: str
    source_authority: str        # primary_regulator | exchange | company | wire | press | social
    published_ns: int            # publisher's stated time
    first_received_ns: int       # when THIS system first had it (backtests key on this)
    original_text: str
    factuality: str              # confirmed | estimate | allegation | rumour | unknown
    entities: tuple = ()         # entity ids named in the event
    stage: str | None = None     # policy lifecycle stage
    effective_date: str | None = None
    corrects: str | None = None  # id of the event it corrects/denies/confirms
    translation: str | None = None
    cluster: str | None = None   # underlying-event key; set by cluster_key() if None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unfamiliar event kind {self.kind!r}: route to REVIEW")
        if self.factuality not in FACTUALITY:
            raise ValueError(f"bad factuality {self.factuality}")
        if self.stage is not None and self.stage not in POLICY_STAGES:
            raise ValueError(f"bad policy stage {self.stage}")
        if self.first_received_ns < 0 or self.published_ns < 0:
            raise ValueError("timestamps required")

    @property
    def id(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:20]

    def cluster_key(self) -> str:
        """Same underlying event across wire copies/reprints: kind + jurisdiction + entities + stage + date."""
        return self.cluster or hashlib.sha256(json.dumps(
            [self.kind, self.jurisdiction, sorted(self.entities), self.stage, self.effective_date]).encode()
        ).hexdigest()[:16]


def validate_classifier_output(raw: object, allowed_kinds: frozenset = EVENT_KINDS) -> dict:
    """Schema gate for any model (e.g. Jev) classification. Anything off-schema -> REVIEW, never trusted."""
    if not isinstance(raw, dict) or set(raw) != {"kind", "confidence"}:
        return {"kind": None, "confidence": None, "route": "REVIEW", "why": "off-schema output"}
    k, c = raw["kind"], raw["confidence"]
    if k not in allowed_kinds or not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 1:
        return {"kind": None, "confidence": None, "route": "REVIEW", "why": "invalid kind/confidence"}
    return {"kind": k, "confidence": float(c), "route": "OK", "why": "classification confidence only"}


# ---------------- assessment ----------------
@dataclass(frozen=True)
class Horizon:
    name: str                    # e.g. "intraday_15m", "swing_5d"
    target: str                  # exact definition, e.g. "mid return from first executable quote after receipt"
    flat_band_bps: float         # |return| <= band -> FLAT
    benchmark: str               # e.g. "sector index" or "none"
    min_independent_comparables: int = 20


@dataclass(frozen=True)
class Assessment:
    event_id: str
    cluster: str
    instrument: str
    horizon: str
    knowledge_ns: int                         # what the system knew at this minute
    paths: tuple                              # Path objects
    business_impact: Impact
    impact_range: tuple | None                # (low, high) in stated units, or None = unknown
    impact_units: str
    new_information: str                      # YES | NO | UNKNOWN (no prior expectation data)
    pre_move_bps: float | None                # move before receipt
    first_quote: tuple | None                 # (bid, ask, ts_ns) first executable after receipt
    p_up: float | None
    p_down: float | None
    p_flat: float | None
    n_comparables: int
    eligible: bool
    status: Status
    reasons: tuple
    invalidation: str
    expiry_ns: int
    competing_explanations: tuple = ()


def direction_probs(comparable_returns_bps: list[tuple[str, float]], band_bps: float) -> tuple[dict, int]:
    """comparables: (cluster_key, return). One vote per independent cluster (mean within cluster).
    Laplace-smoothed P(up/down/flat)."""
    by_cluster: dict[str, list[float]] = {}
    for c, r in comparable_returns_bps:
        by_cluster.setdefault(c, []).append(r)
    rets = [sum(v) / len(v) for v in by_cluster.values()]
    n = len(rets)
    up = sum(r > band_bps for r in rets)
    down = sum(r < -band_bps for r in rets)
    flat = n - up - down
    return {"up": (up + 1) / (n + 3), "down": (down + 1) / (n + 3), "flat": (flat + 1) / (n + 3)}, n


def assess(event: Event, instrument: str, graph: Graph, horizon: Horizon, knowledge_ns: int, date: str,
           impact: Impact, impact_range: tuple | None, impact_units: str, prior_expectation: float | None,
           announced_value: float | None, pre_move_bps: float | None, first_quote: tuple | None,
           comparables: list[tuple[str, float]], cost_bps: float, expiry_ns: int,
           concurrent_events: tuple = ()) -> Assessment:
    """Pure function: same inputs -> same assessment. Caller supplies estimates; this enforces the rules."""
    if knowledge_ns < event.first_received_ns:
        raise ValueError("look-ahead: assessing before the system received the event")
    reasons: list[str] = []
    sec_paths = tuple(p for start in (event.entities or ()) for p in graph.paths(start, knowledge_ns, date)
                      if p.security == instrument)
    direct = instrument in event.entities
    if not direct and not sec_paths:
        impact, reasons = Impact.NONE_IDENTIFIED, ["no dated relationship path to instrument"]
    verified_link = direct or any(p.verified and p.material for p in sec_paths)
    if not direct and sec_paths and not any(p.material for p in sec_paths):
        impact, reasons = Impact.NONE_IDENTIFIED, [f"only ownership paths < {MATERIAL_PCT}%: not material"]
    elif not verified_link:
        reasons.append("only inferred/unverified relationships: cannot increase exposure")
    new_info = ("UNKNOWN" if prior_expectation is None or announced_value is None else
                "NO" if announced_value == prior_expectation else "YES")
    probs, n = direction_probs(comparables, horizon.flat_band_bps)
    status: Status
    if event.kind == "rumour" or event.factuality in ("rumour", "allegation"):
        status = Status.WATCH
        reasons.append(f"{event.factuality}: monitor for confirmation/denial (new event)")
    elif event.stage in ("rumour", "consultation", "proposal", "draft"):
        status = Status.WATCH
        reasons.append(f"policy stage {event.stage}: not enacted")
    elif impact == Impact.NONE_IDENTIFIED:
        status = Status.NO_ACTION
    elif impact in (Impact.UNKNOWN, Impact.MIXED) or concurrent_events:
        status = Status.REVIEW
        reasons.append("mixed/unknown impact or competing explanations")
    elif n < horizon.min_independent_comparables:
        status = Status.INSUFFICIENT_EVIDENCE
        reasons.append(f"{n} independent comparables < {horizon.min_independent_comparables}")
    else:
        status = Status.REVIEW
    edge_bps = None
    if n:
        exp_up = probs["up"] - probs["down"]
        edge_bps = exp_up * horizon.flat_band_bps * 2          # crude: directional skew x typical move
        if abs(edge_bps) <= cost_bps:
            reasons.append(f"expected edge {edge_bps:.1f}bps <= cost {cost_bps}bps: entry skip")
    if pre_move_bps is not None and impact_range and abs(pre_move_bps) >= abs(max(impact_range, key=abs)) * 100:
        reasons.append("pre-receipt move already >= estimated impact: priced in")
    eligible = (status == Status.REVIEW and impact in (Impact.POSITIVE, Impact.NEGATIVE) and verified_link
                and new_info == "YES" and first_quote is not None and edge_bps is not None
                and abs(edge_bps) > cost_bps and not any("priced in" in r for r in reasons)
                and n >= horizon.min_independent_comparables)
    if eligible:
        status = Status.ELIGIBLE
    return Assessment(event_id=event.id, cluster=event.cluster_key(), instrument=instrument, horizon=horizon.name,
                      knowledge_ns=knowledge_ns, paths=sec_paths, business_impact=impact,
                      impact_range=impact_range, impact_units=impact_units, new_information=new_info,
                      pre_move_bps=pre_move_bps, first_quote=first_quote,
                      p_up=probs["up"] if n else None, p_down=probs["down"] if n else None,
                      p_flat=probs["flat"] if n else None, n_comparables=n, eligible=eligible, status=status,
                      reasons=tuple(reasons), invalidation="correction/denial event, or relationship end-dated",
                      expiry_ns=expiry_ns, competing_explanations=tuple(concurrent_events))


# ---------------- forecast ledger (auditor) ----------------
class Ledger:
    """Append-only, hash-chained forecast log in the events table (stream 'forecasts').
    Each record commits to the previous record's hash -> silent edits/reorders are detectable."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def _rows(self) -> list[dict]:
        return [json.loads(p) for (p,) in self.store.db.execute(
            "SELECT payload FROM events WHERE stream='forecasts' ORDER BY id")]

    def issue(self, arm: str, a: Assessment) -> str:
        """Record at issuance, before outcome. arm: PV | PV_NEWS | FULL. Abstentions recorded too."""
        rows = self._rows()
        prev = rows[-1]["hash"] if rows else "genesis"
        body = dict(arm=arm, event=a.event_id, cluster=a.cluster, instrument=a.instrument, horizon=a.horizon,
                    issued_ns=a.knowledge_ns, status=a.status.value, p_up=a.p_up, p_down=a.p_down,
                    p_flat=a.p_flat, impact=a.business_impact.value, eligible=a.eligible, prev=prev)
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        self.store.append_event("forecasts", len(rows), a.knowledge_ns, "forecast", json.dumps({**body, "hash": h}))
        return h

    def verify_chain(self) -> bool:
        prev = "genesis"
        for r in self._rows():
            body = {k: v for k, v in r.items() if k != "hash"}
            if r["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != r["hash"]:
                return False
            prev = r["hash"]
        return True

    def score(self, outcomes: dict[tuple, str]) -> dict:
        """outcomes: (cluster, instrument, horizon) -> 'up'|'down'|'flat'. One scored item per independent
        cluster+instrument+horizon (repeated articles cannot inflate accuracy). Abstentions = coverage gap."""
        seen: dict[tuple, dict] = {}
        for r in self._rows():
            seen.setdefault((r["arm"], r["cluster"], r["instrument"], r["horizon"]), r)   # first issuance only
        rep: dict[str, dict] = {}
        for (arm, cl, inst, hz), r in seen.items():
            s = rep.setdefault(arm, dict(issued=0, predicted=0, correct=0, pending=0))
            s["issued"] += 1
            if r["p_up"] is None:
                continue                                        # abstention
            s["predicted"] += 1
            truth = outcomes.get((cl, inst, hz))
            if truth is None:
                s["pending"] += 1
                continue
            call = max(("up", "down", "flat"), key=lambda k: r[f"p_{k}"])
            s["correct"] += call == truth
        for s in rep.values():
            scored = s["predicted"] - s["pending"]
            s["accuracy"] = f"{s['correct']}/{scored}" if scored else "pending"
            s["coverage"] = f"{s['predicted']}/{s['issued']}"
        return rep
