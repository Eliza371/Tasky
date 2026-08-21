from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def test_workspace(monkeypatch):
    path = Path(__file__).resolve().parents[1] / ".test-runs" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    monkeypatch.setenv("TASKY_DB", str(path / "test.db"))
    return path


def load_db(test_workspace, monkeypatch):
    import importlib
    import src.db as db
    return importlib.reload(db)


def test_redeem_is_single_use(test_workspace, monkeypatch):
    db = load_db(test_workspace, monkeypatch)
    db.init()
    db.create_code("ABCD-1234")
    assert db.redeem_code("abcd-1234", 10) == "ok"
    assert db.redeem_code("ABCD-1234", 11) == "used"
    assert db.has_access(10)
    assert not db.has_access(11)


def test_pending_delivery_is_per_recipient(test_workspace, monkeypatch):
    db = load_db(test_workspace, monkeypatch)
    db.init()
    db.grant_access(10)
    db.grant_access(11)
    db.add_subscriber(10, ["bounty"])
    db.add_subscriber(11, ["bounty"])
    db.insert("Task", "https://example.test/1", "test", "bounty")
    pending = db.get_pending_deliveries()
    assert {row[-1] for row in pending} == {10, 11}
    task_id = pending[0][0]
    db.record_delivery(task_id, 10, True)
    assert {row[-1] for row in db.get_pending_deliveries()} == {11}


def test_categories_are_validated(test_workspace, monkeypatch):
    db = load_db(test_workspace, monkeypatch)
    db.init()
    db.add_subscriber(10, ["bounty", "bogus"])
    assert db.get_categories(10) == ["bounty"]
