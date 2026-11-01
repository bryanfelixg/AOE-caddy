import copy
import json
from pathlib import Path

from aoe2coach.analysis import Coach
from aoe2coach.dashboard import CoachPipeline, Hub
from aoe2coach.summary import (
    MetaRecord, RecordingReader, clock, default_summary_path, render_html, render_text, summarize, write_summary,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observation_feudal_p1.json").read_text(encoding="utf-8"))
VILLAGER_IDS = [e["id"] for e in FIXTURE["entities"] if e.get("owner") == 1 and e["class_id"] == 904]
TC_ID = 3638


def obs_at(t, *, idle_ids=(), world=2, age=None, tc_training=False, wood=None):
    o = copy.deepcopy(FIXTURE)
    o["game_time_ms"] = o["sequence"] = t
    o["world"] = world
    if age is not None:
        o["player"]["current_age"] = age
    if wood is not None:
        o["player"]["attributes"][1] = float(wood)
    for e in o["entities"]:
        if e.get("owner") == 1 and e["class_id"] == 904:
            e["idle"] = e["id"] in idle_ids
        if e["id"] == TC_ID and tc_training:
            e.update(train_count=1, progress_type=102, progress_value=50)
    return o


def series(start, end, step=500, **kwargs):
    return [obs_at(t, **kwargs) for t in range(start, end + 1, step)]


def test_villager_idle_episode_and_ongoing_tc_idle():
    vid = VILLAGER_IDS[0]
    observations = (series(0, 4_500) + series(5_000, 15_000, idle_ids={vid}) + series(15_500, 30_000))
    [m] = summarize(observations)
    assert (m.world, m.civilization, m.first_ms, m.last_ms, m.samples) == (2, "VARANGIANS-CIV", 0, 30_000, 61)
    assert (m.villager_idle.count, m.villager_idle.total_ms, m.villager_idle.ongoing) == (1, 10_500, 0)
    # The fixture's TC never trains, so its idle episode is still active when the recording ends.
    assert (m.tc_idle.count, m.tc_idle.total_ms, m.tc_idle.ongoing) == (1, 30_000, 1)
    # 405 wood in Feudal exceeds the 400 allowance for the whole recording.
    assert (m.stockpile["wood"].count, m.stockpile["wood"].ongoing) == (1, 1)
    assert m.stockpile["food"].count == 0
    assert not m.starts_late and m.gaps == 0


def test_short_idle_below_threshold_not_counted():
    vid = VILLAGER_IDS[0]
    [m] = summarize(series(0, 2_000) + series(2_500, 6_500, idle_ids={vid}) + series(7_000, 9_000))
    assert m.villager_idle.count == 0  # 4 s < 5 s threshold, same rule as live


def test_new_match_splits_summaries_and_marks_ongoing():
    vid = VILLAGER_IDS[0]
    first = series(0, 10_000, idle_ids={vid})
    second = series(0, 3_000, world=3, tc_training=True)
    m1, m2 = summarize(first + second)
    assert (m1.world, m2.world) == (2, 3)
    assert (m1.villager_idle.count, m1.villager_idle.ongoing, m1.villager_idle.total_ms) == (1, 1, 10_000)
    assert m2.tc_idle.count == 0 and m2.villager_idle.count == 0


def test_gaps_and_late_start_reported():
    observations = series(120_000, 125_000) + series(140_000, 145_000)
    [m] = summarize(observations)
    assert m.starts_late
    assert (m.gaps, m.gap_ms) == (1, 15_000)
    text = render_text([m])
    assert "Recording started at 2:00" in text and "1 gap without data (0:15 unobserved)" in text
    # Build steps already satisfied at the first sample of a late recording happened before it.
    assert "before recording began" in text


def test_age_ups_and_villager_timeline():
    observations = series(0, 59_500, age=0) + series(60_000, 130_000, age=1)
    [m] = summarize(observations)
    assert m.age_reached == {0: 0, 1: 60_000}
    assert [t for t, _ in m.villager_timeline] == [0, 60_000, 120_000]
    assert "Feudal Age at 1:00" in render_text([m])


def test_parity_with_live_dashboard_pipeline():
    vid = VILLAGER_IDS[0]
    observations = series(0, 4_500) + series(5_000, 15_000, idle_ids={vid}) + series(15_500, 30_000)
    live = CoachPipeline(Hub())
    events = []
    original = live.coach.update

    def spy(state):
        result = original(state)
        events.extend(result)
        return result
    live.coach.update = spy
    for o in observations:
        live.on_observation(o)
    [m] = summarize(observations, Coach())
    live_villager_episodes = sum(1 for e in events if e.action == "cleared" and e.alert.kind.value == "villager_idle")
    assert live_villager_episodes == m.villager_idle.count - m.villager_idle.ongoing == 1
    assert live.sound_seq == 2  # TC idle + villager idle, as summarised


def test_reader_skips_truncated_lines(tmp_path):
    path = tmp_path / "rec.jsonl"
    rows = [json.dumps({"observation": o}) for o in series(0, 2_000)]
    path.write_text("\n".join(rows) + "\n{\"observation\": {\"world\"", encoding="utf-8")
    reader = RecordingReader(path)
    assert len(list(reader)) == 5 and reader.skipped_lines == 1


def test_write_summary_outputs_html_and_text(tmp_path):
    path = tmp_path / "coach-1.jsonl"
    path.write_text("\n".join(json.dumps({"observation": o}) for o in series(0, 30_000)), encoding="utf-8")
    report, text = write_summary(path)
    assert report == tmp_path / "coach-1.summary.html"
    page = report.read_text(encoding="utf-8")
    assert "<svg" in page and "Town Center idle" in page and "Match result is not recorded" in page
    assert "Town Center idle: 1 episode" in text


def test_html_escapes_untrusted_strings():
    o = obs_at(0)
    o["player"]["civilization_name"] = "<script>alert(1)</script>"
    page = render_html(summarize([o, obs_at(500)]), "<b>file</b>.jsonl")
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "&lt;b&gt;file" in page


def test_empty_recording():
    assert render_text([]) == "No usable observations in this recording."
    assert "No usable observations" in render_html([], "x.jsonl")


def test_helpers():
    assert clock(0) == "0:00" and clock(61_999) == "1:01" and clock(-5) == "0:00"
    assert default_summary_path(Path("data/coach-1.jsonl")) == Path("data/coach-1.summary.html")
    assert default_summary_path(Path("x.log")) == Path("x.log.summary.html")


def test_coach_stop_writes_summary(tmp_path, capsys):
    from aoe2coach.dashboard import _write_post_game_summary
    path = tmp_path / "coach-2.jsonl"
    path.write_text("\n".join(json.dumps({"observation": o}) for o in series(0, 5_000)), encoding="utf-8")
    _write_post_game_summary(path)
    out = capsys.readouterr().out
    assert "Writing post-game summary" in out and "Town Center idle" in out and "coach-2.summary.html" in out
    assert (tmp_path / "coach-2.summary.html").exists()


def test_coach_stop_summary_failure_is_only_a_warning(tmp_path, caplog):
    from aoe2coach.dashboard import _write_post_game_summary
    _write_post_game_summary(tmp_path / "missing.jsonl")  # must not raise
    assert "Could not write post-game summary" in caplog.text


def _meta(build_id, digest=None, settings=None):
    return MetaRecord({"build": {"id": build_id, "name": build_id, "digest": digest}, "settings": settings or {}})


def test_meta_selects_build_and_settings_and_reports_adherence():
    from aoe2coach.build_orders import Library, default_build, parse
    build = parse(TEST_BUILD, "test.toml")
    library = Library({"scouts-22-pop": default_build(), build.id: build}, [])
    items = [_meta(build.id, build.digest, {"alerts": {"tc_idle": {"threshold_s": 20}}})]
    items += series(0, 10_000)
    advancing = obs_at(10_500)
    advancing["technologies"] = [{"id": 101, "state": 2}]
    items += [advancing] + series(11_000, 12_000)
    [m] = summarize(items, library=library)
    assert m.build_name == "Test build" and m.build_notes == []
    assert m.tc_idle.count == 0  # recorded 20 s threshold, not the 1 s default
    up = m.age_ups[2]
    assert (up.clicked_ms, up.clicked_villagers, up.planned_villagers) == (10_500, 15, 14)
    stable = next(s for s in m.build_steps if s.id == "stable")
    assert stable.target_villagers == 10 and stable.done_ms == 0 and stable.done_villagers == 15
    text = render_text([m])
    assert "Castle Age: clicked at 0:10 with 15 villagers (plan: 14 villagers)" in text
    assert "Build order (Test build):" in text and "(plan: 10)" in text


def test_meta_notes_for_edited_or_missing_builds():
    from aoe2coach.build_orders import Library, default_build, parse
    build = parse(TEST_BUILD, "test.toml")
    library = Library({"scouts-22-pop": default_build(), build.id: build}, [])
    [m] = summarize([_meta(build.id, "old-digest")] + series(0, 1_000), library=library)
    assert any("edited since this recording" in n for n in m.build_notes)
    [m] = summarize([_meta("deleted")] + series(0, 1_000), library=library)
    assert any("no longer available" in n for n in m.build_notes) and m.build_name.startswith("22-pop")
    [m] = summarize(series(0, 1_000), library=library)
    assert any("no build-order metadata" in n for n in m.build_notes)


def test_reader_yields_meta_records(tmp_path):
    path = tmp_path / "rec.jsonl"
    path.write_text(json.dumps({"meta": {"build": {"id": "x"}}}) + "\n" + json.dumps({"observation": obs_at(0)}),
                    encoding="utf-8")
    items = list(RecordingReader(path))
    assert isinstance(items[0], MetaRecord) and items[0]["build"]["id"] == "x"
    assert len(items) == 2


TEST_BUILD = """
format = 1
id = "test-build"
name = "Test build"
[[steps]]
villagers = 10
age = "feudal"
task = "Stable"
id = "stable"
build = "Stable"
[[steps]]
villagers = 14
task = "Click Castle"
age_up = "castle"
"""
