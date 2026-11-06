# CZSC 画线与自动模拟量化 v0.3

此目录提供独立应用的安装程序与便携包。应用使用CZSC 1.0.2内核，不要求修改仓库原有代码。

- [Windows安装程序](CZSC-Quant-Setup.exe)
- [便携ZIP](CZSC-Quant-v0.3.zip)
- [验证范围与SHA256](CZSC-Quant-validation.json)

GitHub文件页面点击 Download raw file / 下载原始文件。下载EXE受限时可使用ZIP。

## GitHub 云端自动模拟

[创建 GitHub Codespaces 模拟环境](https://codespaces.new/hanjian0515-max/czsc/tree/codex/quant-installer-v0.3) · [云端操作说明](../../cloud-paper/README.md)

在 GitHub 点击 Create codespace 后，自动安装依赖并尝试连接公开行情、启动 BTC-USD 现货模拟账户（10,000 USD）。在终端运行 `python3 -I cloud-paper/launch.py status` 核对行情与成交。Codespaces 创建成功不代表行情已接通；没有有效信号时保持空仓。休眠或停止期间不运行。

## 使用

1. 准备Python3.12 64位，已有则保留；缺少时可按Python启动器提示运行 py install 3.12。
2. 双击EXE安装；或完整解压ZIP后运行INSTALL_AND_RUN.cmd。
3. 打开“自动量化：模拟/回测”，选择沪深股票或BTC-USD、数据源和运行模式。
4. 历史回测和自动模拟使用独立账户；切换证券须新建账户。
5. 沪深股票需要本地.lc5和同证券.day；BTC支持只读公开行情下载和增量更新。行情不可用时自动暂停。

画线包括原生黄笔/中枢、独立红蓝辅助线、背驰候选与黄金参考带。通达信图层需安装并绑定匹配位数的新版DLL及主图公式，图表选择不复权；量化功能可独立使用，不要求DLL。

**当前是模拟与回测软件，没有真实账户下单接口。** 190项开发测试通过，完整包解压后复测通过；测试采用合成数据/模拟网络和真实CZSC内核，不是盈利证据。Windows实际安装、通达信编译、在线行情仍待实机验收。

更详细的策略、费用、T+1、数据时效和安装步骤见包内README.md。
