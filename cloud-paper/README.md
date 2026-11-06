# GitHub Codespaces 自动模拟交易

[打开 GitHub Codespaces 创建页面](https://codespaces.new/hanjian0515-max/czsc/tree/codex/quant-installer-v0.3)

本目录运行独立的 CZSC 1.0.2 模拟应用，交易标的是 **BTC-USD 现货做多**，初始模拟资金 **10,000 USD**。行情来自 Coinbase 公开只读接口，不需要交易账户或交易密钥。应用没有真实账户下单接口。

## 开始运行

1. 登录 GitHub，打开上方链接，确认仓库为 `hanjian0515-max/czsc`、分支为 `codex/quant-installer-v0.3`。
2. 点击 **Create codespace**。如果 GitHub 提示使用额度或计费要求，按你的账户情况处理。
3. 等待容器准备完成。配置会自动安装隔离的 Python 依赖、下载真实历史行情暖机，再启动自动模拟。
4. 在 Codespaces 的终端执行以下命令，检查实际运行状态：

   ```bash
   python3 -I cloud-paper/launch.py status
   ```

也可以从仓库页面先选择上述分支，再点击 **Code → Codespaces → Create codespace**。GitHub Codespaces 是本应用的运行环境，无需修改此前 Codex 云环境的网络草稿。

已存在的 Codespace 如果早于这份配置创建，建议在该分支新建一个，或在其中重建容器后再检查状态。

## 编辑器提示找不到 Python 解释器

若提示无法解析 `.venv/Scripts/python.exe`，这是旧的 Windows 解释器设置。Codespaces 使用 Linux；模拟启动器独立使用 `cloud-paper/.venv/bin/python`，此弹窗本身不代表模拟进程失败。

仓库已把默认解释器改为 `${workspaceFolder}/cloud-paper/.venv`，由编辑器按系统识别。已有实例可能缓存旧选择，需手动切换：

1. 在网页编辑器按 **Ctrl+Shift+P**，输入并选择 **Python: Select Interpreter**（Python：选择解释器）。
2. 选择 **Enter interpreter path...**（输入解释器路径），填入 `cloud-paper/.venv/bin/python` 的完整路径。默认仓库目录下为 `/workspaces/czsc/cloud-paper/.venv/bin/python`；若工作区目录不同，以终端 `pwd` 为准。
3. 如果解释器文件不存在，在仓库根目录终端执行 `python3 -I cloud-paper/setup.py`，等 `setup_complete` 后再选择。
4. 在终端运行 `python3 -I cloud-paper/launch.py status`，核对真实程序状态、行情和账本。更改编辑器解释器不会自动补发交易信号。

## 查看状态和控制运行

在仓库根目录的 Codespaces 终端使用：

```bash
# 查看模拟进程、最近状态及账户信息
python3 -I cloud-paper/launch.py status

# 查看最近的行情处理结果和报错
tail -n 30 cloud-paper/.runtime/paper.log

# 停止模拟进程，保留账户与行情文件
python3 -I cloud-paper/launch.py stop

# 使用原账户恢复后台运行
python3 -I cloud-paper/launch.py start --background
```

需要在终端直接观察时，先停止后台进程，再运行：

```bash
python3 -I cloud-paper/launch.py start --foreground
```

用 `Ctrl+C` 停止前台运行。停止请求会等待当前网络请求或账户原子处理完成；若 `stop` 暂时返回 `stopping`，继续用 `status` 确认 `running: false` 后再操作账户或缓存文件。停止、重启后继续使用原 SQLite 模拟账本；不要删除账户文件来重置运行结果。切换资金、市场或回测模式应另建独立账户。

进程存在只能说明程序已启动。判断是否在处理真实行情，应查看 `running`、`current_source_age_seconds`、`source_fresh`、`status_age_seconds`、`runner_status`、错误和账本成交记录。`source_age` 是上次轮询时计算的值；`current_source_age_seconds` 是查询状态时计算的行情年龄，`source_fresh` 用来核验当前观测行情是否仍在时效内。`fetching_history` 表示正在下载暖机数据，`blocked_market_data` 表示明确的行情网络连接错误；其他 `error` 请按其 `message` / `errors` 排查。当前策略没有符合条件的买入信号时会保持空仓，成交数为 0 可以是正常结果。

如果日志提示行情连接失败、`403` 或 `blocked_market_data`，本次行情接入未成功，模拟不会据此制造交易。检查 Codespaces 能否访问 `api.exchange.coinbase.com`，解决后重新启动。历史数据、程序测试和等待状态都不等于实时模拟成交。

若长期停机后提示 `History is more than90 days behind`，或明确提示已有行情 JSON 损坏，需要重新下载暖机数据，普通重启不会替换旧文件。先执行 `stop` 并用 `status` 确认 `running: false`，备份整个 `cloud-paper/.runtime/`；然后把 `.runtime/BTC-USD-seed.json` 和 `.runtime/app/runtime/quotes/` 移到备份位置，再执行 `start --background`。启动器将在源文件不存在时重新下载。**保留 `.runtime/account.sqlite`，并确保操作期间没有模拟进程运行。** 新暖机不会补造离线期间的实时成交；其他错误不要按此方式重置数据。

## 策略与模拟方式

- 原生 CZSC 1.0.2 计算 5 分钟、30 分钟和日线结构，保守滞后一笔确认。
- 买入条件为“30 分钟笔区间三买候选 + 日线向上 + 新的 5 分钟向上笔”；这是可测试的工程规则。
- 首次启动建立行情水位，不补发历史信号。之后每 10 秒检查新的已收盘 K 线，信号委托延后一根有效收盘模拟成交。
- 单次资金分配上限 10%，单笔计划风险上限 0.25%，总计划风险上限 1%；单日亏损达到 1% 后停止新开仓。
- 模拟手续费为 0.1%，不利滑点为 0.05%。这些是模拟假设，跳空等情形的损失可能超过计划风险。
- 行情失败或超过 90 秒时效会暂停模拟处理。真实数据不足以形成结构时继续等待。

## 数据保存与运行限制

程序、行情、日志和账户保存在 `cloud-paper/.runtime/`，隔离的 Python 环境保存在 `cloud-paper/.venv/`，均已加入 Git 忽略规则。模拟账本为 `.runtime/account.sqlite`，暖机源文件为 `.runtime/BTC-USD-seed.json`。如需保留结果，请在删除 Codespace 前下载 `.runtime/`。

GitHub Codespaces 会受到休眠、停止、配额和账户设置影响；停止期间程序无法处理行情，这份配置不提供全天候托管。重新启动会尝试恢复原账户并接入新行情，不会补造离线期间的实时成交。

应用从本仓库的 `installers/czsc-quant-v0.3/CZSC-Quant-v0.3.zip` 校验 SHA256 后准备运行。此处不依赖 Windows 安装程序或通达信 DLL，也不改动仓库的 Rust/Python 库 API。

部署配置和本地检查通过不代表已经创建了你的 Codespace、连通了在线行情或产生模拟成交；这些结果应以你的 Codespace 实际状态和账本为准。
