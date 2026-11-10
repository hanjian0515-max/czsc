"""Deterministic monitor safety checks; fixtures are synthetic, not performance data."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import struct
import threading
from types import SimpleNamespace

import pytest
import pandas as pd

from alerts.monitor import AlertStore, CHINA, LocalMonitor, RULES, latest_completed_bar


UNIVERSE = [{"symbol": "600660.SH", "name": "福耀玻璃", "asset_type": "stock"},
            {"symbol": "300308.SZ", "name": "中际旭创", "asset_type": "stock"}]


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=CHINA)


def event(bar="2026-11-09T10:00:00", rule="buy2", symbol="600660.SH", **extra):
    return {"symbol": symbol, "rule": rule, "bar_end": bar, "origin_time": bar,
            "message": "测试结构条件", "evidence": {"close": 12.5}, **extra}


def analysis(asof="2026-11-09T10:00:00", symbol="600660.SH", events=None, **extra):
    return {"symbol": symbol, "asof": asof, "status": "ready",
            "events": [event(asof, symbol=symbol)] if events is None and asof else events or [],
            "quality": {"alertable": True}, "snapshot": {"close": 12.5},
            "bars30_count": 201, **extra}


@pytest.fixture
def harness(tmp_path):
    source = tmp_path / "vipdoc"
    source.mkdir()
    paths = {item["symbol"]: source / (item["symbol"] + ".lc5") for item in UNIVERSE}
    for path in paths.values():
        path.write_bytes(b"readonly fixture")
    state = SimpleNamespace(now=stamp("2026-11-09T10:05:00"),
                            analyses={s: analysis(symbol=s) for s in paths},
                            paths=paths, calls=[], store=AlertStore(tmp_path / "app" / "alerts.sqlite3"),
                            source=source)

    def discoverer(root, symbols):
        state.calls.append(("discover", Path(root), tuple(symbols)))
        return {s: p for s, p in state.paths.items() if s in symbols}

    def reader(path, max_records):
        state.calls.append(("read", Path(path), max_records))
        return next(s for s, p in paths.items() if p == path)

    def analyzer(frame, warmup, now):
        state.calls.append(("analyze", frame, warmup, now))
        value = state.analyses[frame]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    state.dependencies = dict(analyzer=analyzer, reader=reader, discoverer=discoverer,
                              now=lambda: state.now, background=False)
    state.monitor = LocalMonitor(UNIVERSE, state.store, **state.dependencies)
    state.monitor.configure({"data_source": "tdx_local", "source_dir": str(source), "symbols": ["600660.SH"]})

    def advance(at="2026-11-09T10:35:00", asof="2026-11-09T10:30:00", **changes):
        state.now = stamp(at)
        state.analyses["600660.SH"] = analysis(asof, **changes)

    state.advance = advance
    yield state
    state.monitor.close()


def stock(monitor, symbol="600660.SH"):
    return next(s for s in monitor.status()["stocks"] if s["symbol"] == symbol)


def test_store_config_and_events_persist_without_mutating_inputs(tmp_path):
    path = tmp_path / "app" / "alerts.sqlite3"
    store = AlertStore(path)
    assert store.max_id() == 0 and store.load_config() is None
    config = {"source_dir": "D:\\通达信", "rules": ["buy2"]}
    store.save_config(config)
    value = event(message="包含 ' 引号与中文", evidence={"close": 12.5, "nested": [1, 2]})
    before = deepcopy(value)
    saved = store.add_event(value)
    assert value == before
    assert saved["created_at"].endswith("+08:00") and saved["bar_end"].endswith("+08:00")
    reopened = AlertStore(path)
    assert reopened.load_config() == config
    assert reopened.events_after() == [saved]
    assert reopened.max_id() == saved["id"]


def test_real_duplicates_canonicalize_timezone_but_test_events_stay_separate(tmp_path):
    store = AlertStore(tmp_path / "events.db")
    first = store.add_event(event())
    assert store.add_event(event("2026-11-09T02:00:00+00:00")) is None
    assert store.add_event(event(rule="buy3"))["id"] > first["id"]
    one = store.add_event(event(is_test=True))
    two = store.add_event(event(is_test=True))
    assert two["id"] > one["id"] and len(store.events_after()) == 4
    assert all(row["is_test"] for row in store.events_after(one["id"] - 1))


def test_store_pagination_and_invalid_payloads(tmp_path):
    store = AlertStore(tmp_path / "events.db")
    rows = [store.add_event(event(rule=rule)) for rule in RULES]
    assert store.events_after(rows[0]["id"], 2) == rows[1:3]
    for kwargs in ({"after": -1}, {"after": True}, {"limit": 0}, {"limit": 501}, {"limit": "2"}):
        with pytest.raises(ValueError):
            store.events_after(**kwargs)
    for payload in ({}, event(bar="bad date"), event(is_test=1), event(evidence={"bad": float("nan")})):
        with pytest.raises(ValueError):
            store.add_event(payload)
    assert len(store.events_after()) == 4


def test_store_deduplicates_concurrent_writers(tmp_path):
    store = AlertStore(tmp_path / "events.db")
    errors = []

    def write():
        try:
            for _ in range(8):
                store.add_event(event())
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=write) for _ in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert not errors and len(store.events_after()) == 1


@pytest.mark.parametrize("at,expected", [("09:59:59", None), ("10:00:00", "10:00:00"),
    ("10:29:59", "10:00:00"), ("11:30:00", "11:30:00"), ("13:25:00", "11:30:00"),
    ("13:30:00", "13:30:00"), ("20:00:00", "15:00:00")])
def test_latest_completed_bar_uses_exchange_session_not_wallclock_age(at, expected):
    result = latest_completed_bar(stamp("2026-11-09T" + at))
    assert result == (stamp("2026-11-09T" + expected) if expected else None)
    assert latest_completed_bar("2026-11-09T05:25:00+00:00") == stamp("2026-11-09T11:30:00")


def test_first_scan_is_silent_then_new_closed_bar_alerts_once(harness):
    h = harness
    h.monitor.start()
    assert h.monitor.scan_once() and h.store.events_after() == []
    assert stock(h.monitor)["state"] == "ready"
    h.advance(events=[event("2026-11-09T10:30:00", rule=r) for r in RULES])
    h.monitor.scan_once()
    assert [e["rule"] for e in h.store.events_after()] == list(RULES)
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 4
    status = h.monitor.status()
    assert status["max_alert_id"] == h.store.max_id()
    assert status["last_cycle"].endswith("+08:00") and stock(h.monitor)["last_scan"].endswith("+08:00")
    assert stock(h.monitor)["snapshot"]["close"] == 12.5
    assert all(call[2] == 12000 for call in h.calls if call[0] == "read")
    assert all(call[2] == 200 for call in h.calls if call[0] == "analyze")
    assert all(p.read_bytes() == b"readonly fixture" for p in h.paths.values())


def test_restart_and_every_resume_rebaseline_without_replaying(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.monitor.scan_once()
    original = h.store.events_after()
    h.monitor.pause()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00")
    assert h.monitor.scan_once() is False
    h.monitor.start()
    h.monitor.scan_once()
    assert h.store.events_after() == original
    h.monitor.close()
    reopened = LocalMonitor(UNIVERSE, AlertStore(h.store.path), **h.dependencies)
    try:
        reopened.start()
        reopened.scan_once()
        assert h.store.events_after() == original
        h.advance("2026-11-09T11:35:00", "2026-11-09T11:30:00")
        reopened.scan_once()
        assert len(h.store.events_after()) == 2
    finally:
        reopened.close()


def test_rollback_and_revisit_do_not_duplicate_durable_signal(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.monitor.scan_once()
    h.monitor.pause()
    h.advance("2026-11-09T10:05:00", "2026-11-09T10:00:00")
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


@pytest.mark.parametrize("at,asof,state", [
    ("2026-11-09T10:35:00", "2026-11-09T10:00:00", "stale"),
    ("2026-11-10T10:05:00", "2026-11-09T15:00:00", "stale"),
    ("2026-11-09T09:59:00", "2026-11-08T15:00:00", "stale"),
    ("2026-11-09T10:35:00", "2026-11-09T11:00:00", "future_data"),
])
def test_stale_old_day_preopen_and_future_data_never_alert(harness, at, asof, state):
    h = harness
    # Start the older-date and pre-open cases with an older baseline.
    h.now = stamp("2026-11-08T10:05:00")
    h.analyses["600660.SH"] = analysis("2026-11-08T10:00:00")
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(at, asof)
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == state
    assert h.store.events_after() == []


@pytest.mark.parametrize("at,previous,current", [
    ("2026-11-09T13:25:00", "2026-11-09T11:00:00", "2026-11-09T11:30:00"),
    ("2026-11-09T20:00:00", "2026-11-09T14:30:00", "2026-11-09T15:00:00"),
])
def test_lunch_and_afterclose_accept_latest_session_bar(harness, at, previous, current):
    h = harness
    h.now = stamp(previous)
    h.analyses["600660.SH"] = analysis(previous)
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(at, current)
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready"
    assert [e["bar_end"] for e in h.store.events_after()] == [current + "+08:00"]


def test_delayed_backfill_only_notifies_last_bar_not_intermediate_events(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00", events=[
        event("2026-11-09T10:00:00"), event("2026-11-09T10:30:00"),
        event("2026-11-09T11:00:00"), event("2026-11-09T11:30:00"),
        event("2026-11-08T11:00:00")])
    h.monitor.scan_once()
    assert [e["bar_end"] for e in h.store.events_after()] == ["2026-11-09T11:00:00+08:00"]


def test_new_day_drops_previous_day_events_and_uses_current_date(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-10T10:05:00", "2026-11-10T10:00:00", events=[
        event("2026-11-09T15:00:00"), event("2026-11-10T10:00:00")])
    h.monitor.scan_once()
    assert [e["bar_end"] for e in h.store.events_after()] == ["2026-11-10T10:00:00+08:00"]


@pytest.mark.parametrize("changes,state", [
    ({"status": "insufficient_history"}, "insufficient_history"),
    ({"quality": {"alertable": False, "recent_closed_gaps": 1}}, "data_gap"),
    ({"asof": None, "events": []}, "insufficient_history"),
])
def test_insufficient_or_gapped_history_blocks_alerts(harness, changes, state):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.analyses["600660.SH"].update(changes)
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == state and h.store.events_after() == []


def test_gap_repair_same_bar_does_not_replay_and_next_complete_bar_can_alert(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(quality={"alertable": False})
    h.monitor.scan_once()
    h.analyses["600660.SH"]["quality"] = {"alertable": True}
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and not h.store.events_after()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00", quality={"alertable": True})
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_future_timestamp_does_not_poison_valid_baseline(harness):
    h = harness
    h.analyses["600660.SH"] = analysis("2026-11-09T15:00:00")
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "future_data"
    h.analyses["600660.SH"] = analysis()
    h.monitor.scan_once()
    assert h.store.events_after() == []
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_source_regression_is_reported_and_never_rewinds_baseline(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.monitor.scan_once()
    h.analyses["600660.SH"] = analysis()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_regressed"
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_rules_and_event_symbol_are_checked(harness):
    h = harness
    h.monitor.configure({"rules": ["buy3"]})
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(events=[event("2026-11-09T10:30:00"),
        event("2026-11-09T10:30:00", rule="buy3", symbol="300308.SZ"),
        event("2026-11-09T10:30:00", rule="buy3", is_test=True),
        event("bad-time", rule="buy3"), None,
        event("2026-11-09T10:30:00", rule="buy3")])
    h.monitor.scan_once()
    assert [e["rule"] for e in h.store.events_after()] == ["buy3"]


def test_missing_file_and_bad_reader_are_visible_without_simulated_fallback(harness):
    h = harness
    h.paths = {}
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "file_missing"
    assert not [c for c in h.calls if c[0] == "read"] and not h.store.events_after()
    h.monitor._discoverer = lambda root, symbols: {"600660.SH": h.source / "missing.lc5"}
    h.monitor._reader = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("文件正在更新"))
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "error" and "文件正在更新" in stock(h.monitor)["error"]


def test_analyzer_error_and_wrong_symbol_do_not_emit(harness):
    h = harness
    h.analyses["600660.SH"] = ValueError("数据格式损坏")
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "error"
    h.analyses["600660.SH"] = analysis(symbol="300308.SZ")
    h.monitor.scan_once()
    assert "代码" in stock(h.monitor)["error"] and not h.store.events_after()


def test_directory_removal_or_discovery_error_are_reported(harness):
    h = harness
    h.monitor.start()
    h.monitor._discoverer = lambda *args: (_ for _ in ()).throw(OSError("无读取权限"))
    h.monitor.scan_once()
    assert "无读取权限" in stock(h.monitor)["error"]
    h.monitor._config["source_dir"] = str(h.source / "removed")
    h.monitor.scan_once()
    assert "目录不存在" in stock(h.monitor)["error"] and not h.store.events_after()


@pytest.mark.parametrize("changes", [{"interval_seconds": 14}, {"interval_seconds": 3601},
    {"interval_seconds": True}, {"interval_seconds": 15.5}, {"symbols": ["000001.SH"]},
    {"symbols": ["600660.SH", "600660.SH"]}, {"symbols": "600660.SH"},
    {"rules": ["buy1"]}, {"rules": ["buy2", "buy2"]}, {"source_dir": 1},
    {"source_dir": "bad\0path"}, {"sound": True}])
def test_configuration_rejects_invalid_inputs_without_altering_saved_config(harness, changes):
    h = harness
    before = h.monitor.status()["config"]
    with pytest.raises(ValueError):
        h.monitor.configure(changes)
    assert h.monitor.status()["config"] == before == h.store.load_config()


@pytest.mark.parametrize("changes,match", [({"source_dir": ""}, "目录不存在"),
    ({"symbols": []}, "至少选择一只"), ({"rules": []}, "至少选择一种")])
def test_invalid_start_is_friendly_but_configuration_can_be_saved(harness, changes, match):
    h = harness
    h.monitor.configure(changes)
    with pytest.raises(ValueError, match=match):
        h.monitor.start()
    assert not h.monitor.status()["running"]


def test_nonexistent_folder_is_saved_but_start_requires_it(harness):
    h = harness
    h.monitor.configure({"source_dir": str(h.source / "not-yet-created")})
    with pytest.raises(ValueError, match="目录不存在"):
        h.monitor.start()


@pytest.mark.parametrize("item", [
    {"symbol": "000001.SH", "name": "上证指数", "asset_type": "index"},
    {"symbol": "399001.SZ", "name": "深证成指", "asset_type": "stock"},
    {"symbol": "512480.SH", "name": "半导体ETF", "asset_type": "etf"},
    {"symbol": "600660.SH", "name": "福耀玻璃"},
])
def test_universe_only_accepts_explicit_stocks(tmp_path, item):
    with pytest.raises(ValueError):
        LocalMonitor([item], AlertStore(tmp_path / "alerts.db"), background=False)


def test_rules_or_directory_changes_rebaseline_while_interval_change_does_not(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.monitor.configure({"interval_seconds": 15})
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    h.monitor.configure({"rules": ["buy3"]})
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00", events=[event("2026-11-09T11:00:00", "buy3")])
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    other = h.source / "other"
    other.mkdir()
    h.monitor.configure({"source_dir": str(other)})
    h.advance("2026-11-09T11:35:00", "2026-11-09T11:30:00", events=[event("2026-11-09T11:30:00", "buy3")])
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_pause_prevents_scans_and_active_start_is_idempotent(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.monitor.start()
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    h.monitor.pause()
    before = len(h.calls)
    assert h.monitor.scan_once() is False and len(h.calls) == before
    assert stock(h.monitor)["state"] == "paused"


def test_pause_during_analysis_discards_inflight_event_and_scans_do_not_overlap(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    entered, release = threading.Event(), threading.Event()

    def slow_analyzer(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return analysis("2026-11-09T10:30:00")

    h.monitor._analyzer = slow_analyzer
    worker = threading.Thread(target=h.monitor.scan_once)
    worker.start()
    assert entered.wait(2)
    assert h.monitor.scan_once() is False
    h.monitor.pause()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and not h.store.events_after()
    assert stock(h.monitor)["state"] == "paused"


def test_daemon_worker_wakes_for_close_and_test_event_is_clearly_marked(harness):
    h = harness
    entered = threading.Event()

    def discoverer(root, symbols):
        entered.set()
        return {s: h.paths[s] for s in symbols}

    deps = {**h.dependencies, "background": True, "discoverer": discoverer}
    monitor = LocalMonitor(UNIVERSE, h.store, **deps)
    monitor.configure({"interval_seconds": 3600})
    try:
        monitor.start()
        assert entered.wait(2)
        assert monitor._thread.daemon
        tested = monitor.test_alert()
        assert tested["is_test"] and tested["symbol"] == "TEST"
        assert "不代表真实" in tested["message"] and tested["evidence"]["test_only"]
        assert len(h.store.events_after()) == 1
    finally:
        monitor.close()
    assert not monitor._thread.is_alive()
    monitor.close()
    with pytest.raises(ValueError, match="已关闭"):
        monitor.start()
    with pytest.raises(ValueError, match="已关闭"):
        monitor.configure({"interval_seconds": 60})


def test_saved_invalid_config_falls_back_and_reports_warning(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.save_config({"interval_seconds": 1})
    monitor = LocalMonitor(UNIVERSE, store, background=False)
    try:
        status = monitor.status()
        assert status["config_warning"] and status["config"]["interval_seconds"] == 60
    finally:
        monitor.close()


def test_store_rejects_corrupted_config_payload(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO config VALUES(1,?,?)", (json.dumps([]), "now"))
    with pytest.raises(ValueError, match="对象"):
        store.load_config()


def lc5_record(at):
    packed = (at.year - 2004) * 2048 + at.month * 100 + at.day
    return struct.pack("<HHfffffII", packed, at.hour * 60 + at.minute,
                       10.0, 10.5, 9.5, 10.2, 1000.0, 100, 0)


@pytest.fixture
def cached_monitor(tmp_path):
    """Production file discovery/decoding with a lightweight counted analyzer."""
    source = tmp_path / "vipdoc"
    source.mkdir()
    path = source / "sh600660.lc5"
    path.write_bytes(b"".join(lc5_record(datetime(2026, 11, 9, 9 + minute // 60, minute % 60))
                              for minute in (35, 40, 45, 50, 55, 60)))
    state = SimpleNamespace(source=source, path=path, now=stamp("2026-11-09T10:05:00"),
                            calls=[], result=analysis(), error=None, mutate=None,
                            store=AlertStore(tmp_path / "app" / "events.sqlite3"))

    def analyzer(frame, warmup, now):
        state.calls.append((len(frame), warmup, now))
        if state.error:
            raise state.error
        if state.mutate:
            state.mutate()
        return deepcopy(state.result)

    state.monitor = LocalMonitor(UNIVERSE, state.store, analyzer=analyzer,
                                 now=lambda: state.now, background=False)
    state.monitor.configure({"data_source": "tdx_local", "source_dir": str(source), "symbols": ["600660.SH"]})
    yield state
    state.monitor.close()


def test_stable_production_file_reuses_analysis_and_still_refreshes_scan_status(cached_monitor):
    h = cached_monitor
    h.monitor.start()
    h.monitor.scan_once()
    assert len(h.calls) == len(h.monitor._analysis_cache) == 1
    before = stock(h.monitor)["last_scan"]
    h.now = stamp("2026-11-09T10:15:00")
    h.monitor.scan_once()
    h.monitor.scan_once()
    assert len(h.calls) == 1 and h.store.events_after() == []
    assert stock(h.monitor)["last_scan"] != before
    assert stock(h.monitor)["last_scan"] == h.now.isoformat()
    assert stock(h.monitor)["state"] == "ready"
    h.now = stamp("2026-11-09T10:35:00")
    h.monitor.scan_once()
    assert len(h.calls) == 2 and stock(h.monitor)["state"] == "stale"
    h.monitor.scan_once()
    assert len(h.calls) == 2


@pytest.mark.parametrize("change", ["size", "mtime", "inode", "path"])
def test_file_signature_changes_force_reanalysis_even_with_same_30m_cutoff(cached_monitor, change):
    h = cached_monitor
    h.monitor.start()
    h.monitor.scan_once()
    old = h.path.stat()
    if change == "size":
        with h.path.open("ab") as stream:
            stream.write(lc5_record(datetime(2026, 11, 9, 10, 5)))
    elif change == "mtime":
        os.utime(h.path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
    elif change == "inode":
        replacement = h.source / "replacement"
        replacement.write_bytes(h.path.read_bytes())
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(h.path)
        assert h.path.stat().st_ino != old.st_ino
    else:
        folder = h.source / "new-subdir"
        folder.mkdir()
        moved = folder / h.path.name
        h.path.rename(moved)
        h.path = moved
    h.monitor.scan_once()
    assert len(h.calls) == 2 and len(h.monitor._analysis_cache) == 1
    h.monitor.scan_once()
    assert len(h.calls) == 2 and not h.store.events_after()


def test_source_cache_is_one_result_per_symbol_not_a_history_of_file_versions(cached_monitor):
    h = cached_monitor
    h.monitor.start()
    for iteration in range(6):
        info = h.path.stat()
        os.utime(h.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        h.monitor.scan_once()
        assert len(h.monitor._analysis_cache) == 1 <= len(UNIVERSE)
        cached = h.monitor._analysis_cache["600660.SH"][1]
        assert isinstance(cached, dict) and "frame" not in cached
    assert len(h.calls) == 6


def test_cache_does_not_hide_new_signals_or_replay_cached_signals(cached_monitor):
    h = cached_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.now = stamp("2026-11-09T10:35:00")
    h.result = analysis("2026-11-09T10:30:00")
    with h.path.open("ab") as stream:
        stream.write(b"".join(lc5_record(datetime(2026, 11, 9, 10, m)) for m in (5, 10, 15, 20, 25, 30)))
    h.monitor.scan_once()
    assert len(h.calls) == 2 and len(h.store.events_after()) == 1
    h.now = stamp("2026-11-09T10:40:00")
    h.monitor.scan_once()
    assert len(h.calls) == 2 and len(h.store.events_after()) == 1


def test_date_or_preopen_to_first_close_time_advance_invalidates_cache(cached_monitor):
    h = cached_monitor
    h.now = stamp("2026-11-10T09:40:00")
    h.monitor.start()
    h.monitor.scan_once()
    h.now = stamp("2026-11-10T09:55:00")
    h.monitor.scan_once()
    assert len(h.calls) == 1
    h.now = stamp("2026-11-11T09:55:00")
    h.monitor.scan_once()
    assert len(h.calls) == 2
    h.now = stamp("2026-11-11T10:00:00")
    h.monitor.scan_once()
    assert len(h.calls) == 3


def test_configuration_reset_and_pause_restart_clear_cache_but_interval_change_reuses(cached_monitor):
    h = cached_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.monitor.configure({"interval_seconds": 15})
    h.monitor.scan_once()
    assert len(h.calls) == 1
    h.monitor.configure({"rules": ["buy3"]})
    assert not h.monitor._analysis_cache
    h.monitor.scan_once()
    assert len(h.calls) == 2
    other = h.source / "other"
    other.mkdir()
    h.path.rename(other / h.path.name)
    h.monitor.configure({"source_dir": str(other)})
    assert not h.monitor._analysis_cache
    h.monitor.scan_once()
    assert len(h.calls) == 3
    h.monitor.pause()
    assert not h.monitor._analysis_cache
    h.monitor.start()
    h.monitor.scan_once()
    assert len(h.calls) == 4 and not h.store.events_after()


@pytest.mark.parametrize("result,state", [
    (analysis("2026-11-09T11:00:00"), "future_data"),
    (analysis(status="insufficient_history"), "insufficient_history"),
    (analysis(quality={"alertable": False}), "data_gap"),
    (analysis(symbol="300308.SZ"), "error"),
])
def test_invalid_analysis_never_populates_cache(cached_monitor, result, state):
    h = cached_monitor
    h.result = result
    h.monitor.start()
    h.monitor.scan_once()
    h.monitor.scan_once()
    assert len(h.calls) == 2 and not h.monitor._analysis_cache
    assert stock(h.monitor)["state"] == state and not h.store.events_after()


def test_missing_or_bad_source_clears_cached_analysis_without_fallback(cached_monitor):
    h = cached_monitor
    h.monitor.start()
    h.monitor.scan_once()
    assert h.monitor._analysis_cache
    h.path.unlink()
    h.monitor.scan_once()
    assert not h.monitor._analysis_cache and stock(h.monitor)["state"] == "file_missing"
    h.path.write_bytes(b"invalid trailing LC5 bytes")
    h.monitor.scan_once()
    assert not h.monitor._analysis_cache and stock(h.monitor)["state"] == "error"
    assert len(h.calls) == 1 and not h.store.events_after()


def test_file_changing_during_read_is_not_analyzed_or_cached(cached_monitor, monkeypatch):
    from alerts import sources
    h = cached_monitor
    original = sources.read_lc5
    reads = []

    def reader(path, max_records):
        frame = original(path, max_records=max_records)
        reads.append(path)
        if len(reads) == 1:
            with path.open("ab") as stream:
                stream.write(lc5_record(datetime(2026, 11, 9, 10, 5)))
        return frame

    monkeypatch.setattr(sources, "read_lc5", reader)
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_changing"
    assert not h.calls and not h.monitor._analysis_cache and not h.store.events_after()
    h.monitor.scan_once()
    h.monitor.scan_once()
    assert len(reads) == 2 and len(h.calls) == 1 and len(h.monitor._analysis_cache) == 1


def test_file_changing_during_analysis_is_discarded_until_next_stable_scan(cached_monitor):
    h = cached_monitor

    def mutate():
        info = h.path.stat()
        os.utime(h.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))

    h.mutate = mutate
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_changing"
    assert not h.monitor._analysis_cache and not h.monitor._baselines
    h.mutate = None
    h.monitor.scan_once()
    h.monitor.scan_once()
    assert len(h.calls) == 2 and len(h.monitor._analysis_cache) == 1
    assert not h.store.events_after()


def test_custom_injected_sources_remain_uncached_even_if_paths_exist(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.monitor.scan_once()
    assert len([c for c in h.calls if c[0] == "analyze"]) == 2
    assert not h.monitor._analysis_cache


@pytest.fixture
def online_monitor(tmp_path):
    """Synthetic provider/analyzer mechanisms; no real quote or API is claimed."""
    state = SimpleNamespace(now=stamp("2026-11-09T10:05:00"), fetches=[], analyses=[],
                            fetch_error=None, empty=False, result=analysis(),
                            store=AlertStore(tmp_path / "app" / "online.sqlite3"))
    state.source = {"kind": "eastmoney", "label": "synthetic provider mechanism fixture",
                    "provider": "eastmoney", "response_sha256": "0" * 64}
    state.result["source"] = deepcopy(state.source)

    def frame(symbol):
        return pd.DataFrame([{"symbol": symbol, "dt": state.now.replace(tzinfo=None),
                              "open": 12.4, "high": 12.6, "low": 12.3, "close": 12.5,
                              "vol": 1000., "amount": 12500.}])

    def fetcher(symbol, *, now):
        state.fetches.append((symbol, now))
        if state.fetch_error:
            raise state.fetch_error
        return frame(symbol).iloc[:0] if state.empty else frame(symbol)

    def analyzer(value, *, warmup, now):
        state.analyses.append((len(value), warmup, now))
        return deepcopy(state.result)

    def forbid_local(*args, **kwargs):
        raise AssertionError("联网模式不应触碰本地行情目录")

    state.fetcher, state.analyzer, state.frame = fetcher, analyzer, frame
    state.monitor = LocalMonitor(UNIVERSE, state.store, analyzer=analyzer,
        reader=forbid_local, discoverer=forbid_local, online_fetcher=fetcher,
        now=lambda: state.now, background=False)
    state.monitor.configure({"data_source": "eastmoney", "source_dir": str(tmp_path / "absent"),
                             "symbols": ["600660.SH"]})

    def advance(at="2026-11-09T10:35:00", asof="2026-11-09T10:30:00", **extra):
        state.now = stamp(at)
        state.result = analysis(asof, source=deepcopy(state.source), **extra)

    state.advance = advance
    yield state
    state.monitor.close()


def test_legacy_saved_configuration_migrates_to_local_source_without_warning(tmp_path):
    store = AlertStore(tmp_path / "config.sqlite3")
    legacy = {"source_dir": "D:\\old_tdx\\vipdoc", "interval_seconds": 30,
              "symbols": ["600660.SH"], "rules": ["buy3"]}
    store.save_config(legacy)
    monitor = LocalMonitor(UNIVERSE, store, background=False)
    try:
        assert monitor.status()["config"] == {**legacy, "data_source": "tdx_local", "tdx_host": "", "tdx_port": 7709}
        assert monitor.status()["config_warning"] is None
        monitor.configure({"interval_seconds": 60})
        assert store.load_config()["data_source"] == "tdx_local"
    finally:
        monitor.close()


@pytest.mark.parametrize("source", ["", "unknown", None, 1, [], {}])
def test_source_configuration_rejects_unknown_providers(harness, source):
    before = harness.monitor.status()["config"]
    with pytest.raises(ValueError, match="行情来源"):
        harness.monitor.configure({"data_source": source})
    assert harness.monitor.status()["config"] == before


def test_online_source_starts_without_local_folder_and_initial_scan_is_silent(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready"
    assert stock(h.monitor)["source"] == h.source
    assert len(h.fetches) == len(h.analyses) == 1
    assert h.fetches[0] == ("600660.SH", h.now)
    assert h.analyses[0][1:] == (200, h.now)
    assert not h.store.events_after()
    # The exposed source is copied, never mutable shared provider state.
    public = stock(h.monitor)
    public["source"]["provider"] = "tampered"
    assert stock(h.monitor)["source"] == h.source


def test_online_fetches_each_scan_and_emits_only_new_current_closed_event_once(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.result["events"][0]["evidence"]["source"] = deepcopy(h.source)
    h.monitor.scan_once()
    h.monitor.scan_once()
    rows = h.store.events_after()
    assert len(rows) == 1 and rows[0]["is_test"] is False
    assert rows[0]["bar_end"] == "2026-11-09T10:30:00+08:00"
    assert rows[0]["evidence"]["source"] == h.source
    assert len(h.fetches) == 3 and not h.monitor._analysis_cache


@pytest.mark.parametrize("error,state", [(TimeoutError("连接超时"), "network_error"),
    (OSError("接口不可达"), "network_error"), (ValueError("无效行情"), "data_error"),
    (RuntimeError("分析不可用"), "data_error")])
def test_online_failures_remove_usable_snapshot_and_do_not_fall_back(online_monitor, error, state):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["snapshot"]
    h.fetch_error = error
    h.advance()
    h.monitor.scan_once()
    current = stock(h.monitor)
    assert current["state"] == state and str(error) in current["error"]
    assert current["asof"] is None
    assert current["snapshot"] == current["quality"] == current["source"] == {}
    assert len(h.analyses) == 1 and not h.store.events_after()
    h.fetch_error = None
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and len(h.store.events_after()) == 1


def test_online_initial_failure_then_recovery_builds_silent_baseline(online_monitor):
    h = online_monitor
    h.fetch_error = TimeoutError("首次联网失败")
    h.monitor.start()
    h.monitor.scan_once()
    assert not h.monitor._baselines and not h.store.events_after()
    h.fetch_error = None
    h.advance()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and not h.store.events_after()


def test_online_empty_frame_is_not_analyzed_or_replaced_by_cached_quotes(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.empty = True
    h.advance()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_error"
    assert "为空" in stock(h.monitor)["error"]
    assert len(h.analyses) == 1 and not h.store.events_after()


@pytest.mark.parametrize("quality", [None, {}, {"alertable": False},
    {"alertable": None}, {"alertable": 1}, {"alertable": "true"}])
def test_online_requires_explicit_boolean_quality_permission(online_monitor, quality):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(quality=quality)
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_gap" and not h.store.events_after()
    # Repairing the same bar cannot replay a formerly blocked signal.
    h.result["quality"] = {"alertable": True}
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and not h.store.events_after()


@pytest.mark.parametrize("at,asof,expected", [
    ("2026-11-09T10:35:00", "2026-11-09T10:00:00", "stale"),
    ("2026-11-09T10:35:00", "2026-11-09T11:00:00", "future_data"),
    ("2026-11-10T10:05:00", "2026-11-09T15:00:00", "stale"),
    ("2026-11-09T09:59:00", "2026-11-09T09:30:00", "data_regressed")])
def test_online_keeps_the_same_session_freshness_gate(online_monitor, at, asof, expected):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(at, asof)
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == expected and not h.store.events_after()


def test_online_wrong_result_symbol_cannot_emit_for_selected_stock(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance(symbol="300308.SZ")
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_error"
    assert not h.store.events_after() and not stock(h.monitor)["snapshot"]


def test_switching_source_resets_baseline_and_first_online_scan_is_silent(harness):
    h = harness
    original_analyzer = h.monitor._analyzer

    def analyzer(value, **kwargs):
        return original_analyzer(value.symbol.iloc[0] if isinstance(value, pd.DataFrame) else value,
                                 **kwargs)

    h.monitor._analyzer = analyzer
    h.monitor._online_fetcher = lambda symbol, *, now: pd.DataFrame([{"symbol": symbol}])
    h.monitor.start()
    h.monitor.scan_once()
    assert h.monitor._baselines
    h.monitor.configure({"data_source": "eastmoney", "source_dir": ""})
    assert not h.monitor._baselines and not h.monitor._analysis_cache
    h.advance()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and not h.store.events_after()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00")
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    h.monitor.configure({"data_source": "tdx_local", "source_dir": str(h.source)})
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_pause_during_online_fetch_prevents_analysis_and_inflight_alert(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    entered, release = threading.Event(), threading.Event()

    def slow_fetch(symbol, *, now):
        entered.set()
        assert release.wait(3)
        return h.frame(symbol)

    h.monitor._online_fetcher = slow_fetch
    worker = threading.Thread(target=h.monitor.scan_once)
    worker.start()
    assert entered.wait(3)
    h.monitor.pause()
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert stock(h.monitor)["state"] == "paused"
    assert len(h.analyses) == 1 and not h.store.events_after()


def test_one_online_stock_failure_does_not_stop_other_selected_stocks(online_monitor):
    h = online_monitor
    h.monitor.configure({"symbols": [s["symbol"] for s in UNIVERSE]})

    def fetch(symbol, *, now):
        if symbol == "600660.SH":
            raise TimeoutError("单股请求失败")
        return h.frame(symbol)

    def analyze_frame(frame, **kwargs):
        return analysis(symbol=frame.symbol.iloc[0], source=deepcopy(h.source))

    h.monitor._online_fetcher = fetch
    h.monitor._analyzer = analyze_frame
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "network_error"
    assert stock(h.monitor, "300308.SZ")["state"] == "ready"
    assert not h.store.events_after()


def test_online_request_crossing_a_new_close_uses_publish_time_freshness(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-09T10:29:59", "2026-11-09T10:00:00")

    def delayed_fetch(symbol, *, now):
        assert now == stamp("2026-11-09T10:29:59")
        h.now = stamp("2026-11-09T10:30:01")
        return h.frame(symbol)

    h.monitor._online_fetcher = delayed_fetch
    h.monitor.scan_once()
    assert h.analyses[-1][2] == h.now
    assert stock(h.monitor)["state"] == "stale"
    assert stock(h.monitor)["last_scan"] == h.now.isoformat()
    assert not h.store.events_after()


def test_online_request_crossing_midnight_cannot_alert_the_previous_date(online_monitor):
    h = online_monitor
    h.advance("2026-11-09T14:35:00", "2026-11-09T14:30:00")
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-09T23:59:59", "2026-11-09T15:00:00")

    def delayed_fetch(symbol, *, now):
        assert now == stamp("2026-11-09T23:59:59")
        h.now = stamp("2026-11-10T00:00:01")
        return h.frame(symbol)

    h.monitor._online_fetcher = delayed_fetch
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "stale"
    assert h.monitor.status()["last_cycle"] == h.now.isoformat()
    assert not h.store.events_after()


def test_local_slow_analysis_uses_actual_publish_time_and_drops_old_slot(harness):
    h = harness
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-09T10:59:59", "2026-11-09T10:30:00")
    original = h.monitor._analyzer

    def delayed_analysis(*args, **kwargs):
        result = original(*args, **kwargs)
        h.now = stamp("2026-11-09T11:00:01")
        return result

    h.monitor._analyzer = delayed_analysis
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "stale"
    assert stock(h.monitor)["last_scan"] == h.now.isoformat()
    assert h.monitor.status()["last_cycle"] == h.now.isoformat()
    assert not h.store.events_after()


def test_new_installation_defaults_to_direct_tdx_without_local_files(tmp_path):
    monitor = LocalMonitor(UNIVERSE, AlertStore(tmp_path / "new.sqlite3"), background=False)
    try:
        config = monitor.status()["config"]
        assert config["data_source"] == "tdx_online"
        assert config["tdx_host"] == "" and config["tdx_port"] == 7709
        assert monitor.status()["config_warning"] is None
    finally:
        monitor.close()


@pytest.mark.parametrize("data_source", ["tdx_local", "eastmoney"])
def test_existing_provider_choice_survives_tdx_endpoint_migration(tmp_path, data_source):
    legacy = {"data_source": data_source, "source_dir": r"D:\old\vipdoc",
              "interval_seconds": 30, "symbols": ["600660.SH"], "rules": ["buy2"]}
    store = AlertStore(tmp_path / "old.sqlite3")
    store.save_config(legacy)
    monitor = LocalMonitor(UNIVERSE, store, background=False)
    try:
        assert monitor.status()["config"] == {**legacy, "tdx_host": "", "tdx_port": 7709}
        assert monitor.status()["config_warning"] is None
        monitor.configure({"interval_seconds": 60})
        assert store.load_config()["data_source"] == data_source
    finally:
        monitor.close()


@pytest.mark.parametrize("changes", [
    {"tdx_host": "https://example.com"}, {"tdx_host": "host/path"},
    {"tdx_host": "example.com:7709"}, {"tdx_host": "invalid host"},
    {"tdx_host": "300.1.2.3"}, {"tdx_host": None}, {"tdx_host": []},
    {"tdx_port": 0}, {"tdx_port": 65536}, {"tdx_port": True},
    {"tdx_port": 7709.0}, {"tdx_port": "7709"},
])
def test_tdx_endpoint_configuration_is_validated_before_saving(harness, changes):
    before = harness.monitor.status()["config"]
    with pytest.raises(ValueError):
        harness.monitor.configure(changes)
    assert harness.monitor.status()["config"] == before == harness.store.load_config()


def test_tdx_fetcher_must_be_callable(tmp_path):
    with pytest.raises(ValueError, match="通达信行情读取器"):
        LocalMonitor(UNIVERSE, AlertStore(tmp_path / "wrong.sqlite3"), tdx_fetcher=1)


@pytest.fixture
def tdx_monitor(online_monitor):
    """Explicit synthetic TDX transport; never connects to public quote hosts."""
    state = online_monitor
    state.tdx_calls = []
    state.source = {"kind": "tdx", "provider": "tdx", "label": "synthetic TDX fixture",
                    "source_host": "119.147.212.81", "source_port": 7709}
    state.result["source"] = deepcopy(state.source)

    def fetch(symbol, *, now, host, port):
        state.tdx_calls.append((symbol, now, host, port))
        return state.fetcher(symbol, now=now)

    def forbid_other_source(*args, **kwargs):
        pytest.fail("TDX direct mode must not call Eastmoney or local LC5")

    state.monitor._tdx_fetcher = fetch
    state.monitor._online_fetcher = forbid_other_source
    state.monitor.configure({"data_source": "tdx_online", "tdx_host": "119.147.212.81", "tdx_port": 7709})
    return state


def test_tdx_initial_scan_uses_explicit_server_and_silent_baseline(tdx_monitor):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    assert h.tdx_calls == [("600660.SH", h.now, "119.147.212.81", 7709)]
    assert stock(h.monitor)["state"] == "ready"
    assert stock(h.monitor)["source"] == h.source
    assert not h.store.events_after()


def test_tdx_new_closed_signal_records_provenance_once(tdx_monitor):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.result["events"][0]["evidence"]["source"] = deepcopy(h.source)
    h.monitor.scan_once()
    h.monitor.scan_once()
    rows = h.store.events_after()
    assert len(rows) == 1 and rows[0]["is_test"] is False
    assert rows[0]["bar_end"] == "2026-11-09T10:30:00+08:00"
    assert rows[0]["evidence"]["source"] == h.source
    assert len(h.tdx_calls) == 3


@pytest.mark.parametrize("error,state", [
    (OSError("通达信节点连接超时"), "network_error"),
    (ValueError("通达信数据为空"), "data_error"),
    (RuntimeError("通达信依赖缺失"), "data_error"),
])
def test_tdx_failure_clears_old_quote_and_never_uses_another_source(tdx_monitor, error, state):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.fetch_error = error
    h.monitor.scan_once()
    current = stock(h.monitor)
    assert current["state"] == state and str(error) in current["error"]
    assert current["snapshot"] == current["source"] == current["quality"] == {}
    assert current["asof"] is None and not h.store.events_after()
    assert len(h.analyses) == 1


def test_tdx_first_failure_recovery_is_still_silent(tdx_monitor):
    h = tdx_monitor
    h.fetch_error = OSError("synthetic node unavailable")
    h.monitor.start()
    h.monitor.scan_once()
    h.fetch_error = None
    h.advance()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "ready" and not h.store.events_after()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00")
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


@pytest.mark.parametrize("changes", [{"tdx_host": "119.147.212.83"}, {"tdx_port": 7710}])
def test_tdx_server_changes_reset_baseline_and_next_scan_is_silent(tdx_monitor, changes):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    h.monitor.configure(changes)
    assert not h.monitor._baselines and not h.monitor._analysis_cache
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00")
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1
    call = h.tdx_calls[-1]
    assert call[2] == changes.get("tdx_host", "119.147.212.81")
    assert call[3] == changes.get("tdx_port", 7709)
    h.advance("2026-11-09T11:35:00", "2026-11-09T11:30:00")
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 2


def test_switch_from_eastmoney_to_tdx_does_not_replay_existing_signals(online_monitor):
    h = online_monitor
    h.monitor.start()
    h.monitor.scan_once()
    first_fetch_count = len(h.fetches)
    h.monitor._tdx_fetcher = lambda symbol, *, now, host, port: h.frame(symbol)
    h.monitor.configure({"data_source": "tdx_online"})
    h.advance()
    h.monitor.scan_once()
    assert len(h.fetches) == first_fetch_count and not h.store.events_after()
    h.advance("2026-11-09T11:05:00", "2026-11-09T11:00:00")
    h.monitor.scan_once()
    assert len(h.store.events_after()) == 1


def test_pause_during_tdx_request_discards_inflight_data(tdx_monitor):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance()
    entered, release = threading.Event(), threading.Event()

    def delayed_fetch(symbol, *, now, host, port):
        entered.set()
        assert release.wait(3)
        return h.frame(symbol)

    h.monitor._tdx_fetcher = delayed_fetch
    worker = threading.Thread(target=h.monitor.scan_once)
    worker.start()
    assert entered.wait(3)
    h.monitor.pause()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and len(h.analyses) == 1
    assert stock(h.monitor)["state"] == "paused" and not h.store.events_after()


def test_tdx_request_crossing_close_rechecks_current_time(tdx_monitor):
    h = tdx_monitor
    h.monitor.start()
    h.monitor.scan_once()
    h.advance("2026-11-09T10:29:59", "2026-11-09T10:00:00")

    def delayed_fetch(symbol, *, now, host, port):
        h.now = stamp("2026-11-09T10:30:01")
        return h.frame(symbol)

    h.monitor._tdx_fetcher = delayed_fetch
    h.monitor.scan_once()
    assert h.analyses[-1][2] == h.now
    assert stock(h.monitor)["state"] == "stale" and not h.store.events_after()


def test_tdx_empty_data_does_not_reach_analyzer(tdx_monitor):
    h = tdx_monitor
    h.empty = True
    h.monitor.start()
    h.monitor.scan_once()
    assert stock(h.monitor)["state"] == "data_error"
    assert not h.analyses and not h.store.events_after()


def test_tdx_default_dependency_loads_only_tdx_fetcher(tmp_path, monkeypatch):
    from alerts import tdx_online, online
    calls = []

    def fetch(symbol, *, now, host, port):
        calls.append((symbol, host, port))
        return pd.DataFrame([{"symbol": symbol}])

    monkeypatch.setattr(tdx_online, "fetch_five_minute", fetch)
    monkeypatch.setattr(online, "fetch_five_minute", lambda *args, **kwargs: pytest.fail("unexpected HTTP provider"))
    monitor = LocalMonitor(UNIVERSE, AlertStore(tmp_path / "dependency.sqlite3"),
        analyzer=lambda *args, **kwargs: analysis(), now=lambda: stamp("2026-11-09T10:05:00"), background=False)
    monitor.configure({"symbols": ["600660.SH"], "source_dir": str(tmp_path / "does-not-exist")})
    try:
        monitor.start()
        monitor.scan_once()
        assert calls == [("600660.SH", "", 7709)]
        assert stock(monitor)["state"] == "ready" and not monitor.store.events_after()
        assert monitor._reader is None and monitor._discoverer is None and monitor._online_fetcher is None
    finally:
        monitor.close()
