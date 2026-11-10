"""Synthetic binary format fixtures; these are not stock-performance data."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from alerts import sources


def record(dt=datetime(2024, 2, 29, 9, 35), opening=10.25, high=10.75, low=10., close=10.5,
           amount=12345.5, volume=123, packed=None, minutes=None):
    packed = (dt.year - 2004) * 2048 + dt.month * 100 + dt.day if packed is None else packed
    minutes = dt.hour * 60 + dt.minute if minutes is None else minutes
    return struct.pack("<HHfffffII", packed, minutes, opening, high, low, close, amount, volume, 0)


def write(tmp_path, name="sz000001.lc5", content=None):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(record() if content is None else content)
    return path


@pytest.mark.parametrize("name,symbol", [("sz000001.lc5", "000001.SZ"), ("sh600660.lc5", "600660.SH"),
                                          ("SZ300308.LC5", "300308.SZ")])
def test_market_names_and_original_units_are_preserved(tmp_path, name, symbol):
    path = write(tmp_path, name)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    df = sources.read_lc5(path)
    assert list(df.columns) == ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    assert df.symbol.tolist() == [symbol]
    assert df.dt.iloc[0].to_pydatetime() == datetime(2024, 2, 29, 9, 35)
    assert df.open.iloc[0] == 10.25
    assert df.vol.iloc[0] == 123
    assert df.amount.iloc[0] == pytest.approx(12345.5)
    assert df.attrs["price_basis"] == "raw"
    assert df.attrs["total_records"] == df.attrs["loaded_records"] == 1
    assert df.attrs["truncated"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("dt", [datetime(2019, 12, 31, 15), datetime(2020, 1, 2, 9, 35),
                                datetime(2024, 2, 29, 13, 5), datetime(2026, 11, 9, 10)])
def test_packed_day_uses_year_remainder_before_mod100(tmp_path, dt):
    df = sources.read_lc5(write(tmp_path, content=record(dt)))
    assert df.dt.iloc[0].to_pydatetime() == dt


def test_tail_selection_reads_only_requested_records(tmp_path):
    start = datetime(2024, 2, 29, 9, 35)
    payload = b"".join(record(start + timedelta(minutes=5 * i), volume=i + 1) for i in range(8))
    df = sources.read_lc5(write(tmp_path, content=payload), max_records=3)
    assert df.vol.tolist() == [6, 7, 8]
    assert df.attrs["total_records"] == 8 and df.attrs["loaded_records"] == 3
    assert df.attrs["truncated"] is True


def test_tail_selection_does_not_hide_corrupt_trailing_bytes(tmp_path):
    path = write(tmp_path, content=record() * 4 + b"bad")
    with pytest.raises(ValueError, match="32"):
        sources.read_lc5(path, max_records=1)


@pytest.mark.parametrize("max_records", [0, -1, True, 1.5, "3", None])
def test_invalid_tail_limits_rejected(tmp_path, max_records):
    with pytest.raises(ValueError, match="正整数"):
        sources.read_lc5(write(tmp_path), max_records=max_records)


@pytest.mark.parametrize("filename", ["sz000001.lc1", "bj920001.lc5", "880491.lc5", "sh880491.lc5",
                                      "000001.lc5", "sz12345.lc5", "sz000001.csv"])
def test_invalid_names_rejected(tmp_path, filename):
    with pytest.raises(ValueError, match="文件名"):
        sources.read_lc5(write(tmp_path, filename))


def test_empty_rejected(tmp_path):
    with pytest.raises(ValueError, match="为空"):
        sources.read_lc5(write(tmp_path, content=b""))


@pytest.mark.parametrize("minutes", [0, 570, 574, 576, 695, 780, 784, 905, 1440])
def test_wrong_or_non_session_end_times_rejected(tmp_path, minutes):
    with pytest.raises(ValueError, match="收盘时间"):
        sources.read_lc5(write(tmp_path, content=record(minutes=minutes)))


@pytest.mark.parametrize("month_day", [0, 1321, 230])
def test_invalid_calendar_date_rejected(tmp_path, month_day):
    # 2023-02-30, zero month/day and month13 are not valid calendars.
    with pytest.raises(ValueError, match="日期"):
        sources.read_lc5(write(tmp_path, content=record(packed=(2023 - 2004) * 2048 + month_day)))


@pytest.mark.parametrize("changes", [{"opening": 0}, {"close": float("nan")}, {"high": float("inf")},
                                     {"amount": -1}, {"low": -1}, {"high": 10.2}, {"low": 10.6}])
def test_invalid_numbers_or_ohlc_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        sources.read_lc5(write(tmp_path, content=record(**changes)))


def test_zero_volume_and_amount_are_not_multiplied_or_fabricated(tmp_path):
    df = sources.read_lc5(write(tmp_path, content=record(amount=0, volume=0)))
    assert df.vol.iloc[0] == df.amount.iloc[0] == 0


@pytest.mark.parametrize("second", [datetime(2024, 2, 29, 9, 35), datetime(2024, 2, 28, 15)])
def test_duplicate_or_unsorted_timestamps_rejected(tmp_path, second):
    with pytest.raises(ValueError, match="重复|递增"):
        sources.read_lc5(write(tmp_path, content=record() + record(second)))


def test_detects_file_update_while_reading(tmp_path, monkeypatch):
    path = write(tmp_path)
    original = sources.os.fstat
    calls = 0
    def changed(fd):
        nonlocal calls
        calls += 1
        value = original(fd)
        if calls == 2:
            return SimpleNamespace(st_size=value.st_size + 32, st_mtime_ns=value.st_mtime_ns,
                                   st_dev=value.st_dev, st_ino=value.st_ino)
        return value
    monkeypatch.setattr(sources.os, "fstat", changed)
    with pytest.raises(sources.DataChangingError, match="发生变化"):
        sources.read_lc5(path)


def test_discovers_installation_vipdoc_and_direct_fzline(tmp_path):
    sh = write(tmp_path, "vipdoc/sh/fzline/sh600660.lc5")
    sz = write(tmp_path, "vipdoc/sz/fzline/sz000333.lc5")
    wanted = ["600660.SH", "000333.SZ", "300308.SZ"]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [sh, sz]}
    assert sources.discover_lc5(tmp_path, wanted) == {"600660.SH": sh, "000333.SZ": sz}
    assert sources.discover_lc5(tmp_path / "vipdoc", wanted) == {"600660.SH": sh, "000333.SZ": sz}
    assert sources.discover_lc5(sh.parent, wanted) == {"600660.SH": sh}
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_discovers_uppercase_fallback_without_inventing_market(tmp_path):
    path = write(tmp_path, "custom/SZ300308.LC5")
    assert sources.discover_lc5(tmp_path, ["300308.sz", "300308.SZ"]) == {"300308.SZ": path}


def test_duplicate_datasets_raise_instead_of_choosing_conventional_path(tmp_path):
    write(tmp_path, "sh/fzline/sh600660.lc5")
    write(tmp_path, "old-copy/sh600660.lc5")
    with pytest.raises(ValueError, match="多个LC5路径"):
        sources.discover_lc5(tmp_path, ["600660.SH"])


def test_outside_symlink_is_not_read(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    outside = write(tmp_path, "outside/sh600660.lc5")
    (root / "sh600660.lc5").symlink_to(outside)
    with pytest.raises(ValueError, match="目录之外"):
        sources.discover_lc5(root, ["600660.SH"])


def test_missing_individual_asset_is_omitted_and_nothing_created(tmp_path):
    assert sources.discover_lc5(tmp_path, ["600660.SH"]) == {}
    assert list(tmp_path.iterdir()) == []


def test_invalid_root_and_file_root_rejected(tmp_path):
    with pytest.raises(ValueError, match="不存在|不可访问"):
        sources.discover_lc5(tmp_path / "missing", ["600660.SH"])
    file = write(tmp_path)
    with pytest.raises(ValueError, match="不是目录"):
        sources.discover_lc5(file, ["600660.SH"])


@pytest.mark.parametrize("symbols", ["600660.SH", ["880491.TDX"], ["600660"], ["920001.BJ"], [880491], None])
def test_bad_symbols_rejected(tmp_path, symbols):
    with pytest.raises(ValueError):
        sources.discover_lc5(tmp_path, symbols)


def test_bounded_discovery_does_not_silently_skip_extra_tree(tmp_path, monkeypatch):
    write(tmp_path, "a/sh600660.lc5")
    write(tmp_path, "b/sz000333.lc5")
    monkeypatch.setattr(sources, "_MAX_DISCOVERY_ENTRIES", 1)
    with pytest.raises(ValueError, match="过大"):
        sources.discover_lc5(tmp_path, ["600660.SH"])
