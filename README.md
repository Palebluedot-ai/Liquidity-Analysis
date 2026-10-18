# Liquidity Analysis — 多交易所盘口流动性与滑点测算引擎

从实时抓取多交易所公开 L2 盘口深度开始，在静态盘口上模拟大额市价单逐档成交，输出 **成交均价 (VWAP)**、**最后一口价格 (Last Price)**、**不利滑点 (bp)**、**±5/10/25/50/100 bp 深度阶梯**，并提供 **Maker 挂单真实性验证 (`--verify`)** 与 **飞书 (Lark) 交互卡片前端数据结构 (`--lark-card`)**。

---

## 核心功能

1. **币对支持（预设下拉菜单 + 自定义输入）**
   - **预设下拉列表 (`PRESET_ASSETS`)**：`BTC`、`ETH`、`SOL (Solana)`、`BNB`、`XRP`、`DOGE`、`SUI`、`AVAX`、`LINK`、`TON`、`ADA`、`LTC`、`UNI`、`AAVE`、`APT`、`ARB`、`OP`、`HSK`
   - **自定义输入**：支持直接输入币种全称/简称（如 `Solana`、`PEPE`、`WIF`）或显式指定交易对（如 `BTCUSDT`、`BTC/USD`、`ETH-HKD`、`BTCHKD`），引擎会自动匹配各交易所最深的现货盘口。
2. **双币种金额输入（USD 美金 / HKD 港币）**
   - 支持 `--currency USD` 或 `--currency HKD`，自动拉取实时 `USD/HKD` 汇率（支持 `--fx-rate` 手动覆盖），同时输出 USD 与 HKD 计价的成交均价、盘口总深度与成本。
   - 支持人性化金额简写：`15000000`、`15M`、`500k`、`1000万`、`2000w`。
   - 当以 `HKD` 查询且包含 `HashKey` 时，自动同时对比 **HashKey USD 盘口 (`BTCUSD`)** 与 **HashKey 原生 HKD 盘口 (`BTCHKD` / `ETHHKD`)**。
3. **多交易所实时 L2 盘口支持**
   - `binance`：Binance Spot（最高 5,000 档，`data-api.binance.vision`）
   - `okx`：OKX Spot（最高 5,000 档，`www.okx.com/api/v5/market/books-full`）
   - `coinbase`：Coinbase Exchange（全量聚合 L2 盘口 ~20,000+ 档）
   - `kraken`：Kraken Spot（最高 500 档）
   - `hashkey` / `hashkey_hkd`：HashKey Exchange HK（最高 500 档，支持 `USD` / `HKD` / `USDT` 交易对）
   - `hashkey_global`：HashKey Global（最高 500 档）
   - `bybit`：Bybit Spot（最高 200 档）
4. **盘口 Maker 单真实性与防闪单验证 (`--verify`)**
   - **结构与序列号校验**：买盘严格递减、卖盘严格递增、买卖不倒挂 (`best_bid < best_ask`)、序列号 (`lastUpdateId` / `seqId` / `ts`) 单调递增。
   - **公开逐笔成交交叉核验**：对比最新真实成交价 (`Public Trades`) 是否落在盘口买一/卖一合理区间内。
   - **多帧留存率与深度稳定性检测**：连续采样多次盘口，计算前 50 档价格留存率及 `10bp` / `25bp` 挂单深度变异系数 (`CV%`)，识别高频闪单（Spoofing / Phantom Liquidity）。

---

## 快速用法

```bash
# 1. 多交易所横向对比：用 1000 万港币 (HKD) 买入 Solana (SOL)
python3 estimate_slippage.py --symbol Solana --side buy --notional 1000万 --currency HKD --exchange all

# 2. 多交易所横向对比：用 500 万美金 (USD) 卖出 BTC
python3 estimate_slippage.py --symbol BTC --side sell --notional 5M --currency USD --exchange all

# 3. 单交易所测算（兼容原版参数）：在 Binance 卖出 1500 万美金 BTC
python3 estimate_slippage.py --symbol BTCUSDT --side sell --notional 15000000 --exchange binance

# 4. 验证盘口 Maker 挂单真实性与多帧留存稳定性
python3 estimate_slippage.py --symbol BTC --exchange binance --verify --verify-samples 3

# 5. 查看下拉菜单预设币对与支持交易所列表
python3 estimate_slippage.py --list-presets

# 6. 生成飞书 (Lark) 交互卡片 JSON（查询表单卡片 / 结果报告卡片）
python3 estimate_slippage.py --lark-card query
python3 estimate_slippage.py --symbol BTC --side buy --notional 500万 --currency HKD --exchange all --lark-card result
```

---

## 目录结构

- [`estimate_slippage.py`](estimate_slippage.py)：命令行主入口（完全向后兼容原版参数，并支持多交易所、USD/HKD、验证模式与飞书卡片输出）
- [`liquidity_analysis/`](liquidity_analysis/)：核心业务包
  - [`models.py`](liquidity_analysis/models.py)：盘口快照、滑点结果、深度阶梯、验证报告数据模型
  - [`symbols.py`](liquidity_analysis/symbols.py)：预设币种列表、别名归一化、交易对解析与金额简写解析
  - [`fx.py`](liquidity_analysis/fx.py)：实时 `USD/HKD` 汇率获取与跨计价币种换算
  - [`exchanges.py`](liquidity_analysis/exchanges.py)：Binance、OKX、Coinbase、Kraken、HashKey HK/Global、Bybit 公开行情适配器
  - [`engine.py`](liquidity_analysis/engine.py)：逐档吃单引擎、深度阶梯计算、多所并发对比、Maker 挂单真实性验证
  - [`lark_card.py`](liquidity_analysis/lark_card.py)：飞书交互卡片表单生成与回调处理
- [`tests/test_liquidity_analysis.py`](tests/test_liquidity_analysis.py)：单元测试套件
