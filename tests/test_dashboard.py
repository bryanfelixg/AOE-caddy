import copy
import http.client
import json
from pathlib import Path
import threading

import pytest

from aoe2coach.analysis import Coach
from aoe2coach.bridge import ReadOnlyBridge
from aoe2coach.collector import record
from aoe2coach.dashboard import CoachPipeline, Hub, build_view, make_server, replay
from aoe2coach.telemetry import PlayerState, Resources, TownCenter, TownCenterState, Villager

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observation_feudal_p1.json").read_text(encoding="utf-8"))


def obs_at(t, *, idle_ids=(), world=None):
    o = copy.deepcopy(FIXTURE)
    o["game_time_ms"] = t
    o["sequence"] = t
    if world is not None:
        o["world"] = world
    for e in o["entities"]:
        if e.get("owner") == 1 and e["class_id"] == 904:
            e["idle"] = e["id"] in idle_ids
    return o


VILLAGER_IDS = [e["id"] for e in FIXTURE["entities"] if e.get("owner") == 1 and e["class_id"] == 904]


def state(t, idle=(), tc_state=TownCenterState.TRAINING, wood=0):
    return PlayerState(world=1, sequence=t, game_time_ms=t, player_id=1, current_age=0, housing_headroom=5,
                       resources=Resources(0, wood, 0, 0),
                       villagers=tuple(Villager(i, 83, i in idle, 0.0, 0.0) for i in range(1, 4)),
                       town_centers=(TownCenter(9, 1, 102, 10, tc_state, 0.0, 0.0),), villager_queue=None)


# --- view model ----------------------------------------------------------------------

def test_view_without_state_shows_connection_only():
    view = build_view(None, Coach(), status="connecting", message="Starting", sound_seq=0, updated_at=None)
    assert view["connection"] == {"status": "connecting", "message": "Starting", "updated_at": None,
                                  "game_advanced_at": None}
    assert view["game"] is None and view["alerts"] == []
    json.dumps(view)


def test_view_groups_idle_villagers_and_orders_by_severity():
    coach = Coach(build_order=())
    for t in range(0, 25_001, 500):
        coach.update(state(t, idle={1, 2}, tc_state=TownCenterState.IDLE, wood=500))
    s = state(25_000, idle={1, 2}, tc_state=TownCenterState.IDLE, wood=500)
    view = build_view(s, coach, status="live", message="", sound_seq=3, updated_at=123.0)
    titles = [(a["severity"], a["title"]) for a in view["alerts"]]
    assert ("high", "2 idle villagers") in titles
    assert ("high", "Town Center idle - queue a villager") in titles
    assert ("medium", "500 wood banked - spend it") in titles
    assert [a["severity"] for a in view["alerts"]] == sorted((a["severity"] for a in view["alerts"]),
                                                            key=["high", "medium", "low"].index)
    villager_alert = next(a for a in view["alerts"] if a["kind"] == "villager_idle")
    assert villager_alert["seconds"] == 25
    assert view["resources"]["wood"] == {"amount": 500, "stockpiling": True}
    assert view["resources"]["food"]["stockpiling"] is False
    assert view["game"]["idle_villagers"] == 2 and view["game"]["age"] == "Dark Age"
    json.dumps(view)


def test_view_build_order_progress_from_real_fixture():
    pipeline = CoachPipeline(Hub())
    pipeline.on_observation(obs_at(FIXTURE["game_time_ms"]))
    view = pipeline.hub.snapshot()[1]
    status = {s["id"]: s["status"] for s in view["build_order"]}
    assert status["lumber_camp"] == "done" and status["mill"] == "done" and status["stable"] == "done"
    assert status["scouts"] == "pending"  # stable is still a foundation
    assert view["connection"]["status"] == "live"
    assert view["next_villager"]


# --- pipeline -------------------------------------------------------------------------

def test_sound_seq_counts_only_sounding_raises():
    pipeline = CoachPipeline(Hub(), clock=lambda: 1.0)
    vid = VILLAGER_IDS[0]
    for t in range(1_000_000, 1_006_001, 500):
        pipeline.on_observation(obs_at(t, idle_ids={vid}))
    # TC idle (1 s) + villager idle (5 s) both sound; wood stockpile (20 s) has not been reached
    assert pipeline.sound_seq == 2
    version, view = pipeline.hub.snapshot()
    assert version > 0 and view["sound_seq"] == 2


def test_paused_game_keeps_updating_but_freezes_game_advanced_at():
    # Live recording showed the bridge keeps sending new sequences with frozen game time while paused.
    wall = iter([100.0, 101.0, 102.0, 103.0, 104.0])
    pipeline = CoachPipeline(Hub(), clock=lambda: next(wall))
    o = obs_at(5_000)
    for seq in range(4):
        o = copy.deepcopy(o)
        o["sequence"] += 1
        pipeline.on_observation(o)
    conn = pipeline.hub.snapshot()[1]["connection"]
    assert (conn["updated_at"], conn["game_advanced_at"]) == (103.0, 100.0)
    pipeline.on_observation(obs_at(5_800))
    conn = pipeline.hub.snapshot()[1]["connection"]
    assert conn["game_advanced_at"] == conn["updated_at"] == 104.0


def test_unusable_observation_reports_status_instead_of_crashing():
    pipeline = CoachPipeline(Hub())
    bad = obs_at(1_000)
    bad["information_policy"] = "omniscient"
    pipeline.on_observation(bad)
    view = pipeline.hub.snapshot()[1]
    assert view["connection"]["status"] == "waiting"
    assert "player_view" in view["connection"]["message"]


def test_bridge_status_message_published():
    pipeline = CoachPipeline(Hub())
    pipeline.on_status("Bridge not ready: waiting_for_match")
    assert pipeline.hub.snapshot()[1]["connection"]["message"] == "Bridge not ready: waiting_for_match"


def test_hub_wait_returns_on_publish_and_close():
    hub = Hub()
    version, _ = hub.snapshot()
    threading.Timer(0.05, hub.publish, args=({"x": 1},)).start()
    new_version, view = hub.wait(version, timeout=2)
    assert new_version == version + 1 and view == {"x": 1}
    threading.Timer(0.05, hub.close).start()
    assert hub.wait(new_version, timeout=2)[0] == new_version
    assert hub.closed


# --- replay ---------------------------------------------------------------------------

def test_replay_paces_by_game_time(tmp_path):
    path = tmp_path / "r.jsonl"
    rows = [obs_at(1_000), obs_at(3_000), obs_at(4_000), obs_at(500, world=9)]
    path.write_text("\n".join(json.dumps({"captured_at_utc": "x", "observation": o}) for o in rows) + "\n\n")
    seen, sleeps = [], []
    assert replay(path, lambda o: seen.append(o["game_time_ms"]), speed=2.0, sleep=sleeps.append) == 4
    assert seen == [1_000, 3_000, 4_000, 500]
    assert sleeps == [1.0, 0.5]  # no sleep across a world change


def test_replay_through_pipeline_matches_live_rules(tmp_path):
    vid = VILLAGER_IDS[0]
    rows = [obs_at(t, idle_ids={vid}) for t in range(2_000_000, 2_006_001, 500)]
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(json.dumps({"observation": o}) for o in rows))
    replayed = CoachPipeline(Hub())
    replay(path, replayed.on_observation, speed=0)
    live = CoachPipeline(Hub())
    for o in rows:
        live.on_observation(o)
    assert replayed.hub.snapshot()[1]["alerts"] == live.hub.snapshot()[1]["alerts"]
    assert replayed.sound_seq == live.sound_seq == 2


# --- HTTP server ------------------------------------------------------------------------

@pytest.fixture
def server():
    hub = Hub()
    srv = make_server(hub, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield hub, srv.server_address[1]
    hub.close()
    srv.shutdown()
    srv.server_close()


def get(port, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    return resp, conn


def test_server_binds_loopback_only():
    srv = make_server(Hub(), port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_second_server_on_same_port_fails_clearly():
    first = make_server(Hub(), port=0)
    try:
        with pytest.raises(OSError, match="already in use"):
            make_server(Hub(), port=first.server_address[1])
    finally:
        first.server_close()


def test_serves_page_and_state(server):
    hub, port = server
    resp, conn = get(port, "/")
    body = resp.read().decode("utf-8")
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/html")
    assert "EventSource" in body and "AOE-caddy" in body
    assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
    conn.close()
    hub.publish({"hello": "world"})
    resp, conn = get(port, "/state")
    assert json.loads(resp.read()) == {"hello": "world"}
    conn.close()


def test_rejects_foreign_host_header_and_unknown_path(server):
    _, port = server
    resp, conn = get(port, "/state", host=f"evil.example:{port}")
    assert resp.status == 403
    conn.close()
    resp, conn = get(port, "/nope")
    assert resp.status == 404
    conn.close()


def test_event_stream_pushes_updates(server):
    hub, port = server
    resp, conn = get(port, "/events")
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"

    def next_event():
        while True:
            line = resp.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])

    first = next_event()
    assert first["connection"]["status"] == "connecting"
    hub.publish({"n": 2})
    assert next_event() == {"n": 2}
    conn.close()


# --- collector callbacks ----------------------------------------------------------------

def fake_transport(sequences):
    it = iter(sequences)

    def fake(player, request):
        op = request["operation"]
        if op == "status":
            return {"ok": True, "data": {"state": "ready"}}
        if op == "open":
            return {"ok": True, "data": {"session": "s", "information_policy": "player_view"}}
        if op == "close":
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {"world": 1, "sequence": next(it), "game_time_ms": 1,
                                     "information_policy": "player_view", "entities": []}}
    return fake


def test_record_without_output_calls_back_on_new_observations():
    seen = []
    assert record(ReadOnlyBridge(1, fake_transport([1, 1, 2])), None, hz=10, count=2,
                  on_observation=lambda o: seen.append(o["sequence"])) == 2
    assert seen == [1, 2]


def test_record_reports_bridge_status():
    calls = {"n": 0}

    def fake(player, request):
        if request["operation"] == "status":
            calls["n"] += 1
            return {"ok": True, "data": {"state": "waiting_for_match" if calls["n"] == 1 else "ready"}}
        return fake_transport([5])(player, request)

    messages = []
    record(ReadOnlyBridge(1, fake), None, hz=10, count=1, recovery_delay_s=0, on_status=messages.append)
    assert messages == ["Bridge not ready: waiting_for_match"]
