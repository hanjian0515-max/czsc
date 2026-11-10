"""Create the Windows source/launcher delivery, excluding caches and user state."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXCLUDED={'.venv','.cache','.pytest_cache','__pycache__','app_data','.git','node_modules','data','output'}


def package(output):
    output=Path(output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    count=0
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path in sorted(ROOT.rglob('*')):
            relative=path.relative_to(ROOT)
            if not path.is_file() or path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
                continue
            if path.name=='.env' or path.name.startswith('.env.'):
                continue
            if path.suffix.lower() in {'.pyc','.pyo','.zip','.sqlite','.sqlite3','.db'} or path.resolve()==output:
                continue
            data=path.read_bytes()
            if path.suffix.lower() in {'.cmd','.ps1','.vbs'}:
                data=data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            archive.writestr('ChanQuant-Alerts/'+relative.as_posix(),data)
            count+=1
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('压缩文件校验失败')
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    print(f'{output}\n{count} files; {output.stat().st_size:,} bytes\nSHA256 {digest}')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT.parent/'chanquant-desktop-alerts-windows.zip')
    package(parser.parse_args().output)
