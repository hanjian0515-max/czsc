"""Read-only Eastmoney five-minute OHLCV input for the real-signal engine.

This module never fabricates quotes, fills missing bars, reads a local demo,
or retries a failed request with stale data. Public endpoint availability and
the amount of minute history it supplies are not guaranteed. The monitor must
still check freshness, complete F30 bars and its normal structure warm-up.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from http.client import HTTPException
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd


ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 12_000
FIELDS = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
END5 = set(range(575, 691, 5)) | set(range(785, 901, 5))
CHINA = ZoneInfo("Asia/Shanghai")
_SYMBOL = re.compile(r"([0-9]{6})\.(SH|SZ)")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class OnlineNetworkError(OSError):
    """The actual remote request failed; no cached/data substitute was used."""


class OnlineDataError(ValueError):
    """A response did not contain validated five-minute bars for this asset."""


def _same_endpoint(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "push2his.eastmoney.com"
                and parsed.port in (None, 443)
                and parsed.path == "/api/qt/stock/kline/get"
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except (TypeError, ValueError):
        return False


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The fixed public endpoint requires no redirect. Reject even a same-
        # host redirect so future location/header changes cannot widen access.
        raise OnlineNetworkError("联网行情接口发生重定向，已停止请求；未读取其他地址")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OnlineDataError("联网行情响应存在重复JSON字段")
        result[key] = value
    return result


def _invalid_constant(value):
    raise OnlineDataError("联网行情响应包含非有限JSON数值")


def _numeric(text, row, field):
    try:
        # Strip whitespace but never interpret units, booleans, expressions,
        # missing-value markers, or an HTML error as a price.
        if not isinstance(text, str) or not _NUMBER.fullmatch(text.strip()):
            raise ValueError("empty")
        value = float(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OnlineDataError(f"联网行情第{row}条{field}不是数值") from exc
    if not math.isfinite(value):
        raise OnlineDataError(f"联网行情第{row}条{field}不是有限数值")
    return value


def _parse(raw, symbol, code, market):
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OnlineDataError("联网行情返回内容不是合法JSON；可能为接口错误或访问限制") from exc
    if not isinstance(payload, dict) or type(payload.get("rc")) is not int or payload["rc"] != 0:
        raise OnlineDataError("联网行情接口未返回成功状态，未使用任何替代行情")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("code") != code:
        raise OnlineDataError("联网行情代码与所选股票不一致或暂无该股数据")
    if "market" in data and (type(data["market"]) not in (int, str) or data["market"] not in (market, str(market))):
        raise OnlineDataError("联网行情市场与所选沪深市场不一致")
    klines = data.get("klines")
    if not isinstance(klines, list) or not klines:
        raise OnlineDataError("联网接口未提供5分钟历史，请检查股票是否有行情或稍后重试")
    if len(klines) > MAX_RECORDS:
        raise OnlineDataError("联网行情条数超过请求上限，未截断或使用异常响应")
    records, previous = [], None
    for number, kline in enumerate(klines, 1):
        parts = kline.split(",") if isinstance(kline, str) else []
        if len(parts) != 11:
            raise OnlineDataError(f"联网行情第{number}条应为11个字段")
        if not _STAMP.fullmatch(parts[0]):
            raise OnlineDataError(f"联网行情第{number}条时间格式非法")
        try:
            stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise OnlineDataError(f"联网行情第{number}条日期非法") from exc
        if stamp.hour * 60 + stamp.minute not in END5 or stamp.weekday() >= 5:
            raise OnlineDataError(f"联网行情第{number}条不是A股5分钟收盘时点")
        if previous is not None and stamp <= previous:
            raise OnlineDataError(f"联网行情第{number}条重复或倒序，未静默排序或去重")
        previous = stamp
        # Eastmoney f52/f53/f54/f55 are O/C/H/L; f56 is volume in
        # hands (100 shares), f57 is the actual turnover amount in CNY.
        # f58..f61 are unused derived statistics. Providers may mark these
        # unavailable (e.g. "-") while supplying valid OHLC/volume/amount;
        # do not make them a dependency of the actual eight-column input.
        values = [_numeric(v, number, f"f{52+i}") for i, v in enumerate(parts[1:7])]
        opening, close, high, low, hands, amount = values
        if min(opening, close, high, low) <= 0 or hands < 0 or amount < 0:
            raise OnlineDataError(f"联网行情第{number}条价格或成交量额非法")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise OnlineDataError(f"联网行情第{number}条OHLC关系非法")
        shares = hands * 100
        if not math.isfinite(shares):
            raise OnlineDataError(f"联网行情第{number}条成交量换算溢出")
        records.append((stamp, symbol, opening, close, high, low, shares, amount))
    return pd.DataFrame.from_records(records, columns=FIELDS)


def _retrieved_at(now):
    try:
        stamp = pd.Timestamp(now if now is not None else datetime.now(CHINA))
        if pd.isna(stamp):
            raise ValueError("empty")
        stamp = stamp.tz_localize(CHINA) if stamp.tz is None else stamp.tz_convert(CHINA)
        return stamp.isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise OnlineDataError("联网行情获取时间非法") from exc


def fetch_five_minute(symbol, *, now=None, opener=None):
    """Fetch one explicitly identified SH/SZ asset without interpolation.

    ``opener`` may be an ``OpenerDirector`` or a callable taking
    ``(Request, timeout=...)`` for offline mechanism tests. The actual path
    uses normal certificate validation and rejects redirects. A valid
    response may still be stale or insufficient for structure analysis;
    those conditions must never be turned into a synthetic live alert.
    """
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        raise OnlineDataError("联网行情代码必须为六位代码.SH或.SZ")
    code, exchange = symbol.split(".")
    market = 1 if exchange == "SH" else 0
    retrieved_at = _retrieved_at(now)
    query = urllib.parse.urlencode({
        "secid": f"{market}.{code}", "klt": 5, "fqt": 0,
        "beg": 0, "end": 20500101, "lmt": MAX_RECORDS,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    })
    url = ENDPOINT + "?" + query
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 ChanQuant-Alerts/1.0",
        "Accept": "application/json", "Referer": "https://quote.eastmoney.com/",
    }, method="GET")
    if opener is None:
        open_request = urllib.request.build_opener(_RejectRedirects()).open
    else:
        open_request = getattr(opener, "open", opener)
        if not callable(open_request):
            raise OnlineDataError("联网行情opener必须为可调用对象")
    length = None
    try:
        with open_request(request, timeout=TIMEOUT_SECONDS) as response:
            if not _same_endpoint(response.geturl()):
                raise OnlineNetworkError("联网行情响应跳转到其他地址，已拒绝读取")
            status = getattr(response, "status", None)
            if status != 200:
                raise OnlineNetworkError(f"联网行情接口HTTP状态异常（{status}）")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise OnlineDataError("联网行情响应长度字段非法") from exc
                if not 0 <= length <= MAX_RESPONSE_BYTES:
                    raise OnlineDataError("联网行情响应过大或长度非法")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except OnlineNetworkError:
        raise
    except urllib.error.HTTPError as exc:
        raise OnlineNetworkError(f"联网行情请求失败（HTTP {exc.code}），请检查网络或稍后重试") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, HTTPException, OSError) as exc:
        raise OnlineNetworkError("无法获取联网5分钟行情：连接失败或超时，请检查网络后重试") from exc
    if not isinstance(raw, bytes) or not raw:
        raise OnlineDataError("联网行情返回空内容")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OnlineDataError("联网行情响应超过4MiB，已拒绝解析")
    if length is not None and len(raw) != length:
        raise OnlineDataError("联网行情响应长度与实际内容不符，未使用截断响应")
    frame = _parse(raw, symbol, code, market)
    frame.attrs.update(provider="eastmoney", source_format="EASTMONEY-5M",
                       source_url=url, price_basis="raw", retrieved_at=retrieved_at,
                       response_sha256=hashlib.sha256(raw).hexdigest(),
                       volume_unit="shares", loaded_records=len(frame))
    return frame
