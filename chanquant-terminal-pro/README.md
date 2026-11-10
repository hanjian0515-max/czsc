# ChanQuant Terminal Pro · 个股预警助手

Windows 桌面入口启动本机预警程序，读取通达信本地 `.lc5` 五分钟文件，合成已收盘三十分钟 K 线，监控截图中的 **43 只个股**。支持二买辅助、三买辅助、同日 23 买同步及优化二买确认，提供个股详情、桌面通知、声音和 CSV 日志。

## GitHub 下载

[下载 Windows 预警运行包](downloads/ChanQuant-Alerts.zip?raw=true)（约 64KB，需 Python）。如进入文件预览页，点击 **Download raw file**。下载后完整解压，再按下方步骤启动。

运行包包含预警功能和43只个股研究卡；测试、界面截图与离线66标的看板保留在仓库源码目录中。

## Windows 使用

安装 **64 位 Python 3.12**，完整解压本项目，再双击 `创建桌面快捷方式.cmd`。以后双击桌面的 `ChanQuant-Alerts` 即可；也可直接运行 `启动个股预警.cmd`。首次启动联网安装分析依赖，随后自动打开本机页面。

填写通达信 `vipdoc` 目录，选择股票和规则，保存设置并开始监控。点击“启用桌面通知”和“测试提醒”核对通知通道。**通达信文件需要自行持续更新；首次扫描静默，旧数据与缺口不发实时信号。** 冷启动要逐股回放历史，43 股可能需要几分钟，请观察各股扫描状态。

完整步骤、信号定义及常见问题见 [DESKTOP_ALERTS.md](DESKTOP_ALERTS.md)。界面由本机 Python 服务和浏览器提供；这是带双击启动脚本的源码包，需要 Python，并非独立 EXE。普通用户不需要 Node、Playwright 或测试工具。

## 已提供的研究和可视化

- [66 标的离线交互看板](visualization/dashboard.html)：五阵营、资产筛选、搜索、收藏和主题关系。
- [43 只个股的条件化研究卡](research/stock_analysis_43.html)：业务驱动、30M 检查重点、成立及失效条件；预警页面也可点击个股查看。
- [完整截图名单](research/universe_66_source.csv)：43 股、11 ETF、4 交易所指数和 8 通达信参考指数。监控池只包含个股。
- [研习院原文核验与公式差异](research/SOURCE_AUDIT.md)：尚未取得原文，未声称完整复现课程规则。

没有用户本地行情时不提供当前价格或已确认买点。四类信号采用 CZSC 辅助结构定义，笔中枢参考不自动等同递归走势中枢；工具只读预警，没有券商连接和自动报单，也没有验证正收益。

## 开发验证

```bash
python -m pip install -r requirements-alerts.txt
python desktop_alerts.py --self-check
python desktop_alerts.py --no-browser
python -m pytest tests -q
```

测试另需安装 `pytest`。浏览器验收脚本为 `tests/browser_desktop.cjs`，在 Linux/云开发环境使用 Node Playwright 和 Chromium，可通过 `CQ_TEST_PYTHON` 指定安装了依赖的 Python。测试仅使用本机回环地址；通知通道在页面中记录验证，不能据此声称已测试 Windows 原生弹窗。

原四阶段终端蓝图中的 ZLO 原公式、完整递归分类及 MiniQMT 自动执行仍属规划。当前版本尚未经过 Windows 真机的安装、通达信更新和系统通知全流程验证。
