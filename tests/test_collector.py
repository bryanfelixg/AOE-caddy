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
