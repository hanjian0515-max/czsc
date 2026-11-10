"""Build the small Windows runtime ZIP from an explicit source-file allowlist."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
RUNTIME_FILES=(
    'desktop_alerts.py','alerts/__init__.py','alerts/sources.py',
    'alerts/signal_engine.py','alerts/monitor.py','alerts/online.py','alerts/web.html',
    'research/universe_66_source.csv','research/stock_analysis_43.json',
    'requirements-alerts.txt','DESKTOP_ALERTS.md','start-alerts.cmd',
    'create-desktop-shortcut.cmd','create-desktop-shortcut.vbs',
    'create-desktop-shortcut.ps1','启动个股预警.cmd','创建桌面快捷方式.cmd',
)
RUNTIME_README='''# ChanQuant 个股预警助手运行包 1.1

安装64位Python 3.12，完整解压，再双击“创建桌面快捷方式.cmd”。
也可直接双击“启动个股预警.cmd”。首次启动联网安装分析依赖。

升级前先在旧页面点击“退出程序”，完整解压覆盖原程序目录，再启动。
运行包不含.venv/app_data，覆盖时保留这些本地环境和设置。
行情来源选择“联网5分钟行情（东方财富）”，点击“检查行情连接”。
确认来源、数据截止和历史数量后保存设置并开始监控，无需本地LC5文件。
也可继续选通达信本地模式并设置vipdoc目录，文件须持续更新。
支持43股、30M二买辅助、三买辅助、同日23买同步、优化二买确认。
手动启用浏览器通知并点击“测试通知通道”。首次扫描只建立基线，不弹旧信号。

详细使用方法见 DESKTOP_ALERTS.md。此运行包不含截图、开发测试和历史研究看板，预警功能与完整包一致。
接口失败、数据过旧、缺口或历史不足时不发信号，不用模拟数据补位。
公开接口可能延迟或限流；云端公网取数及Windows真机全流程尚未验证。
需要Python；不是独立EXE，不自动交易。测试通知通道不代表真实买点。
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
