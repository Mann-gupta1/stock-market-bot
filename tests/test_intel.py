"""Intel rules, using the 15% steel tariff / one promoter / three companies example."""
import pytest

from trader.intel import (Entity, Event, Graph, Horizon, Impact, Ledger, Relationship, Status, assess,
                          validate_classifier_output)
from trader.store import Store

T0 = 1_000


def graph():
    g = Graph()
    for e in [Entity("P", "person", "A. Promoter", "IN"),
              Entity("HOLD", "company", "Promoter Holdings", "IN", (("CIN", "U1"),)),
              Entity("STEEL", "company", "SteelCo", "IN", (("CIN", "L1"),)),
              Entity("AUTO", "company", "AutoCo", "IN", (("CIN", "L2"),)),
              Entity("SOFT", "company", "SoftCo", "IN", (("CIN", "L3"),)),
              Entity("XNSE:STEEL", "security", "SteelCo eq", "IN", (("ISIN", "IN1"),)),
              Entity("XNSE:AUTO", "security", "AutoCo eq", "IN", (("ISIN", "IN2"),)),
              Entity("XNSE:SOFT", "security", "SoftCo eq", "IN", (("ISIN", "IN3"),)),
              Entity("XNSE:HOLD", "security", "Promoter Holdings eq", "IN", (("ISIN", "IN4"),))]:
        g.add_entity(e)
    R = Relationship
    for r in [R("P", "HOLD", "beneficial_owner", "filing:1", 0, "2020-01-01", pct=90, verified=True),
              R("HOLD", "STEEL", "legal_owner", "shp:1", 0, "2020-01-01", pct=55, verified=True),
              R("HOLD", "AUTO", "legal_owner", "shp:2", 0, "2020-01-01", pct=40, verified=True),
              R("HOLD", "SOFT", "legal_owner", "shp:3", 0, "2020-01-01", pct=30, verified=True),
              R("STEEL", "XNSE:STEEL", "listed_as", "listing", 0, "2000-01-01", pct=100, verified=True),
              R("AUTO", "XNSE:AUTO", "listed_as", "listing", 0, "2000-01-01", pct=100, verified=True),
              R("SOFT", "XNSE:SOFT", "listed_as", "listing", 0, "2000-01-01", pct=100, verified=True),
              R("HOLD", "XNSE:HOLD", "listed_as", "listing", 0, "2000-01-01", verified=True),
              R("STEEL", "AUTO", "supplier", "annual report", 0, "2022-01-01", verified=True),
              R("STEEL", "HOLD", "legal_owner", "cross holding", 0, "2020-01-01", pct=2, verified=True)]:  # cycle
        g.add(r)
    return g


HZ = Horizon("swing_5d", "close-to-close return from first executable quote, sector-adjusted", 50.0, "NIFTY METAL", 20)


def tariff(**kw):
    base = dict(kind="tariff", jurisdiction="IN", source="gazette:123", source_authority="primary_regulator",
                published_ns=T0, first_received_ns=T0 + 5, original_text="15% duty on imported HR coil",
                factuality="confirmed", entities=("STEEL",), stage="enacted", effective_date="2026-12-01")
    return Event(**{**base, **kw})


def test_names_never_merge_only_registry_ids():
    g = graph()
    assert g.add_entity(Entity("STEEL2", "company", "SteelCo", "IN")) == "STEEL2"            # same name: separate
    assert g.add_entity(Entity("X", "company", "Other name", "IN", (("CIN", "L1"),))) == "STEEL"


def test_paths_point_in_time_cycle_safe_and_ownership_multiplied():
    g = graph()
    ps = g.paths("STEEL", knowledge_ns=10, date="2026-11-02")
    parent = [p for p in ps if p.security == "XNSE:HOLD" and len(p.hops) == 2]
    assert parent and parent[0].effective_pct == pytest.approx(55)          # shock on STEEL reaches listed owner
    assert not any(p.security == "XNSE:SOFT" for p in ps)                   # never leaks to sibling via parent
    g.add(Relationship("STEEL", "SOFT", "guarantee", "late filing", 10**12, "2026-01-01", verified=True))
    assert not any(p.security == "XNSE:SOFT" for p in g.paths("STEEL", 10, "2026-11-02"))    # not yet disclosed
    assert any(p.security == "XNSE:SOFT" for p in g.paths("STEEL", 10**12, "2026-11-02"))    # once disclosed


def test_three_companies_three_conclusions():
    g = graph()
    common = dict(graph=g, horizon=HZ, knowledge_ns=T0 + 10, date="2026-11-02", impact_units="EBITDA %",
                  pre_move_bps=20.0, first_quote=(100.0, 100.1, T0 + 11), cost_bps=30.0, expiry_ns=T0 + 10**15)
    comps = [(f"c{i}", 120.0 if i % 4 else -80.0) for i in range(30)]
    steel = assess(tariff(), "XNSE:STEEL", impact=Impact.POSITIVE, impact_range=(2.0, 6.0),
                   prior_expectation=10.0, announced_value=15.0, comparables=comps, **common)
    auto = assess(tariff(), "XNSE:AUTO", impact=Impact.MIXED, impact_range=(-3.0, 0.0),
                  prior_expectation=10.0, announced_value=15.0, comparables=comps, **common)
    soft = assess(tariff(), "XNSE:SOFT", impact=Impact.POSITIVE, impact_range=None,
                  prior_expectation=None, announced_value=None, comparables=comps, **common)
    assert steel.status == Status.ELIGIBLE and steel.new_information == "YES"
    assert auto.status == Status.REVIEW and auto.paths                       # supplier path shown, mixed impact
    assert soft.status == Status.NO_ACTION and soft.business_impact == Impact.NONE_IDENTIFIED


def test_expected_announcement_is_not_new_and_not_eligible():
    a = assess(tariff(), "XNSE:STEEL", graph(), HZ, T0 + 10, "2026-11-02", Impact.POSITIVE, (2.0, 6.0), "%",
               prior_expectation=15.0, announced_value=15.0, pre_move_bps=20.0, first_quote=(1, 1.1, 1),
               comparables=[(f"c{i}", 120.0) for i in range(30)], cost_bps=30, expiry_ns=10**15)
    assert a.new_information == "NO" and not a.eligible


def test_proposal_and_rumour_only_watch():
    for ev in (tariff(stage="proposal"), tariff(kind="rumour", factuality="rumour", stage=None)):
        a = assess(ev, "XNSE:STEEL", graph(), HZ, T0 + 10, "2026-11-02", Impact.POSITIVE, (1, 2), "%", 1, 2,
                   0.0, (1, 1.1, 1), [(f"c{i}", 100.0) for i in range(30)], 10, 10**15)
        assert a.status == Status.WATCH and not a.eligible


def test_few_independent_comparables_insufficient_and_lookahead_rejected():
    many_copies = [("same-cluster", 200.0)] * 100                       # 100 articles, 1 underlying event
    a = assess(tariff(), "XNSE:STEEL", graph(), HZ, T0 + 10, "2026-11-02", Impact.POSITIVE, (2, 6), "%", 10, 15,
               0.0, (1, 1.1, 1), many_copies, 10, 10**15)
    assert a.status == Status.INSUFFICIENT_EVIDENCE and a.n_comparables == 1
    with pytest.raises(ValueError, match="look-ahead"):
        assess(tariff(), "XNSE:STEEL", graph(), HZ, T0, "2026-11-02", Impact.POSITIVE, None, "%", None, None,
               None, None, [], 10, 10**15)


def test_unverified_inferred_link_never_eligible():
    g = graph()
    g.add_entity(Entity("XNSE:OTHER", "security", "Other", "IN", (("ISIN", "IN9"),)))
    g.add(Relationship("STEEL", "XNSE:OTHER", "customer", "news guess", 0, "2020-01-01", inferred=True))
    a = assess(tariff(), "XNSE:OTHER", g, HZ, T0 + 10, "2026-11-02", Impact.POSITIVE, (2, 6), "%", 10, 15, 0.0,
               (1, 1.1, 1), [(f"c{i}", 120.0) for i in range(30)], 10, 10**15)
    assert not a.eligible and any("unverified" in r for r in a.reasons)


def test_unfamiliar_event_and_bad_classifier_output_go_to_review():
    with pytest.raises(ValueError, match="REVIEW"):
        tariff(kind="alien_invasion")
    assert validate_classifier_output({"kind": "tariff", "confidence": 1.7})["route"] == "REVIEW"
    assert validate_classifier_output({"kind": "tariff", "confidence": 0.8, "set_limit": 1})["route"] == "REVIEW"
    assert validate_classifier_output({"kind": "tariff", "confidence": 0.8})["route"] == "OK"


def test_ledger_counts_one_per_underlying_event_and_detects_tamper():
    s = Store()
    L = Ledger(s)
    comps = [(f"c{i}", 120.0) for i in range(30)]
    for copy in range(20):                                  # 20 wire copies of one tariff story
        ev = tariff(source=f"wire:{copy}", first_received_ns=T0 + copy)
        a = assess(ev, "XNSE:STEEL", graph(), HZ, T0 + 50, "2026-11-02", Impact.POSITIVE, (2, 6), "%", 10, 15,
                   0.0, (1, 1.1, 1), comps, 10, 10**15)
        L.issue("FULL", a)
    rep = L.score({(tariff().cluster_key(), "XNSE:STEEL", "swing_5d"): "up"})
    assert rep["FULL"]["accuracy"] == "1/1" and rep["FULL"]["issued"] == 1
    assert L.verify_chain()
    s.db.execute("DROP TRIGGER events_no_update")
    s.db.execute("UPDATE events SET payload=replace(payload, 'ELIGIBLE', 'NO_ACTION') WHERE id=2")
    assert not L.verify_chain()
