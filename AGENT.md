# AGENT.md — 开发者与 AI Agent 一步一步协同维护手册 (Step-by-Step Maintenance Playbook)

> **适用对象**：后续接手或共同维护本项目的工程师、量化研究员以及各类 AI 编程助手（Antigravity / Claude Code / Cursor / Codex / Windsurf）。  
> **核心原则**：任何代码修改必须遵守 **① 零拍脑袋魔法常数（100% 实盘 API 数据或经验分布校准）**、**② 高内聚低耦合（分层职责边界不可破坏）**、**③ Rolling 增量数据只增不减**、**④ 全量单元测试 100% 通过后才可提交**。

---

## 目录 (Table of Contents)

1. [第一部分：架构红线与不可违背的设计铁律 (Core Invariants)](#第一部分架构红线与不可违背的设计铁律-core-invariants)
2. [第二部分：各大交易所历史逐笔 (Tick-by-Tick) 与 L2 数据服务全景图](#第二部分各大交易所历史逐笔-tick-by-tick-与-l2-数据服务全景图)
3. [第三部分：一步一步（Step-by-Step）常见研发与维护任务指南](#第三部分一步一步step-by-step常见研发与维护任务指南)
   - [任务 1：如何一步一步新增一家交易所（New Exchange Onboarding）](#任务-1如何一步一步新增一家交易所new-exchange-onboarding)
   - [任务 2：如何一步一步新增预设币种或特殊计价币对（New Symbol / Asset）](#任务-2如何一步一步新增预设币种或特殊计价币对new-symbol--asset)
   - [任务 3：如何一步一步调整或升级五项真实成本微观结构模型](#任务-3如何一步一步调整或升级五项真实成本微观结构模型)
   - [任务 4：如何一步一步维护与扩展 365天+ 四大场景 Rolling 增量库](#任务-4如何一步一步维护与扩展-365天-四大场景-rolling-增量库)
   - [任务 5：如何一步一步修改飞书交互卡片与长连接机器人](#任务-5如何一步一步修改飞书交互卡片与长连接机器人)
4. [第四部分：故障排查手册 (Troubleshooting Guide)](#第四部分故障排查手册-troubleshooting-guide)
5. [第五部分：每次提交前的标准自检清单 (Pre-Commit Checklist)](#第五部分每次提交前的标准自检清单-pre-commit-checklist)

---

## 第一部分：架构红线与不可违背的设计铁律 (Core Invariants)

在修改任何代码前，请先确认以下 4 条项目级红线：

1. **严禁引入拍脑袋经验公式（Zero Arbitrary Magic Constants）**：
   - 早期版本曾存在类似 `8.5 + 10 * sqrt(...)` 的硬编码经验参数，现已**全部废除**。
   - 所有滑点、做市商退价（`mm_retreat_bp`）、拆单时间风险（`hft_frontrun_bp`）、法币换汇损耗（`fiat_usdt_friction_bp`）必须来自：
     - [`exchanges.py:fetch_kline_microstructure`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py#L422-L645) 实时拉取的 180 根 1m K 线微观结构参数（$V_{1\text{m}}, \text{OFI}_{1\text{m}}, \sigma_{1\text{m}}, Y_{75}, \theta_{\text{perm}}, \rho_{\text{OFI}}$）；
     - [`exchanges.py:fetch_live_usdt_fiat_friction`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py#L648-L735) 实时穿透的 `USDTZUSD` / `USDTHKD` 500 档订单簿与实时基差；
     - [`historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py) 沉淀的 365+ 天四大场景真实分布。
2. **严格保持「高内聚、低耦合」的分层依赖方向**：
   - 依赖方向必须严格单向：`CLI / LarkBot` $\to$ `lark_card.py` $\to$ `engine.py` $\to$ (`exchanges.py`, `historical_tracker.py`, `fx.py`, `symbols.py`) $\to$ `models.py`。
   - **禁止反向依赖**：
     - [`models.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/models.py) 只能包含纯 `@dataclass` 定义，严禁 import 任何网络请求或业务逻辑模块；
     - [`exchanges.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py) 和 [`historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py) 的 Layer 1 负责外部 REST API 适配，不得耦合飞书卡片渲染或 CLI 打印逻辑；
     - [`historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py) 的 Layer 2（`classify_bars_into_scenarios`, `evaluate_order_across_4_scenarios`）必须是纯数学函数，与网络请求完全解耦。
3. **日常查询零长周期网络阻塞（Zero Latency on Hot Path）**：
   - 用户在 CLI 或飞书点击「立即测算」时，[`evaluate_order_across_4_scenarios`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L1133-L1235) 只从内存缓存读取 [`liquidity_analysis/data/historical_scenarios_1y.json`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/data/historical_scenarios_1y.json)（耗时 `< 0.1 ms`），**绝不在用户实时点击卡片时同步重跑 365 天全量爬虫**。长周期数据更新统一走 `--rolling-sync` 或 `--refresh-1y`。
4. **Rolling 增量数据只增不减**：
   - [`liquidity_analysis/data/rolling_hf_bars.json.gz`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/data/rolling_hf_bars.json.gz)、[`rolling_large_order_cases.jsonl`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/data/rolling_large_order_cases.jsonl) 和 [`rolling_live_telemetry.jsonl`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/data/rolling_live_telemetry.jsonl) 是团队不断沉淀的核心数据资产，切勿随意清空或覆盖删除。

---

## 第二部分：各大交易所历史逐笔 (Tick-by-Tick) 与 L2 数据服务全景图

当你需要进一步深挖某次历史极端行情的**毫秒级/纳秒级逐笔成交（Tick-by-Tick Trades）**或历史订单簿快照时，以下是我们实测验证过的各大交易所服务能力对比与代码调用入口：

| 交易所 | 1. 公开 REST API 历史逐笔成交 (无需 API Key) | 2. 官方免费离线逐笔/L2 数据归档仓库 | 3. 本项目已封装的逐笔 API 适配器函数 |
| :--- | :--- | :--- | :--- |
| **Binance** | ✅ **极强（2017 年至今，9 年全量）**<br>接口：`GET /api/v3/aggTrades?symbol=BTCUSDT&startTime=<ms>&endTime=<ms>`<br>特点：毫秒级精度，带 `m` (`isBuyerMaker`) 精确区分主动买/卖，且将同一毫秒同一价格被同一笔 Taker 单吃掉的挂单聚合，通过 `l - f + 1` 可直接算出单笔大单瞬间击穿了多少张 Maker 挂单！ | ✅ **官方免费数据站**：<br>`https://data.binance.vision/`<br>提供每日/每月 `.zip` 下载：<br>• `data/spot/daily/trades/` (原始逐笔)<br>• `data/spot/daily/aggTrades/` (聚合逐笔) | [`fetch_binance_agg_trades`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L84-L120) |
| **Kraken** | ✅ **极强（2013 年至今，13 年全量纳秒级）**<br>接口：`GET /0/public/Trades?pair=XBTUSD&since=<nanoseconds>`<br>特点：欧美所中唯一支持在公开 REST 接口直接传入**任意历史纳秒时间戳**跳回过去 13 年任意一秒拉取原始逐笔成交的交易所！返回精确 `b/s`（主动买卖方向）及 `m/l`（市价单 vs 限价过盘单）。 | ✅ **官方免费 Google Drive 历史包**：<br>Kraken 官方支持文档提供全量历史 `OHVT` 与 `Trades` CSV 季度压缩包下载。 | [`fetch_kraken_tick_trades_since`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L123-L157) |
| **OKX** | ✅ **强（支持按毫秒时间戳回溯最近 ~90 天逐笔）**<br>接口：`GET /api/v5/market/history-trades?instId=BTC-USDT&type=2&after=<ts_ms>&limit=100`<br>特点：`type=2` 支持直接传入毫秒时间戳跳至过去 3 个月内任意时刻拉取逐笔成交（含 `tradeId`, `px`, `sz`, `side`, `ts`）。 | ✅ **官方免费历史数据下载中心**：<br>`https://www.okx.com/data-download`<br>免费提供多年每日 `trades`（全量逐笔成交）与 `l2-orderbook` 历史归档包。 | [`fetch_okx_historical_tick_trades`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L160-L196) |
| **Coinbase** | ✅ **强（2015 年至今，按 `trade_id` 游标全量回溯）**<br>接口：`GET /products/BTC-USD/trades?after=<trade_id>&limit=1000`<br>特点：支持传入 `after=<trade_id>` 从 2015 年第 1 笔交易一直查到今天（配合二分查找 `trade_id` 可在 ~25 次请求内定位过去 11 年任意一秒的逐笔成交）。 | ⚠️ 官方无免费 S3 压缩包直链，完全依赖上述 REST `/trades?after=` 游标接口。 | [`fetch_coinbase_historical_tick_trades`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L199-L233) |
| **Bybit** | ⚠️ 弱（REST `/v5/market/recent-trade` 仅返回最新 1,000 笔，不支持历史时间戳回溯）。 | ✅ **官方免费公开 HTTP 目录**：<br>`https://public.bybit.com/spot/BTCUSDT/`<br>无需登录即可直接下载建所至今每天的 `.csv.gz` 逐笔成交文件。 | [`exchanges.py:fetch_recent_trades`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py#L323-L419)（实时近期） |
| **HashKey** | ⚠️ 弱（REST `/quote/v1/trades` 仅返回最新 60 笔实时成交，无历史逐笔接口；但 K 线 `/quote/v1/klines?endTime=` 支持 365+ 天回溯）。 | ❌ 官方暂不对外提供免费历史逐笔 CSV 下载。**因此必须依靠本项目的 Rolling 增量仓库持续沉淀数据。** | [`exchanges.py:fetch_recent_trades`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py#L323-L419) + Rolling 库 |

> **第三方商业级 L2 全量逐笔挂撤单（Order Book Tick-by-Tick）数据源补充**：  
> 如果未来需要回放历史某一秒钟完整的 **L2/L3 每一笔挂单与撤单事件（Order Book Diff Stream）**，各大交易所公开 REST API 均不保存历史盘口快照（因为每天高达数十 GB），行业标准方案是接入 **Tardis.dev**、**Kaiko** 或 **CoinAPI**，或者保持本项目 [`estimate_slippage.py --verify`](file:///Users/chao/Projects/Liquidity%20Analysis/estimate_slippage.py) 在实盘持续采样写入 [`rolling_live_telemetry.jsonl`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/data/rolling_live_telemetry.jsonl)。

---

## 第三部分：一步一步（Step-by-Step）常见研发与维护任务指南

### 任务 1：如何一步一步新增一家交易所（New Exchange Onboarding）

假设你需要新增一家交易所（例如 `bitfinex` 或 `gate`），请严格按以下 **5 个步骤** 修改，不要跳步：

- **Step 1：在 [`liquidity_analysis/symbols.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/symbols.py) 中注册交易对格式**
  1. 打开 [`liquidity_analysis/symbols.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/symbols.py)，找到 [`resolve_exchange_symbol(exchange, base, prefer_quote)`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/symbols.py#L105-L168)。
  2. 为新交易所添加交易对拼接规则（例如 `BTC` + `USDT` 是拼成 `BTCUSDT`、`BTC-USDT` 还是 `tBTCUSD`），并返回 `(exchange_symbol, actual_base, actual_quote)`。
- **Step 2：在 [`liquidity_analysis/exchanges.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/exchanges.py) 中实现实时行情适配器**
  1. 在 `SUPPORTED_EXCHANGES` 字典（第 24 行）中添加 `"new_ex": "NewExchange Name"`。如果希望默认参与 `all` 横向对比，同时加入 `DEFAULT_COMPARE_EXCHANGES`。
  2. 实现 `_fetch_new_ex_book(session, symbol, base, quote, limit)`，将返回的 `bids`（按价格从高到低排序）与 `asks`（按价格从低到高排序）封装为 `OrderBookSnapshot`，并在 `fetch_orderbook()` 中注册分支。
  3. 在 `fetch_recent_trades()` 中添加该交易所的最新逐笔成交解析分支（用于 `--verify` 撤单成交比核验）。
  4. 在 `fetch_kline_microstructure()` 中添加该交易所最近 180 根 1 分钟 K 线的拉取分支，确保能实时算出 `avg_1m_vol_usd`、`avg_1m_ofi_usd`、`sigma_1m_bp`、`empirical_impact_y` 和 `permanent_impact_ratio`。
- **Step 3：在 [`liquidity_analysis/engine.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py) 中配置默认 Taker 费率**
  1. 打开 [`liquidity_analysis/engine.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py)，在 `EXCHANGE_TAKER_FEE_BP`（第 38 行）中添加该交易所的标准现货 Taker 费率（单位 bp，如 `10.0` 代表 `0.10%`）。
- **Step 4：在 [`liquidity_analysis/historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py) 中接入 365 天历史与 Rolling 增量同步（可选但推荐）**
  1. 在 Layer 1 实现 `fetch_<new_ex>_historical_bars()`，并在 `fetch_incremental_bars_since()` 中添加增量分支。
  2. 将新交易所加入 `EXCHANGE_STUDY_CONFIGS` 列表，运行 `.venv/bin/python estimate_slippage.py --rolling-sync` 生成基准。
- **Step 5：在 [`tests/test_liquidity_analysis.py`](file:///Users/chao/Projects/Liquidity%20Analysis/tests/test_liquidity_analysis.py) 中补充单元测试**
  1. 添加符号解析与模拟快照评估测试，运行 `.venv/bin/pytest -v` 确保全部通过。

---

### 任务 2：如何一步一步新增预设币种或特殊计价币对（New Symbol / Asset）

1. **Step 1**：打开 [`liquidity_analysis/symbols.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/symbols.py)。
2. **Step 2**：在 `PRESET_ASSETS` 列表（第 11 行）中添加新币种代码（例如 `"SUI"` 或 `"ONDO"`）。飞书交互卡片的下拉菜单会自动读取 `PRESET_ASSETS`，无需手动改飞书代码！
3. **Step 3**：如果该币种有常见英文全称或中文别名，在 `SYMBOL_ALIASES` 字典（第 33 行）中添加映射（例如 `"ondo finance": "ONDO"`）。
4. **Step 4**：如果某家交易所不支持该币种（如 HashKey HK 仅支持合规白名单币种），在 `HASHKEY_HK_SUPPORTED_BASES` 或对应白名单集合中核对。

---

### 任务 3：如何一步一步调整或升级五项真实成本微观结构模型

所有核心定量计算高内聚在 [`liquidity_analysis/engine.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py) 的 [`compute_real_execution_breakdown()`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py#L207-L482) 函数中：

1. **Step 1 — 检查输入数据源**：
   - 确认新指标所需的原始数据能否从 `fetch_kline_microstructure()`（1m K 线）、`fetch_live_usdt_fiat_friction()`（稳定币盘口）或 `raw_levels`（当前 L2 盘口）中直接算出。
2. **Step 2 — 扩展数据模型字段**：
   - 若需对外展示新指标，先在 [`liquidity_analysis/models.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/models.py) 的 [`RealExecutionBreakdown`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/models.py#L112-L178) 中添加带默认值的字段（务必提供默认值如 `float = 0.0` 以保持向后兼容）。
3. **Step 3 — 在 [`compute_real_execution_breakdown()`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py#L207-L482) 中编写计算逻辑**：
   - 保持纯函数风格，并确保当离线单测未传入 `symbol` 时（`kline_stats is None` 分支）有确定性的 fallback 值，防止离线单元测试因网络依赖失败。
4. **Step 4 — 同步更新报告与飞书卡片视图**：
   - 更新 [`engine.py:build_otc_pitch_lines`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/engine.py#L968-L1075) 与 [`lark_card.py:build_result_card`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/lark_card.py#L279-L406)。

---

### 任务 4：如何一步一步维护与扩展 365天+ 四大场景 Rolling 增量库

[`liquidity_analysis/historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py) 管理着 `liquidity_analysis/data/` 下的四个核心文件：

1. **日常增量更新（耗时 ~5 秒）**：
   ```bash
   .venv/bin/python estimate_slippage.py --rolling-sync
   ```
   - 内部调用 [`incremental_rolling_sync()`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L1011-L1130)：读取 `rolling_hf_bars.json.gz` 中各所的 `last_synced_ts_ms`，仅拉取新增 K 线，按 `ts` 去重合并（保留最近 `730 天 = 2 年`），自动将新发现的大单案例追加至 `rolling_large_order_cases.jsonl`，并重新生成 `historical_scenarios_1y.json`。
2. **如何修改四大场景的分类阈值**：
   - 打开 [`liquidity_analysis/historical_tracker.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py)，定位到 Layer 2 的 [`classify_bars_into_scenarios(bars, min_net_usd, lookback=8)`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L714-L828)。
   - 修改分类逻辑后，无需重新拉取网络数据！只需加载本地 `rolling_hf_bars.json.gz` 并运行一次 `--rolling-sync`，即可在 3 秒内基于本地已存的 73,000+ 根 K 线重算全部四大场景统计。

---

### 任务 5：如何一步一步修改飞书交互卡片与长连接机器人

1. **修改查询表单下拉项**：
   - 打开 [`liquidity_analysis/lark_card.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/lark_card.py)。
   - 常用金额档位定义在 `PRESET_NOTIONAL_OPTIONS`（第 37 行），OTC 报价档位定义在 `OTC_QUOTE_BP_OPTIONS`（第 50 行）。
   - 修改表单控件后，确保 `parse_lark_form_params()`（第 95 行）中对应的 `form_value` 字段名一致。
2. **本地预览与校验飞书卡片 JSON（无需启动机器人即可测试）**：
   ```bash
   # 验证查询卡片 JSON 结构
   .venv/bin/python estimate_slippage.py --lark-card query

   # 验证测算结果卡片 JSON 结构
   .venv/bin/python estimate_slippage.py --symbol BTC --side buy --notional 2M --exchange binance,okx --lark-card result
   ```
3. **长连接机器人事件处理（[`liquidity_analysis/lark_bot.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/lark_bot.py)）**：
   - 注意：飞书卡片回调要求在 **3 秒内** 返回 HTTP/WebSocket 响应，否则飞书客户端会报错“操作超时”。
   - 因此在 [`lark_bot.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/lark_bot.py) 中，收到 `card.action.trigger` 回调时，必须**先立即返回一个「⏳ 正在实时抓取盘口与 K 线...」的过渡卡片**，然后通过后台线程 `threading.Thread` 完成多所并发测算，再调用飞书 `im.v1.message.patch` 接口异步更新原卡片！切勿把耗时网络请求直接写在同步回调返回里。

---

## 第四部分：故障排查手册 (Troubleshooting Guide)

| 常见问题现象 | 根因定位 | 解决方案 |
| :--- | :--- | :--- |
| **Binance 接口返回 HTTP 451 / 403** | 部分云厂商 IP 被 `api.binance.com` 限制访问 | 项目已默认使用币安官方公开行情镜像站 `https://data-api.binance.vision`（全球免限制），请确保不要改回 `api.binance.com`。 |
| **OKX 历史 K 线返回空数组 `[]`** | `history-candles` 并发过高触发了 OKX 的限流（20次/2秒） | 在 [`historical_tracker.py:fetch_okx_historical_bars`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/historical_tracker.py#L273-L353) 中保持 `max_workers=2` 及 `time.sleep(0.12)` 节流配置。 |
| **Python 3.13 下启动 `lark_bot.py` 报错 `event loop is already running`** | `lark-oapi` 内部直接调用了 `loop.run_until_complete()` | 确保 [`lark_bot.py`](file:///Users/chao/Projects/Liquidity%20Analysis/liquidity_analysis/lark_bot.py) 顶部保留 `from .ws_patch import apply_ws_patch; apply_ws_patch()`。 |
| **`pytest` 报错 `command not found`** | 未激活项目下的 `.venv` 虚拟环境 | 使用 `.venv/bin/pytest` 运行测试，或先执行 `source .venv/bin/activate`。 |

---

## 第五部分：每次提交前的标准自检清单 (Pre-Commit Checklist)

任何开发者或 AI Agent 在执行 `git commit` 和 `git push` 之前，**必须按顺序执行以下 4 条命令并确保全部返回 0（无报错）**：

```bash
# 1. 运行全量单元测试套件（必须 100% PASS）
.venv/bin/pytest -v

# 2. 验证实盘多所测算 + 五项成本 + Almgren-Chriss 包络 + 365天四大场景输出正常
.venv/bin/python estimate_slippage.py --symbol BTC --side buy --notional 2M --exchange binance,okx,kraken

# 3. 验证盘口 Maker 挂单真伪与 CFR 撤单成交比取证功能正常
.venv/bin/python estimate_slippage.py --symbol BTC --exchange okx --verify --verify-samples 2

# 4. 验证 Rolling 增量同步功能正常
.venv/bin/python estimate_slippage.py --rolling-sync
```
