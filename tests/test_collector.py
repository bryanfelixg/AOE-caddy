import json
from aoe2coach.bridge import ReadOnlyBridge
from aoe2coach.collector import record


def test_record_deduplicates_and_writes_jsonl(tmp_path):
    observations = iter([1, 1, 2])
    def fake(player, request):
        op = request["operation"]
        if op == "status":
            return {"ok": True, "data": {"state": "ready"}}
        if op == "open":
            return {"ok": True, "data": {"session": "abc", "information_policy": "player_view"}}
        if op == "close":
            return {"ok": True, "data": {}}
        if op == "observe":
            return {"ok": True, "data": {"world": 1, "sequence": next(observations),
                    "game_time_ms": 5000, "information_policy": "player_view", "entities": []}}
        raise AssertionError(op)

    output = tmp_path / "test.jsonl"
    assert record(ReadOnlyBridge(1, fake), output, hz=10, count=2) == 2
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["observation"]["sequence"] for row in lines] == [1, 2]
    assert all("captured_at_utc" in row for row in lines)


def _transport(samples):
    """samples: list of (world, sequence, game_time_ms)."""
    it = iter(samples)

    def fake(player, request):
        op = request["operation"]
        if op == "status":
            return {"ok": True, "data": {"state": "ready"}}
        if op == "open":
            return {"ok": True, "data": {"session": "abc", "information_policy": "player_view"}}
        if op == "close":
            return {"ok": True, "data": {}}
        world, seq, t = next(it)
        return {"ok": True, "data": {"world": world, "sequence": seq, "game_time_ms": t,
                "information_policy": "player_view",
                "entities": [{"id": 1, "access": "full", "owner": 1}, {"id": 2, "access": "position", "owner": 0},
                             {"id": 3, "access": "position", "owner": 0}]}}
    return fake


def test_skip_paused_saves_only_advancing_game_time(tmp_path):
    # Paused: new sequences, same game time. A new match may restart at the same game time.
    samples = [(1, 1, 1000), (1, 2, 1000), (1, 3, 1000), (1, 4, 1500), (2, 5, 1500)]
    seen = []
    output = tmp_path / "paused.jsonl"
    assert record(ReadOnlyBridge(1, _transport(samples)), output, hz=10, count=3, skip_paused=True,
                  on_observation=lambda o: seen.append(o["sequence"])) == 3
    saved = [json.loads(line)["observation"]["sequence"] for line in output.read_text().splitlines()]
    assert saved == [1, 4, 5]
    assert seen == [1, 2, 3, 4, 5]  # the dashboard still sees every sample


def test_drop_position_only_entities_from_saved_rows(tmp_path):
    seen = []
    output = tmp_path / "compact.jsonl"
    record(ReadOnlyBridge(1, _transport([(1, 1, 1000)])), output, hz=10, count=1, drop_position_only=True,
           on_observation=lambda o: seen.append(len(o["entities"])))
    row = json.loads(output.read_text())
    assert [e["id"] for e in row["observation"]["entities"]] == [1]
    assert row["omitted_position_only_entities"] == 2
    assert seen == [3]  # callbacks receive the full observation


def test_defaults_keep_full_recording(tmp_path):
    output = tmp_path / "full.jsonl"
    record(ReadOnlyBridge(1, _transport([(1, 1, 1000), (1, 2, 1000)])), output, hz=10, count=2)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 2 and all(len(r["observation"]["entities"]) == 3 for r in rows)
    assert all("omitted_position_only_entities" not in r for r in rows)

def test_technologies_every_nth_poll_compacted_and_meta_lines(tmp_path):
    payloads, seq = [], iter(range(1, 100))

    def fake(player, request):
        op = request["operation"]
        if op == "status":
            return {"ok": True, "data": {"state": "ready"}}
        if op == "open":
            return {"ok": True, "data": {"session": "abc", "information_policy": "player_view"}}
        if op == "close":
            return {"ok": True, "data": {}}
        payloads.append(request["payload"]["technologies"])
        n = next(seq)
        data = {"world": 1, "sequence": n, "game_time_ms": n * 500, "information_policy": "player_view",
                "entities": []}
        if request["payload"]["technologies"] == "all":
            data["technologies"] = [{"id": 22, "state": 3, "cost": []}, {"id": 101, "state": 2, "cost": []},
                                    {"id": 8, "state": 1, "cost": []}]
        return {"ok": True, "data": data}

    metas = iter([{"build": "a"}, {"build": "a"}, {"build": "b"}, {"build": "b"}, {"build": "b"}])
    seen = []
    output = tmp_path / "tech.jsonl"
    record(ReadOnlyBridge(1, fake), output, hz=10, count=5, technologies_every=4, compact_technologies=True,
           on_observation=lambda o: seen.append("technologies" in o), meta=lambda: next(metas))
    assert payloads == ["all", "none", "none", "none", "all"]
    assert seen == [True, False, False, False, True]  # callbacks get the full list
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [("meta" in r) for r in rows] == [True, False, False, True, False, False, False]
    assert rows[0]["meta"] == {"build": "a"} and rows[3]["meta"] == {"build": "b"}
    first = rows[1]["observation"]
    assert "technologies" not in first
    assert first["technology_states"] == {"researched": [22], "researching": [101]}
