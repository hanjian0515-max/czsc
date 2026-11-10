"""Causal, read-only F30 research events from local A-share F5 end bars.

These are CZSC auxiliary definitions, not a verified reproduction of any
school course. This module does not notify, connect to a broker, or trade.
"""
from __future__ import annotations

import math
import os
from numbers import Integral
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("CZSC_HOME", str(Path(__file__).resolve().parents[1] / ".cache" / "czsc"))

RULE_LABELS = {
    "buy2": "30M二买辅助",
    "buy3": "30M三买辅助",
    "buy23_day": "同日23买同步",
    "buy2_optimized": "优化二买确认",
}
FIELDS = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
END30 = (600, 630, 660, 690, 810, 840, 870, 900)
END5 = set(range(575, 691, 5)) | set(range(785, 901, 5))
CONFIG = [
    {"name": "cxt_second_bs_V230320", "freq": "30分钟", "di": 1, "ma_type": "SMA", "timeperiod": 21},
    {"name": "cxt_third_buy_V230228", "freq": "30分钟", "di": 1},
]
SIGNAL_KEYS = {"buy2": ("30分钟_D1#SMA#21_BS2辅助V230320", "二买"),
               "buy3": ("30分钟_D1_三买辅助V230228", "三买")}
NOTE = "CZSC辅助结构研究预警，不是已验证课程规则、预测收益或自动交易指令"


def _local(value):
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("时间戳不能为空")
    return result.tz_convert("Asia/Shanghai").tz_localize(None) if result.tz is not None else result


def _iso(value):
    return _local(value).isoformat()


def _normalize_five(frame, now=None):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("5分钟行情为空")
    if frame.columns.duplicated().any() or not set(FIELDS) <= set(frame.columns):
        raise ValueError("5分钟行情必须包含唯一的标准8列: " + ",".join(FIELDS))
    result = frame[FIELDS].copy(deep=True)
    try:
        result["dt"] = [_local(v) for v in result.dt]
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("5分钟时间戳不合法") from exc
    if result.dt.duplicated().any() or not result.dt.is_monotonic_increasing:
        raise ValueError("时间必须严格升序且唯一；不会静默排序或去重")
    minutes = result.dt.dt.hour * 60 + result.dt.dt.minute
    if not minutes.isin(END5).all() or (result.dt.dt.second.ne(0) | result.dt.dt.microsecond.ne(0) | result.dt.dt.nanosecond.ne(0)).any():
        raise ValueError("需要A股5分钟收盘标签，09:35至11:30、13:05至15:00")
    result["symbol"] = result.symbol.astype(str).str.strip().str.upper()
    if not result.symbol.str.fullmatch(r"\d{6}\.(SH|SZ|BJ)").all() or result.symbol.nunique() != 1:
        raise ValueError("一次只能分析一只带市场后缀的股票")
    for name in FIELDS[2:]:
        try:
            series = pd.to_numeric(result[name], errors="raise")
            if pd.api.types.is_bool_dtype(series.dtype):
                raise ValueError("布尔值不是行情")
            result[name] = series.astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}必须为有限数值") from exc
        if not np.isfinite(result[name]).all():
            raise ValueError(f"{name}包含空值或非有限数")
    if result[["open", "close", "high", "low"]].le(0).any().any():
        raise ValueError("价格必须为正")
    if result[["vol", "amount"]].lt(0).any().any():
        raise ValueError("量额不能为负")
    if (result.high < result[["open", "close", "low"]].max(axis=1)).any() or (result.low > result[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("OHLC关系不合法")
    original_rows = len(result)
    if now is not None:
        result = result[result.dt <= _local(now)].copy()
    if result.empty:
        raise ValueError("没有已收盘的5分钟行情")
    return result.reset_index(drop=True), original_rows - len(result)


def _aggregate_thirty(five, now=None):
    rows, audit = [], []
    cutoff = _local(now) if now is not None else five.dt.iloc[-1]
    for day, group in five.groupby(five.dt.dt.normalize(), sort=False):
        minute = group.dt.dt.hour * 60 + group.dt.dt.minute
        for end in END30:
            target = day + pd.Timedelta(minutes=end)
            part = group[minute.gt(end - 30) & minute.le(end)]
            expected = set(range(end - 25, end + 1, 5))
            observed = set((part.dt.dt.hour * 60 + part.dt.dt.minute).tolist())
            complete = len(part) == 6 and observed == expected and target <= cutoff
            audit.append({"dt": target, "count": len(part), "complete": complete, "due": target <= cutoff})
            if complete:
                rows.append({"dt": target, "symbol": five.symbol.iloc[0], "open": part.open.iloc[0],
                             "close": part.close.iloc[-1], "high": part.high.max(), "low": part.low.min(),
                             "vol": part.vol.sum(), "amount": part.amount.sum()})
    return pd.DataFrame(rows, columns=FIELDS), pd.DataFrame(audit)


def _features(thirty):
    result = thirty.copy()
    close = result.close
    diff = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    result["macd_hist"] = diff - diff.ewm(span=9, adjust=False, min_periods=9).mean()
    result["sma60"] = close.rolling(60, min_periods=60).mean()
    result["sma60_lag5"] = result.sma60.shift(5)
    return result


def _native_records(thirty):
    try:
        from czsc import BarGenerator, CzscSignals, format_standard_kline
    except ImportError as exc:
        raise RuntimeError("需要安装czsc==1.0.2；请使用启动脚本完成依赖安装") from exc
    bg = BarGenerator(base_freq="30分钟", freqs=[], market="A股", max_count=max(2000, len(thirty) + 1))
    engine = CzscSignals(bg, signals_config=CONFIG)
    feature_frame = _features(thirty)
    records = []
    for i, bar in enumerate(format_standard_kline(thirty, "30分钟")):
        engine.update_signals(bar)
        row = feature_frame.iloc[i]
        c = engine.kas["30分钟"]
        record = {"dt": row["dt"], "symbol": row.symbol, "open": float(row.open), "close": float(row.close),
                  "high": float(row.high), "low": float(row.low), "bar_count": i + 1,
                  "macd_hist": float(row.macd_hist), "sma60": float(row.sma60), "sma60_lag5": float(row.sma60_lag5),
                  "bi_count": len(c.bi_list), "center_low": None, "center_high": None,
                  "center_start": None, "center_end": None, "center_kind": "CZSC笔中枢"}
        for name, (key, _) in SIGNAL_KEYS.items():
            value = engine.s.get(key)
            if not isinstance(value, str):
                raise RuntimeError(f"CZSC接口不兼容，未返回{key}")
            record[f"{name}_raw"] = value
        for center in reversed(c.zs_list):
            if center.is_valid():
                record.update(center_low=float(center.zd), center_high=float(center.zg),
                              center_start=_iso(center.sdt), center_end=_iso(center.edt))
                break
        records.append(record)
    return records


def _number(value):
    return float(value) if value is not None and math.isfinite(value) else None


def _events_from_records(records, warmup):
    """Pure event state machine; tests may supply explicitly synthetic raw states."""
    events = []
    previous = {"buy2": False, "buy3": False}
    previous_day = None
    seen2 = seen3 = None
    fired = False
    pending = None
    final_flags = {}

    def add(rule, row, origin, extra=None):
        evidence = {"close": row["close"], "bar_count": row["bar_count"], "buy2_raw": row["buy2_raw"],
                    "buy3_raw": row["buy3_raw"], "center_low": row.get("center_low"),
                    "center_high": row.get("center_high"), "center_kind": "CZSC笔中枢", "note": NOTE}
        if extra:
            evidence.update(extra)
        events.append({"symbol": row["symbol"], "rule": rule, "bar_end": _iso(row["dt"]),
                       "origin_time": _iso(origin), "message": RULE_LABELS[rule] + "：已收盘条件首次满足，需人工核验", "evidence": evidence})

    for i, row in enumerate(records):
        stamp = _local(row["dt"])
        if stamp.date() != previous_day:
            previous_day = stamp.date()
            seen2 = seen3 = None
            fired = False
            pending = None
        eligible = row["bar_count"] > warmup
        fresh = {}
        states = {}
        for name, (_, target) in SIGNAL_KEYS.items():
            state = row[f"{name}_raw"].split("_")[0] == target
            states[name] = state
            fresh[name] = eligible and state and not previous[name]
            previous[name] = state
            if fresh[name]:
                add(name, row, stamp, {"signal_k_low": row["low"], "signal_k_high": row["high"]})
        if fresh["buy2"] and seen2 is None:
            seen2 = stamp
        if fresh["buy3"] and seen3 is None:
            seen3 = stamp
        if eligible and seen2 is not None and seen3 is not None and not fired:
            add("buy23_day", row, min(seen2, seen3), {"buy2_first_at": _iso(seen2), "buy3_first_at": _iso(seen3)})
            fired = True
        confirmed = False
        trend = all(_number(row.get(k)) is not None for k in ["macd_hist", "sma60", "sma60_lag5"])
        trend = trend and row["macd_hist"] > 0 and row["close"] > row["sma60"] and row["sma60"] > row["sma60_lag5"]
        if pending is not None:
            age = i - pending["offset"]
            # A missing intermediate slot cannot silently become a valid one-to-three-bar confirmation.
            old_stamp = _local(records[i-1]["dt"])
            old_slot = END30.index(old_stamp.hour * 60 + old_stamp.minute)
            current_slot = END30.index(stamp.hour * 60 + stamp.minute)
            gap = current_slot != old_slot + 1
            if age > 3 or row["close"] < pending["low"] or gap:
                pending = None
            elif trend and row["close"] > pending["high"]:
                add("buy2_optimized", row, pending["time"], {"origin_low": pending["low"], "reference_high": pending["high"],
                    "confirmation_time": _iso(stamp), "wait_bars": age, "macd_hist": row["macd_hist"],
                    "sma60": row["sma60"], "sma60_lag5": row["sma60_lag5"]})
                pending = None
                confirmed = True
        if fresh["buy2"] and not confirmed:
            pending = {"offset": i, "time": stamp, "high": row["high"], "low": row["low"]}
        final_flags = {"buy2_state": states["buy2"], "buy3_state": states["buy3"], "trend60_macd_pass": bool(trend),
                       "optimized_pending_origin": _iso(pending["time"]) if pending else None}
    return events, final_flags


def analyze_five_minute(frame, warmup=200, now=None):
    """Analyze completed F5 bars and return historical research events for a monitor.

    The caller is responsible for first-scan suppression, freshness and ledger
    deduplication. Quality ``alertable`` rejects recent closed missing slots;
    in-progress/future slots never count as gaps. No quotes are synthesized.
    """
    if isinstance(warmup, (bool, np.bool_)) or not isinstance(warmup, Integral) or warmup < 1:
        raise ValueError("warmup必须为正整数")
    five, excluded = _normalize_five(frame, now)
    thirty, audit = _aggregate_thirty(five, now)
    due = audit[audit.due]
    partial = int((due["count"].gt(0) & ~due.complete).sum())
    empty = int(due["count"].eq(0).sum())
    quality = {"input_five_count": len(five), "future_five_excluded": excluded,
               "complete_thirty_count": len(thirty), "closed_partial_slots": partial, "closed_empty_slots": empty,
               "in_progress_or_future_slots": int((~audit.due).sum()), "recent_closed_gaps": 0,
               "alertable": False, "note": "只聚合6个完整5分钟收盘标签；不补分钟，不以滚动高低通道冒充中枢"}
    if thirty.empty:
        return {"symbol": five.symbol.iloc[0], "asof": None, "last_five_asof": _iso(five.dt.iloc[-1]),
                "bars30_count": 0, "status": "insufficient_history", "snapshot": {}, "events": [], "quality": quality}
    window_start = thirty.dt.iloc[-min(len(thirty), warmup)]
    recent = due[due.dt >= window_start]
    quality["recent_closed_gaps"] = int((~recent.complete).sum())
    quality["alertable"] = len(thirty) > warmup and not quality["recent_closed_gaps"]
    if len(thirty) <= warmup:
        snapshot = {"close": float(thirty.close.iloc[-1]), "buy2_raw": "预热中", "buy3_raw": "预热中", "bi_count": 0,
                    "center_low": None, "center_high": None, "center_kind": "CZSC笔中枢", "note": NOTE}
        events = []
        status = "insufficient_history"
    else:
        records = _native_records(thirty)
        events, flags = _events_from_records(records, warmup)
        last = records[-1]
        snapshot = {key: value for key, value in last.items() if key not in {"dt", "symbol", "open", "high", "low"}}
        for key in ["macd_hist", "sma60", "sma60_lag5"]:
            snapshot[key] = _number(snapshot[key])
        snapshot.update(flags, note=NOTE)
        status = "ready"
    return {"symbol": str(five.symbol.iloc[0]), "asof": _iso(thirty.dt.iloc[-1]),
            "last_five_asof": _iso(five.dt.iloc[-1]), "bars30_count": len(thirty),
            "status": status, "snapshot": snapshot, "events": events, "quality": quality}
