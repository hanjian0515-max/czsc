"""Offline mechanism fixtures only: no quote here is historical market evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import urllib.error
import urllib.parse

import pandas as pd
import pytest

from alerts import online


STAMP = "2026-11-10 09:35"


def row(stamp=STAMP, **changes):
    fields = [stamp, "10.10", "10.20", "10.40", "10.00", "123.45", "765432.10", "4.00", "1.00", ".10", ".20"]
    for key, value in changes.items():
        fields[int(key)] = value
    return ",".join(fields)


def body(klines=None, *, code="600660", market=1, rc=0):
    data = {"code": code, "klines": [row()] if klines is None else klines}
    if market is not None:
        data["market"] = market
    return json.dumps({"rc": rc, "data": data}, ensure_ascii=False).encode()


class Response:
    def __init__(self, content=None, *, url=online.ENDPOINT, status=200, length=None):
        self.content = body() if content is None else content
        self.url, self.status = url, status
        self.headers = {} if length is None else {"Content-Length": length}
        self.read_limits, self.closed = [], False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limits.append(limit)
        return self.content[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error, self.calls = error, []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def fetch(payload=None, **kwargs):
    transport = Opener(Response(content=payload))
    return online.fetch_five_minute("600660.SH", opener=transport, now="2026-11-10 10:01", **kwargs)


def test_actual_field_order_units_and_traceable_raw_response():
    raw = body([row(), row("2026-11-10 09:40")])
    transport = Opener(Response(raw, length=str(len(raw))))
    result = online.fetch_five_minute("600660.SH", opener=transport, now=datetime(2026, 11, 10, 2, 1, tzinfo=timezone.utc))
    assert list(result.columns) == online.FIELDS
    assert result.dt.tolist() == [pd.Timestamp(STAMP), pd.Timestamp("2026-11-10 09:40")]
    assert result.symbol.tolist() == ["600660.SH"] * 2
    assert result.open.tolist() == [10.1, 10.1] and result.close.tolist() == [10.2, 10.2]
    assert result.high.tolist() == [10.4, 10.4] and result.low.tolist() == [10., 10.]
    assert result.vol.tolist() == pytest.approx([12345., 12345.])
    assert result.amount.tolist() == pytest.approx([765432.1, 765432.1])
    assert result.attrs["provider"] == "eastmoney"
    assert result.attrs["source_format"] == "EASTMONEY-5M"
    assert result.attrs["price_basis"] == "raw"
    assert result.attrs["volume_unit"] == "shares"
    assert result.attrs["loaded_records"] == 2
    assert result.attrs["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result.attrs["retrieved_at"] == "2026-11-10T10:01:00+08:00"
    assert transport.response.closed


@pytest.mark.parametrize("symbol,code,market", [("600660.SH", "600660", 1), ("000858.SZ", "000858", 0), ("300308.SZ", "300308", 0)])
def test_fixed_https_get_unadjusted_five_minutes_and_explicit_market(symbol, code, market):
    transport = Opener(Response(body(code=code, market=market)))
    result = online.fetch_five_minute(symbol, opener=transport, now="2026-11-10 10:01")
    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    parsed = urllib.parse.urlsplit(request.full_url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname == "push2his.eastmoney.com"
    assert parsed.path == "/api/qt/stock/kline/get"
    assert params["secid"] == [f"{market}.{code}"] and params["klt"] == ["5"]
    assert params["fqt"] == ["0"] and params["lmt"] == ["12000"]
    assert params["fields2"] == ["f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"]
    assert request.get_method() == "GET" and 0 < timeout <= 10
    assert request.get_header("Authorization") is None
    assert result.attrs["source_url"] == request.full_url


def test_default_transport_installs_redirect_rejection_without_disabling_tls(monkeypatch):
    transport, handlers = Opener(), []
    def build(*values):
        handlers.extend(values)
        return transport
    monkeypatch.setattr(online.urllib.request, "build_opener", build)
    online.fetch_five_minute("600660.SH", now="2026-11-10 10:01")
    assert len(handlers) == 1 and isinstance(handlers[0], online._RejectRedirects)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("target", ["http://push2his.eastmoney.com/api/qt/stock/kline/get", "https://example.org/quotes", online.ENDPOINT])
def test_even_same_host_redirect_is_not_followed(target):
    handler = online._RejectRedirects()
    with pytest.raises(online.OnlineNetworkError, match="重定向"):
        handler.redirect_request(None, None, 302, "redirect", {}, target)


@pytest.mark.parametrize("url", ["https://example.com/api/qt/stock/kline/get", "http://push2his.eastmoney.com/api/qt/stock/kline/get", "https://push2his.eastmoney.com:443/other", "https://user:password@push2his.eastmoney.com/api/qt/stock/kline/get", "https://push2his.eastmoney.com:444/api/qt/stock/kline/get", "https://push2his.eastmoney.com:bad/api/qt/stock/kline/get", online.ENDPOINT+"#fragment"])
def test_response_url_outside_exact_endpoint_rejected_before_read(url):
    transport = Opener(Response(url=url))
    with pytest.raises(online.OnlineNetworkError):
        online.fetch_five_minute("600660.SH", opener=transport)
    assert transport.response.read_limits == [] and transport.response.closed


@pytest.mark.parametrize("symbol", [None, 600660, "600660", "600660.sh", " 600660.SH", "600660.SH ", "600660.BJ", "https://example.org", "600660.SH?x=y"])
def test_invalid_symbol_never_performs_request(symbol):
    transport = Opener()
    with pytest.raises(online.OnlineDataError, match="代码"):
        online.fetch_five_minute(symbol, opener=transport)
    assert not transport.calls


@pytest.mark.parametrize("bad_now", ["not-time", pd.NaT, "2026-99-99"])
def test_invalid_retrieval_clock_never_performs_request(bad_now):
    transport = Opener()
    with pytest.raises(online.OnlineDataError, match="获取时间"):
        online.fetch_five_minute("600660.SH", opener=transport, now=bad_now)
    assert not transport.calls


@pytest.mark.parametrize("error", [TimeoutError("offline"), OSError("offline"), urllib.error.URLError("blocked"), urllib.error.HTTPError(online.ENDPOINT, 429, "limit", {}, None), HTTPException("untrusted raw response")])
def test_network_failure_is_reported_once_without_retry_or_data_fallback(error):
    transport = Opener(error=error)
    with pytest.raises(online.OnlineNetworkError):
        online.fetch_five_minute("600660.SH", opener=transport)
    assert len(transport.calls) == 1


def test_interrupted_http_body_is_friendly_network_error_without_raw_content():
    class Interrupted(Response):
        def read(self, limit):
            raise IncompleteRead(b"untrusted-body-must-not-be-shown", 999)
    transport = Opener(Interrupted())
    with pytest.raises(online.OnlineNetworkError) as caught:
        online.fetch_five_minute("600660.SH", opener=transport)
    assert "untrusted-body" not in str(caught.value)
    assert transport.response.closed and len(transport.calls) == 1


@pytest.mark.parametrize("length", [str(online.MAX_RESPONSE_BYTES+1), "-1", "not-length"])
def test_bad_or_oversize_declared_body_not_read(length):
    transport = Opener(Response(length=length))
    with pytest.raises(online.OnlineDataError):
        online.fetch_five_minute("600660.SH", opener=transport)
    assert transport.response.read_limits == []


def test_read_is_bounded_and_oversize_body_rejected(monkeypatch):
    monkeypatch.setattr(online, "MAX_RESPONSE_BYTES", 100)
    transport = Opener(Response(b"x"*102))
    with pytest.raises(online.OnlineDataError, match="4MiB"):
        online.fetch_five_minute("600660.SH", opener=transport)
    assert transport.response.read_limits == [101]


def test_truncated_body_cannot_be_accepted_as_valid_json():
    transport = Opener(Response(body(), length=str(len(body())+3)))
    with pytest.raises(online.OnlineDataError, match="长度与实际"):
        online.fetch_five_minute("600660.SH", opener=transport)


@pytest.mark.parametrize("raw", [b"", b"<html>Access denied</html>", b"\xffbad", b"{", b"[]", b'{"rc":0,"rc":1,"data":{}}', b'{"rc":0,"data":{"code":"600660","extra":NaN}}', b'{"rc":0,"data":{"code":"600660","extra":Infinity}}'])
def test_bad_json_duplicate_keys_or_nonfinite_json_rejected(raw):
    with pytest.raises(online.OnlineDataError):
        fetch(raw)


@pytest.mark.parametrize("rc", [False, True, "0", None, 1, -1])
def test_unsuccessful_or_wrong_type_api_code_rejected(rc):
    with pytest.raises(online.OnlineDataError, match="成功状态"):
        fetch(body(rc=rc))


@pytest.mark.parametrize("code,market", [("600000", 1), (600660, 1), ("600660", 0), ("600660", False), ("600660", 1.0), ("600660", "SH")])
def test_code_and_market_must_match_request(code, market):
    with pytest.raises(online.OnlineDataError):
        fetch(body(code=code, market=market))


@pytest.mark.parametrize("market", [None, 1, "1"])
def test_market_absent_or_explicitly_matching_supported(market):
    assert len(fetch(body(market=market))) == 1


@pytest.mark.parametrize("klines", [[], None, "wrong", [None], [{}], [row()+",0"], ["2026-11-10 09:35,1,2"]])
def test_no_data_or_bad_row_shape_rejected(klines):
    raw = body() if klines is None else body(klines)
    if klines is None:
        payload = json.loads(raw);payload["data"]["klines"] = None;raw = json.dumps(payload).encode()
    with pytest.raises(online.OnlineDataError):
        fetch(raw)


@pytest.mark.parametrize("stamp", ["2026-2-03 09:35", "2026-02-30 09:35", "2026-11-10 09:35:00", "2026-11-10 09:30", "2026-11-10 11:35", "2026-11-10 13:00", "2026-11-10 15:05", "2026-11-08 09:35"])
def test_noncanonical_calendar_or_session_end_rejected(stamp):
    with pytest.raises(online.OnlineDataError):
        fetch(body([row(stamp)]))


@pytest.mark.parametrize("stamp", ["2026-11-10 09:35", "2026-11-10 09:30"])
def test_duplicate_or_reverse_rows_are_not_silently_fixed(stamp):
    with pytest.raises(online.OnlineDataError):
        fetch(body([row(), row(stamp)]))


@pytest.mark.parametrize("column,value", [(1,"0"),(2,"NaN"),(3,"Infinity"),(4,"10.30"),(3,"10.00"),(5,"-1"),(6,"-1"),(1,""),(2,"-"),(5,"1_000"),(6,"1e999"),(5,"1e308")])
def test_bad_prices_volume_amount_or_numeric_fields_rejected(column, value):
    with pytest.raises(online.OnlineDataError):
        fetch(body([row(**{str(column):value})]))


def test_unavailable_optional_statistics_are_ignored_not_passed_to_engine():
    result = fetch(body([row(**{"7":"-", "8":"--", "9":"", "10":"<script>unused</script>"})]))
    assert list(result.columns) == online.FIELDS
    assert result.close.iloc[0] == 10.2 and result.vol.iloc[0] == pytest.approx(12345.)
    assert "unused" not in result.to_json(date_format="iso") and "unused" not in json.dumps(result.attrs)


@pytest.mark.parametrize("column", range(1, 7))
@pytest.mark.parametrize("value", ["-", "NaN", "Infinity"])
def test_necessary_ohlc_volume_amount_still_require_finite_numbers(column, value):
    with pytest.raises(online.OnlineDataError):
        fetch(body([row(**{str(column):value})]))


def test_server_history_over_request_cap_rejected_without_trimming(monkeypatch):
    monkeypatch.setattr(online, "MAX_RECORDS", 1)
    with pytest.raises(online.OnlineDataError, match="条数"):
        fetch(body([row(), row("2026-11-10 09:40")]))


def test_future_bar_is_preserved_for_causal_engine_filtering_not_relabelled():
    transport = Opener(Response(body([row(), row("2026-11-10 09:40")])))
    result = online.fetch_five_minute("600660.SH", opener=transport, now="2026-11-10 09:36")
    assert result.dt.iloc[-1] == pd.Timestamp("2026-11-10 09:40")
    assert result.attrs["loaded_records"] == 2


def test_zero_turnover_is_not_invented_from_price_times_volume():
    result = fetch(body([row(**{"6":"0"})]))
    assert result.amount.iloc[0] == 0 and result.vol.iloc[0] > 0
