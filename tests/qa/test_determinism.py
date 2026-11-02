"""Item 1: identical replay inputs -> identical decisions."""
from trader import cli


def test_replay_twice_same_decision_hash(tmp_path):
    md = str(tmp_path / "md.db")
    assert cli.synth(md, 13, 11) > 0
    a = cli.replay(md)
    b = cli.replay(md, str(tmp_path / "state.db"))               # file-backed state, fresh
    assert a.decisions == b.decisions and a.decision_hash() == b.decision_hash()
    assert any("|True|" in d for d in a.decisions), "no accepted order: test exercises nothing"
    assert a.store.positions() == b.store.positions()
    assert [(o.client_id, o.state, o.filled_qty) for o in a.store.orders()] == \
           [(o.client_id, o.state, o.filled_qty) for o in b.store.orders()]

