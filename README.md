# mcd-deal-finder · 麦当劳省钱助手

> 一个面向 AI Agent 的实用 Skill：接入麦当劳 MCP 后，自动查券、算券、配餐，
> 用**服务端权威试算**校验每一分钱，给出可追溯的最省点单方案。

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-orange.svg)](docs/MCP_INTEGRATION.md)
[![无外部依赖](https://img.shields.io/badge/dependencies-0-lightgrey.svg)](requirements.txt)

---

## 它解决什么问题

「今天去麦当劳，怎么花最少钱？」

这个问题看着简单，实际有四道坎：

| 坎 | 常见翻车方式 |
|---|---|
| 券用不上 | 不知道有券、券已领完、券没达门槛、券不能叠加 |
| 价格算不准 | 菜单价 ≠ 实付价，还有划线价、随单购、运费、配送费 |
| 套餐选不对 | 3 个人点 6 份套餐，或 1 个人买双人餐 |
| 推荐不落地 | 算出来「很省」，但压根不是能点的商品组合 |

本 Skill 把这四件事都做掉，并且**每一个金额都能追溯到接口原始返回**。

### 实际效果

```
输入：3 个人午餐，预算 90，不要辣

📍 麦当劳武汉万福国际广场餐厅
👥 3 人 · 预算 ¥90.00 · 午餐

🎫 已剔除 2 张不可用券：
   - 可乐类券（无金额信息，需领券后到门店查询）
   - 早餐专属立减6（时段不匹配）

【B · 均衡之选】★ 推荐
  · 人气经典随心配  ¥14.90
  · 精选超值随心配  ¥13.90
  · 精选单人餐三件套  ¥34.50  (划线 ¥46.50，省 ¥12.00)
  划线价合计      ¥75.30
  ├ 商品促销/随单购 -¥27.00
  ├ 随心配专属立减3  -¥3.00
  实付            ¥48.30
  ✅ 官方 calculate-price 权威价：¥48.30
  人均            ¥16.10   共省 ¥27.00（35.9%）
```

---

## 核心特性

### 1. 三层优惠模型

麦当劳的优惠不是一个数，而是三层叠加。混在一起算必然出错：

```
第 1 层  商品内置优惠    划线价 - 现价        促销优惠 / 麦金卡优惠
第 2 层  优惠券抵扣      cash / percent 券     需满足门槛，接口常不给面额
第 3 层  随单购          canWithOrder 商品     现价已是到手价，但下单必须同步选卡
```

### 2. 券名反推 —— 让券真正参与计算

`query-store-coupons` 接口**不返回面额和门槛**，只给券名。但麦当劳券命名高度规整：

| 券名 | 解析结果 |
|---|---|
| `满50减15` | 门槛 ¥50，面额 ¥15 |
| `立减6` / `9.9元券` | 面额 ¥6 / ¥9.9 |
| `85折` / `全场8折` | 折扣率 0.85 / 0.8 |
| `买一送一` / `第二份半价` | 按 5 折估算（精确值以服务端为准） |
| `免费脆薯饼` | **解析不出 → 排除计算，仅提示** |

解析不出的券**绝不硬猜**，自动标记为「无金额信息」并排除在计算外。

### 3. 服务端权威试算

菜单 `currentPrice` 只是**标价**，实付要靠 `calculate-price`。
每个主推方案都会走一次服务端试算，用返回的权威金额覆盖本地估算：

```
共省 = originalPrice - price     ← 以服务端为准
```

⚠️ 不能把「内置优惠 + 券抵扣」简单相加——服务端 `price` 含运费/餐具费，会差一个运费额。
这是实测踩过的坑。

### 4. 三策略推荐 + 不推荐蠢方案

- **A 极致省钱** — 人均最低
- **B 均衡之选** — 默认推荐 ★
- **C 预算贴合** — 最接近预算上限、吃得最饱

打分公式内置两道**防蠢护栏**：

```python
score = 节省率×100 - 预算偏离惩罚 - 吃不完惩罚 - 吃不饱惩罚 + 套餐加成

# 吃不饱罚 25 分：防止「极致省钱」退化成只推一杯饮料
# 超量方案（waste_risk=2）直接从候选池剔除：防止 3 人份推 9 件
```

### 5. 不可绕过的设计红线

| ❌ 禁止 | 原因 |
|---|---|
| 编造价格 / 券 / 活动 | 用户会拿去买单，差额由用户承担 |
| 把过期 / 领完 / 无金额的券算进方案 | 无效推荐 |
| 静默降级 | 用户以为是最优解，实际是次优解 |
| 未经确认自动领券 | 写操作，且可能占用用户权益 |
| 用缓存冒充实时价 | 价格与活动是动态的 |
| 代用户下单 | 超出 Skill 边界，涉及金钱交易 |

---

## 快速开始

### 环境要求

- Python 3.9+（**零第三方依赖**，仅标准库）
- 一个可用的麦当劳 MCP 服务（见 [MCP 接入说明](docs/MCP_INTEGRATION.md)）

### 安装

```bash
git clone https://github.com/zhangaoyu3940/mcd-deal-finder.git
cd mcd-deal-finder
```

### 作为 Skill 使用

将 `mcd-deal-finder/` 整个目录放入你的 Agent Skill 目录：

```bash
# WorkBuddy
cp -r mcd-deal-finder ~/.workbuddy/skills/

# Claude Code
cp -r mcd-deal-finder ~/.claude/skills/
```

### 试运行（无需 MCP）

```bash
cd mcd-deal-finder

# 演示：三种人数/预算场景下的推荐结果
python scripts/deal_optimizer.py --demo

# 回归：9 场景 + 5 项不变量校验
python scripts/mock_mcp_server.py --run-regression

# 探测当前环境的 MCP 能力
python scripts/mcd_client.py --probe
```

`--demo` 输出：

```
场景：3 人 / 80 元 / 不吃辣
👥 3 人 · 预算 ¥80.00 · 午餐
🎫 已剔除 2 张不可用券：过期券（已过期）、抢光的券（已领完）
【A · 极致省钱】...
```

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                     Skill 层 (SKILL.md)                  │
│         定位 · 触发条件 · 执行流程 · 使用限制            │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│         deal_optimizer.py  优惠计算引擎                  │
│  ┌────────────┐ ┌─────────────┐ ┌──────────────────┐  │
│  │ 券叠加计算  │ │ 组合打分     │ │ calculate-price  │  │
│  │ 单券/叠加   │ │ 三策略排序   │ │ 权威试算校验     │  │
│  └────────────┘ └─────────────┘ └──────────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│        mcd_client.py  MCP 适配层                         │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────┐  │
│  │ 能力探测    │ │ 参数适配    │ │ 字段归一          │  │
│  │ 别名映射    │ │ beType枚举  │ │ 金额统一「分」    │  │
│  └────────────┘ └────────────┘ └──────────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         │
                  ┌──────▼───────┐
                  │  mcd-mcp     │
                  │  麦当劳 MCP  │
                  └──────────────┘
```

### 设计要点

**能力探测 + 别名映射** — 不硬编码任何工具名。`CAPABILITY_ALIASES` 把 8 个语义能力
（`store_locate` / `menu_browse` / `coupon_available` …）映射到实际工具，
换 MCP 只需加别名，无需改逻辑。

**降级链** — `L1 完整 → L2 无领取 → L3 仅单品 → L4 无券 → L0 停摆`，
每级都必须显式声明当前状态，绝不静默降级。

**整数分运算** — 全程 `int` 分计算，仅渲染层除 100，杜绝浮点误差。

---

## 项目结构

```
mcd-deal-finder/
├── mcd-deal-finder/              # Skill 本体（可直接安装）
│   ├── SKILL.md                  # 主定义：定位/触发/流程/限制
│   ├── scripts/
│   │   ├── mcd_client.py         # MCP 适配层
│   │   ├── deal_optimizer.py     # 优惠计算引擎
│   │   └── mock_mcp_server.py    # 真实契约 mock + 回归测试
│   └── references/
│       ├── mcp-contract.md       # mcd-mcp 接口契约
│       ├── pricing-rules.md      # 三层优惠模型 + 打分公式
│       └── fallback.md           # F1~F13 兜底话术
├── docs/
│   ├── MCP_INTEGRATION.md        # 接入指南
│   └── COMPETITION.md            # 参赛声明
├── examples/
│   └── demo_output.md            # 真实场景输出示例
├── requirements.txt
├── LICENSE
├── README.md
└── .gitignore
```

---

## 开发与测试

```bash
# 语法检查
python -m py_compile mcd-deal-finder/scripts/*.py

# 完整回归（修改计算逻辑后必跑）
python mcd-deal-finder/scripts/mock_mcp_server.py --run-regression
```

回归覆盖 9 个场景，并强制校验 5 项不变量：

| 场景 | 覆盖的兜底分支 |
|---|---|
| `normal` | 正常链路 |
| `no_coupon` | F4 无可用券 |
| `coupon_sold_out` | F5 已领完 |
| `coupon_expired` | F6 已过期 |
| `time_conflict` | F10 时段冲突 |
| `unstackable` | F8 叠加被拒 |
| `no_menu` | F1/F3 菜单不可用 |
| `no_store` | F1/F2 门店定位失败 |
| `no_mcp` | F1 无 MCP 通道 |

| 不变量 | 意义 |
|---|---|
| 负数应付 = 0 | 折扣不会把价格算成负数 |
| 券抵扣 ≤ 现价 | 抵扣不超过商品金额 |
| 随单购商品识别 | 不漏掉真正的低价项 |
| 无金额券泄漏 = 0 | 解析不出的券绝不进计算 |
| 券 ID 去重 | 同 ID 多券码只算一次 |

---

## 扩展

### 接入其他麦当劳 MCP 实现

编辑 `mcd_client.py` 中的 `CAPABILITY_ALIASES`，把新工具名加到对应能力的别名数组：

```python
CAPABILITY_ALIASES = {
    "menu_browse": [
        "mcp__mcd-mcp__query-meals",   # 优先精确匹配
        "your_mcp__get_menu",           # 兜底
        "get_menu",
    ],
    ...
}
```

改完重跑 `python scripts/mock_mcp_server.py --run-regression` 确认无退化。

### 新增优惠类型

1. 在 `mcd_client.py` 的 `_CTYPE_MAP` 注册类型
2. 在 `deal_optimizer.py` 的 `coupon_discount()` 加分支
3. 在 `parse_amount_from_title()` 的 `_NAME_RE` 补券名正则
4. 补一条回归用例

---

## 常见问题

**Q：为什么某些场景只输出 1 个方案？**
A：候选池去重后可能只有 1 个「不超量且吃饱」的组合。此时只输出 1 个是**正确行为**——
为凑满 3 个而推荐超量或吃不饱的方案是错的。

**Q：为什么标记「无金额信息」的券不参与计算？**
A：`available-coupons` 接口只返回券名和状态，没有任何金额。估算金额等于编造数据。

**Q：算出来的价格和 App 里不一样？**
A：本 Skill 的 `run()` 会对主推方案调用 `calculate-price` 权威试算，
结果以 `✅ 官方权威价` 标记。若仍有差异，通常是门店/时段不同或活动已变更。

**Q：会不会自动下单？**
A：不会。`auto-bind-coupons`（领券）需要用户明确确认，且**不调用任何下单接口**。

---

## 免责声明

本项目为**技术演示与学习用途**，与麦当劳公司无任何关联、授权或背书。
价格与优惠信息以麦当劳官方渠道为准，本项目输出的方案仅供参考。
详见 [COMPETITION.md](docs/COMPETITION.md) 与 [LICENSE](LICENSE)。

---

## License

[MIT](LICENSE) © 2026 mcd-deal-finder contributors

「麦当劳」「McDonald's」及相关标识为其权利人的商标。
本项目使用这些标识仅为说明兼容目标，不暗示任何关联或授权。