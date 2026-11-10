"""Read-only TDX quote-protocol five-minute history, with no quote fallback.

This connects to quote servers over TCP. It does not read the memory of an
installed terminal, use a brokerage account, write LC5 files, or trade. Public
server reachability must be checked on the user's actual network.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
import hashlib
import ipaddress
import json
import math
from numbers import Real
import re
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd


FIELDS = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
PAGE_SIZE = 800
HISTORY_TARGET = 3200
TIMEOUT_SECONDS = 2
REQUEST_BUDGET_SECONDS = 16
COOLDOWN_SECONDS = 30
MAX_CONFIG_STATES = 32
CHINA = ZoneInfo("Asia/Shanghai")
END5 = set(range(575, 691, 5)) | set(range(785, 901, 5))
_SYMBOL = re.compile(r"([0-9]{6})\.(SH|SZ)")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_STATE_LOCK = threading.RLock()
_STATES = OrderedDict()
_AUTO_NAMES = ("上海电信主站Z1", "北京联通主站Z1", "上证云成都电信一",
               "杭州电信主站J1", "上海电信主站Z80", "北京联通主站Z80")


class TdxNetworkError(OSError):
    """Connection or protocol transport failed; no cached price was returned."""


class TdxDataError(ValueError):
    """A response was not valid five-minute data for the requested security."""


def validate_endpoint(host, port):
    """Return a normalized optional IPv4/DNS host and an explicit TCP port."""
    if not isinstance(host, str) or len(host) > 253:
        raise TdxDataError("通达信主站须为IPv4地址或DNS名称；留空使用自动节点")
    value = host.strip()
    if value:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if (re.fullmatch(r"[0-9.]+", value)
                    or any(not _HOST_LABEL.fullmatch(label) for label in value.split("."))):
                raise TdxDataError("通达信主站格式错误，请填写IPv4或DNS名称，不含协议和路径")
            value = value.lower()
        else:
            if address.version != 4:
                raise TdxDataError("当前通达信客户端仅支持IPv4主站")
            value = str(address)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TdxDataError("通达信端口须为1到65535的整数")
    return value, port


def reset_connection_state():
    """Forget only endpoint/cooldown metadata, never market data (none stored)."""
    with _STATE_LOCK:
        _STATES.clear()


def _default_servers():
    try:
        from pytdx.config.hosts import hq_hosts
    except ImportError as exc:
        raise TdxDataError("需要pytdx==1.72，请用新版启动脚本安装通达信依赖") from exc
    entries = {name: (name, host, port) for name, host, port in hq_hosts}
    servers = [entries[name] for name in _AUTO_NAMES if name in entries]
    if not servers:
        raise TdxDataError("未找到通达信内置节点，请填写正在使用的主站地址")
    return servers


def _make_client(**kwargs):
    try:
        from pytdx.hq import TdxHq_API
    except ImportError as exc:
        raise TdxDataError("需要pytdx==1.72，请用新版启动脚本安装通达信依赖") from exc
    return TdxHq_API(**kwargs)


def _close_client(client):
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass
    finally:
        # pytdx 1.72 can raise on shutdown of a never-connected socket before
        # closing it. Still close that actual socket without masking the cause.
        connection = getattr(client, "client", None)
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _is_network_error(exc, depth=0):
    if isinstance(exc, OSError):
        return True
    if exc.__class__.__name__ == "TdxConnectionError" and exc.__class__.__module__.startswith("pytdx"):
        return True
    original = getattr(exc, "original_exception", None)
    return depth < 3 and isinstance(original, Exception) and _is_network_error(original, depth + 1)


def _value(value, number, field):
    if isinstance(value, bool) or not isinstance(value, (Real, str)):
        raise TdxDataError(f"通达信第{number}条{field}不是合法数值")
    if isinstance(value, str) and not _NUMBER.fullmatch(value.strip()):
        raise TdxDataError(f"通达信第{number}条{field}不是合法数值")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise TdxDataError(f"通达信第{number}条{field}不是合法数值") from exc
    if not math.isfinite(result):
        raise TdxDataError(f"通达信第{number}条{field}不是有限数值")
    return result


def _parse_page(rows, symbol, code, market):
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise TdxDataError("通达信分钟页格式或条数异常")
    normalized, previous = [], None
    for number, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise TdxDataError("通达信分钟行格式异常")
        if "code" in row:
            candidate = row["code"]
            if isinstance(candidate, int) and not isinstance(candidate, bool) and 0 <= candidate <= 999999:
                candidate = f"{candidate:06d}"
            if candidate != code:
                raise TdxDataError("通达信返回代码与所选个股不一致")
        if "market" in row and (type(row["market"]) not in (int, str) or row["market"] not in (market, str(market))):
            raise TdxDataError("通达信返回市场与所选个股不一致")
        text = row.get("datetime")
        if not isinstance(text, str) or not _STAMP.fullmatch(text):
            raise TdxDataError(f"通达信第{number}条时间格式异常")
        try:
            stamp = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise TdxDataError(f"通达信第{number}条日期异常") from exc
        if stamp.hour * 60 + stamp.minute not in END5 or stamp.weekday() >= 5:
            raise TdxDataError(f"通达信第{number}条不是A股5分钟收盘时点")
        for field in ("year", "month", "day", "hour", "minute"):
            if field in row and (type(row[field]) is not int or row[field] != getattr(stamp, field)):
                raise TdxDataError(f"通达信第{number}条日期字段不一致")
        if previous is not None and stamp <= previous:
            raise TdxDataError("通达信分钟页内时间重复或倒序，未排序或去重")
        previous = stamp
        opening, close, high, low, volume, amount = [_value(row.get(field), number, field)
            for field in ("open", "close", "high", "low", "vol", "amount")]
        if min(opening, close, high, low) <= 0 or min(volume, amount) < 0:
            raise TdxDataError("通达信价格或量额非法")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise TdxDataError("通达信OHLC关系非法")
        normalized.append((stamp, symbol, opening, close, high, low, volume, amount))
    return normalized


def _get_node(server, code, market, symbol, factory, deadline):
    name, host, port = server
    client = None
    try:
        if time.monotonic() >= deadline:
            raise TdxNetworkError("通达信取数超过本次总时限")
        try:
            client = factory(heartbeat=False, auto_retry=False, raise_exception=True)
        except TdxDataError:
            raise
        except Exception as exc:
            raise TdxDataError("通达信客户端初始化失败，请检查依赖安装") from exc
        try:
            connected = client.connect(host, port, time_out=TIMEOUT_SECONDS)
        except Exception as exc:
            raise TdxNetworkError("通达信主站连接或握手失败") from exc
        if not connected:
            raise TdxNetworkError("通达信主站连接失败")
        pages = []
        for start in range(0, HISTORY_TARGET, PAGE_SIZE):
            if time.monotonic() >= deadline:
                raise TdxNetworkError("通达信取数超过本次总时限")
            try:
                rows = client.get_security_bars(0, market, code, start, PAGE_SIZE)
            except Exception as exc:
                if _is_network_error(exc):
                    raise TdxNetworkError("通达信分钟数据传输失败") from exc
                raise TdxDataError("通达信分钟响应无法解析，未换用其他行情") from exc
            if rows is None:
                raise TdxDataError("通达信未返回分钟数据页")
            page = _parse_page(rows, symbol, code, market)
            if not page:
                if not pages:
                    raise TdxDataError("通达信未提供该股5分钟历史")
                break
            if pages and page[-1][0] >= pages[-1][0][0]:
                raise TdxDataError("通达信分钟分页时间重叠或顺序异常，未去重拼接")
            pages.append(page)
            if len(page) < PAGE_SIZE:
                break
        if time.monotonic() >= deadline:
            raise TdxNetworkError("通达信取数超过本次总时限")
        records = [row for page in reversed(pages) for row in page]
        frame = pd.DataFrame.from_records(records, columns=FIELDS)
        digest_rows = [[row[0].isoformat(), *row[1:]] for row in records]
        digest = hashlib.sha256(json.dumps(digest_rows, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return frame, digest
    finally:
        _close_client(client)


def _record_state(key, *, successful=None, failed=False):
    with _STATE_LOCK:
        state = dict(_STATES.get(key, {}))
        if successful is not None:
            state.update(last_good=successful, until=0)
        elif failed:
            state["until"] = time.monotonic() + COOLDOWN_SECONDS
        _STATES[key] = state
        _STATES.move_to_end(key)
        while len(_STATES) > MAX_CONFIG_STATES:
            _STATES.popitem(last=False)


def fetch_five_minute(symbol, *, now=None, host="", port=7709, client_factory=None):
    """Fetch up to 3200 actual F5 bars; short history stays short.

    The optional factory has the TdxHq_API constructor signature. It exists for
    offline mechanism tests. No quote DataFrame is stored between requests.
    Explicit hosts never fail over to another server. Automatic endpoints use
    only the selected bundled pytdx hosts and discard incomplete attempts.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise TdxDataError("通达信行情代码必须为六位代码.SH或.SZ")
    host, port = validate_endpoint(host, port)
    if client_factory is not None and not callable(client_factory):
        raise TdxDataError("通达信客户端工厂必须可调用")
    try:
        stamp = pd.Timestamp(now if now is not None else datetime.now(CHINA))
        if pd.isna(stamp):
            raise ValueError("empty")
        stamp = stamp.tz_localize(CHINA) if stamp.tz is None else stamp.tz_convert(CHINA)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TdxDataError("通达信获取时间非法") from exc
    code, exchange = symbol.split(".")
    market = 1 if exchange == "SH" else 0
    key = ("manual", host, port) if host else ("auto", port)
    with _STATE_LOCK:
        state = dict(_STATES.get(key, {}))
    if state.get("until", 0) > time.monotonic():
        raise TdxNetworkError("通达信节点刚连接失败，30秒内暂停重复请求；可检查网络后稍候重试")
    servers = [("自定义通达信主站", host, port)] if host else [(name,address,port) for name,address,_ in _default_servers()]
    last_good = state.get("last_good")
    if last_good in servers:
        servers = [last_good] + [server for server in servers if server != last_good]
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    factory = client_factory or _make_client
    attempted = []
    for server in servers:
        attempted.append(f"{server[1]}:{server[2]}")
        try:
            frame, digest = _get_node(server, code, market, symbol, factory, deadline)
        except TdxNetworkError:
            if time.monotonic() >= deadline:
                break
            continue
        # Data/protocol validation failures deliberately leave this loop. A
        # different server must not hide malformed input or mix page sources.
        frame.attrs.update(provider="tdx", source_format="TDX-HQ-5M", price_basis="raw",
            source_host=server[1], source_port=server[2], source_name=server[0],
            source_url=f"tdx://{server[1]}:{server[2]}", retrieved_at=stamp.isoformat(),
            loaded_records=len(frame), history_target=HISTORY_TARGET, response_sha256=digest,
            response_hash_basis="normalized_tdx_ohlcv_rows", volume_unit="source")
        _record_state(key, successful=server)
        return frame
    _record_state(key, failed=True)
    raise TdxNetworkError("无法连接通达信行情主站（" + ", ".join(attempted) +
        "）。请确认电脑能访问该主站，或填写通达信通信设置中的主站地址和端口")
