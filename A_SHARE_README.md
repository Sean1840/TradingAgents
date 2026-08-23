# TradingAgents · A 股（沪深京）适配说明

> 本仓库在 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
> 多智能体 LLM 交易框架之上，集成了 [HiThink-Tech/Financial-API](https://github.com/HiThink-Tech/Financial-API)
> （同花顺 / 通达信官方数据服务）作为 A 股数据源，并做了完整的 A 股微观结构适配与中文输出管道。
> 本文件单独整理 **A 股相关逻辑**：数据供应商、规则注入、特色数据工具、持久化存储与报告输出；
> 框架本体（agent 编排、辩论、风控）请见 [README.md](README.md)。

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [架构总览](#2-架构总览)
3. [数据供应商](#3-数据供应商)
4. [供应商路由](#4-供应商路由)
5. [A 股规则注入](#5-a-股规则注入)
6. [A 股特色数据工具](#6-a-股特色数据工具)
7. [Agent 适配明细](#7-agent-适配明细)
8. [基准指数映射](#8-基准指数映射)
9. [持久化 OHLCV 存储](#9-持久化-ohlcv-存储)
10. [输出管道与交付物](#10-输出管道与交付物)
11. [运行方式](#11-运行方式)
12. [环境变量与配置](#12-环境变量与配置)
13. [工程要点与已知修复](#13-工程要点与已知修复)
14. [测试](#14-测试)
15. [致谢与上游项目](#15-致谢与上游项目)

---

## 1. 背景与动机

TradingAgents 原版面向美股（T+0、无涨跌幅限制、连续定价）。直接套用到 A 股会产生几类失真：

- **交易制度**：T+1、±10%/±20%/±5% 涨跌停、100/200 股一手、卖出印花税——原版 prompt 完全不知情；
- **数据源**：yfinance 对 A 股的财报/估值覆盖弱且口径偏美股；
- **信息源**：StockTwits / Reddit 对 A 股几乎无覆盖，散户情绪缺少代理；
- **政策权重**：A 股对国务院/央行/证监会等政策信号高度敏感，原版新闻流没有国内官方源；
- **估值陷阱**：周期股 TTM PE 在盈利低谷/峰值会严重失真，需要显式提示交叉验证。

本仓库的 A 股适配即围绕上述五点展开：数据层换 HiThink、规则层注入 A 股制度、
工具层增加涨停池/龙虎榜/热股榜/交易日历、信息层增加东方财富公告/新浪 7x24/官方政策新闻、
输出层落地中文报告 + 股价走势图 + 持久化数据。

## 2. 架构总览

```
┌──────────────────────── 多智能体分析层（LangGraph）────────────────────────┐
│ 分析师(基本面/市场/新闻/情绪) → 多空研究员辩论 → 交易员 → 风控团队 → 组合经理  │
│        每个 agent 的 system prompt 追加 A 股规则上下文（仅 A 股生效）          │
│        市场/新闻分析师额外绑定 A 股特色工具（见 §6）                           │
└──────────────┬──────────────────────────────────────────────┬─────────────┘
               │ 调用（供应商路由 route_to_vendor）              │ 输出
┌──────────────▼──────────────────────────────────────────────▼─────────────┐
│ 数据层（tradingagents/dataflows）                           输出层（output/）│
│  hithink_common  API Key/请求/缓存/重试/符号解析                <股票名>-<代码>/ │
│  hithink_stock   OHLCV（前/后复权，store-aware）               ├ <生成时间>/    │
│  hithink_fundamentals  估值+财报三表（中文标签）                │  ├ 分析报告.md  │
│  hithink_indicator  技术指标（stockstats）                    │  └ 分析报告.html │
│  hithink_special  涨停/跌停/炸板/连板/热股/龙虎榜/交易日历      ├ data/ 中间数据  │
│  hithink_store  持久化 OHLCV（跨次累加，突破 360 天上限）       └ .store/ohlcv/  │
│  cn_news  东财公告 / 新浪7x24 / 官方政策新闻（新华社、人民日报）                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 3. 数据供应商

### 3.1 `hithink` —— 同花顺（HiThink）A 股数据（`tradingagents/dataflows/hithink_*.py`）

| 模块 | 提供 | 说明 |
|---|---|---|
| `hithink_common.py` | 通用层 | API Key 读取（env + `%APPDATA%\hithink-finance\credentials.env` 回退）、`_request` 带重试/退避 + env 开关的磁盘缓存、`_date_to_ms/_ms_to_date`（Asia/Shanghai）、`resolve_symbol/resolve_symbol_info`（thscode / 裸代码 / 中文名均可）、错误映射（缺 Key → `VendorNotConfiguredError`，限流 → `VendorRateLimitError`，无数据 → `NoMarketDataError`） |
| `hithink_stock.py` | 行情 | `get_stock`：OHLCV CSV（默认前复权）；`fetch_ohlcv_frame`：**store 感知**——只请求缺失的 ≤360 天切片，命中部分直接复用存储 |
| `hithink_fundamentals.py` | 财务 | `get_fundamentals`（公司名 + PE/PB/PS/PCF 等估值 + 成长/盈利/偿债/营运/现金流五类能力指标）；三大报表（资产负债表/利润表/现金流量表）以**中文标签** CSV 输出；**前视过滤**：只返回 `period_end ≤ 分析日期` 的报告期 |
| `hithink_indicator.py` | 指标 | 通过 stockstats 计算，指标参数表与 yfinance 路径共享 `indicator_descriptions.py` |
| `hithink_special.py` | 特色数据 | 见 §6，失败时返回 `DATA_UNAVAILABLE: ...` 哨兵而非抛错 |
| `hithink_store.py` | 持久化 | 见 §9 |

### 3.2 `cnnews` —— 中文新闻（`tradingagents/dataflows/cn_news.py`，免 Key）

| 函数 | 来源 | 说明 |
|---|---|---|
| `get_news` | 东方财富公告 | 按 ticker 检索 A 股公司公告（业绩预告、减持、异动、并购等） |
| `get_global_news` | 新浪 7×24 快讯 | `feed.mix.sina.com.cn` 滚动流（lid=2516） |
| `get_policy_news` | 官方政策新闻 | ① 新浪直播流（`zhibo_id=152`）按 `POLICY_KEYWORDS`（国务院/央行/证监会/两会/政治局/国常会/降准降息/关税…）过滤；② 新华社·时政、人民日报·时政 RSS；**严格日期过滤**，无解析日期的条目直接丢弃（前视安全） |

### 3.3 `yfinance` —— 兜底

非 A 股符号（或 A 股数据缺口）时回退；基准指数（上证综指等）也经 yfinance 取数。

## 4. 供应商路由

`tradingagents/dataflows/interface.py`：

- `VENDOR_LIST`：`yfinance / fred / polymarket / alpha_vantage / hithink / cnnews`；
- `VENDOR_METHODS`：每个方法（`get_stock_data` / `get_indicators` / `get_fundamentals` / 三表 / `get_news` / `get_global_news` …）→ 各供应商实现；
- `route_to_vendor`：按 `data_vendors` / `tool_vendors` 配置的供应商链逐个尝试，**第一个成功者胜出**；核心类目失败抛错（响亮失败），可选类目（宏观、预测市场）失败降级为哨兵；
- 供应商链可通过环境变量 JSON 覆盖（见 §12），如 `core_stock_apis: "hithink,yfinance"` 表示优先 HiThink、失败回退 yfinance。

## 5. A 股规则注入

`tradingagents/agents/utils/a_share_rules.py` 的 `get_a_share_rules_context()` 生成一段**条件式**规则文本
（"仅当标的是 A 股时生效"，对非 A 股无害），追加到**全部** agent 的 system prompt：

- **T+1**：当日买入次日才能卖出，任何买入建议必须能承受隔夜跳空；
- **涨跌停**：主板 ±10%、创业板/科创板 ±20%、ST ±5%、北交所 ±30%；涨停无法买入、跌停无法卖出，**禁止提交无法成交的委托**，并显式标注涨跌停日；
- **交易单位**：100 股/手（科创板 200 股），零股只能卖；
- **成本**：卖出印花税 0.05%（2023-08 起）+ 佣金 + 过户费；
- **披露节奏**：一季报(~4月)/半年报(~8月)/三季报(~10月)/年报(~次年4月) + 业绩预告/快报——"过去一周基本面"几乎无意义，锚定最新报告期；
- **风险标识**：ST/*ST、商誉减值、限售解禁、大股东减持、质押爆仓、`-U` 未盈利后缀；
- **周期股 PE 陷阱**：周期峰值的低 TTM PE 具有欺骗性，需交叉验证 PB / ROE / 股息率 / 周期位置；
- **微观结构**：游资主导短线，用涨停池 / 龙虎榜 / 热股榜作为资金与情绪信号。

## 6. A 股特色数据工具

`tradingagents/agents/utils/a_share_market_tools.py`（langchain `@tool` 包装，底层调用 `hithink_special`）：

| 工具 | 数据 | 用途 |
|---|---|---|
| `get_market_context(curr_date)` | 涨停池 Top10（含连板数/封单/题材）、跌停池、炸板池（含开板次数）、连板天梯（2板…7+板） | 判断市场 risk-on / risk-off 环境，建仓/减仓前先看市场情绪 |
| `get_dragon_tiger(date?, thscode?)` | 龙虎榜，机构 vs 游资净买入 | 验证机构/游资资金流向（可过滤单只标的） |
| `get_hot_stocks(period)` | 热股榜（日榜/小时榜），热度 + 排名趋势 | A 股散户/游资关注度代理 |
| `is_trading_day(date)` | 是否 A 股交易日（中国节假日日历） | 确认日期可交易，避免在休市日提交易 |

`tradingagents/agents/utils/news_data_tools.py` 提供 `get_policy_news` 工具（见 §3.2），
二者均在 `agent_utils.py` 与 `graph/trading_graph.py` 中注册/导出。

> **重要**：这些工具必须在 `trading_graph.py::_create_tool_nodes()` 的
> `market` / `news` ToolNode 里注册（已注册），否则 LLM 能"看到"工具却无法执行，
> 会出现 "not a valid tool" 报错（历史修复见 §13）。

## 7. Agent 适配明细

| Agent | 适配内容 |
|---|---|
| 基本面分析师 | + A 股规则上下文；用 `get_fundamentals` + 三表（中文标签、前视过滤） |
| 市场分析师 | + A 股规则上下文；绑定 4 个市场工具；prompt 显式要求：**涨停/跌停日 K 线不作常规技术信号**（涨跌停扭曲 RSI/MACD、封板日不可成交）；用 `get_verified_market_snapshot` 作为精确数值唯一依据 |
| 新闻分析师 | + A 股规则上下文；绑定 `get_policy_news` / `get_market_context` / `is_trading_day`；prompt 把政策新闻列为 A 股第一权重输入 |
| 情绪分析师 | A 股时注入**热股榜**作为散户关注度代理（海外社交源对 A 股近乎无覆盖，空块不算信号） |
| 多空研究员 / 交易员 / 风控三人组 / 组合经理 | + A 股规则上下文（T+1、涨跌停、PE 陷阱等贯穿全程） |

## 8. 基准指数映射

`tradingagents/default_config.py` 的 `benchmark_map`：

| 后缀 | 基准 |
|---|---|
| `.SH`（如 `600519.SH`）、`.BJ` | `000001.SS`（上证综指，经 yfinance） |
| `.SZ` | `399001.SZ`（深证成指） |

不再一律回退到 SPY。可用 `TRADINGAGENTS_BENCHMARK_TICKER` 强制覆盖。

## 9. 持久化 OHLCV 存储

同花顺单次请求窗口上限 360 天。为支持更长跨度的分析，`tradingagents/dataflows/hithink_store.py`
维护**跨次累加的逐代码 OHLCV 存储** `output/.store/ohlcv/<code>.csv`（`Date/Open/High/Low/Close/Volume/Turnover`）：

- `TRADINGAGENTS_HITHINK_STORE=1` 时，每次拉取的 K 线合并进存储（按 Date 去重）；
- 后续运行 `fetch_ohlcv_frame` 只请求**缺失的 ≤360 天切片**，已存历史直接复用；
- 效果：只要持续运行同一标的，分析窗口会不断向历史延伸（示例：有研硅 688432 的分析已覆盖
  2024-08-21 → 2026-08-21 共 485 个交易日，远超单次 360 天上限）；
- `scripts/` 侧另可用 `hithink_report_gen.py --backfill N` 主动补拉 N 段历史。

## 10. 输出管道与交付物

### 10.1 布局（`tradingagents/report_io.py`，根目录 `output/`，可用 `TRADINGAGENTS_OUTPUT_DIR` 覆盖）

```
output/
├── 江波龙-301308/                      # <股票名>-<代码>
│   ├── 20260824-003724/                # <YYYYMMDD-HHMMSS> 每次生成一个目录
│   │   ├── 江波龙-301308_TradingAgents分析报告_2026-05-01_2026-08-21.md
│   │   └── 江波龙-301308_TradingAgents分析报告_2026-05-01_2026-08-21.html
│   └── data/                           # 中间数据（运行日志、API 缓存），跨次复用
│       └── 20260824-003724-run.log
└── .store/ohlcv/301308.csv             # 持久化 OHLCV（§9）
```

**文件名规范**：`<股票名>-<代码>_TradingAgents分析报告_<数据起>_<数据止>.md/.html`——
股票名 + 代码 + **数据有效范围**（本次分析实际使用的行情窗口）三重标识，文件可脱离目录直接共享。

### 10.2 报告内容（`scripts/log_to_reports.py` + `tradingagents/report_chart.py`）

- 从运行日志提取五大章节：市场/技术分析、情绪分析、新闻与宏观、基本面、交易员最终提案；
- 自动生成**最终交易提案卡片**（BUY/HOLD/SELL + 入场/止损/仓位）；
- 内置**股价走势可视化**：ASCII 迷你走势图 + SVG 折线图（MA20、区间最低/最高、终点收盘标注）；
  日志缺少 CSV 时自动回退到持久化 OHLCV 存储（按本次数据窗口裁剪）；
- HTML 版为自包含样式页面（中文、可分享）。

### 10.3 数据报告生成器（`hithink_report_gen.py`）

针对单只标的输出原始 HiThink 快照数据报告（行情/估值/财报/K线/热榜），
`--fresh` 强制绕过缓存、`--backfill N` 补拉历史进存储。

## 11. 运行方式

```bash
# 1) 配置（见 §12），然后运行完整多智能体分析
python run_a_share_analysis.py 600519.SH 2026-08-21     # 贵州茅台
python run_a_share_analysis.py 688432.SH 2026-08-21     # 有研硅
python run_a_share_analysis.py 贵州茅台 2026-08-21       # 中文名也可

# 2) 保留运行日志，再转成报告（md + html 落在 output/<股票名>-<代码>/<时间>/）
python run_a_share_analysis.py 301308.SZ 2026-08-21 2>&1 | tee run.log
python scripts/log_to_reports.py run.log 301308.SZ 江波龙

# 3) 纯数据报告
python hithink_report_gen.py 688432.SH 有研硅
python hithink_report_gen.py 600519.SH 贵州茅台 --backfill 2
```

## 12. 环境变量与配置

`.env`（或导出为环境变量；`TRADINGAGENTS_*` 会在 `default_config.py` 导入时自动套用）：

```bash
# --- LLM（两者结合：数据来自同花顺，分析来自 LLM 多智能体）---
TRADINGAGENTS_LLM_PROVIDER=deepseek           # 或 qwen / glm / kimi / openai ...
DEEPSEEK_API_KEY=sk-xxxx
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash

# --- 输出 ---
TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese

# --- 数据供应商：A 股走同花顺，回退 yfinance；新闻走中文源 ---
# (JSON 覆盖，与默认配置一层深合并)
TRADINGAGENTS_DATA_VENDORS={"core_stock_apis":"hithink,yfinance","technical_indicators":"hithink,yfinance","fundamental_data":"hithink,yfinance","news_data":"cnnews,yfinance"}

# --- 同花顺 Key（二选一：env 或 hithink-finance CLI 凭据文件）---
HITHINK_FINANCE_API_KEY=sk-xxxx        # https://fuyao.aicubes.cn/admin
# 或放 %APPDATA%\hithink-finance\credentials.env

# --- 缓存 / 持久化 ---
TRADINGAGENTS_HITHINK_CACHE=1          # 磁盘缓存同花顺响应（output/.cache/hithink/，TTL 见下）
TRADINGAGENTS_HITHINK_CACHE_TTL=21600  # 秒，默认 6h
TRADINGAGENTS_HITHINK_STORE=1          # 累加 K 线到 output/.store/ohlcv/（§9）

# --- 可选 ---
TRADINGAGENTS_OUTPUT_DIR=output        # 交付物根目录
TRADINGAGENTS_BENCHMARK_TICKER=        # 强制覆盖基准指数
```

## 13. 工程要点与已知修复

- **httpx2 Brotli 解码补丁**（`tradingagents/compat.py`，包导入时自动应用）：
  DeepSeek 等上游返回 `content-encoding: br`，httpx2 2.12 的 `BrotliDecoder` 与 google-brotli
  的 `process()` 签名不兼容会抛 `TypeError`；补丁去掉 `output_buffer_limit` 关键字。
- **A 股工具必须注册进 ToolNode**：工具在 LLM 侧 `bind_tools` 只是"让模型看见"，真正执行需要
  `trading_graph.py::_create_tool_nodes()` 里 `market`/`news` 两个 ToolNode 也注册同名工具，
  否则调用报 "not a valid tool" 且分析师会误报"工具不可用"。
- **调试日志完整捕获并行工具调用**：`debug=True` 流式打印按 `(类型, 内容, 工具调用签名)` 去重，
  并行调用（一个回合多个工具）的**全部**结果都会落日志，K 线 CSV 不再丢失。
- **报告章节分类的稳健性**：新闻报告可能提及"基本面/ROE"、基本面报告标题可能是"资产负债结构"
  而非"资产负债表"、新闻标题可能是"新闻与趋势研究报告"——分类器按
  `news → sentiment → fundamentals → market` 顺序用强特征判定，避免互相抢段。
- **前视安全**：财务报表只取 `报告期 ≤ 分析日期`；RSS 无日期条目直接丢弃。

## 14. 测试

```bash
python -m pytest tests/test_hithink.py tests/test_cn_news.py \
  tests/test_hithink_store.py tests/test_report_chart.py \
  tests/test_a_share_context.py tests/test_market_toolnode.py \
  tests/test_env_overrides.py tests/test_dataflows_config.py -q
```

覆盖：供应商路由/错误分类、中文新闻解析、存储合并与窗口规划、图表生成与章节分类、
12 个 agent 的规则注入、ToolNode 注册、环境变量覆盖等。全仓测试 640+ 通过。

## 15. 致谢与上游项目

本仓库的 A 股能力建立在两个开源项目之上，特此致谢：

1. **[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)** ——
   多智能体 LLM 交易框架（分析师/研究员/交易员/风控/组合经理 + LangGraph 编排）。
   学术参考：[arXiv:2412.20138](https://arxiv.org/abs/2412.20138)。
2. **[HiThink-Tech/Financial-API](https://github.com/HiThink-Tech/Financial-API)** ——
   同花顺（HiThink）金融数据服务：A 股行情、财报、估值、特色数据（涨跌停/龙虎榜/热股榜等）。
   本仓库通过其 REST API（`https://fuyao.aicubes.cn`，`X-api-key` 鉴权）取数。

> 本仓库为研究用途。分析由 LLM 生成，仅供信息参考，不构成任何投资建议。
