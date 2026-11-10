"""Read-only, session-aware desktop alert monitoring and durable event storage.

First scans establish a baseline. Only a newly observed *latest completed*
Shanghai-session 30-minute bar can notify; historical catch-up events remain
unnotified. No broker connection, order routing or notification transport is
implemented in this module.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, time
import json
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time as clock
from typing import Callable
from zoneinfo import ZoneInfo


CHINA = ZoneInfo("Asia/Shanghai")
RULES = ("buy2", "buy3", "buy23_day", "buy2_optimized")
DATA_SOURCES = ("tdx_online", "tdx_local", "eastmoney")
END_TIMES = (time(10), time(10, 30), time(11), time(11, 30),
             time(13, 30), time(14), time(14, 30), time(15))


def _local_stamp(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("行情时间不是合法ISO时间戳") from exc
    else:
        raise ValueError("行情时间为空或不是合法时间戳")
    if result.tzinfo is None:
        result = result.replace(tzinfo=CHINA)
    return result.astimezone(CHINA)


def latest_completed_bar(now) -> datetime | None:
    """Return today's last completed standard F30 END, including lunch gaps.

    This deliberately does not invent yesterday's session or a holiday
    calendar. Before 10:00 there is no completed current-date F30 bar.
    Current-date matching in the monitor prevents old-day alerts.
    """
    current = _local_stamp(now)
    eligible = [t for t in END_TIMES if t <= current.time().replace(tzinfo=None)]
    return datetime.combine(current.date(), eligible[-1], CHINA) if eligible else None


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"不支持保存的数据类型: {type(value).__name__}")


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default)


class AlertStore:
    """SQLite app-state store; it never writes into the market-data directory.

    Real signals are unique by symbol/rule/normalized bar END. Explicit
    transport-test events are separate records even at the same test clock.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS config (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                payload TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, rule TEXT NOT NULL, bar_end TEXT NOT NULL,
                is_test INTEGER NOT NULL CHECK(is_test IN (0,1)),
                created_at TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS real_event_identity
                ON events(symbol,rule,bar_end) WHERE is_test=0""")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load_config(self) -> dict | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT payload FROM config WHERE singleton=1").fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError) as exc:
            raise ValueError("已保存配置无法解析，请重新保存配置") from exc
        if not isinstance(value, dict):
            raise ValueError("已保存配置应为对象")
        return value

    def save_config(self, value: dict):
        if not isinstance(value, dict):
            raise ValueError("配置必须为对象")
        payload = _json_dump(value)
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO config(singleton,payload,updated_at) VALUES(1,?,?)
                ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,
                updated_at=excluded.updated_at""", (payload, datetime.now(CHINA).isoformat()))

    def add_event(self, event: dict) -> dict | None:
        if not isinstance(event, dict):
            raise ValueError("预警必须为对象")
        value = deepcopy(event)
        for key in ("symbol", "rule", "message"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"预警缺少有效{key}")
        value["symbol"] = value["symbol"].strip().upper()
        value["rule"] = value["rule"].strip()
        value["bar_end"] = _local_stamp(value.get("bar_end")).isoformat()
        if value.get("origin_time") is not None:
            value["origin_time"] = _local_stamp(value["origin_time"]).isoformat()
        is_test = value.get("is_test", False)
        if not isinstance(is_test, bool):
            raise ValueError("is_test必须为布尔值")
        value["is_test"] = is_test
        value.pop("id", None)
        value.pop("created_at", None)
        created_at = datetime.now(CHINA).isoformat()
        payload = _json_dump(value)
        with self._lock, self._connection() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO events
                (symbol,rule,bar_end,is_test,created_at,payload) VALUES(?,?,?,?,?,?)""",
                (value["symbol"], value["rule"], value["bar_end"], int(is_test), created_at, payload))
            if cursor.rowcount == 0:
                return None
            identifier = cursor.lastrowid
        return {**value, "id": identifier, "created_at": created_at}

    def events_after(self, after: int = 0, limit: int = 200) -> list[dict]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after必须为非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit必须为1到500的整数")
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT id,created_at,payload FROM events WHERE id>? ORDER BY id LIMIT ?",
                              (after, limit)).fetchall()
        return [{**json.loads(payload), "id": identifier, "created_at": created_at}
                for identifier, created_at, payload in rows]

    def max_id(self) -> int:
        with self._lock, self._connection() as db:
            return int(db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])


class LocalMonitor:
    """Monitor explicit local LC5 stock files with silent per-session baselines.

    ``background=False`` is useful for deterministic manual scans and tests;
    production defaults to a daemon worker. Analyzer, file discovery, reader
    and current time can be injected without synthesizing live market data.
    """

    def __init__(self, universe, store: AlertStore, analyzer=None, reader=None,
                 discoverer=None, now: Callable | None = None, *, background=True,
                 online_fetcher=None, tdx_fetcher=None):
        if not isinstance(background, bool):
            raise ValueError("background必须为布尔值")
        if online_fetcher is not None and not callable(online_fetcher):
            raise ValueError("联网行情读取器必须可调用")
        if tdx_fetcher is not None and not callable(tdx_fetcher):
            raise ValueError("通达信行情读取器必须可调用")
        if not isinstance(universe, list) or not universe:
            raise ValueError("股票池不能为空")
        self._universe = {}
        for item in universe:
            if not isinstance(item, dict) or item.get("asset_type", item.get("type")) != "stock":
                raise ValueError("预警池仅接受明确标记stock的个股，不接受ETF或指数")
            symbol, name = item.get("symbol"), item.get("name")
            if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError("股票代码必须含正确市场后缀")
            if re.fullmatch(r"000[0-9]{3}\.SH|399[0-9]{3}\.SZ|899[0-9]{3}\.BJ", symbol):
                raise ValueError("指数代码不能进入个股预警池")
            if symbol in self._universe or not isinstance(name, str) or not name.strip():
                raise ValueError("股票代码重复或名称无效")
            self._universe[symbol] = deepcopy(item)
        self.store = store
        self._analyzer, self._reader, self._discoverer = analyzer, reader, discoverer
        self._online_fetcher = online_fetcher
        self._tdx_fetcher = tdx_fetcher
        self._now = now or (lambda: datetime.now(CHINA))
        self._background = background
        self._lock, self._scan_lock = threading.RLock(), threading.Lock()
        self._wake, self._closed = threading.Event(), threading.Event()
        self._thread = None
        self._running, self._generation = False, 0
        self._baselines = {}
        # One result per stock, never frames or an unbounded history of file
        # versions. Only the production stable-file source pair may use it.
        self._analysis_cache = {}
        self._last_cycle = None
        self._config_warning = None
        self._config = {"data_source": "tdx_online", "tdx_host": "", "tdx_port": 7709,
                        "source_dir": r"D:\tdx\vipdoc", "interval_seconds": 60,
                        "symbols": list(self._universe), "rules": list(RULES)}
        try:
            saved = store.load_config()
            if saved:
                # The first release saved local LC5 settings without a
                # provider field. Preserve that choice during migration.
                if "data_source" not in saved:
                    saved = {**saved, "data_source": "tdx_local"}
                self._config = self._validate_config({**self._config, **saved})
        except (ValueError, TypeError) as exc:
            self._config_warning = str(exc)
        self._stocks = {}
        self._reset_statuses()

    def _clock(self) -> datetime:
        return _local_stamp(self._now())

    def _validate_config(self, config):
        if set(config) != {"data_source", "source_dir", "interval_seconds", "symbols", "rules", "tdx_host", "tdx_port"}:
            raise ValueError("配置含未知字段")
        if config["data_source"] not in DATA_SOURCES:
            raise ValueError("行情来源必须为tdx_online、tdx_local或eastmoney")
        from .tdx_online import validate_endpoint
        tdx_host, tdx_port = validate_endpoint(config["tdx_host"], config["tdx_port"])
        folder = config["source_dir"]
        if not isinstance(folder, str) or "\x00" in folder or len(folder) > 4096:
            raise ValueError("行情目录必须为有效文本路径")
        interval = config["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, int) or not 15 <= interval <= 3600:
            raise ValueError("扫描间隔必须为15到3600秒的整数")
        symbols, rules = config["symbols"], config["rules"]
        if not isinstance(symbols, list) or any(not isinstance(s, str) or s not in self._universe for s in symbols):
            raise ValueError("所选代码必须来自本个股池")
        if not isinstance(rules, list) or any(not isinstance(r, str) or r not in RULES for r in rules):
            raise ValueError("预警规则无效")
        if len(set(symbols)) != len(symbols) or len(set(rules)) != len(rules):
            raise ValueError("代码或规则不能重复")
        return {"data_source": config["data_source"], "tdx_host": tdx_host, "tdx_port": tdx_port,
                "source_dir": folder.strip(), "interval_seconds": interval,
                "symbols": symbols.copy(), "rules": rules.copy()}

    def _reset_statuses(self):
        self._stocks = {s: {"symbol": s, "name": item["name"],
                           "state": "waiting" if s in self._config["symbols"] else "not_selected",
                           "asof": None, "last_scan": None, "error": None,
                           "snapshot": {}, "quality": {}, "source": {}}
                        for s, item in self._universe.items()}

    def configure(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("配置必须为对象")
        with self._lock:
            if self._closed.is_set():
                raise ValueError("预警程序已关闭")
            updated = self._validate_config({**self._config, **value})
            reset = any(updated[k] != self._config[k] for k in ("data_source", "tdx_host", "tdx_port", "source_dir", "symbols", "rules"))
            # Save first: a persistence failure must not silently alter the active config.
            self.store.save_config(updated)
            self._config = updated
            self._config_warning = None
            if reset:
                self._generation += 1
                self._baselines.clear()
                self._analysis_cache.clear()
                self._reset_statuses()
            result = deepcopy(updated)
        self._wake.set()
        return result

    def start(self):
        with self._lock:
            if self._closed.is_set():
                raise ValueError("预警程序已关闭")
            if not self._config["symbols"]:
                raise ValueError("请至少选择一只个股")
            if not self._config["rules"]:
                raise ValueError("请至少选择一种预警")
            if self._config["data_source"] == "tdx_local" and (
                    not self._config["source_dir"] or not Path(self._config["source_dir"]).is_dir()):
                raise ValueError("通达信行情目录不存在，请选择包含LC5文件的本地目录")
            if self._running:
                return
            self._running = True
            self._generation += 1
            self._baselines.clear()
            self._analysis_cache.clear()
            self._reset_statuses()
            if self._background and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._worker, name="ChanQuant-local-alerts", daemon=True)
                self._thread.start()
        self._wake.set()

    def pause(self):
        with self._lock:
            self._running = False
            self._generation += 1
            self._analysis_cache.clear()
            for s in self._config["symbols"]:
                self._stocks[s]["state"] = "paused"
        self._wake.set()

    def close(self):
        self._closed.set()
        self.pause()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def status(self) -> dict:
        with self._lock:
            result = {"running": self._running, "closed": self._closed.is_set(),
                      "config": deepcopy(self._config), "stocks": deepcopy(list(self._stocks.values())),
                      "last_cycle": self._last_cycle, "config_warning": self._config_warning,
                      "time_zone": "Asia/Shanghai", "freshness_policy": "latest completed same-date F30 session END only",
                      "first_scan_policy": "silent baseline; no historical alerts"}
        result["max_alert_id"] = self.store.max_id()
        return result

    def test_alert(self):
        now = self._clock().isoformat()
        return self.store.add_event({"symbol": "TEST", "name": "系统测试", "rule": "TEST", "bar_end": now,
            "origin_time": now, "is_test": True,
            "message": "测试提醒：仅检查提醒通道，不代表真实个股信号", "evidence": {"test_only": True}})

    def _active(self, generation):
        return self._running and not self._closed.is_set() and generation == self._generation

    def _set_error(self, symbol, generation, now, message, state="error", *, clear_data=False):
        with self._lock:
            if self._active(generation):
                self._analysis_cache.pop(symbol, None)
                self._stocks[symbol].update(state=state, error=str(message), last_scan=now.isoformat())
                if clear_data:
                    # A failed fetch cannot display a prior quote as this
                    # scan's usable online snapshot. Durable event logs stay.
                    self._stocks[symbol].update(asof=None, snapshot={}, quality={}, source={},
                                                bars30_count=None, last_five_asof=None)

    def _dependencies(self, data_source="tdx_local"):
        if self._analyzer is None:
            from .signal_engine import analyze_five_minute
            self._analyzer = analyze_five_minute
        if data_source == "eastmoney":
            if self._online_fetcher is None:
                from .online import fetch_five_minute
                self._online_fetcher = fetch_five_minute
        elif data_source == "tdx_online":
            if self._tdx_fetcher is None:
                from .tdx_online import fetch_five_minute
                self._tdx_fetcher = fetch_five_minute
        elif self._reader is None or self._discoverer is None:
            from .sources import discover_lc5, read_lc5
            self._reader = self._reader or read_lc5
            self._discoverer = self._discoverer or discover_lc5

    def _cache_key(self, path, now):
        """Identify a stable production source and the analyzer's F30 cutoff.

        Injected sources are intentionally uncached: a test/custom reader can
        change its result without changing the apparent file. An inaccessible
        or imaginary Path also never establishes a cached-data fallback.
        """
        from .sources import discover_lc5, read_lc5
        if self._reader is not read_lc5 or self._discoverer is not discover_lc5 or not isinstance(path, Path):
            return None
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        cutoff = latest_completed_bar(now)
        return (str(resolved), info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino,
                now.date().isoformat(), cutoff.isoformat() if cutoff else None)

    def _analyze_local(self, symbol, path, generation, config, now):
        from .sources import DataChangingError
        before = self._cache_key(path, now)
        with self._lock:
            cached = self._analysis_cache.get(symbol)
            if before is None or cached is None or cached[0] != before:
                self._analysis_cache.pop(symbol, None)
                cached = None
        if cached is not None:
            if self._cache_key(path, now) != before:
                raise DataChangingError("LC5在复用分析期间发生变化，等待下一轮稳定文件")
            # Re-run freshness, baseline and pause checks even on a cache hit.
            self._accept_analysis(symbol, cached[1], generation, config, self._clock())
            return
        frame = self._reader(path, max_records=12000)
        if before is not None and self._cache_key(path, now) != before:
            raise DataChangingError("LC5读取期间发生变化，等待下一轮稳定文件")
        result = self._analyzer(frame, warmup=200, now=now)
        if before is not None and self._cache_key(path, now) != before:
            raise DataChangingError("LC5分析期间发生变化，等待下一轮稳定文件")
        self._accept_analysis(symbol, result, generation, config, self._clock())
        with self._lock:
            # Invalid/future/gapped analyses are retried, never cached as a
            # substitute for a healthy source. An old generation cannot refill
            # a cache cleared by pause, restart or reconfiguration.
            if before is not None and self._active(generation) and self._stocks[symbol]["state"] in {"ready", "stale"}:
                self._analysis_cache[symbol] = (before, deepcopy(result))

    def scan_once(self) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if not self._active(self._generation):
                    return False
                generation = self._generation
                config = deepcopy(self._config)
            try:
                self._dependencies(config["data_source"])
                if config["data_source"] == "tdx_local":
                    folder = Path(config["source_dir"])
                    if not folder.is_dir():
                        raise ValueError("通达信行情目录不存在或已被移除")
                    files = self._discoverer(folder, config["symbols"])
                    if not isinstance(files, dict):
                        raise ValueError("本地文件发现结果无效")
                else:
                    files = None
            except Exception as exc:
                for symbol in config["symbols"]:
                    self._set_error(symbol, generation, self._clock(), exc,
                                    "data_error" if config["data_source"] != "tdx_local" else "error",
                                    clear_data=config["data_source"] != "tdx_local")
                with self._lock:
                    if self._active(generation):
                        self._last_cycle = self._clock().isoformat()
                return True
            for symbol in config["symbols"]:
                with self._lock:
                    if not self._active(generation):
                        return True
                if config["data_source"] != "tdx_local":
                    try:
                        if config["data_source"] == "tdx_online":
                            frame = self._tdx_fetcher(symbol, now=self._clock(),
                                host=config["tdx_host"], port=config["tdx_port"])
                        else:
                            frame = self._online_fetcher(symbol, now=self._clock())
                        if frame is None or getattr(frame, "empty", True):
                            raise ValueError("联网行情为空，未计算信号")
                        with self._lock:
                            if not self._active(generation):
                                return True
                        result = self._analyzer(frame, warmup=200, now=self._clock())
                        # A slow request/analysis may cross the next session
                        # close. Publish against the actual current cutoff,
                        # never the start time of a potentially long batch.
                        self._accept_analysis(symbol, result, generation, config, self._clock())
                    except Exception as exc:
                        self._set_error(symbol, generation, self._clock(), exc,
                                        "network_error" if isinstance(exc, OSError) else "data_error",
                                        clear_data=True)
                    continue
                if symbol not in files:
                    self._set_error(symbol, generation, self._clock(), "未找到该股LC5文件，请先下载本地5分钟历史", "file_missing")
                    continue
                try:
                    self._analyze_local(symbol, files[symbol], generation, config, self._clock())
                except Exception as exc:
                    from .sources import DataChangingError
                    self._set_error(symbol, generation, self._clock(), exc,
                                    "data_changing" if isinstance(exc, DataChangingError) else "error")
            with self._lock:
                if self._active(generation):
                    self._last_cycle = self._clock().isoformat()
            return True
        finally:
            self._scan_lock.release()

    def _accept_analysis(self, symbol, analysis, generation, config, now):
        if not isinstance(analysis, dict) or analysis.get("symbol", symbol) != symbol:
            raise ValueError("行情或分析结果代码与所选股票不一致")
        snapshot, quality = analysis.get("snapshot") or {}, analysis.get("quality") or {}
        source = analysis.get("source") or {}
        if not isinstance(snapshot, dict) or not isinstance(quality, dict) or not isinstance(source, dict):
            raise ValueError("分析证据格式无效")
        events = analysis.get("events", [])
        if not isinstance(events, list):
            raise ValueError("预警事件格式无效")
        asof_value = analysis.get("asof")
        asof = _local_stamp(asof_value) if asof_value is not None else None
        expected = latest_completed_bar(now)
        fresh = asof is not None and expected is not None and asof == expected
        quality_ok = quality.get("alertable") is True
        ready = analysis.get("status") == "ready"
        state, error = "ready", None
        if asof is not None and asof > now:
            state, error = "future_data", "行情时间晚于当前北京时间，未发预警"
        elif not ready or asof is None:
            state, error = "insufficient_history", "历史或结构预热不足，未发预警"
        elif not quality_ok:
            state, error = "data_gap", "行情未通过完整性检查或缺少明确质量许可，未发预警；请检查5分钟历史"
        elif not fresh:
            state, error = "stale", "等待来源更新到当前交易日最新已完成30分钟K线"
        with self._lock:
            if not self._active(generation):
                return
            previous = self._baselines.get(symbol)
            if asof is not None and asof <= now and previous is not None and asof < previous:
                state, error = "data_regressed", "行情截止时间倒退，未发预警；请检查来源或重新开始监控"
            self._stocks[symbol].update(state=state, error=error, asof=asof.isoformat() if asof else None,
                last_scan=now.isoformat(), snapshot=deepcopy(snapshot), quality=deepcopy(quality), source=deepcopy(source),
                bars30_count=analysis.get("bars30_count"), last_five_asof=analysis.get("last_five_asof"))
            # Do not let a bad future timestamp poison the later valid baseline.
            if asof is not None and asof <= now:
                self._baselines[symbol] = max(previous, asof) if previous is not None else asof
            if previous is None or state != "ready" or asof is None or asof <= previous:
                return
            for event in events:
                if not isinstance(event, dict) or event.get("rule") not in config["rules"]:
                    continue
                if event.get("symbol", symbol) != symbol or event.get("is_test"):
                    continue
                try:
                    event_time = _local_stamp(event.get("bar_end"))
                except ValueError:
                    continue
                # Only the latest newly closed bar may alert. Backfilled intermediate bars do not.
                if event_time != asof or event_time <= previous or event_time.date() != now.date() or event_time > now:
                    continue
                value = {**event, "symbol": symbol, "name": self._universe[symbol]["name"],
                         "bar_end": event_time.isoformat(), "is_test": False}
                self.store.add_event(value)

    def _worker(self):
        while not self._closed.is_set():
            with self._lock:
                running = self._running
            if not running:
                self._wake.wait(60)
                self._wake.clear()
                continue
            self.scan_once()
            with self._lock:
                interval = self._config["interval_seconds"]
            deadline = clock.monotonic() + interval
            while not self._closed.is_set():
                with self._lock:
                    if not self._running:
                        break
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    break
                awakened = self._wake.wait(min(remaining, 60))
                self._wake.clear()
                if awakened:
                    break
