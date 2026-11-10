"""Read-only discovery and decoding of conventional TDX five-minute files.

The little-endian 32-byte layout is ``<HHfffffII``: packed calendar date,
minutes since midnight, O/H/L/C, amount, unsigned volume and reserved data.
Prices and volume retain their source units. This module neither downloads
quotes nor changes the selected data directory.
"""
from __future__ import annotations

from numbers import Integral
from datetime import datetime
import hashlib
import math
import os
from pathlib import Path
import re
import struct

import pandas as pd


_RECORD = struct.Struct("<HHfffffII")
_STEM = re.compile(r"(sh|sz)([0-9]{6})", re.IGNORECASE)
_SYMBOL = re.compile(r"([0-9]{6})\.(SH|SZ)", re.IGNORECASE)
_COLUMNS = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
_SESSION_ENDS = set(range(575, 691, 5)) | set(range(785, 901, 5))
_MAX_DISCOVERY_ENTRIES = 200_000


class DataChangingError(OSError):
    """The source changed during a scan; retry a later stable snapshot."""


def _identity(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def _file_symbol(path: Path) -> str:
    match = _STEM.fullmatch(path.stem)
    if path.suffix.lower() != ".lc5" or match is None or match.group(2).startswith("880"):
        raise ValueError(f"LC5文件名无效: {path.name}；应为sh/sz+六位代码.lc5")
    return f"{match.group(2)}.{match.group(1).upper()}"


def _requested_symbols(symbols):
    if isinstance(symbols, str):
        raise ValueError("symbols应为带市场后缀的代码列表")
    result = []
    try:
        values = list(symbols)
    except TypeError as exc:
        raise ValueError("symbols应为代码列表") from exc
    for value in values:
        if not isinstance(value, str):
            raise ValueError("代码必须是六位代码.SH或.SZ")
        normalized = value.strip().upper()
        match = _SYMBOL.fullmatch(normalized)
        if match is None or match.group(1).startswith("880"):
            raise ValueError(f"不支持的沪深LC5代码: {value}")
        if normalized not in result:
            result.append(normalized)
    return result


def discover_lc5(root, symbols) -> dict[str, Path]:
    """Find requested files beneath the chosen folder, never infer a market.

    Accepts an installation directory, ``vipdoc`` or a market's ``fzline``.
    Conventional locations are inspected first, then one bounded traversal
    checks the rest of the selected tree and detects duplicates. A missing
    individual asset is omitted. An invalid root, escaping symlink or two
    different matching paths raises instead of silently picking a dataset.
    Asset-class exclusion (e.g. an exchange index) belongs to the monitor's
    explicit stock universe, not to a guess made from a directory name.
    """
    requested = _requested_symbols(symbols)
    chosen = Path(root).expanduser()
    try:
        base = chosen.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"通达信数据目录不存在或不可访问: {chosen}") from exc
    if not base.is_dir():
        raise ValueError(f"通达信数据路径不是目录: {chosen}")
    if not requested:
        return {}
    wanted = set(requested)
    matches: dict[str, list[Path]] = {symbol: [] for symbol in requested}
    visited = set()

    def inspect(path):
        path = Path(path)
        if path in visited:
            return
        visited.add(path)
        try:
            symbol = _file_symbol(path)
        except ValueError:
            return
        if symbol not in wanted:
            return
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"无法读取目标LC5文件路径: {path}") from exc
        if not resolved.is_relative_to(base):
            raise ValueError(f"目标LC5链接指向所选目录之外，未读取: {path}")
        if path.is_file():
            matches[symbol].append(path)

    for symbol in requested:
        code, market = symbol.split(".")
        market = market.lower()
        filename = f"{market}{code}.lc5"
        for candidate in [base / market / "fzline" / filename,
                          base / "vipdoc" / market / "fzline" / filename,
                          base / filename]:
            if candidate.exists() or candidate.is_symlink():
                inspect(candidate)
    try:
        # rglob does not follow directory symlinks; explicit target-file
        # symlinks are resolved above and must remain beneath the chosen root.
        for count, path in enumerate(base.rglob("*"), 1):
            if count > _MAX_DISCOVERY_ENTRIES:
                raise ValueError("所选目录过大，请直接选择通达信vipdoc或fzline目录")
            if path.suffix.lower() == ".lc5":
                inspect(path)
    except OSError as exc:
        raise ValueError(f"扫描通达信数据目录失败: {base}") from exc
    duplicates = {symbol: paths for symbol, paths in matches.items() if len(paths) > 1}
    if duplicates:
        details = "; ".join(f"{symbol}: {', '.join(map(str, paths))}" for symbol, paths in duplicates.items())
        raise ValueError(f"发现同一代码多个LC5路径，请选择更具体的数据目录: {details}")
    return {symbol: matches[symbol][0] for symbol in requested if matches[symbol]}


def read_lc5(path, max_records=12_000) -> pd.DataFrame:
    """Read a stable, validated tail of one LC5 file without modifying it.

    A corrupt or partial trailing record is never truncated away. When a
    file is being updated, ``DataChangingError`` takes precedence over
    interpreting the incomplete snapshot. Only the loaded history window
    is validated; ``attrs['truncated']`` records tail selection explicitly.
    Current incomplete bars are kept here and filtered using the analyzer's
    clock, so this decoder never invents a current-data freshness claim.
    """
    if isinstance(max_records, (bool,)) or not isinstance(max_records, Integral) or max_records < 1:
        raise ValueError("max_records必须是正整数")
    path = Path(path)
    symbol = _file_symbol(path)
    before = path.stat()
    size = before.st_size
    total = size // _RECORD.size
    loaded = min(int(max_records), total)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise DataChangingError(f"LC5正在更新，等待下一轮重试: {path}")
        stream.seek((total - loaded) * _RECORD.size)
        content = stream.read(loaded * _RECORD.size)
        after_handle = os.fstat(stream.fileno())
    try:
        after_path = path.stat()
    except OSError as exc:
        raise DataChangingError(f"LC5扫描时被移动或替换，等待下一轮重试: {path}") from exc
    if _identity(before) != _identity(after_handle) or _identity(before) != _identity(after_path):
        raise DataChangingError(f"LC5扫描期间发生变化，等待下一轮重试: {path}")
    if size == 0:
        raise ValueError(f"LC5文件为空: {path}")
    if size % _RECORD.size:
        raise ValueError(f"LC5尾部不完整，文件长度不是32字节整数倍: {path}")
    if len(content) != loaded * _RECORD.size:
        raise DataChangingError(f"LC5读取长度变化，等待下一轮重试: {path}")

    rows = []
    previous = None
    for relative, record in enumerate(_RECORD.iter_unpack(content), 1):
        packed_date, minutes, opening, high, low, close, amount, volume, _reserved = record
        number = total - loaded + relative
        year = 2004 + packed_date // 2048
        month_day = packed_date % 2048
        month, day = divmod(month_day, 100)
        try:
            if minutes >= 1440 or minutes not in _SESSION_ENDS:
                raise ValueError("not an A-share five-minute end label")
            hour, minute = divmod(minutes, 60)
            stamp = datetime(year, month, day, hour, minute)
        except ValueError as exc:
            raise ValueError(f"LC5第{number}条日期或5分钟收盘时间非法: {path}") from exc
        if previous is not None and stamp <= previous:
            raise ValueError(f"LC5第{number}条时间重复或未严格递增: {path}")
        previous = stamp
        for name, value in {"open": opening, "high": high, "low": low, "close": close,
                            "amount": amount, "vol": volume}.items():
            if not math.isfinite(value) or value < 0 or (name in {"open", "high", "low", "close"} and value == 0):
                raise ValueError(f"LC5第{number}条{name}不是合法价格或量额: {path}")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise ValueError(f"LC5第{number}条OHLC关系错误: {path}")
        rows.append((stamp, symbol, opening, close, high, low, volume, amount))
    result = pd.DataFrame.from_records(rows, columns=_COLUMNS)
    result.attrs.update(total_records=total, loaded_records=loaded, truncated=loaded < total,
                        source_path=str(path), source_mtime_ns=before.st_mtime_ns,
                        snapshot_sha256=hashlib.sha256(content).hexdigest(),
                        price_basis="raw", volume_unit="source", source_format="TDX-LC5-32")
    return result
