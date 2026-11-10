"""Mechanism fixtures below are synthetic prices, not historical market claims."""
import copy
import json
import math

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from alerts import signal_engine as engine


def five_frame(days=4):
    rows = []
    for day in pd.bdate_range("2025-01-02", periods=days):
        for minute in sorted(engine.END5):
            i = len(rows)
            price = 20 + .003 * i + .8 * math.sin(i / 14)
            rows.append({"dt": day + pd.Timedelta(minutes=minute), "symbol": "600660.SH",
                         "open": price, "close": price + .02, "high": price + .1, "low": price - .1,
                         "vol": 1000., "amount": 20000.})
    return pd.DataFrame(rows)


def record(stamp, count, state2=False, state3=False, **extra):
    return {"dt": pd.Timestamp(stamp), "symbol": "600660.SH", "bar_count": count,
            "close": 10., "high": 10.2, "low": 9.8, "bi_count": 15,
            "buy2_raw": ("二买" if state2 else "其他") + "_任意_任意_0",
            "buy3_raw": ("三买" if state3 else "其他") + "_任意_任意_0",
            "macd_hist": .1, "sma60": 9.5, "sma60_lag5": 9.4,
            "center_low": 8.5, "center_high": 9., **extra}


def test_complete_half_hour_aggregates_and_does_not_mutate():
    source = five_frame(1)
    original = source.copy(deep=True)
    normalized, excluded = engine._normalize_five(source)
    bars, audit = engine._aggregate_thirty(normalized)
    assert excluded == 0 and len(bars) == 8 and audit.complete.all()
    first = source.iloc[:6]
    assert bars.dt.iloc[0] == pd.Timestamp("2025-01-02 10:00")
    assert bars.open.iloc[0] == first.open.iloc[0]
    assert bars.close.iloc[0] == first.close.iloc[-1]
    assert bars.high.iloc[0] == first.high.max()
    assert bars.low.iloc[0] == first.low.min()
    assert bars.vol.iloc[0] == 6000 and bars.amount.iloc[0] == 120000
    assert_frame_equal(source, original)


def test_future_five_bar_never_completes_a_future_thirty():
    source = five_frame(1)
    result = engine.analyze_five_minute(source, warmup=200, now="2025-01-02 10:25")
    assert result["asof"] == "2025-01-02T10:00:00"
    assert result["last_five_asof"] == "2025-01-02T10:25:00"
    assert result["quality"]["closed_partial_slots"] == 0
    assert result["quality"]["closed_empty_slots"] == 0
    assert result["quality"]["future_five_excluded"] == 37
    assert result["events"] == []


def test_no_complete_thirty_is_insufficient_not_fake_quote():
    result = engine.analyze_five_minute(five_frame(1).iloc[:5])
    assert result["asof"] is None and result["bars30_count"] == 0
    assert result["snapshot"] == {} and result["events"] == []


def test_timezone_converts_before_stripping():
    source = five_frame(1)
    utc = source.copy()
    utc["dt"] = utc.dt.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    assert_frame_equal(engine._normalize_five(utc)[0], engine._normalize_five(source)[0])


def test_missing_five_bar_does_not_create_aggregate_or_alertable_quality(monkeypatch):
    source = five_frame(2).drop(index=7).reset_index(drop=True)
    monkeypatch.setattr(engine, "_native_records", lambda bars: [record(r.dt, i+1) for i,r in enumerate(bars.itertuples())])
    result = engine.analyze_five_minute(source, warmup=2)
    assert result["bars30_count"] == 15
    assert result["quality"]["closed_partial_slots"] == 1
    # An old gap outside the latest two complete bars is retained as audit, not hidden.
    assert result["quality"]["recent_closed_gaps"] == 0
    source = five_frame(2).drop(index=89).reset_index(drop=True)
    result = engine.analyze_five_minute(source, warmup=2)
    assert result["quality"]["recent_closed_gaps"] == 1
    assert not result["quality"]["alertable"]


@pytest.mark.parametrize("change", ["empty", "duplicate", "unordered", "missing_column", "wrong_time", "many_symbols", "nonfinite", "bad_ohlc", "negative_volume"])
def test_invalid_quotes_are_rejected(change):
    frame = five_frame(1)
    if change == "empty": frame = frame.iloc[:0]
    elif change == "duplicate": frame.loc[1,"dt"] = frame.loc[0,"dt"]
    elif change == "unordered": frame = frame.iloc[::-1]
    elif change == "missing_column": frame = frame.drop(columns="amount")
    elif change == "wrong_time": frame.loc[0,"dt"] = pd.Timestamp("2025-01-02 09:30")
    elif change == "many_symbols": frame.loc[0,"symbol"] = "600000.SH"
    elif change == "nonfinite": frame.loc[0,"amount"] = float("nan")
    elif change == "bad_ohlc": frame.loc[0,"high"] = 1.
    elif change == "negative_volume": frame.loc[0,"vol"] = -1
    with pytest.raises(ValueError): engine.analyze_five_minute(frame)


@pytest.mark.parametrize("warmup", [0, -1, True, 1.5])
def test_invalid_warmup_is_rejected(warmup):
    with pytest.raises(ValueError): engine.analyze_five_minute(five_frame(1), warmup=warmup)


@pytest.mark.parametrize("reverse", [False,True])
def test_sync_both_orders_once_per_day(reverse):
    states = [(False,False),(not reverse,reverse),(False,False),(reverse,not reverse),(True,True)]
    rows = [record(f"2025-01-02 {stamp}",i+1,*states[i]) for i,stamp in enumerate(["10:00","10:30","11:00","11:30","13:30"])]
    events,_ = engine._events_from_records(rows,1)
    sync = [r for r in events if r["rule"] == "buy23_day"]
    assert len(sync) == 1 and sync[0]["bar_end"] == "2025-01-02T11:30:00"
    assert sync[0]["evidence"]["buy2_first_at"] != sync[0]["evidence"]["buy3_first_at"]


def test_sync_resets_day_but_raw_state_does_not():
    rows = [record("2025-01-02 14:30",1), record("2025-01-02 15:00",2,True),
            record("2025-01-03 10:00",3,True,True)]
    events,_ = engine._events_from_records(rows,1)
    assert [e["rule"] for e in events] == ["buy2","buy3"]
    assert not any(e["rule"] == "buy23_day" for e in events)


def test_two_same_bar_events_can_sync_and_warmup_does_not_make_false_event():
    rows = [record("2025-01-02 10:00",1,True),record("2025-01-02 10:30",2,True),
            record("2025-01-02 11:00",3),record("2025-01-02 11:30",4,True,True)]
    events,_ = engine._events_from_records(rows,1)
    assert [e["rule"] for e in events] == ["buy2","buy3","buy23_day"]
    assert all(e["bar_end"] == "2025-01-02T11:30:00" for e in events)


def test_optimized_is_after_signal_and_carries_real_center_evidence():
    rows = [record("2025-01-02 10:00",1),record("2025-01-02 10:30",2,True),
            record("2025-01-02 11:00",3,False,close=10.3,high=10.4)]
    events,_ = engine._events_from_records(rows,1)
    event = next(e for e in events if e["rule"] == "buy2_optimized")
    assert event["origin_time"] == "2025-01-02T10:30:00"
    assert event["bar_end"] == "2025-01-02T11:00:00"
    assert event["evidence"]["wait_bars"] == 1
    assert event["evidence"]["center_low"] == 8.5
    assert event["evidence"]["center_high"] == 9.
    assert event["evidence"]["center_kind"] == "CZSC笔中枢"
    assert len([e for e in events if e["rule"] == "buy2_optimized"]) == 1


@pytest.mark.parametrize("case", ["stop", "expired", "new_day", "macd", "trend", "gap"])
def test_optimized_invalidations(case):
    rows = [record("2025-01-02 10:00",1),record("2025-01-02 10:30",2,True)]
    if case == "stop": rows += [record("2025-01-02 11:00",3,close=9.7),record("2025-01-02 11:30",4,close=10.3)]
    elif case == "expired": rows += [record("2025-01-02 "+stamp,i+3,close=10.3 if i==3 else 10.) for i,stamp in enumerate(["11:00","11:30","13:30","14:00"])]
    elif case == "new_day": rows += [record("2025-01-03 10:00",3,close=10.3)]
    elif case == "macd": rows += [record("2025-01-02 11:00",3,close=10.3,macd_hist=-.1)]
    elif case == "trend": rows += [record("2025-01-02 11:00",3,close=10.3,sma60_lag5=9.6)]
    elif case == "gap": rows += [record("2025-01-02 11:30",3,close=10.3)]
    assert not any(e["rule"] == "buy2_optimized" for e in engine._events_from_records(rows,1)[0])


def test_native_api_prefix_causality_and_json_safe_result():
    frame = five_frame(45)
    prefix = frame.iloc[:48*36].copy()
    cutoff = prefix.dt.iloc[-1]
    original = copy.deepcopy(frame)
    a = engine.analyze_five_minute(prefix, now=cutoff)
    b = engine.analyze_five_minute(frame, now=cutoff)
    # The excluded-future count deliberately differs; analysis must not.
    for key in ["symbol", "asof", "last_five_asof", "bars30_count", "status", "snapshot", "events"]:
        assert a[key] == b[key]
    assert b["quality"]["future_five_excluded"] == len(frame) - len(prefix)
    assert a["status"] == "ready" and a["bars30_count"] == 288
    assert a["snapshot"]["bi_count"] > 0
    assert isinstance(a["snapshot"]["buy2_raw"],str)
    assert a["snapshot"]["center_kind"] == "CZSC笔中枢"
    assert all(pd.Timestamp(e["bar_end"]) <= cutoff for e in a["events"])
    assert a["quality"]["alertable"]
    json.dumps(a,allow_nan=False)
    assert_frame_equal(frame,original)
