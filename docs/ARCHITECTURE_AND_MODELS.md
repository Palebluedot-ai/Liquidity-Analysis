# ARCHITECTURE_AND_MODELS.md — 定量微观结构数学模型推导与 7 大交易所 API 报文协议规范

本文档为 [`Liquidity-Analysis`](https://github.com/Palebluedot-ai/Liquidity-Analysis) 的深度技术与量化白皮书，详细记录代码库中每一个数学公式的学术推导、实盘参数校准逻辑、各交易所公开 REST API 的请求/响应格式，以及三层 Rolling 增量数据仓库的字段字典。

---

## 目录 (Table of Contents)

1. [第一部分：做市商不亏钱铁律与五项真实成本数学推导](#第一部分做市商不亏钱铁律与五项真实成本数学推导)
2. [第二部分：Almgren-Chriss 最优执行包络与导数极值点 $T^*$ 证明](#第二部分almgren-chriss-最优执行包络与导数极值点-t-证明)
3. [第三部分：连续盘口快照撤单成交比 (CFR) 取证算法](#第三部分连续盘口快照撤单成交比-cfr-取证算法)
4. [第四部分：365天+ 两级漏斗历史回溯与四大场景分类算法](#第四部分365天-两级漏斗历史回溯与四大场景分类算法)
5. [第五部分：7 大交易所公开 API 报文协议与限流规范详表](#第五部分7-大交易所公开-api-报文协议与限流规范详表)
6. [第六部分：数据模型 (`models.py`) 与持久化文件字段字典](#第六部分数据模型-modelspy-与持久化文件字段字典)

---

## 第一部分：做市商不亏钱铁律与五项真实成本数学推导

对应代码位置：
- [`liquidity_analysis/exchanges.py:fetch_kline_microstructure`](../liquidity_analysis/exchanges.py)
- [`liquidity_analysis/exchanges.py:fetch_live_usdt_fiat_friction`](../liquidity_analysis/exchanges.py)
- [`liquidity_analysis/engine.py:compute_real_execution_breakdown`](../liquidity_analysis/engine.py)

### 1.1 为什么做市商（Market Maker）在大单进场时必须毫秒级撤单退价？

站在做市商（如 Wintermute、Jump、Flow Traders 等高频做市商）视角，其在公开限价订单簿（CLOB）上挂出双边报价 $(P_{\text{bid}}, P_{\text{ask}})$ 的唯一目标是**赚取点差并保持期望收益非负（$\mathbb{E}[\Pi_{\text{MM}}] \ge 0$）**。当一笔规模为 $Q$（如 `$2,000,000 USD`）的大单吃入盘口时，做市商面临三项刚性风险成本：

1. **Glosten-Milgrom (1985) 逆向选择成本（Adverse Selection Cost）**：
   - 订单簿是匿名的。当接连出现同向 Taker 扫盘（使得 1 分钟净订单流不平衡 $\text{OFI}$ 激增）时，做市商的贝叶斯后验概率判断对手方大概率为知情交易者（Informed Trader）或大体量机构。
   - 若做市商不撤单，其以原价卖出的筹码将在大单扫完后立即承受永久性价格上涨（Permanent Price Impact）。
2. **Avellaneda-Stoikov (2008) 库存风险偏斜（Inventory Risk Skew）**：
   - 做市商的保留价格（Reservation Price）满足：
     $$r(s, q, t) = s - q \cdot \gamma \cdot \sigma^2 \cdot (T - t)$$
   - 当大买单吃空做市商前排卖单后，做市商背上空头库存（$q < 0$）。为了防止继续被吃成更大空头，做市商必须将全部剩余卖单向上大幅平移 $\Delta P_{\text{skew}} = |q| \cdot \gamma \cdot \sigma^2 \tau$。
3. **跨永续合约（Perpetual Futures）Delta 对冲成本底线（Perp Hedge Floor）**：
   - 现货做市商接到大额现货仓位后，通常在毫秒级内去 Binance / OKX 永续合约市场以 Taker 身份反向对冲 Delta。
   - 顶层 VIP9 做市商在永续合约的 Taker 费率约为 `1.5 ~ 2.0 bp`，外加期现基差与盘口半点差 `0.5 ~ 1.0 bp`，合计形成 **`mm_perp_hedge_floor_bp = 2.5 bp`** 的绝对保本底线。

---

### 1.2 从最近 180 根 1 分钟 K 线在线校准微观结构参数

为了彻底消除人为拍脑袋常数，[`fetch_kline_microstructure`](../liquidity_analysis/exchanges.py) 每次实时拉取目标交易对最近 180 根 `1m` K 线，在线计算：

1. **单分钟成交额 $V_{1\text{m}, t}$ 与净订单流 $\text{OFI}_{1\text{m}, t}$**：
   - 在 Binance 上，K 线第 7 项为 `quote_asset_volume` ($V_t$)，第 10 项为 `taker_buy_quote_asset_volume` ($V_{\text{buy}, t}$)。因此主动卖单额为 $V_{\text{sell}, t} = V_t - V_{\text{buy}, t}$，单分钟净订单流绝对值为：
     $$|\text{OFI}_t| = |V_{\text{buy}, t} - V_{\text{sell}, t}|$$
   - 在不直接返回 `taker_buy` 字段的交易所（OKX / Coinbase / Kraken / HashKey），采用微观结构经典的 **Close-Location Value (CLV)** 估计法：
     $$\text{CLV}_t = \frac{|Close_t - Open_t|}{High_t - Low_t} \in [0, 1], \qquad |\widehat{\text{OFI}}_t| = V_t \cdot \text{ clip}\Big(0.20 + 0.65 \cdot \text{CLV}_t,\ [0.18, 0.85]\Big)$$
2. **1 分钟抗极值波动率 $\sigma_{1\text{m}}$ (`sigma_1m_bp`)**：
   - 计算每分钟对数收益率基点 $r_t = \frac{Close_t - Open_t}{Open_t} \times 10^4$，对前后 5% 极值做 Winsorize 截尾后求标准差，得到稳健的 $\sigma_{1\text{m}}$。
3. **平方根市场冲击定律系数 $Y_{75}$ (`empirical_impact_y`)**：
   - 根据经典的 **Square-Root Market Impact Law**（Gatheral 2010 / Bouchaud et al. 2018），单根 K 线顺向峰值冲击 $\text{Peak\_bp}_t$ 与净流量满足：
     $$\text{Peak\_bp}_t = Y_t \cdot \sigma_{1\text{m}} \cdot \sqrt{\frac{|\text{OFI}_t|}{\overline{\text{OFI}}_{1\text{m}}}} \implies Y_t = \frac{\text{Peak\_bp}_t}{\sigma_{1\text{m}} \cdot \sqrt{|\text{OFI}_t| / \overline{\text{OFI}}_{1\text{m}}}}$$
   - 筛选净流量高于中位数的活跃 K 线样本，取 $Y_t$ 的 **75 分位数 $Y_{75}$** 作为大单进场时的冲击弹性系数。
4. **1 分钟扫盘 VWAP 冲击 $A_{\text{sweep},1\text{m}}(Q)$**：
   - 因为幂律冲击曲线 $p(x) \propto x^{1/2}$ 从 $0$ 积分到 $Q$ 的平均成交价（VWAP）等于峰值价格位移的 $\int_0^1 x^{1/2} dx = \frac{2}{3}$，故：
     $$A_{\text{sweep},1\text{m}}(Q) = \frac{2}{3} \cdot Y_{75} \cdot \sigma_{1\text{m}} \cdot \sqrt{\frac{Q}{\overline{\text{OFI}}_{1\text{m}}}}$$

---

## 第二部分：Almgren-Chriss 最优执行包络与导数极值点 $T^*$ 证明

对应代码位置：[`liquidity_analysis/engine.py:compute_real_execution_breakdown`](../liquidity_analysis/engine.py)

当专业交易员质疑：*“我用算法把大单拆成 $T$ 分钟均匀执行，瞬时冲击不就摊薄了吗？”*  
根据 **Almgren & Chriss (2001, *Optimal Execution of Portfolio Transactions*)**，将总规模 $Q$ 在时长 $T$（分钟）内均匀拆单执行时，总成本由三部分构成：

1. **不可消除的永久冲击与做市商对冲底线 $I_{\text{perm}}(Q)$**：
   $$I_{\text{perm}}(Q) = \theta_{\text{perm}} \cdot A_{\text{sweep},1\text{m}}(Q) + \text{MM\_Perp\_Hedge\_Floor}$$
2. **随拆单时间 $T$ 递减的临时冲击超额项 $I_{\text{temp}}(T)$**：
   - 将订单拆入 $T$ 个 1 分钟桶中，每分钟子单流量变为 $Q/T$，临时冲击按 $\frac{1}{\sqrt{T}}$ 衰减：
     $$I_{\text{temp}}(T) = \max\left(0,\ \frac{A_{\text{sweep},1\text{m}}(Q)}{\sqrt{T}} - \text{static\_slip\_bp}\right)$$
3. **随拆单时间 $T$ 递增的布朗运动价格漂移风险 $R_{\text{timing}}(T)$**：
   - 在 $T$ 分钟均匀 TWAP 过程中，未执行头寸的平均暴露方差为 $\frac{1}{3} \sigma_{1\text{m}}^2 T$。在 90% 置信度（单侧分位点 $z_{0.90} = 1.282$）下：
     $$R_{\text{timing}}(T) = \underbrace{\frac{1.282 \cdot \sigma_{1\text{m}}}{\sqrt{3}}}_{B_{\text{timing}}} \cdot \sqrt{T} = B_{\text{timing}} \sqrt{T}$$

### 求解全局最优拆单时长 $T^*$
忽略与 $T$ 无关的常数项，关于拆单时间 $T$ 的可变成本项为：
$$f(T) = \frac{A_{\text{sweep},1\text{m}}}{\sqrt{T}} + B_{\text{timing}} \sqrt{T}$$
对 $T$ 求一阶导数并令 $f'(T) = 0$：
$$f'(T) = -\frac{1}{2} A_{\text{sweep},1\text{m}} T^{-3/2} + \frac{1}{2} B_{\text{timing}} T^{-1/2} = 0 \implies \boxed{T^* = \frac{A_{\text{sweep},1\text{m}}}{B_{\text{timing}}}}$$
代入 $T^*$ 可得**任何拆单算法在数学上绝对无法突破的可变成本下界**：
$$\min_{T \ge 1} f(T) = 2 \sqrt{A_{\text{sweep},1\text{m}} \cdot B_{\text{timing}}}$$
代码同时计算 $T=1$（`strategy_sweep_1m_bp`）、$T=T^*$（`strategy_optimal_bp`）与 $T=T_{\text{slow}}$（`strategy_slow_twap_bp`），形成完整的 **3-Regime Execution Frontier**。

---

## 第三部分：连续盘口快照撤单成交比 (CFR) 取证算法

对应代码位置：[`liquidity_analysis/engine.py:verify_orderbook`](../liquidity_analysis/engine.py)

为了向客户实锤证明盘口挂单是“幽灵流动性（Ghost Liquidity）”，`verify_orderbook` 执行以下取证算法：

1. 在间隔 $\Delta t \approx 1.2\text{s}$ 内连续抓取 $K$ 帧 L2 订单簿快照 $S_1, S_2, \dots, S_K$，并拉取同一时间窗口 `[t_1, t_K]` 内的公开逐笔成交列表 $Trades$。
2. 对相邻两帧 $S_i$ 与 $S_{i+1}$，统计距离最优报价 10bp 价格带内的每一个价格档位 $p$：
   - 若档位 $p$ 上的挂单量减少 $\Delta q_p = \max(0,\ q_{i}(p) - q_{i+1}(p)) > 0$，则累计 **10bp 内消失挂单总额**：
     $$\text{Vanished}_{10\text{bp}} = \sum_{p \in \text{Band}_{10\text{bp}}} p \cdot \Delta q_p$$
3. 统计同一时间窗 `[t_i, t_{i+1}]` 内落在 10bp 价格带内的 **真实公开逐笔成交总额** $\text{Traded}_{10\text{bp}}$。
4. 根据质量守恒定律，**消失挂单中未发生真实成交的部分 100% 属于做市商主动撤单（Cancelled Liquidity）**：
   $$\text{Cancelled}_{10\text{bp}} = \max\Big(0,\ \text{Vanished}_{10\text{bp}} - \text{Traded}_{10\text{bp}}\Big)$$
   $$\text{Cancel\_Share\_\%} = \frac{\text{Cancelled}_{10\text{bp}}}{\text{Vanished}_{10\text{bp}}} \times 100\%, \qquad \text{CFR} = \frac{\text{Cancelled}_{10\text{bp}}}{\max(1.0,\ \text{Traded}_{10\text{bp}})}$$

---

## 第四部分：365天+ 两级漏斗历史回溯与四大场景分类算法

对应代码位置：[`liquidity_analysis/historical_tracker.py`](../liquidity_analysis/historical_tracker.py)

### 4.1 为什么采用「两级漏斗下钻（Two-Stage Hierarchical Funnel）」？
如果直接拉取 5 家交易所过去 365 天的全部 1 分钟 K 线（`5 × 525,600 = 262 万根`），单次初始化需要数千次 HTTP 请求、耗时数十分钟。
通过两级漏斗设计：
- **Stage 1（宏观与波动率扫描层）**：用 `1h` / `4h` / `6h` / `1d` K 线在 3~5 秒内扫描完过去 365 天的全部宏观窗口，按 `Quote_Volume * (1 + Range_bp / 50)` 评分定位全年高成交/高波动异动锚点；
- **Stage 2（高频微观结构与逐笔下钻层）**：在全年均匀分布的 24 个半月周期网格 + Top 异动锚点上，并发拉取 `5m` / `15m` 高频 K 线（以及在 Kraken 上调用 `/0/public/Trades?since=<ns>` 重建逐笔成交桶），仅需 **~35 秒** 即可采集 **73,000+ 根** 覆盖全年的高质量高频样本，并通过 **Rolling 增量同步（`--rolling-sync`）** 在日常 5 秒内完成增量累积。

### 4.2 四大市场场景分类决策树 (`classify_bars_into_scenarios`)
对每一根净流量 $|\text{OFI}_i| \ge \text{Min\_Large\_Order\_USD}$ 的大单 K 线，考察其前序 $L=8$ 根 K 线的累计收益率 $R_{\text{prior}}$（bp）与平均振幅 $\overline{\text{Range}}_{\text{prior}}$（bp）：
1. **③ 极端踩踏 / 恐慌清算期 (`extreme_stampede`)**：
   $$\overline{\text{Range}}_{\text{prior}} \ge P_{85}(\text{Range}) \quad \text{或} \quad |R_{\text{prior}}| \ge 75\text{ bp} \quad \text{或} \quad \text{Peak\_bp}_i \ge 80\text{ bp}$$
2. **② 上涨追高 / 顺势突破期 (`momentum_chase`)**：
   $$\big(\text{Dir}_i = \text{BUY} \ \land\ R_{\text{prior}} \ge +25\text{ bp}\big) \quad \text{或} \quad \big(\text{Dir}_i = \text{SELL} \ \land\ R_{\text{prior}} \le -25\text{ bp}\big)$$
3. **④ 逆势抄底 / 逢高派发期 (`contrarian_dip`)**：
   $$\big(\text{Dir}_i = \text{BUY} \ \land\ R_{\text{prior}} \le -25\text{ bp}\big) \quad \text{或} \quad \big(\text{Dir}_i = \text{SELL} \ \land\ R_{\text{prior}} \ge +25\text{ bp}\big)$$
4. **① 正常平稳震荡期 (`normal_calm`)**：
   $$R_{\text{prior}} \in (-25\text{ bp},\ +25\text{ bp}) \quad \text{且盘前波动率处于常态区间}$$

---

## 第五部分：7 大交易所公开 API 报文协议与限流规范详表

| 交易所 | 基础域名 (Base URL) | 实时深度接口 | 历史 K 线分页参数 | 历史逐笔成交接口与分页参数 | 限流与并发注意事项 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Binance** | `https://data-api.binance.vision` | `GET /api/v3/depth?symbol=BTCUSDT&limit=5000` | `GET /api/v3/klines?symbol=BTCUSDT&interval=5m&startTime=<ms>&limit=1000` | `GET /api/v3/aggTrades?symbol=BTCUSDT&startTime=<ms>&endTime=<ms>&limit=1000` | 务必使用 `data-api.binance.vision` 避免云主机 IP 451 限制；并发建议 `max_workers <= 6`。 |
| **OKX** | `https://www.okx.com` | `GET /api/v5/market/books-full?instId=BTC-USDT&sz=5000` | `GET /api/v5/market/history-candles?instId=BTC-USDT&bar=15m&after=<ts_ms>&limit=100` | `GET /api/v5/market/history-trades?instId=BTC-USDT&type=2&after=<ts_ms>&limit=100` | 注意 OKX 的 `after=<ts>` 表示**向更早的历史时间戳翻页**（即返回 `< ts` 的记录）；限流较严，并发请保持 `max_workers=2` + `sleep(0.12)`。 |
| **Coinbase** | `https://api.exchange.coinbase.com` | `GET /products/BTC-USD/book?level=2` | `GET /products/BTC-USD/candles?granularity=900&start=<ISO>&end=<ISO>` | `GET /products/BTC-USD/trades?after=<trade_id>&limit=1000` | `candles` 单次最多返回 300 根；`trades` 中返回的 `side` 代表 **Maker 方向**（即 `side="buy"` 表示 Taker 为 `SELL`）。 |
| **Kraken** | `https://api.kraken.com` | `GET /0/public/Depth?pair=XBTUSD&count=500` | `GET /0/public/OHLC?pair=XBTUSD&interval=15` (固定返回最新 720 根) | `GET /0/public/Trades?pair=XBTUSD&since=<纳秒时间戳>` (单次返回 1000 笔及下一页游标 `last`) | `OHLC` 不支持向历史翻页超过 720 根，但 `/0/public/Trades?since=<ns>` 支持回溯至 2013 年任意纳秒！ |
| **HashKey HK** | `https://api-pro.hashkey.com` | `GET /quote/v1/depth?symbol=BTCUSD&limit=500` | `GET /quote/v1/klines?symbol=BTCUSD&interval=15m&endTime=<ms>&limit=1000` | `GET /quote/v1/trades?symbol=BTCUSD&limit=60` | 支持通过 `endTime=<ms>` 向历史翻页达 365+ 天。 |
| **HashKey Global** | `https://api-glb.hashkey.com` | `GET /quote/v1/depth?symbol=BTCUSDT&limit=500` | `GET /quote/v1/klines?symbol=BTCUSDT&interval=15m&endTime=<ms>&limit=1000` | `GET /quote/v1/trades?symbol=BTCUSDT&limit=60` | 与 HashKey HK 协议完全一致。 |
| **Bybit** | `https://api.bybit.com` | `GET /v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=200` | `GET /v5/market/kline?category=spot&symbol=BTCUSDT&interval=15&start=<ms>&end=<ms>` | `GET /v5/market/recent-trade?category=spot&symbol=BTCUSDT&limit=1000` | 现货 L2 公开接口最高返回 200 档。 |

---

## 第六部分：数据模型 (`models.py`) 与持久化文件字段字典

### 6.1 `RealExecutionBreakdown` 核心字段说明

| 字段名 | 类型 | 单位 | 说明 |
| :--- | :--- | :--- | :--- |
| `static_slippage_bp` | `float` | `bp` | ① 静态 L2 订单簿 VWAP 滑点 |
| `mm_retreat_bp` | `float` | `bp` | ② 做市商保本退价与 K 线冲击溢价（含 `2.5 bp` Perp 对冲底线） |
| `hft_frontrun_bp` | `float` | `bp` | ③ Almgren-Chriss 拆单时间暴露与动量抢跑风险 |
| `taker_fee_bp` | `float` | `bp` | ④ 交易所现货 Taker 手续费 |
| `fiat_usdt_friction_bp` | `float` | `bp` | ⑤ 法币 $\leftrightarrow$ USDT 盘口穿透滑点 + 稳定币偏离基差 |
| `total_orderbook_cost_bp` | `float` | `bp` | 盘面五项全链路总成本（①+②+③+④+⑤） |
| `otc_quote_bp` | `float` | `bp` | 对标的 OTC Block Trade 全包一口价点数（默认 `30.0 bp`） |
| `otc_savings_bp` | `float` | `bp` | 走 OTC 相比盘面净节省的基点数（`total_orderbook_cost_bp - otc_quote_bp`） |
| `strategy_sweep_1m_bp` | `float` | `bp` | Almgren-Chriss 包络：1 分钟极速扫盘全包成本 |
| `strategy_optimal_bp` | `float` | `bp` | Almgren-Chriss 包络：$T^*$ 数学导数极值点理论最低拆单成本下界 |
| `strategy_slow_twap_bp` | `float` | `bp` | Almgren-Chriss 包络：慢速 TWAP 全包成本 |
| `historical_matrix` | `HistoricalScenarioMatrix` | 对象 | 365天+ 四大市场场景冲击分位数与真实历史案例矩阵 |
