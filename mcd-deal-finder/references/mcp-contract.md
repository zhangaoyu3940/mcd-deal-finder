# 麦当劳 MCP 契约（mcd-mcp 真实接口）

本文件基于 **mcd-mcp 连接器实测返回**编写，是适配层的唯一事实来源。
实测时间：2026-10-10，门店：麦当劳武汉万福国际广场餐厅（storeCode=1960198）。

---

## 1. 语义能力映射

| 语义键 | 真实工具 | 必填参数 |
|---|---|---|
| `store_locate` | `mcp__mcd-mcp__query-nearby-stores` | `beType`, `searchType` |
| `store_locate`（外送） | `mcp__mcd-mcp__delivery-query-stores` | `beType`, `addressId` |
| `menu_browse` | `mcp__mcd-mcp__query-meals` | `storeCode`, `orderType`, `beType` |
| `coupon_available`（门店） | `mcp__mcd-mcp__query-store-coupons` | `storeCode`, `orderType`, `beType` |
| `coupon_available`（全国） | `mcp__mcd-mcp__available-coupons` | 无 |
| `coupon_owned` | `mcp__mcd-mcp__query-my-coupons` | 无 |
| `coupon_claim` | `mcp__mcd-mcp__auto-bind-coupons` | 无（一键领全部） |
| `combo_inspect` | `mcp__mcd-mcp__query-meal-detail` | `storeCode`, `orderType`, `beType`, `code` |
| `order_price_preview` | `mcp__mcd-mcp__calculate-price` | `storeCode`, `orderType`, `beType`, `items` |
| `promo_activity` | `mcp__mcd-mcp__campaign-calendar` | 可选 `specifiedDate` |
| `promo_activity`（团餐） | `mcp__mcd-mcp__query-promotions` | 仅 `beType=6` |

---

## 2. beType / orderType 强制枚举

**所有菜单/券/试算类工具都必须传 `beType` 与 `orderType`，缺一即报错。**

| beType | 含义 | orderType | beCode |
|---|---|---|---|
| 1 | 到店自取 | 1 | **不传** |
| 5 | 得来速（车道取餐） | 1 | **必传**（来自 query-nearby-stores） |
| 2 | 麦乐送到家 | 2 | **必传**（来自 delivery-query-stores） |
| 6 | 企业团餐 | 2 | **必传** |

⚠️ **陷阱**：到店自取（beType=1）时传了 `beCode` 会直接报错。
⚠️ 外送场景**必须先经 `delivery-query-addresses` 拿 `addressId`**，不能只靠经纬度。

---

## 3. 门店字段（query-nearby-stores）

```json
{"storeCode":"1960198","storeName":"麦当劳武汉万福国际广场餐厅",
 "address":"鹦鹉大道530-3号万福国际广场","distance":850,
 "longitude":114.29,"latitude":30.58,"reservation":true,
 "reservationTimeOptions":[
   {"date":"2026-10-10","today":true,
    "reservationOptionText":"下午茶(14:44至16:45)，夜市(17:14至21:45)"}]}
```

| 目标字段 | 来源 | 说明 |
|---|---|---|
| `store_code` | `storeCode` | 后续所有工具入参，**必须完整展示给用户** |
| `be_code` | `beCode` | beType=5/2/6 时存在 |
| `supports_reservation` | `reservation` | 是否支持预约 |
| `reservation_slots` | `reservationTimeOptions[]` | **每一条都必须展示**，缺一条用户就无法预约 |

`distance` 在收藏/默认搜索下常为 0，不可据此排序。

---

## 4. 菜单两级结构（query-meals）

真实结构是**两层**，必须合并处理：

```
data.categories[] = {name, meals:[{code, tags}]}     ← 索引层：只有 code + tags
data.meals        = { "<code>": {name, currentPrice, originalPrice, ...} }  ← 详情层
data.frequent     = {code, tags}
```

字段映射：

| 目标字段 | 来源 | 说明 |
|---|---|---|
| `item_id` | `code` | 商品编码，试算时用 |
| `name` | `meals[code].name` | |
| `price_cents` | `currentPrice` | **字符串元**（"33.5"），需 ×100 |
| `original_price_cents` | `originalPrice` | 划线价 |
| `built_in_discount_cents` | 两者之差 | 商品自带优惠（促销/随单购） |
| `discount_type` | `discountType` | 促销优惠/麦金卡优惠/早餐卡优惠/null |
| `can_with_order` | `canWithOrder` | 是否可享随单购价 |
| `with_order` | `withOrder` | `{membershipCode, specId}` |
| `category` | `categories[].name` | **含真实换行**，如 `"随心配\n1+1"`，须清洗 |
| `is_combo` | 名称含"套餐/三件套/随心配" 或 tags 含"套餐" | 无显式字段 |

⚠️ 同一 `code` 会出现在多个分类下（如薯条同时属于"小食甜品"和"小食拼盘"），
按 code 去重并合并分类与 tags。
⚠️ 索引层可能没有对应详情（`meals[code]` 缺失），此时**跳过**，不可生成空名商品。

### 随单购机制（省钱核心）

`canWithOrder=true` 的商品，其 `currentPrice` **已经是随单购到手价**
（如「燕麦奶铁」划线 ¥23 → 随单购 ¥9.9）。
但**下单时必须同步选择随单购卡，否则按原价购买**。
推荐这类商品时必须提示用户，否则会出现「算的价格对不上」。

---

## 5. 优惠券

### query-store-coupons（推荐口径）

```json
{"couponId":"CP01","couponCode":"CP01-A","title":"满50减15",
 "tradeDateTime":"2026-10-15 23:59:59","remainCount":500,
 "products":[{"productName":"全场商品","productCode":"ALL"}]}
```

| 目标字段 | 来源 |
|---|---|
| `coupon_id` | `couponId`（**分组标识**，同一 ID 可能多行） |
| `coupon_codes` | `couponCode`（同 ID 多码，存为列表） |
| `title` | `title` |
| `expires_at` | `tradeDateTime` |
| `remain` | `remainCount` 或 `stock` |
| `product_codes` | `products[].productCode` → `scope="item:<code>"` |

⚠️ **该接口不返回面额与门槛**。但麦当劳券命名高度规整，可用
`parse_amount_from_title()` 从标题反推：

| 券名 | 解析结果 |
|---|---|
| 满50减15 | 门槛 ¥50，面额 ¥15 |
| 立减6 / 9.9元券 | 面额 ¥6 / ¥9.9 |
| 85折 / 全场8折 | 折扣率 0.85 / 0.8 |
| 买一送一 / 第二份半价 | 按 5 折估算（**精确值以 calculate-price 为准**） |
| 免费脆薯饼 / 可乐类券 | **解析不出金额 → 不参与计算**，仅提示 |

⚠️ 同一 `couponId` 会出现多行（每个 couponCode 一行），**必须按 ID 去重**。

### available-coupons（全国可领）

返回 **Markdown 文本**，无 couponId / 面额 / 门槛：

```
- 优惠券标题：人气麦旋风买一送一 \
  状态：可领取 \
  优惠券图片：<img src="...">
```

只能用 `parse_available_coupons_markdown()` 解析标题与状态，
生成的券 `type="unknown"`、`has_amount_info=False`，**自动被排除在金额计算之外**。
仅用于提示"有券可领，建议去领"。

### query-my-coupons（卡包）

⚠️ 接口文档明确声明：**返回用户拥有的券，不进行门店/渠道/配送方式等下单规则校验，
不承诺可用于当前订单**。

因此只能作补充信息，**不可单独作为可用性依据**。优先用 `query-store-coupons`。

---

## 6. 金额单位（最容易出错处）

| 接口 | 单位 |
|---|---|
| query-meals 的 `currentPrice` / `originalPrice` | **元**（字符串，如 `"33.5"`） |
| calculate-price 的所有金额字段 | **分**（整数，如 `4380`） |
| 内部计算 | 统一 **分（int）** |

### calculate-price 返回口径

```json
{"productOriginalPrice":4380,"productPrice":4380,"originalPrice":4380,
 "discount":0,"price":4380,
 "productList":[{"productCode":"9900013304","quantity":2,
                 "originalSubtotal":2980,"subtotal":2980}],
 "takeWayList":[{"code":"eat-in","title":"堂食"}]}
```

```
productPrice  = 商品现价合计（已含内置促销）
originalPrice = 商品划线价合计
discount      = originalPrice - price（商品侧优惠：内置 + 券抵扣）
price         = 实付（含运费 / 餐具费）
共省          = originalPrice - price（可能因运费而大于 discount）
```

⚠️ `price` 可能含运费/餐具费，因此**「共省」必须用 `originalPrice - price` 计算，
不能用本地拆分相加**，否则明细与总额不自洽。
⚠️ `enjoyed.balance` 是门槛差额——**存在这个字段说明有券因未达门槛而没生效**，
应提示用户"再加 ¥X 可再用券"。

---

## 7. 响应解析（宽松 JSON）

真实返回**不是纯 JSON**，字段说明表和输出格式要求会拼在 JSON 前后：

```
- **门店名称**: data[].storeName
...说明表...
{"success":true,"code":200,"data":[...]}
## 展示结果时，每个门店的 storeCode 必须完整展示
```

必须用 `parse_json_loose()` 提取第一个平衡的 `{...}` 片段，**直接 `json.loads` 会失败**。
解析后统一用 `mcd_data()` 取 `data` 并校验 `success` 标志。

---

## 8. 硬约束

1. **禁止编造价格**：菜单价来自 `currentPrice`，权威实付来自 `calculate-price`。
2. **禁止编造券面额**：接口不给金额时只能从券名反推；反推不出就不参与计算。
3. **领券必须确认**：`auto-bind-coupons` 是**一键领全部**的写操作，
   调用前必须把将领取的券列给用户并获得明确同意。
4. **不下单**：本 Skill 不调用 `create-order` / `mall-create-order` 等任何下单接口。
5. **必须传 beType/orderType**：这是 mcd-mcp 的硬性要求，缺失即报错。
6. **storeCode 必须完整展示**：这是接口文档的明确要求，便于用户核对门店。
7. **试算为准**：菜单 `currentPrice` 只是标价，最终金额以 `calculate-price` 为准。

---

## 9. 扩展别名（第三方封装）

若未来接入其他麦当劳 MCP，只需往 `CAPABILITY_ALIASES` 的对应数组里追加工具名，
无需改动任何逻辑代码。新增别名后必须重跑：

```bash
python scripts/mock_mcp_server.py --run-regression
```