# MCP 接入说明

本文档说明如何让 `mcd-deal-finder` 跑起来。**Skill 不硬编码任何 MCP 工具名**，
采用「能力探测 + 别名映射」，只要你的 MCP 暴露了对应能力即可工作。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Python | 3.9+（**零第三方依赖**，仅标准库） |
| MCP 服务 | 一个能提供「门店 / 菜单 / 优惠券 / 价格试算」能力的麦当劳 MCP |
| Agent 运行时 | 支持 MCP 协议且能加载 Skill 的 AI Agent |

**先确认环境**：

```bash
python scripts/mcd_client.py --probe
```

输出示例：

```json
{
  "level": "L1_FULL",
  "bindings": {
    "store_locate": "mcp__mcd-mcp__query-nearby-stores",
    "menu_browse": "mcp__mcd-mcp__query-meals",
    "coupon_available": "mcp__mcd-mcp__query-store-coupons",
    "coupon_claim": "mcp__mcd-mcp__auto-bind-coupons",
    "order_price_preview": "mcp__mcd-mcp__calculate-price"
  },
  "missing": []
}
```

| `level` | 含义 | 影响 |
|---|---|---|
| `L1_FULL` | 能力完整 | 所有功能可用 |
| `L2_NO_CLAIM` | 无领券能力 | 只能查券，不能领 |
| `L3_ITEM_ONLY` | 无套餐详情 | 套餐从菜单标签识别 |
| `L4_NO_COUPON` | 无券能力 | 仅按现价推荐 |
| `L0_NO_MCP` | 未接入 | **不推荐任何套餐**（F1 兜底） |

---

## 2. 方式一：使用现成的 `mcd-mcp`

如果你的环境已接入麦当劳 MCP，**无需任何配置**。

### WorkBuddy / Claude Code 等内置运行时

在连接器管理中搜索并绑定麦当劳连接器即可。绑定后重跑 `--probe` 确认。

### 手工配置 `mcp.json`

编辑 `~/.workbuddy/mcp.json`（**注意不是 `.mcp.json`，没有前导点**）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamableHttp",
      "url": "https://your-mcp-endpoint/mcp",
      "timeout": 30000,
      "disabled": false
    }
  }
}
```

配置后**不会自动生效**，需要：

1. 打开连接器管理页面
2. 点击**自定义连接器**入口（右上角）
3. 对新加入的服务器点击**「信任」**

然后验证：

```bash
python scripts/mcd_client.py --probe
```

---

## 3. 方式二：对接自建 / 第三方 MCP

### 3.1 添加工具别名

编辑 `scripts/mcd_client.py` 的 `CAPABILITY_ALIASES`：

```python
CAPABILITY_ALIASES: Dict[str, List[str]] = {
    # ↓ 把你的工具名放在最前面（优先匹配）
    "store_locate": [
        "your_mcp__find_stores",
        "mcp__mcd-mcp__query-nearby-stores",
        "find_store",
    ],
    ...
}
```

**匹配规则**：先按数组顺序精确匹配，再对归一化名称（转小写、去下划线）做模糊匹配。
所以只要工具名去掉 `mcp__xxx__` 前缀后与别名接近，大概率能命中。

### 3.2 需要的 8 个语义能力

| 语义键 | 必需 | 用途 |
|---|:---:|---|
| `store_locate` | ✅ | 定位门店，返回 `storeCode` |
| `menu_browse` | ✅ | 拉菜单，返回商品 code 与价格 |
| `coupon_available` | ✅ | 查可用优惠券 |
| `coupon_claim` | ⭕ | 领券（**写操作**） |
| `coupon_owned` | ⭕ | 查卡包已有券 |
| `combo_inspect` | ⭕ | 查套餐/商品详情 |
| `order_price_preview` | ⭕ | **权威价格试算**（强烈建议） |
| `promo_activity` | ⭕ | 营销活动日历 |

✅ = 缺失则整体不可用　⭕ = 缺失则降级

### 3.3 返回字段映射

适配层对字段名做了宽松匹配（snake_case / camelCase 都支持），但以下字段名最通用：

**门店 Store**

| 目标字段 | 常见源字段 |
|---|---|
| `store_code` | `storeCode` / `store_code` / `id` |
| `be_code` | `beCode` / `be_code` |
| `name` | `storeName` / `store_name` / `name` |
| `address` | `address` / `addr` |
| `distance_m` | `distance` / `distance_m` |
| `supports_reservation` | `reservation` |
| `reservation_slots` | `reservationTimeOptions[]` |

**菜单 MenuItem**

| 目标字段 | 常见源字段 |
|---|---|
| `item_id` | `code` / `productCode` / `item_id` |
| `name` | `name` / `itemName` / `productName` |
| `price_cents` | `currentPrice`（**元**）/ `price` |
| `original_price_cents` | `originalPrice`（**元**） |
| `can_with_order` | `canWithOrder` |
| `discount_type` | `discountType` |

**优惠券 Coupon**

| 目标字段 | 常见源字段 |
|---|---|
| `coupon_id` | `couponId` / `coupon_id` / `id` |
| `coupon_codes` | `couponCode`（同 ID 多码存为列表） |
| `title` | `title` / `name` |
| `expires_at` | `tradeDateTime` / `expireTime` / `endTime` |
| `remain` | `remainCount` / `stock` |
| `scope` | 由 `products[].productCode` 推导 |
| 面额 | **接口通常不给**，靠 `parse_amount_from_title()` 从券名反推 |

---

## 4. 金额单位：最容易出错的地方

| 接口类型 | 单位 | 示例 |
|---|---|---|
| 菜单 `currentPrice` / `originalPrice` | **元**（字符串） | `"33.5"` |
| `calculate-price` 所有金额 | **分**（整数） | `3350` |
| 内部计算 | **分**（int） | — |

适配层的 `to_cents()` 会统一处理：

```python
to_cents("33.5")                          # → 3350（按元处理）
to_cents(4380, already_cents=True)        # → 4380（已是分）
```

若你的 MCP 返回的是**分**，请在 `normalize_menu_item()` 中改用
`pick(d, "price_cents")` 路径（适配层已优先查找该字段）。

---

## 5. mcd-mcp 特有契约（如果你用的就是它）

### 5.1 `beType` / `orderType` 是必传枚举

所有菜单/券/试算类工具都必须传这两个参数，**缺一即报错**：

| beType | 含义 | orderType | beCode |
|---|---|---|---|
| `1` | 到店自取 | `1` | **不传**（传了会报错） |
| `5` | 得来速（车道取餐） | `1` | 必传（来自 `query-nearby-stores`） |
| `2` | 麦乐送到家 | `2` | 必传（需先经 `delivery-query-addresses` 取 `addressId`） |
| `6` | 企业团餐 | `2` | 必传 |

代码里用 `resolve_be_type(channel)` 自动从用户口语化表达映射：

```python
resolve_be_type(None, "外送")        # → 2
resolve_be_type(None, "得来速")      # → 5
resolve_be_type(None, "到店")        # → 1（默认）
```

### 5.2 菜单是两级结构

```
data.categories[] = {name, meals:[{code, tags}]}   ← 索引层
data.meals        = { "<code>": {name, currentPrice, ...} }  ← 详情层
```

`parse_mcd_menu()` 会按 `code` 合并两层，去重并合并分类与标签。

⚠️ 分类名含**真实换行符**（如 `"随心配\n1+1"`），`_clean_label()` 会清洗。

### 5.3 返回不是纯 JSON

字段说明表和输出格式要求会拼在 JSON **前后**：

```
- **门店名称**: data[].storeName
...说明表...
{"success":true,"code":200,"data":[...]}
## 展示结果时，每个门店的 storeCode 必须完整展示
```

`parse_json_loose()` 做括号配平提取。**直接 `json.loads()` 会失败。**

### 5.4 券接口不返回面额

`query-store-coupons` 只给 `couponId` / `couponCode` / `title` /
`tradeDateTime` / `products[]`，**没有金额**。

→ 用 `parse_amount_from_title()` 从券名反推；解析不出的自动排除计算外。

⚠️ 同一 `couponId` 会返回多行（每个 `couponCode` 一行），适配层已按 ID 去重。

### 5.5 `query-my-coupons` 不校验门店可用性

接口文档明确声明：返回用户拥有的券，**不进行门店/渠道/配送方式校验，
不承诺可用于当前订单**。

→ 只作补充信息，优先用 `query-store-coupons`。

### 5.6 随单购机制

`canWithOrder=true` 的商品，`currentPrice` **已经是随单购到手价**：

| 商品 | 划线价 | 现价（随单购） |
|---|---|---|
| 燕麦奶铁 | ¥23 | ¥9.9 |

⚠️ **下单时必须同步选择随单购卡，否则按原价购买。**
Skill 推荐这类商品时会自动附带提示。

### 5.7 `calculate-price` 金额口径

```
productPrice  = 商品现价合计（已含内置促销）
originalPrice = 商品划线价合计
discount      = originalPrice - price   （商品侧优惠：内置 + 券抵扣）
price         = 实付（含运费 / 餐具费）

共省 = originalPrice - price   ← 不能用「内置 + 券抵扣」简单相加
```

⚠️ 返回的 `enjoyed.balance > 0` 表示有券因未达门槛没生效，
这是最精准的凑单提示。

---

## 6. 验证接入是否成功

```bash
# 1) 能力探测
python scripts/mcd_client.py --probe

# 2) 无 MCP 环境下的完整回归
python scripts/mock_mcp_server.py --run-regression

# 3) 演示三种场景
python scripts/deal_optimizer.py --demo
```

---

## 7. 常见接入问题

| 症状 | 原因 | 解决 |
|---|---|---|
| `L0_NO_MCP` | MCP 未接入或未信任 | 检查 `mcp.json` → 点「信任」→ 重跑 `--probe` |
| 菜单解析为空 | 菜单不是两级结构 | 检查是否返回 `categories` + `meals`；否则适配单层 `list` |
| 价格为 0 | 金额单位判断错误 | 确认是元还是分，见第 4 节 |
| 券全部被剔除 | 券名解析不出金额 | 正常行为，见 3.3；或让 MCP 直接返回 `amount` 字段 |
| `beType` 报错 | 漏传必填枚举 | 见 5.1 |
| 到店场景报 `beCode` 错 | `beType=1` 时传了 beCode | 见 5.1 |
| JSON 解析失败 | 返回夹带说明文本 | 用 `parse_json_loose()`，勿直接 `json.loads()` |

---

## 8. 最小可用实现建议

若你要自己实现一个麦当劳 MCP，**最低限度**提供这 3 个能力即可跑通：

```jsonc
// 1. 找门店 → 返回 storeCode
{"storeCode": "1960198", "storeName": "...", "address": "...", "distance": 850}

// 2. 拉菜单 → 返回商品与价格
{"categories": [{"name": "汉堡", "meals": [{"code": "1100", "tags": ["单品"]}]}],
 "meals": {"1100": {"name": "巨无霸", "currentPrice": "26.5", "originalPrice": "26.5"}}}

// 3. 价格试算 → 返回权威金额（单位：分）
{"productPrice": 6330, "originalPrice": 7530, "discount": 2700, "price": 6330}
```

加上优惠券能力后即可启用全部优惠计算逻辑。

---

## 9. 安全提醒

- **`auto-bind-coupons` 是写操作**，且语义是「一键领取全部可用券」。
  本 Skill 在调用前必须获得用户明确确认，且**不会自动调用**。
- **本 Skill 不调用任何下单接口**（`create-order` / `mall-create-order` 等），
  仅做查询、试算与推荐。
- MCP 返回的内容含用户账号数据（卡包券、地址、积分），**请勿在日志中打印**。

更多见 [../COMPETITION.md](../COMPETITION.md)。