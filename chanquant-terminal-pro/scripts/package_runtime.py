"""Build the small Windows runtime ZIP from an explicit source-file allowlist."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
RUNTIME_FILES=(
    'desktop_alerts.py','alerts/__init__.py','alerts/sources.py',
    'alerts/signal_engine.py','alerts/monitor.py','alerts/web.html',
    'research/universe_66_source.csv','research/stock_analysis_43.json',
    'requirements-alerts.txt','DESKTOP_ALERTS.md','start-alerts.cmd',
    'create-desktop-shortcut.cmd','create-desktop-shortcut.vbs',
    'create-desktop-shortcut.ps1','启动个股预警.cmd','创建桌面快捷方式.cmd',
)
RUNTIME_README='''# ChanQuant 个股预警助手运行包

安装64位Python 3.12，完整解压，再双击“创建桌面快捷方式.cmd”。
也可直接双击“启动个股预警.cmd”。首次启动联网安装分析依赖。

设置通达信vipdoc目录，保持本地5分钟数据更新，保存后开始监控。
支持43股、30M二买辅助、三买辅助、同日23买同步、优化二买确认。
手动启用浏览器通知并点击测试提醒。首次扫描只建立基线，不弹旧信号。

详细使用方法见 DESKTOP_ALERTS.md。此运行包不含截图、开发测试和历史研究看板，预警功能与完整包一致。
需要Python；不是独立EXE，不自动交易。尚未完成Windows真机验证。
'''


def build(output):
    output=Path(output).resolve()
    for relative in RUNTIME_FILES:
        source=ROOT/relative
        if not source.is_file() or source.is_symlink():
            raise ValueError('运行文件缺失或为链接: '+relative)
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for relative in RUNTIME_FILES:
            source=ROOT/relative
            data=source.read_bytes()
            if source.suffix.lower() in {'.cmd','.ps1','.vbs'}:
                data=data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            archive.writestr('ChanQuant-Alerts/'+relative,data)
        archive.writestr('ChanQuant-Alerts/README.md',RUNTIME_README.encode('utf-8'))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('运行包CRC校验失败')
    print(f'{output}\n{output.stat().st_size:,} bytes; {len(RUNTIME_FILES)+1} files')
    print('SHA256 '+hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'downloads/ChanQuant-Alerts.zip')
    build(parser.parse_args().output)
