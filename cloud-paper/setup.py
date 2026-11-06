"""Prepare the standalone CZSC PAPER application in GitHub Codespaces."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
PACKAGE_SHA256 = "6bff8927ec8d01aa82c3273d09185eb792c384877573c8fcd51b0feacc86f858"
PACKAGE_ROOT = "CZSC-TDX-native"


def package_members(archive):
    """Reject paths/symlinks that could escape this application directory."""
    entries, seen, size = [], set(), 0
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        if (item.filename.startswith("/") or "\\" in item.filename
                or ".." in path.parts or not path.parts
                or path.parts[0] != PACKAGE_ROOT
                or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError("Unsafe package entry: " + item.filename)
        relative = path.parts[1:]
        if not relative:
            if not item.is_dir():
                raise ValueError("Invalid package root")
            continue
        key = "/".join(relative)
        if key in seen:
            raise ValueError("Duplicate package entry: " + key)
        seen.add(key)
        size += item.file_size
        if size > 150_000_000:
            raise ValueError("Package expanded size is too large")
        entries.append((item, relative))
    required = {"quant_crypto.py", "quant_runner.py", "quant_strategy.py",
                "quant_ledger.py", "export_czsc.py", "tdx_local.py", "requirements-lock.txt"}
    if not required.issubset(seen):
        raise ValueError("Package lacks required PAPER engine files")
    return entries


def extract_package(package: Path, destination: Path, expected_hash=PACKAGE_SHA256):
    """Verify before extracting; repeated setup validates existing program files."""
    if hashlib.sha256(package.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("CZSC package SHA256 mismatch; no code was extracted")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("Application destination must not be a symbolic link")
    with zipfile.ZipFile(package) as archive:
        entries = package_members(archive)
        if destination.exists():
            for info, parts in entries:
                target = destination.joinpath(*parts)
                if any(p.is_symlink() for p in [target, *target.parents]
                       if p == destination or destination in p.parents):
                    raise ValueError("Installed application contains a symbolic link")
                if info.is_dir():
                    if not target.is_dir():
                        raise ValueError("Installed application directory is missing")
                elif not target.is_file() or target.read_bytes() != archive.read(info):
                    raise ValueError("Installed program differs from the verified package: " + str(target))
            return destination
        temporary = Path(tempfile.mkdtemp(prefix="app-extract-", dir=destination.parent))
        try:
            for info, parts in entries:
                target = temporary.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        output.write(archive.read(info))
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return destination


def main():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("The tested lock requires Python 3.12")
    package = HERE.parent / "installers/czsc-quant-v0.3/CZSC-Quant-v0.3.zip"
    runtime = HERE / ".runtime"
    if runtime.is_symlink():
        raise ValueError("The PAPER runtime directory must not be a symbolic link")
    app = extract_package(package, runtime / "app")
    venv = HERE / ".venv"
    if venv.is_symlink():
        raise ValueError("The PAPER virtual environment must not be a symbolic link")
    python = venv / "bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-I", "-m", "venv", str(venv)], check=True)
    subprocess.run([str(python), "-I", "-m", "pip", "install", "-r",
                    str(app / "requirements-lock.txt")], check=True)
    subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True)
    subprocess.run([str(python), "-I", "-c",
                    "import czsc; assert czsc.__version__ == '1.0.2', czsc.__version__"], check=True)
    print(json.dumps({"status": "setup_complete", "paper_only": True,
                      "app": str(app), "python": str(python)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print("PAPER setup failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
