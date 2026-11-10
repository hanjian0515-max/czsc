"""Offline integration checks against a real loopback HTTP server and SQLite.

Synthetic notification records test transport/storage only, never stock returns.
The production monitor is used with its background scanner disabled, so no
market data, external network access, browser or order service is involved.
"""
from __future__ import annotations

import codecs
import csv
from datetime import datetime, timedelta
import http.client
import io
import json
import threading
from types import SimpleNamespace

import pytest

import desktop_alerts
from alerts.monitor import AlertStore, CHINA, LocalMonitor


@pytest.fixture
def app(tmp_path):
    universe = desktop_alerts.load_universe()
    store = AlertStore(tmp_path / "state" / "alerts.sqlite3")
    monitor = LocalMonitor(universe, store, background=False,
                           now=lambda: datetime(2026, 11, 9, 10, 5, tzinfo=CHINA))
    source = tmp_path / "vipdoc"
    source.mkdir()
    server = desktop_alerts.AlertHTTPServer(("127.0.0.1", 0), monitor, store, universe,
                                           token="synthetic-http-session-token")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(path, method="GET", data=None, raw=None, token=True, headers=None):
        supplied = {"X-CQ-Token": server.token} if token else {}
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            supplied["Content-Type"] = "application/json"
        supplied.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=supplied)
            response = connection.getresponse()
            body = response.read()
            result = SimpleNamespace(status=response.status, headers=dict(response.getheaders()), body=body)
            result.json = lambda: json.loads(body)
            return result
        finally:
            connection.close()

    value = SimpleNamespace(server=server, thread=thread, monitor=monitor, store=store,
                            universe=universe, source=source, port=port, request=request)
    try:
        yield value
    finally:
        server.shutdown()
        server.server_close()
        monitor.close()
        thread.join(3)
        assert not thread.is_alive(), "loopback HTTP test server did not shut down"


def add_events(app, count, *, message="合成提醒，仅用于传输测试"):
    start = datetime(2026, 1, 1, tzinfo=CHINA)
    rows = []
    for number in range(count):
        stamp = (start + timedelta(minutes=number)).isoformat()
        rows.append(app.store.add_event({"symbol": "600660.SH", "rule": "buy2", "bar_end": stamp,
            "origin_time": stamp, "message": message, "evidence": {"fixture": True, "sequence": number}}))
    return rows


def test_public_health_and_stock_pool_excludes_all_reference_instruments(app):
    health = app.request("/api/health", token=False)
    assert health.status == 200
    assert health.json() == {"app": desktop_alerts.APP_ID, "version": "1.0", "read_only": True}
    response = app.request("/api/stocks")
    assert response.status == 200
    stocks = response.json()["stocks"]
    symbols = {row["symbol"] for row in stocks}
    assert len(stocks) == len(symbols) == 43
    assert all(row["asset_type"] == "stock" for row in stocks)
    assert {"600660.SH", "300308.SZ", "601318.SH"} <= symbols
    assert not {"000001.SH", "399001.SZ", "399300.SZ", "000016.SH", "880491.TDX",
                "512480.SH", "515880.SH", "159949.SZ"} & symbols
    assert all(isinstance(row["themes"], list) for row in stocks)
    assert set(response.json()["rules"]) == {"buy2", "buy3", "buy23_day", "buy2_optimized"}


@pytest.mark.parametrize("path,method", [("/api/stocks", "GET"), ("/api/status", "GET"),
    ("/api/alerts", "GET"), ("/api/export-alerts.csv", "GET"), ("/api/directories", "GET"),
    ("/api/start", "POST"), ("/api/config", "POST"), ("/api/test-alert", "POST"),
    ("/api/shutdown", "POST")])
def test_all_private_routes_require_current_session_token(app, path, method):
    response = app.request(path, method, raw=b"{}" if method == "POST" else None, token=False)
    assert response.status == 403 and response.json()["ok"] is False
    assert not app.monitor.status()["running"] and app.store.max_id() == 0


def test_wrong_token_and_query_string_token_are_rejected(app):
    assert app.request("/api/status", headers={"X-CQ-Token": "old-session"}).status == 403
    assert app.request("/api/status?token=" + app.server.token, token=False).status == 403
    assert app.request("/api/status").status == 200


@pytest.mark.parametrize("path,method", [("/api/health", "GET"), ("/", "GET"),
    ("/api/status", "GET"), ("/api/start", "POST")])
def test_host_rebinding_is_rejected_even_for_public_routes(app, path, method):
    response = app.request(path, method, raw=b"{}" if method == "POST" else None,
                           headers={"Host": "external.invalid:" + str(app.port)})
    assert response.status == 403
    assert "本机" in response.json()["error"]


@pytest.mark.parametrize("origin", ["https://external.invalid", "null", "http://127.0.0.1:1",
                                    "https://localhost:17343"])
def test_cross_origin_control_and_private_reads_are_rejected(app, origin):
    for path, method in (("/api/status", "GET"), ("/api/test-alert", "POST")):
        response = app.request(path, method, raw=b"{}" if method == "POST" else None,
                               headers={"Origin": origin})
        assert response.status == 403 and "跨域" in response.json()["error"]
    assert app.store.max_id() == 0


def test_same_origin_and_localhost_hosts_are_supported(app):
    response = app.request("/api/status", headers={"Host": f"localhost:{app.port}",
                          "Origin": f"http://localhost:{app.port}"})
    assert response.status == 200
    assert len(response.json()["stocks"]) == 43
    response = app.request("/api/status", headers={"Origin": f"http://127.0.0.1:{app.port}"})
    assert response.status == 200


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_html_replaces_exactly_one_token_and_sets_browser_security_headers(app, path):
    template = (desktop_alerts.ROOT / "alerts" / "web.html").read_text(encoding="utf-8")
    assert template.count("__CQ_TOKEN__") == 1
    response = app.request(path, token=False)
    html = response.body.decode("utf-8")
    assert response.status == 200 and response.headers["Content-Type"].startswith("text/html")
    assert "__CQ_TOKEN__" not in html and html.count(app.server.token) == 1
    assert "const TOKEN=" + json.dumps(app.server.token) + ";" in html
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "connect-src 'self'", "object-src 'none'",
                      "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in csp
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("config", [{"interval_seconds": 1}, {"symbols": ["000001.SH"]},
    {"rules": ["buy1"]}, {"sound": True}, {"source_dir": 123}])
def test_invalid_config_returns_400_without_changing_monitor_or_saved_config(app, config):
    before = app.monitor.status()["config"]
    response = app.request("/api/config", "POST", data=config)
    assert response.status == 400 and response.json()["ok"] is False
    assert app.monitor.status()["config"] == before
    assert app.store.load_config() is None


@pytest.mark.parametrize("body", [b"{", b"not-json", b"[]", b"null", b"true", b'"string"', b"\xff"])
def test_malformed_or_nonobject_json_returns_400(app, body):
    response = app.request("/api/config", "POST", raw=body)
    assert response.status == 400 and response.json()["ok"] is False
    assert app.store.load_config() is None


@pytest.mark.parametrize("length", ["1048577", "-1", "not-an-integer"])
def test_oversized_or_invalid_content_length_is_rejected_before_reading_body(app, length):
    response = app.request("/api/config", "POST", raw=b"{}", headers={"Content-Length": length})
    assert response.status == 400 and response.json()["ok"] is False
    assert app.store.load_config() is None


def test_configuration_persists_and_start_stop_control_real_monitor(app):
    initial = app.request("/api/status").json()
    assert initial["running"] is False
    assert app.request("/api/start", "POST", data={}).status == 400
    config = {"source_dir": str(app.source), "interval_seconds": 30,
              "symbols": ["600660.SH", "300308.SZ"], "rules": ["buy2", "buy23_day"]}
    saved = app.request("/api/config", "POST", data=config)
    assert saved.status == 200 and saved.json()["config"] == config
    assert AlertStore(app.store.path).load_config() == config
    assert app.request("/api/start", "POST", data={}).status == 200
    assert app.monitor.status()["running"] is True
    status = app.request("/api/status").json()
    assert status["running"] is True and status["config"] == config
    assert {s["symbol"] for s in status["stocks"] if s["state"] == "waiting"} == set(config["symbols"])
    assert app.monitor._thread is None  # No synthetic scanner or source substitution.
    assert app.request("/api/stop", "POST", data={}).status == 200
    assert app.monitor.status()["running"] is False
    assert not app.request("/api/alerts").json()["alerts"]


def test_test_notification_is_persisted_and_cannot_be_mistaken_for_stock_signal(app):
    response = app.request("/api/test-alert", "POST", data={})
    assert response.status == 200 and response.json()["ok"] is True
    row = response.json()["alert"]
    assert row["is_test"] is True and row["symbol"] == row["rule"] == "TEST"
    assert "不代表真实" in row["message"] and row["evidence"]["test_only"] is True
    assert app.request("/api/alerts").json() == {"alerts": [row], "last_id": row["id"]}
    assert app.request("/api/status").json()["max_alert_id"] == row["id"]
    again = app.request("/api/test-alert", "POST", data={}).json()["alert"]
    assert again["id"] > row["id"] and again["is_test"] is True


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_csv_export_has_utf8_bom_and_quotes_spreadsheet_formulas(app, prefix):
    message = prefix + 'SUM(1,2),中文"内容"\n第二行'
    row = add_events(app, 1, message=message)[0]
    response = app.request("/api/export-alerts.csv")
    assert response.status == 200 and response.body.startswith(codecs.BOM_UTF8)
    assert response.headers["Content-Type"] == "text/csv; charset=utf-8"
    assert response.headers["Content-Disposition"] == 'attachment; filename="chanquant-alerts.csv"'
    exported = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"), newline="")))
    assert len(exported) == 1 and exported[0]["message"] == "'" + message
    assert exported[0]["symbol"] == row["symbol"]
    assert json.loads(exported[0]["evidence"]) == row["evidence"]


def test_csv_export_streams_all_store_pages(app):
    rows = add_events(app, 505)
    response = app.request("/api/export-alerts.csv")
    exported = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert [int(row["id"]) for row in exported] == [row["id"] for row in rows]


def test_default_alert_pagination_does_not_skip_records_after_full_200_row_page(app):
    rows = add_events(app, 425)
    received, cursor = [], 0
    for expected_size in (200, 200, 25, 0):
        response = app.request(f"/api/alerts?after={cursor}")
        assert response.status == 200
        page = response.json()
        assert len(page["alerts"]) == expected_size
        received.extend(page["alerts"])
        expected_cursor = max([cursor] + [row["id"] for row in page["alerts"]])
        assert page["last_id"] == expected_cursor
        cursor = page["last_id"]
    assert [row["id"] for row in received] == [row["id"] for row in rows]


def test_explicit_limit_returns_only_that_many_and_last_id_only_advances_returned_rows(app):
    rows = add_events(app, 9)
    response = app.request("/api/alerts?after=0&limit=3").json()
    assert response["alerts"] == rows[:3] and response["last_id"] == rows[2]["id"]
    next_page = app.request(f"/api/alerts?after={response['last_id']}&limit=3").json()
    assert next_page["alerts"] == rows[3:6]
    assert app.request("/api/alerts?after=9999&limit=3").json() == {"alerts": [], "last_id": 9999}


@pytest.mark.parametrize("query", ["after=not-number", "limit=not-number"])
def test_bad_pagination_parameters_return_400(app, query):
    response = app.request("/api/alerts?" + query)
    assert response.status == 400 and response.json()["ok"] is False


def test_recent_initial_load_returns_only_recent_history_and_safe_cursor(app):
    rows = add_events(app, 425)
    history = app.request("/api/alerts?recent=1&limit=200").json()
    assert history["alerts"] == rows[-200:]
    assert history["last_id"] == rows[-1]["id"]
    assert app.request(f"/api/alerts?after={history['last_id']}").json()["alerts"] == []
    new = app.monitor.test_alert()
    update = app.request(f"/api/alerts?after={history['last_id']}").json()
    assert update == {"alerts": [new], "last_id": new["id"]}
    small = app.request("/api/alerts?recent=1&limit=3").json()
    assert [row["id"] for row in small["alerts"]] == [rows[-2]["id"], rows[-1]["id"], new["id"]]


def test_empty_recent_load_has_zero_cursor_and_does_not_drop_first_event(app):
    assert app.request("/api/alerts?recent=1&limit=200").json() == {"alerts": [], "last_id": 0}
    row = app.monitor.test_alert()
    assert app.request("/api/alerts?after=0").json() == {"alerts": [row], "last_id": row["id"]}


def test_polling_insert_after_read_is_delivered_next_poll_instead_of_skipped(app, monkeypatch):
    rows = add_events(app, 3)
    original = app.store.events_after
    inserted = []

    def read_then_insert(after=0, limit=200):
        result = original(after, limit)
        if not inserted:
            inserted.append(app.monitor.test_alert())
        return result

    monkeypatch.setattr(app.store, "events_after", read_then_insert)
    first = app.request("/api/alerts?after=0").json()
    assert first == {"alerts": rows, "last_id": rows[-1]["id"]}
    second = app.request(f"/api/alerts?after={first['last_id']}").json()
    assert second == {"alerts": inserted, "last_id": inserted[0]["id"]}


def test_recent_watermark_is_taken_before_history_read_to_avoid_losing_concurrent_insert(app, monkeypatch):
    rows = add_events(app, 3)
    original = app.store.events_after
    inserted = []

    def read_then_insert(after=0, limit=200):
        result = original(after, limit)
        if not inserted:
            inserted.append(app.monitor.test_alert())
        return result

    monkeypatch.setattr(app.store, "events_after", read_then_insert)
    history = app.request("/api/alerts?recent=1&limit=2").json()
    assert history == {"alerts": rows[-2:], "last_id": rows[-1]["id"]}
    update = app.request(f"/api/alerts?after={history['last_id']}").json()
    assert update == {"alerts": inserted, "last_id": inserted[0]["id"]}


def test_unknown_authenticated_routes_return_404(app):
    for path, method in (("/api/not-found", "GET"), ("/api/not-found", "POST")):
        response = app.request(path, method, raw=b"{}" if method == "POST" else None)
        assert response.status == 404 and response.json()["ok"] is False


def test_shutdown_pauses_monitor_and_stops_real_http_server(app):
    app.monitor.configure({"source_dir": str(app.source)})
    app.monitor.start()
    assert app.monitor.status()["running"]
    response = app.request("/api/shutdown", "POST", data={})
    assert response.status == 200 and response.json() == {"ok": True}
    app.thread.join(3)
    assert not app.thread.is_alive() and app.monitor.status()["running"] is False
