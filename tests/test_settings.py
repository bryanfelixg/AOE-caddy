import json

import pytest

from aoe2coach import settings as s
from aoe2coach.settings import Settings, SettingsError


def test_defaults_match_previous_hard_coded_behaviour():
    d = Settings()
    assert (d.alerts.villager_idle.threshold_s, d.alerts.tc_idle.threshold_s, d.alerts.housed.threshold_s) == (5, 1, 1)
    assert (d.alerts.stockpile.threshold_s, d.alerts.stockpile.limit_dark_feudal,
            d.alerts.stockpile.limit_castle_imperial) == (20, 400, 800)
    assert d.alerts.house_soon.headroom == 2 and not d.alerts.build_order.sound
    assert (d.sound.volume, d.sound.min_gap_s) == (80, 2.0)
    assert (d.connection.player, d.connection.port, d.connection.hz) == (1, 8765, 2.0)
    assert d.recording.enabled and d.recording.compact and d.build_order == "scouts-22-pop"


def test_partial_overlay_keeps_other_values():
    result = s.from_dict({"alerts": {"tc_idle": {"threshold_s": 3}}, "sound": {"volume": 40}})
    assert result.alerts.tc_idle.threshold_s == 3.0 and result.alerts.tc_idle.enabled
    assert result.sound.volume == 40 and result.alerts.villager_idle.threshold_s == 5


def test_every_problem_is_reported():
    with pytest.raises(SettingsError) as caught:
        s.from_dict({"alerts": {"tc_idle": {"threshold_s": -1, "enabled": "yes"}, "bogus": {}},
                     "connection": {"port": 80, "player": 2.5}, "build_order": "", "extra": 1})
    errors = caught.value.errors
    assert "alerts.tc_idle.threshold_s: must be between 0 and 600" in errors
    assert "alerts.tc_idle.enabled: expected true or false" in errors
    assert "alerts.bogus: unknown setting" in errors
    assert "connection.port: must be between 1024 and 65535" in errors
    assert "connection.player: expected a whole number" in errors
    assert "build_order: expected non-empty text" in errors
    assert "extra: unknown setting" in errors


def test_bool_is_not_a_number_and_numbers_are_coerced():
    with pytest.raises(SettingsError):
        s.from_dict({"sound": {"volume": True}})
    assert isinstance(s.from_dict({"connection": {"hz": 3}}).connection.hz, float)
    assert s.from_dict({"connection": {"port": 9000.0}}).connection.port == 9000


def test_load_save_roundtrip_and_missing_file(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    assert s.load(path) == Settings()
    custom = s.from_dict({"alerts": {"villager_idle": {"threshold_s": 7}}, "build_order": "my-build"})
    s.save(custom, path)
    assert s.load(path) == custom
    assert json.loads(path.read_text())["build_order"] == "my-build"
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]  # no temp files left


def test_corrupt_file_is_reported_and_not_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="not valid JSON"):
        s.load(path)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_save_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    s.save(Settings(), path)
    before = path.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(s.os, "replace", boom)
    with pytest.raises(OSError):
        s.save(s.from_dict({"sound": {"volume": 1}}), path)
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_overrides_take_precedence_and_are_validated():
    base = s.from_dict({"connection": {"port": 9000}})
    result = s.with_overrides(base, {"connection.port": 9100, "build_order": "x"})
    assert result.connection.port == 9100 and result.build_order == "x"
    with pytest.raises(SettingsError):
        s.with_overrides(base, {"connection.port": 1})


def test_describe_lists_fields_with_bounds_and_restart_flags():
    fields = {f["path"]: f for f in s.describe()}
    assert fields["alerts.tc_idle.threshold_s"]["min"] == 0 and fields["alerts.tc_idle.threshold_s"]["default"] == 1
    assert fields["connection.port"]["restart"] and not fields["sound.volume"]["restart"]
    assert fields["build_order"]["type"] == "str"
