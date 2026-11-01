import pytest

from aoe2coach.bridge import BridgeProtocolError, BridgeRemoteError, ReadOnlyBridge


def test_protocol_and_read_only_allowlist():
    requests = []

    def fake(player, request):
        requests.append(request)
        if request["operation"] == "open":
            return {"ok": True, "data": {"session": "abc", "information_policy": "player_view"}}
        return {"ok": True, "data": {"world": 1, "sequence": 2, "information_policy": "player_view"}}

    bridge = ReadOnlyBridge(1, fake)
    bridge.open()
    obs = bridge.observe()
    bridge.close()
    assert obs["world"] == 1
    assert [r["operation"] for r in requests] == ["open", "observe", "close"]
    assert requests[1]["session"] == "abc"
    assert requests[1]["payload"] == {"object_types": "none", "technologies": "none"}
    with pytest.raises(ValueError):
        bridge.request("act", {"actions": []})


def test_refuses_omniscient_and_closes_session():
    ops = []

    def fake(player, request):
        ops.append(request["operation"])
        if request["operation"] == "open":
            return {"ok": True, "data": {"session": "s", "information_policy": "omniscient"}}
        return {"ok": True, "data": {}}

    bridge = ReadOnlyBridge(1, fake)
    with pytest.raises(BridgeProtocolError, match="omniscient"):
        bridge.open()
    assert ops == ["open", "close"]
    assert bridge.session is None


def test_remote_error():
    bridge = ReadOnlyBridge(2, lambda p, r: {"ok": False, "error": {"code": "player_in_use", "message": "busy"}})
    with pytest.raises(BridgeRemoteError) as caught:
        bridge.open()
    assert caught.value.code == "player_in_use"


def test_rejects_observation_policy_change():
    def fake(p, r):
        if r["operation"] == "open":
            return {"ok": True, "data": {"session": "s", "information_policy": "player_view"}}
        return {"ok": True, "data": {"information_policy": "omniscient"}}
    bridge = ReadOnlyBridge(1, fake)
    bridge.open()
    with pytest.raises(BridgeProtocolError):
        bridge.observe()
