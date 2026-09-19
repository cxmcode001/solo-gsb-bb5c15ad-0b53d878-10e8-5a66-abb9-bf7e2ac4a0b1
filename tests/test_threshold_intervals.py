"""Tests for GET /participants/{id}/threshold-intervals."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "threshold-test.db"))
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _intervals(client, participant_id="alice", threshold=5, **params):
    params.setdefault("from_revision", 0)
    params.setdefault("to_revision", 1)
    r = client.get(
        f"/participants/{participant_id}/threshold-intervals",
        params={"threshold": threshold, **params},
    )
    assert r.status_code == 200, params
    return r.json()


def test_merges_consecutive_qualifying_revisions(client):
    _post(client, "e1", "alice", 10)   # rev1: 10
    _post(client, "e2", "alice", -8)   # rev2: 2
    _post(client, "e3", "alice", 3)    # rev3: 5
    _post(client, "e4", "carol", 99)   # rev4: 5 (third party)
    _post(client, "e5", "alice", 1)    # rev5: 6
    _post(client, "e6", "alice", -6)   # rev6: 0
    _post(client, "e7", "alice", 5)    # rev7: 5

    body = _intervals(client, threshold=5, from_revision=0, to_revision=7)
    assert body["participant_id"] == "alice"
    assert body["threshold"] == 5
    assert body["from_revision"] == 0
    assert body["to_revision"] == 7
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 10, "end_points": 10},
        # rev3..rev5 merge: carol's rev4 event leaves alice at 5
        {"start_revision": 3, "end_revision": 5, "start_points": 5, "end_points": 6},
        {"start_revision": 7, "end_revision": 7, "start_points": 5, "end_points": 5},
    ]
    assert [i["start_revision"] for i in body["intervals"]] == sorted(
        i["start_revision"] for i in body["intervals"]
    )


def test_threshold_is_inclusive(client):
    _post(client, "e1", "alice", 5)   # rev1: 5
    _post(client, "e2", "alice", -1)  # rev2: 4
    body = _intervals(client, threshold=5, from_revision=0, to_revision=2)
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 5, "end_points": 5}
    ]


def test_non_positive_threshold_qualifies_from_zero(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", -20)  # rev2: -10
    # threshold 0: revision 0 and 1 qualify, -10 does not
    body = _intervals(client, threshold=0, from_revision=0, to_revision=2)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "start_points": 0, "end_points": 10}
    ]
    # a negative threshold also catches the -10 total
    body = _intervals(client, threshold=-15, from_revision=0, to_revision=2)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 2, "start_points": 0, "end_points": -10}
    ]


def test_subrange_does_not_extend_across_its_boundary(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", -10)
    _post(client, "e3", "alice", 10)
    # rev1 qualifies but starts before the window: no extension backwards
    body = _intervals(client, threshold=5, from_revision=2, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 10, "end_points": 10}
    ]


def test_equal_from_and_to_single_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", -10)
    assert _intervals(client, threshold=5, from_revision=2, to_revision=2)["intervals"] == []
    assert _intervals(client, threshold=5, from_revision=1, to_revision=1)["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 10, "end_points": 10}
    ]


def test_unknown_participant_is_zero_points(client):
    _post(client, "e1", "alice", 10)
    body = _intervals(client, participant_id="ghost", threshold=1,
                      from_revision=0, to_revision=1)
    assert body["intervals"] == []
    # threshold 0: always qualifies, endpoints carry the 0 total
    body = _intervals(client, participant_id="ghost", threshold=0,
                      from_revision=0, to_revision=1)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "start_points": 0, "end_points": 0}
    ]


def test_empty_ledger_revision_zero(client):
    body = _intervals(client, threshold=0, from_revision=0, to_revision=0)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "start_points": 0, "end_points": 0}
    ]
    assert _intervals(client, threshold=1, from_revision=0, to_revision=0)["intervals"] == []


def test_correction_chain_counts_only_tip(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 3, correction_of="e1")   # rev2: 3
    _post(client, "e3", "alice", 8, correction_of="e2")   # rev3: 8
    body = _intervals(client, threshold=5, from_revision=1, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 10, "end_points": 10},
        {"start_revision": 3, "end_revision": 3, "start_points": 8, "end_points": 8},
    ]


def test_cross_participant_correction_drops_and_rises(client):
    _post(client, "e1", "alice", 10)   # rev1: alice 10, bob 0
    _post(client, "e2", "bob", 5)      # rev2: alice 10, bob 5
    # correction moves the 10-point event from alice to bob
    _post(client, "e3", "bob", 10, correction_of="e1")  # rev3: alice 0, bob 15
    _post(client, "e4", "alice", 7, correction_of="e3")  # rev4: alice 7, bob 5

    alice = _intervals(client, "alice", threshold=8, from_revision=0, to_revision=4)
    assert alice["intervals"] == [
        {"start_revision": 1, "end_revision": 2, "start_points": 10, "end_points": 10}
    ]
    bob = _intervals(client, "bob", threshold=8, from_revision=0, to_revision=4)
    assert bob["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 15, "end_points": 15}
    ]
    # the incoming participant rises through the cross-participant correction;
    # rev2 (5), rev3 (15) and rev4 (5) all qualify, so they merge into one
    # interval whose endpoints carry the boundary totals
    bob_low = _intervals(client, "bob", threshold=5, from_revision=0, to_revision=4)
    assert bob_low["intervals"] == [
        {"start_revision": 2, "end_revision": 4, "start_points": 5, "end_points": 5},
    ]


def test_forked_correction_superseded_only_once(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # second branch off e1
    # totals: rev1 10, rev2 12, rev3 32 (both tips effective; e1 debited once)
    body = _intervals(client, threshold=10, from_revision=0, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 32},
    ]
    # threshold above every branch tip but below their sum proves the fork
    # keeps BOTH leaves effective (no double subtraction of the ancestor)
    body = _intervals(client, threshold=20, from_revision=0, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 32, "end_points": 32}
    ]


def test_fork_branch_reattributed_to_other_participant(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")   # fork: alice at 32
    _post(client, "e4", "bob", 25, correction_of="e3")     # e3 branch -> bob
    # alice: rev1 10, rev2 12, rev3 32, rev4 12 — one interval rev2..rev4
    alice = _intervals(client, "alice", threshold=11, from_revision=0, to_revision=4)
    assert alice["intervals"] == [
        {"start_revision": 2, "end_revision": 4, "start_points": 12, "end_points": 12},
    ]
    bob = _intervals(client, "bob", threshold=10, from_revision=0, to_revision=4)
    assert bob["intervals"] == [
        {"start_revision": 4, "end_revision": 4, "start_points": 25, "end_points": 25}
    ]


def test_matches_leaderboard_total_at_every_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    _post(client, "e3", "alice", 20, correction_of="e1")
    _post(client, "e4", "bob", 1, correction_of="e2")
    _post(client, "e5", "carol", 100)

    body = _intervals(client, threshold=-1000, from_revision=0, to_revision=5)
    # with such a low threshold one interval spans the whole window
    assert len(body["intervals"]) == 1
    span = body["intervals"][0]
    points_by_rev = {0: 0}
    for rev in range(1, 6):
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        totals = {e["participant_id"]: e["total_points"] for e in lb["entries"]}
        points_by_rev[rev] = totals.get("alice", 0)
    assert span["start_points"] == points_by_rev[0]
    assert span["end_points"] == points_by_rev[5]
    # and per-revision boundary values agree with /leaderboard for a mid threshold
    body = _intervals(client, threshold=15, from_revision=0, to_revision=5)
    for interval in body["intervals"]:
        assert interval["start_points"] == points_by_rev[interval["start_revision"]]
        assert interval["end_points"] == points_by_rev[interval["end_revision"]]
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            assert points_by_rev[rev] >= 15
    covered = {
        rev
        for i in body["intervals"]
        for rev in range(i["start_revision"], i["end_revision"] + 1)
    }
    assert covered == {rev for rev, pts in points_by_rev.items() if pts >= 15}


def test_query_is_read_only(client):
    _post(client, "e1", "alice", 10)
    before = client.get("/events").json()
    for params in (
        {"threshold": 5, "from_revision": 0, "to_revision": 1},
        {"threshold": 0, "from_revision": 0, "to_revision": 0},
    ):
        r = client.get("/participants/ghost/threshold-intervals", params=params)
        assert r.status_code == 200
    assert client.get("/events").json() == before
    assert client.get("/leaderboard").json()["revision"] == 1


def test_missing_params_422(client):
    url = "/participants/alice/threshold-intervals"
    assert client.get(url).status_code == 422
    assert client.get(url, params={"threshold": 1, "to_revision": 1}).status_code == 422
    assert client.get(url, params={"threshold": 1, "from_revision": 0}).status_code == 422
    assert client.get(
        url, params={"from_revision": 0, "to_revision": 1}
    ).status_code == 422


def test_non_integer_params_422(client):
    r = client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": "x", "from_revision": 0, "to_revision": 1},
    )
    assert r.status_code == 422
    r = client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": "0", "to_revision": "x"},
    )
    assert r.status_code == 422


def test_negative_from_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": -1, "to_revision": 1},
    ).status_code == 422


def test_from_greater_than_to_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": 2, "to_revision": 1},
    ).status_code == 422


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": 0, "to_revision": 2},
    ).status_code == 404


def test_to_revision_beyond_current_404_empty(client):
    # empty ledger: any to_revision > 0 is out of range
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": 0, "to_revision": 99},
    ).status_code == 404


def test_ordering_checked_before_range(client):
    # both out of range and wrongly ordered: ordering wins -> 422
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 1, "from_revision": 9, "to_revision": 5},
    ).status_code == 422
