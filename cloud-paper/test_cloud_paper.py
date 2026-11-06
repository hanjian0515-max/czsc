"""No-network tests of package integrity and the PAPER process lifecycle.

Market callbacks below are explicitly synthetic test fixtures. No production
source file or signal is fabricated, and these tests are not trade evidence.
"""
from __future__ import annotations

import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("cloud_setup", "setup.py")
launcher = load("cloud_launch", "launch.py")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.package = self.root / "package.zip"
        self.target = self.root / "runtime/app"
        self.addCleanup(self.directory.cleanup)

    def archive(self, extra=None):
        with zipfile.ZipFile(self.package, "w") as archive:
            for name in ("quant_crypto.py", "quant_runner.py", "quant_strategy.py",
                         "quant_ledger.py", "export_czsc.py", "tdx_local.py", "requirements-lock.txt"):
                archive.writestr("CZSC-TDX-native/" + name, "# test package fixture\n")
            if extra:
                extra(archive)
        return hashlib.sha256(self.package.read_bytes()).hexdigest()

    def test_hash_failure_never_creates_application(self):
        self.archive()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            setup.extract_package(self.package, self.target)
        self.assertFalse(self.target.exists())

    def test_verified_extraction_preserves_runtime_state_on_repeat(self):
        digest = self.archive()
        setup.extract_package(self.package, self.target, digest)
        ledger = self.target.parent / "account.sqlite"
        ledger.write_bytes(b"existing-account-evidence")
        setup.extract_package(self.package, self.target, digest)
        self.assertEqual(ledger.read_bytes(), b"existing-account-evidence")
        self.assertTrue((self.target / "quant_runner.py").is_file())

    def test_traversal_is_rejected_before_writing(self):
        digest = self.archive(lambda archive: archive.writestr("CZSC-TDX-native/../../escaped", "x"))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            setup.extract_package(self.package, self.target, digest)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse(self.target.exists())

    def test_symlink_archive_entry_is_rejected(self):
        def add(archive):
            info = zipfile.ZipInfo("CZSC-TDX-native/outside")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        digest = self.archive(add)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            setup.extract_package(self.package, self.target, digest)

    def test_existing_modified_program_is_not_silently_reused(self):
        digest = self.archive()
        setup.extract_package(self.package, self.target, digest)
        (self.target / "quant_runner.py").write_text("modified\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            setup.extract_package(self.package, self.target, digest)

    def test_existing_program_symlink_is_not_followed(self):
        digest = self.archive()
        setup.extract_package(self.package, self.target, digest)
        target = self.target / "quant_runner.py"
        target.unlink()
        outside = self.root / "outside"
        outside.write_text("must remain unchanged")
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            setup.extract_package(self.package, self.target, digest)
        self.assertEqual(outside.read_text(), "must remain unchanged")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.script = self.root / "launch.py"
        shutil.copyfile(HERE / "launch.py", self.script)
        self.runtime = self.root / ".runtime"
        self.app = self.runtime / "app"
        self.app.mkdir(parents=True)
        # Real ledger exercises persistence while market/strategy callbacks are
        # intentionally absent. The copy is taken from the verified bundle.
        package = HERE.parent / "installers/czsc-quant-v0.3/CZSC-Quant-v0.3.zip"
        with zipfile.ZipFile(package) as archive:
            (self.app / "quant_ledger.py").write_bytes(archive.read("CZSC-TDX-native/quant_ledger.py"))
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").symlink_to(sys.executable)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.shutdown)

    def command(self, *args):
        return subprocess.run([sys.executable, "-I", str(self.script), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=10)

    def shutdown(self):
        if self.runtime.exists() and not self.runtime.is_symlink():
            self.command("stop")

    def fixture(self, *, denied=False, transaction=False):
        if denied:
            crypto = 'def save_crypto_history(*a, **k):\n    raise OSError("synthetic test fixture: 403 Forbidden")\n'
        else:
            crypto = 'def save_crypto_history(path, **k):\n    path.write_text(\'{"source":"synthetic_test_fixture"}\')\n'
        (self.app / "quant_crypto.py").write_text(crypto)
        transaction_body = (
            '    from pathlib import Path\n'
            '    import time\n'
            '    root = Path(ledger.path).parent\n'
            '    ledger.db.execute("BEGIN IMMEDIATE")\n'
            '    ledger.db.execute("INSERT INTO events(details) VALUES (?)", '
            '(\'{"source":"synthetic_test_fixture","event":"transaction_stop_test"}\',))\n'
            '    (root / "transaction-open").touch()\n'
            '    time.sleep(.6)\n'
            '    ledger.db.execute("COMMIT")\n'
            '    (root / "transaction-committed").touch()\n') if transaction else ''
        (self.app / "quant_runner.py").write_text(
            'def validate_config(value):\n    return value\n'
            'def refresh(config, ledger, first=False):\n' + transaction_body +
            '    return {"paper_only":True,"symbol":"BTC-USD","status":"waiting_bar",'
            '"source_kind":"synthetic_test_fixture","source_age":12,'
            '"signal":None,"errors":[],"ledger":ledger.snapshot(),"executions":[]}\n')

    def wait_state(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = launcher.read_json(self.runtime / "status.json")
            if state and state.get("status") == expected:
                return state
            time.sleep(.03)
        self.fail("Expected state " + expected + "; actual " + str(state))

    def test_market_denial_keeps_real_zero_trade_account_and_exits(self):
        self.fixture(denied=True)
        result = self.command("start", "--foreground")
        self.assertEqual(result.returncode, 1, result.stdout)
        state = self.wait_state("blocked_market_data")
        self.assertIn("403", state["message"])
        self.assertEqual(state["ledger"]["cash"], 10000)
        self.assertEqual(state["ledger"]["execution_count"], 0)
        self.assertFalse((self.runtime / "BTC-USD-seed.json").exists())
        status = json.loads(self.command("status").stdout)
        self.assertFalse(status["running"])
        self.assertEqual(status["status"], "blocked_market_data")

    def test_invalid_download_data_is_error_and_does_not_claim_network_denial(self):
        self.fixture()
        (self.app / "quant_crypto.py").write_text(
            'def save_crypto_history(*a, **k):\n'
            '    raise ValueError("Invalid OHLCV: synthetic validation-error fixture")\n')
        result = self.command("start", "--foreground")
        self.assertEqual(result.returncode, 1, result.stdout)
        state = json.loads(self.command("status").stdout)
        self.assertEqual(state["status"], "error")
        self.assertFalse(state["running"])
        self.assertEqual(state["ledger"]["cash"], 10000)
        self.assertFalse((self.runtime / "BTC-USD-seed.json").exists())

    def check_transaction_stop(self, signum):
        self.fixture(transaction=True)
        self.assertEqual(self.command("start", "--background").returncode, 0)
        deadline = time.monotonic() + 5
        while not (self.runtime / "transaction-open").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.runtime / "transaction-open").exists())
        if signum == signal.SIGTERM:
            stopped = self.command("stop")
            self.assertEqual(stopped.returncode, 0, stopped.stdout)
        else:
            owner = launcher.read_json(self.runtime / "worker.json")
            os.kill(owner["pid"], signum)
        state = self.wait_state("stopped")
        self.assertTrue((self.runtime / "transaction-committed").exists())
        self.assertEqual(state["ledger"]["cash"], 10000)
        self.assertEqual(state["ledger"]["execution_count"], 0)
        self.assertNotIn("errors", state)

    def test_sigterm_during_atomic_bar_finishes_transaction_before_snapshot(self):
        self.check_transaction_stop(signal.SIGTERM)

    def test_sigint_during_atomic_bar_finishes_transaction_before_snapshot(self):
        self.check_transaction_stop(signal.SIGINT)

    def test_background_duplicate_start_stop_and_restore(self):
        self.fixture()
        self.assertEqual(self.command("start", "--background").returncode, 0)
        state = self.wait_state("waiting_bar")
        original = launcher.read_json(self.runtime / "worker.json")
        self.assertEqual(state["ledger"]["execution_count"], 0)
        status = json.loads(self.command("status").stdout)
        self.assertTrue(status["running"])
        self.assertEqual(status["source_kind"], "synthetic_test_fixture")
        self.assertEqual(self.command("start", "--background").returncode, 0)
        self.assertEqual(launcher.read_json(self.runtime / "worker.json"), original)
        stopped = self.command("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertFalse(json.loads(stopped.stdout)["running"])
        self.assertEqual(self.command("start", "--background").returncode, 0)
        restored = self.wait_state("waiting_bar")
        self.assertEqual(restored["ledger"]["cash"], state["ledger"]["cash"])
        self.assertEqual(restored["ledger"]["execution_count"], 0)

    def test_account_lock_rejects_competing_worker_without_overwriting_owner(self):
        self.fixture()
        owner = {"test_fixture": "existing-owner"}
        launcher.atomic_json(self.runtime / "worker.json", owner)
        with (self.runtime / "account.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            result = self.command("_worker", "--token", "competitor")
        self.assertEqual(result.returncode, 73)
        self.assertEqual(launcher.read_json(self.runtime / "worker.json"), owner)
        self.assertFalse((self.runtime / "account.sqlite").exists())

    def test_simultaneous_background_starts_share_one_account_worker(self):
        self.fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.command, "start", "--background") for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual([r.returncode for r in results], [0, 0], [r.stdout for r in results])
        state = self.wait_state("waiting_bar")
        self.assertEqual(state["ledger"]["execution_count"], 0)
        self.assertTrue(json.loads(self.command("status").stdout)["running"])

    def test_forged_owner_never_signals_unrelated_process(self):
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            launcher.atomic_json(self.runtime / "worker.json", {
                "pid": victim.pid, "start_ticks": launcher.process_start(victim.pid),
                "token": "not-a-paper-worker"})
            result = self.command("stop")
            self.assertEqual(result.returncode, 0)
            self.assertIsNone(victim.poll())
        finally:
            victim.terminate()
            victim.wait(timeout=5)

    def test_dead_worker_does_not_leave_status_claiming_live(self):
        launcher.atomic_json(self.runtime / "worker.json", {
            "pid": 99999999, "start_ticks": "1", "token": "dead-test-worker"})
        launcher.atomic_json(self.runtime / "status.json", {
            "paper_only": True, "status": "waiting_bar", "source_age": 4})
        state = json.loads(self.command("status").stdout)
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "stopped_unexpectedly")
        self.assertEqual(state["last_status"], "waiting_bar")

    def test_queried_freshness_ages_even_when_last_observation_did_not_change(self):
        launcher.atomic_json(self.runtime / "status.json", {
            "paper_only": True, "status": "waiting_bar", "source_age": 10,
            "source_at_utc": 2000000000,
            "updated_at": "2033-05-18T03:33:30+00:00"})
        current = launcher.session_status(self.runtime, now=2000000200)
        self.assertEqual(current["source_age"], 10)
        self.assertEqual(current["current_source_age_seconds"], 200)
        self.assertFalse(current["source_fresh"])
        self.assertEqual(current["status_age_seconds"], 190)
        future = launcher.session_status(self.runtime, now=1999999999)
        self.assertFalse(future["source_fresh"])
        self.assertEqual(future["current_source_age_seconds"], -1)

    def test_non_market_runner_error_is_not_misreported_as_network_failure(self):
        output = {"status": "error", "errors": ["database configuration differs"],
                  "message": "simulator paused"}
        state = launcher.output_state(output)
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["runner_status"], "error")
        network = launcher.output_state({"status": "error", "errors": ["HTTP Error 403: Forbidden"]})
        self.assertEqual(network["status"], "blocked_market_data")

    def test_local_runner_error_exits_instead_of_retrying_same_corrupt_state(self):
        self.fixture()
        (self.app / "quant_runner.py").write_text(
            'def validate_config(value):\n    return value\n'
            'def refresh(config, ledger, first=False):\n'
            '    return {"status":"error","errors":["database configuration differs"],'
            '"message":"synthetic local-error fixture","ledger":ledger.snapshot()}\n')
        result = self.command("start", "--foreground")
        self.assertEqual(result.returncode, 1, result.stdout)
        state = json.loads(self.command("status").stdout)
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["ledger"]["execution_count"], 0)

    def test_symbolic_link_runtime_is_rejected_before_logs(self):
        shutil.rmtree(self.runtime)
        outside = self.root / "outside"
        outside.mkdir()
        self.runtime.symlink_to(outside, target_is_directory=True)
        result = self.command("start", "--background")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
