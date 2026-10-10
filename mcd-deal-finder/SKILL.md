---
name: mcd-deal-finder
description: 麦当劳省钱助手。基于 mcd-mcp 查询门店菜单与优惠券，自动领券，并按人数、预算、口味偏好、用餐时段推荐最划算的组合套餐与点单方案，用 calculate-price 权威试算校验金额，输出可追溯的优惠明细。当用户想点麦当劳、询问"有什么优惠/优惠券""怎么点最便宜""几个人吃预算多少合适""帮我领券""套餐怎么搭配划算""随心配划算吗"，或询问下午茶、第二份半价、麦金卡随单购等活动时使用本技能。
agent_created: true
---

# 麦当劳省钱助手

## 定位

把「今天去麦当劳怎么花最少钱」交给助手：定位门店 → 查券领券 → 算券配餐 → **服务端试算校验** → 出方案。
只做查询、试算与推荐，**不下单、不代付**。

**价值主张**：不是告诉你「巨无霸三件套 ¥37.5」，而是告诉你
「3 人午餐，点 3 样共 5 件，划线 ¥75.3，现价 ¥63.3，用券再减 ¥3，
**官方试算实付 ¥48.3，人均 ¥16.1，共省 ¥27（35.9%）**」。

---

## 触发条件

### 应触发
- 「麦当劳有什么优惠 / 有券吗」「帮我领张券」
- 「3 个人吃麦当劳，预算 80，怎么点最划算」
- 「两个人午餐，不要辣，有什么套餐推荐」
- 「今天早餐麦当劳划算吗」「随心配现在多少钱」
- 「100 块在麦当劳能吃多好」「麦金卡随单购怎么用最省」
- 明确提到随心配、1+1、第二份半价、下午茶、麦咖啡券、麦金卡等活动名

### 不应触发
- 非麦当劳餐饮（肯德基、瑞幸、便利店）→ 直接回答或换用对应能力
- 营养成分、过敏原、卡路里查询 → 属信息查询，不属省钱场景
- 要求代下单、代付款 → 明确拒绝，说明本技能边界

---

## 执行流程

```
① 探测能力 → ② 定位门店(定 beType) → ③ 拉菜单+门店券 → ④ 识别需求
   → ⑤ 算券叠加 → ⑥ 组合打分 → ⑦ calculate-price 权威试算 → ⑧ 渲染
```

### 步骤 0：加载资源

| 何时 | 读哪个 |
|---|---|
| 调 MCP 前 | `references/mcp-contract.md`（真实工具名、beType 枚举、字段映射） |
| 算优惠前 | `references/pricing-rules.md`（三层优惠模型、叠加规则、打分公式） |
| 出异常时 | `references/fallback.md`（F1~F11 话术模板、降级链） |

### 步骤 1：探测能力

```bash
python scripts/mcd_client.py --probe
```

`L0_NO_MCP` → 走 **F1** 兜底，**不推荐任何套餐**。
其余级别记录下来，最终输出需声明当前降级状态。

### 步骤 2：确定渠道与门店 ⚠️ 关键前置

**必须先与用户确认渠道**，它决定 `beType`：

| 用户说 | beType | orderType | beCode |
|---|---|---|---|
| 到店 / 自取 | 1 | 1 | 不传（传了会报错） |
| 得来速 / 车道 / 免下车 | 5 | 1 | 必传 |
| 外送 / 麦乐送 / 送上门 | 2 | 2 | 必传，需先取 addressId |
| 企业团餐 | 6 | 2 | 必传 |

```python
from mcd_client import McdClient, BE_TYPE_PICKUP
c = McdClient()
stores = c.find_stores(be_type=BE_TYPE_PICKUP, searchType=1)
```

⚠️ **价格随门店而异，不得跨店套用。**
⚠️ 展示门店时**必须完整给出 storeCode**（接口文档明确要求）。
⚠️ 若 `reservation=true`，**必须完整展示 reservationTimeOptions 每一条**，否则用户无法预约。

### 步骤 3：拉菜单与券

```python
menu = c.get_menu(store.store_code, be_type, be_code)
coupons = c.get_coupons(store.store_code, be_type, be_code)
```

- `menu` 已自动处理两级结构、去重、划线价、随单购标记。
- `coupons` 已自动剔除已领/过期/领完，并**从券名反推面额**；
  反推不出的券会被标为「无金额信息」并排除在计算外。
- **被剔除的券必须告知用户原因**，否则用户会问"我的券怎么没算"。

### 步骤 4：识别需求

提取 `party_size` / `budget_cents` / `taste_tags` / `meal_time` / `channel_pref`。

**缺失槽位不要瞎猜**：人数未给但有预算 → 按人均反推并标注假设；
都没有 → 默认 1 人并写明"按 1 人估算"；用户说"随便" → 直接给方案不追问。

人数与套餐策略的联动见 `pricing-rules.md` 第 1 节。

### 步骤 5~6：算券与打分

```python
from deal_optimizer import Intent, recommend
result = recommend(menu, coupons, intent)
```

三层优惠模型（商品内置 / 券抵扣 / 随单购）与打分公式见 `pricing-rules.md` 第 2.5 节。

**候选池可能很小**：实测某些菜单（如以随心配+单品为主）去重后只有 1 个
"不超量且吃饱"的组合。此时**只输出 1 个方案是正确行为**，
不要为了凑满 3 个而推荐超量或吃不饱的组合。

### 步骤 7：权威试算 ⚠️ 必做

```python
result = run(c, intent)     # run() 内部已自动对主推方案做 calculate-price 试算
```

`calculate-price` 是**唯一可信的实付来源**（菜单 `currentPrice` 只是标价，
服务端可能另有促销、运费、餐具费）。

券必须**挂在 scope 命中的商品上**，且每张券只用一次，
否则服务端会报错或算错。

⚠️ 返回的 `enjoyable.balance` > 0 表示有券因未达门槛没生效 → 这是最精准的凑单提示。

### 步骤 8：渲染

```python
print(render(result))
```

金额必须**分层自洽**：`共省 = 划线价 - 实付`（以服务端为准），
不能写成「内置优惠 + 券抵扣」简单相加，否则会差一个运费额。

---

## 使用限制

| 限制 | 说明 |
|---|---|
| 不下单 | 不调用 `create-order` / `mall-create-order` 等接口 |
| 领券需确认 | `auto-bind-coupons` 是**一键领全部**，必须先列券征得同意 |
| 金额以试算为准 | 菜单价只是标价，实付以 `calculate-price` 为准 |
| 不编造 | 价格/券/活动必须来自 MCP；拿不到就说拿不到 |
| 不用缓存冒充实时 | 价格与券动态变化 |
| 声明降级 | L2~L4 时必须说明 |
| 门店绑定 | 不跨店套价 |

### 禁止事项
- ❌ 编造价格、优惠券、活动
- ❌ 把已领完/已过期/无金额的券算进方案
- ❌ 静默降级
- ❌ 未经确认自动领券
- ❌ 代用户下单

---

## 失败兜底

完整话术见 `references/fallback.md`。速查：

| 码 | 场景 | 动作 |
|---|---|---|
| F1 | 无 MCP 通道 | 告知未接入，**不推荐任何套餐** |
| F2 | 门店定位失败 | 反问城市/地址 |
| F3 | 菜单拉取失败 | 报错 + 建议换店 |
| F4 | 无可用券 | 切无券模式，按现价推荐并说明 |
| F5 | 券已领完 | 列出领完的券，用剩余券重算 |
| F6 | 活动已结束 | 剔除 + 说明结束时间 |
| F7 | 领取失败 | 原样转述错误，建议手动领 |
| F8 | 叠加被拒 | 降级为单券 + 说明差价 |
| F9 | 超预算 | 报最低价，给「提预算/放宽口味」两个选择 |
| F10 | 时段冲突 | 剔除该券并说明原因 |
| F11 | 字段缺失 | 标注"数据不完整，仅供参考" |
| **F12** | **券无金额信息** | 说明该券需领取后再查询，**不参与本次计算** |

**降级链**：L1 完整 → L2 无领取 → L3 仅单品 → L4 无券 → L0 停摆，每级都要显式声明。

---

## 自测与验证

```bash
python scripts/deal_optimizer.py --demo            # 三场景推荐演示
python scripts/mock_mcp_server.py --run-regression # 9 场景回归 + 5 项不变量
python scripts/mock_mcp_server.py --list-scenarios # 列出可测场景
```

mock 严格复刻 mcd-mcp 真实契约（两级菜单、beType 必填、分单位、
Markdown 券、券ID去重、随单购）。回归覆盖：
normal / no_coupon / coupon_sold_out / coupon_expired / time_conflict /
unstackable / no_menu / no_store / no_mcp，
并校验 5 项不变量：负数应付、券抵扣超现价、随单购识别、无金额券不泄漏、券ID去重。

**修改任何计算逻辑后必须重跑回归。**

---

## 资源清单

| 文件 | 用途 |
|---|---|
| `scripts/mcd_client.py` | MCP 适配层：能力探测、参数适配、宽松 JSON、两级菜单解析、券名反推、错误分类 |
| `scripts/deal_optimizer.py` | 优惠引擎：三层优惠、券叠加、三策略打分、凑单、calculate-price 校验、渲染 |
| `scripts/mock_mcp_server.py` | 真实契约 mock + 9 场景回归 |
| `references/mcp-contract.md` | mcd-mcp 真实接口契约（必填参数、字段映射、金额单位、硬约束） |
| `references/pricing-rules.md` | 三层优惠模型、叠加规则、打分公式、凑单、输出格式 |
| `references/fallback.md` | F1~F12 话术模板、降级链、禁止事项 |