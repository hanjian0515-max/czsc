"""Persistent, headless BTC-USD PAPER session for Linux GitHub Codespaces.

No broker credentials or order endpoint is present. A running process is not
proof of fresh market data, a strategy signal, or a simulated execution.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / ".runtime"
CAPITAL = 10000
INTERVAL = 10


class StopRequested(Exception):
    """Cooperative stop at a market-request boundary, never inside a ledger write."""


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".paper-state-", delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False, allow_nan=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def process_start(pid):
    try:
        # comm can contain spaces and parentheses; split after its closing ')'.
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def is_worker(owner, script=Path(__file__).resolve()):
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return False
    pid, token = owner["pid"], owner.get("token")
    if pid <= 1 or not isinstance(token, str) or not token:
        return False
    if process_start(pid) != owner.get("start_ticks"):
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (os.fsencode(str(script)) in args and b"_worker" in args
                and os.fsencode(token) in args)
    except OSError:
        return False


def session_status(runtime=RUNTIME, now=None):
    state = read_json(runtime / "status.json") or {
        "status": "not_started", "paper_only": True, "symbol": "BTC-USD"}
    owner = read_json(runtime / "worker.json")
    running = is_worker(owner)
    state["running"] = running
    clock = time.time() if now is None else float(now)
    try:
        updated = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
        if updated.tzinfo is None:
            raise ValueError("Status timestamp must contain a timezone")
        state["status_age_seconds"] = round(clock - updated.timestamp(), 3)
    except (KeyError, TypeError, ValueError, AttributeError):
        state["status_age_seconds"] = None
    source = state.get("source_at_utc")
    if isinstance(source, (int, float)) and not isinstance(source, bool) and math.isfinite(source):
        age = round(clock - source, 3)
        state["current_source_age_seconds"] = age
        state["source_fresh"] = 0 <= age <= 90
    else:
        state["current_source_age_seconds"] = None
        state["source_fresh"] = False
    if not running and state.get("status") not in {
            "not_started", "blocked_market_data", "error", "stopped"}:
        state["last_status"] = state.get("status")
        state["status"] = "stopped_unexpectedly"
    return state


def save_status(value, runtime=RUNTIME, **fields):
    result = {"paper_only": True, "symbol": "BTC-USD", "capital": CAPITAL,
              "currency": "USD", "status": value,
              "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    atomic_json(runtime / "status.json", result)
    return result


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def output_state(output):
    """Keep the real runner status and actual ledger, rather than a PID claim."""
    state = dict(output)
    state["runner_status"] = state.pop("status", "error")
    status = state["runner_status"]
    failure = (str(state.get("errors", [])) + " " + str(state.get("message", ""))).lower()
    if status == "error" and any(text in failure for text in (
            "403", "forbidden", "urlopen", "http error", "timed out",
            "connection", "network", "certificate verify", "ssl:")):
        status = "blocked_market_data"
    state["status"] = status
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def worker(token, runtime=RUNTIME):
    runtime.mkdir(parents=True, exist_ok=True)
    if runtime.is_symlink():
        raise ValueError("PAPER runtime must not be a symbolic link")
    lock = (runtime / "account.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("This account already has an active PAPER worker.", file=sys.stderr)
        return 73
    ledger = None
    requested = threading.Event()
    def stop_signal(_signum, _frame):
        # Raising KeyboardInterrupt here could interrupt BEGIN IMMEDIATE before
        # the engine's normal Exception rollback. Finish the current atomic bar.
        requested.set()

    old_term = signal.signal(signal.SIGTERM, stop_signal)
    old_int = signal.signal(signal.SIGINT, stop_signal)

    def stopped():
        fields = {"running": False}
        if ledger is not None:
            fields["ledger"] = ledger.snapshot()
        print_json(save_status("stopped", runtime, **fields))
        return 0

    try:
        owner = {"pid": os.getpid(), "start_ticks": process_start(os.getpid()), "token": token}
        atomic_json(runtime / "worker.json", owner)
        print_json(save_status("starting", runtime, running=True))
        app = runtime / "app"
        if not (app / "quant_runner.py").is_file():
            raise RuntimeError("Application not prepared; run python3 -I cloud-paper/setup.py")
        # Deliberately load only the extracted standalone engine. The worker is
        # invoked with -I, so repository czsc/ and PYTHONPATH cannot shadow pip CZSC.
        sys.path.insert(0, str(app))
        import quant_crypto
        from quant_ledger import Config, QuantLedger
        from quant_runner import refresh, validate_config

        original_request = getattr(quant_crypto, "public_json", None)
        if original_request is not None:
            def market_request(*args, **kwargs):
                if requested.is_set():
                    raise StopRequested
                result = original_request(*args, **kwargs)
                if requested.is_set():
                    raise StopRequested
                return result
            quant_crypto.public_json = market_request

        ledger = QuantLedger(runtime / "account.sqlite",
                             Config(market="crypto", mode="paper", capital=CAPITAL))
        source = runtime / "BTC-USD-seed.json"
        if not source.exists():
            print_json(save_status("fetching_history", runtime, running=True,
                                   ledger=ledger.snapshot(), message="Downloading real public warm-up data"))
            try:
                quant_crypto.save_crypto_history(source, product="BTC-USD", days=7)
            except StopRequested:
                return stopped()
            except Exception as error:
                failure = output_state({"status": "error", "message": str(error),
                                        "errors": [str(error)]})
                print_json(save_status(failure["status"], runtime, running=False,
                                       runner_status=failure["runner_status"], message=str(error),
                                       errors=[str(error)], ledger=ledger.snapshot()))
                return 1
        config = validate_config({"source": str(source), "asset_market": "crypto",
                                  "code": "BTC-USD", "capital": CAPITAL,
                                  "ledger": str(runtime / "account.sqlite"),
                                  "mode": "paper", "online": True, "interval": INTERVAL})
        first = True
        while not requested.is_set():
            output = refresh(config, ledger, first=first)
            if requested.is_set():
                break
            state = output_state(output)
            atomic_json(runtime / "status.json", state)
            print_json(output)
            # A forbidden endpoint needs a configuration/location change, not
            # an endless retry of the same explicit denial.
            if output.get("status") == "error":
                denied = any(text in str(output.get("errors", [])).lower()
                             for text in ("403", "forbidden"))
                if state["status"] != "blocked_market_data" or denied:
                    return 1
            if output.get("status") != "error":
                first = False
            requested.wait(INTERVAL)
        return stopped()
    except StopRequested:
        return stopped()
    except Exception as error:
        fields = {"running": False, "message": str(error), "errors": [str(error)]}
        if ledger is not None:
            fields["ledger"] = ledger.snapshot()
        print_json(save_status("error", runtime, **fields))
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if ledger is not None:
            ledger.close()
        lock.close()


def start(background=False, runtime=RUNTIME):
    if runtime.is_symlink():
        raise ValueError("PAPER runtime must not be a symbolic link")
    runtime.mkdir(parents=True, exist_ok=True)
    owner = read_json(runtime / "worker.json")
    if is_worker(owner):
        print_json(session_status(runtime))
        return 0
    python = HERE / ".venv/bin/python"
    if not python.is_file():
        print_json(save_status("error", runtime, running=False,
                              message="Run python3 -I cloud-paper/setup.py first"))
        return 1
    command = [str(python), "-I", str(Path(__file__).resolve()), "_worker", "--token", uuid.uuid4().hex]
    if not background:
        os.execv(str(python), command)
    with (runtime / "paper.log").open("a", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print_json(session_status(runtime))
            if child.returncode == 73 and is_worker(read_json(runtime / "worker.json")):
                return 0
            return child.returncode
        if is_worker(read_json(runtime / "worker.json")):
            print_json(session_status(runtime))
            return 0
        time.sleep(.05)
    print_json({"paper_only": True, "status": "starting",
                "message": "Worker spawned; inspect status and paper.log for readiness"})
    return 0


def stop(runtime=RUNTIME):
    owner = read_json(runtime / "worker.json")
    if not is_worker(owner):
        print_json(session_status(runtime))
        return 0
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe process control requires Linux pidfd support")
    try:
        descriptor = os.pidfd_open(owner["pid"])
    except ProcessLookupError:
        print_json(session_status(runtime))
        return 0
    try:
        # The pidfd pins this exact process, even if the numeric PID is reused.
        if not is_worker(owner):
            print_json(session_status(runtime))
            return 0
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 25
    while is_worker(owner) and time.monotonic() < deadline:
        time.sleep(.05)
    result = session_status(runtime)
    if is_worker(owner):
        result.update(status="stopping", message="Waiting for the current request/atomic bar to finish")
        print_json(result)
        return 1
    print_json(result)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="CZSC GitHub Codespaces PAPER controls")
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    modes = start_parser.add_mutually_exclusive_group()
    modes.add_argument("--background", action="store_true")
    modes.add_argument("--foreground", action="store_true")
    commands.add_parser("status")
    commands.add_parser("stop")
    worker_parser = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if args.command == "start":
        return start(background=args.background)
    if args.command == "status":
        print_json(session_status())
        return 0
    if args.command == "stop":
        return stop()
    return worker(args.token)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print_json({"paper_only": True, "status": "error", "message": str(error)})
        raise SystemExit(1)
