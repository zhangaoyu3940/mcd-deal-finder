# 优惠计算与组合推荐逻辑

所有金额单位统一为 **分（int）**，仅在渲染层除以 100 显示。禁止浮点累加。

---

## 1. 需求识别（Intent Extraction）

从用户自然语言中提取 5 个槽位，缺失的**不要瞎猜默认值**，按下方策略处理：

| 槽位 | 字段名 | 抽取线索 | 缺失时行为 |
|---|---|---|---|
| 人数 | `party_size` | "两个人""3人""一家人""4个" | 若预算给了 → 按人均反推；否则默认 1 并在输出中标注"按 1 人估算" |
| 预算 | `budget_cents` | "50 以内""人均 30""不超过 80" | None=不设上限，但必须输出"最省方案" |
| 口味 | `taste_tags` | "不吃辣""要牛肉""素食"" Kids" | 转为 tags 过滤器，不影响可选项生成 |
| 时间 | `meal_time` | "早餐""午餐""下午茶""夜宵""明天" | 从当前时间推断时段，落不到就提示"按全天可用券计算" |
| 场景 | `channel_pref` | "外送""到店""自助点餐" | 取门店 `channels` 全集 |

人数推断优先级：
1. 显式数字（"3 人"）→ 直接用
2. 中文量词（"一家四口"→4、"两个"→2）
3. 英文（"a couple"→2、"family of 3"→3）

**人数与套餐规则联动**（麦当劳真实套餐逻辑）：

| party_size | 策略 |
|---|---|
| 1 | 优先「随心配」/ 单品 + 券；不推双人套餐（吃不完且更贵） |
| 2 | 优先双人套餐 / 1+1 组合 |
| 3–4 | 优先「超值套餐」「欢聚餐」，或 N 份单人 + 家庭桶 |
| ≥5 | 建议「派对桶」「全家桶」+ 儿童餐（Kids），提示提前备餐 |

---

## 2. 优惠叠加规则（Coupon Stacking）

### 2.1 单券抵扣计算

```python
def coupon_discount(coupon, base_cents):
    """返回该券对 base_cents 可抵扣的金额（分），不可用返回 0"""
    if coupon.claimed:
        return 0
    if coupon.expires_at and now() > coupon.expires_at:
        return 0
    # 门槛
    if base_cents < coupon.threshold_cents:
        return 0
    # 适用范围
    if coupon.scope != "all" and not match_scope(coupon, items):
        return 0
    # 类型
    if coupon.type == "cash":
        return min(coupon.face_value_cents, base_cents)
    if coupon.type == "percent":
        return base_cents - int(base_cents * coupon.percent)   # percent=0.85 → 减 15%
    if coupon.type == "delivery_fee":
        return min(coupon.face_value_cents, base_cents)  # 仅当 channel=delivery
    if coupon.type == "combo":
        return coupon.face_value_cents if coupon.is_combo_item else 0
    return 0
```

### 2.2 券组合选择（关键：不可叠加则取单张最优）

```python
def best_coupon_combos(coupons, items, channel):
    """返回 (最优单券, 最优叠加组合)，叠加组合可能为空列表"""
    applicable = [c for c in coupons
                  if not c.claimed
                  and (c.expires_at is None or c.expires_at > now())
                  and (c.remain is None or c.remain > 0)]
    base = sum(i.price_cents for i in items)

    # 维度 1：单张最优
    single_best = max(applicable,
                      key=lambda c: coupon_discount(c, base),
                      default=None)

    # 维度 2：可叠加组合（仅 stackable=True 的券才允许叠加）
    stackable = [c for c in applicable if c.stackable]
    # 按面额降序贪心：先应用门槛低的面额券（更易满足）
    greedy = sorted(stackable, key=lambda c: (c.threshold_cents, -c.face_value_cents))
    combo, cur = [], 0
    for c in greedy:
        d = coupon_discount(c, cur)     # 注意：门槛按「已折后金额」判定
        if d > 0:
            combo.append(c)
            cur -= d
    # 只有当叠加组合总优惠 > 最好单券时才采用叠加
    total_combo = base - cur
    best_combo = combo if (base - cur) > (base - coupon_discount(single_best, base)) else [single_best]
    return single_best, best_combo
```

> **叠加贪心的关键**：门槛按「已折后金额」判定。若先用了大额券导致金额低于另一张券的门槛，那张券就浪费了。
> 排序用 `(门槛升序, 面额降序)` 让低门槛券先吃掉，避免高门槛券失效。

### 2.3 叠加封顶规则

| 规则 | 说明 |
|---|---|
| 单笔订单最多应用 2 张券 | 超过 2 张的组合直接丢弃（防止算出纸面最优但下单被拒） |
| 折扣券不与折扣券叠加 | 两种 percent 券只能取其一 |
| 免配送费券 + 满减券 可叠 | 分属不同费用项，允许 |
| 套餐内含单品不可再叠单品券 | 若某单品属于已选套餐，scope 判定时跳过 |

---

## 2.5 三层优惠模型（mcd-mcp 真实口径）

与 mcd-mcp 对接后，优惠不是一个数，而是**三层叠加**，必须分层展示：

```
第 1 层：商品内置优惠   originalPrice - currentPrice
         来源：促销优惠 / 麦金卡优惠 / 早餐卡优惠（随单购）
         特点：自动生效，无需券

第 2 层：优惠券抵扣       scope 命中的 cash / percent 券
         来源：query-store-coupons
         特点：需满足门槛；面额靠券名反推（接口不给）

第 3 层：随单购          canWithOrder=true 的商品
         来源：菜单 canWithOrder / withOrder
         特点：currentPrice 已是到手价，但下单必须同步选卡，否则按原价
```

计算顺序：
```python
list_price   = Σ currentPrice × qty           # 菜单现价（第1层已含）
coupon_cut   = best_coupon_combo(coupons, lines)   # 第2层
payable      = list_price - coupon_cut        # 权威值以 calculate-price 为准
total_saved  = original_price - payable       # 第1+2层 + 运费差
```

**禁止把 `total_saved` 写成第1层 + 第2层简单相加**——服务端 `price` 含运费/餐具费，
两者会差一个运费额。实测踩过的坑，必须以 `calculate-price` 的
`originalPrice - price` 为准。

### 随单购商品的处理

```python
if item.can_with_order:
    # currentPrice 已是随单购到手价，可直接用于计算
    effective = item.price_cents
    # 但必须提示用户：下单时要同步选随单购卡
    warnings.append("含随单购商品，下单时需同步选择随单购卡，否则按原价购买")
```

试算时要把 `withOrder` 参数传给 `calculate-price`，否则服务端会按原价返回。

---

## 2.6 门槛差额提示（凑单升级）

`calculate-price` 返回的 `enjoyable.balance` 表示**门槛差额**——
说明有券因为金额不够而没生效。这是精准的凑单提示：

```python
if data.get("enjoyable", {}).get("balance", 0) > 0:
    gap = data["enjoyable"]["balance"]
    # 提示："当前方案还差 ¥X 可用「券名」，值得吗？"
    topup = suggest_topup(plan.lines, coupons, budget)
    if topup:
        warnings.append(f"再加 {topup['add']}（{fmt_cents(topup['add_cents'])}）"
                        f"即可用券，净省 {fmt_cents(topup['gain_cents'])}")
```

比本地估算门槛更准，**优先使用服务端返回的 balance**。

---

## 3. 组合推荐引擎

### 3.1 候选生成

```
候选池 = 菜单单品 × 数量组合  +  套餐组合  +  套餐×N
数量范围：1..party_size（单品场景）
限制：跳过 taste_tags 冲突项（如 taste=素食 时跳过 category=荤类）
```

### 3.2 打分公式

每个候选方案计算一个综合分，用于排序：

```python
def score(combo_total, budget, is_combo, taste_match, waste_risk):
    """分值越高越优"""
    saving_rate = (original_total - combo_total) / max(original_total, 1)

    # 预算贴合度：正好花完预算 > 低于预算 > 超预算
    if budget:
        if combo_total > budget:
            budget_penalty = (combo_total - budget) / budget * 100   # 超支重罚
        elif combo_total >= budget * 0.85:
            budget_penalty = 0            # 最佳区间：85%~100%
        else:
            budget_penalty = (budget * 0.85 - combo_total) / budget * 20   # 花太少，轻罚
    else:
        budget_penalty = 0

    waste_penalty = {0: 0, 1: 3, 2: 10}[waste_risk]   # 吃不完程度

    return (
        saving_rate * 100
        - budget_penalty
        + taste_match * 5
        - waste_penalty
        + (2 if is_combo else 0)
    )
```

`waste_risk` 判定：`0` = 恰好；`1` = 偏多（超出 1 人份）；`2` = 明显超量（如 4 人点了 8 人份）。

### 3.3 输出 Top-N

取 score 最高的 3 个方案，**必须覆盖不同策略**，避免三个方案雷同：

- 方案 A：**极致省钱**（人均最低，允许牺牲口味/新鲜度）
- 方案 B：**均衡之选**（默认推荐，省 ≥15% 且口味匹配度最高）
- 方案 C：**预算贴合**（最接近用户预算上限，吃得最饱）

---

## 4. 凑单引擎（低于门槛时）

当最优方案金额低于某张券的 `threshold_cents` 时，尝试补单：

```python
def suggest_topup(items, coupon, budget, max_topup_cents=2000):
    """在 coupon.threshold 之前找最便宜的加购项（限制 ≤ max_topup_cents，即 20 元内）"""
    gap = coupon.threshold_cents - sum(i.price_cents for i in items)
    if gap <= 0:
        return None
    options = [
        {"name": "加购中薯条", "price_cents": 900, "delta_after_discount": -900},
        {"name": "加购中可乐", "price_cents": 700, "delta_after_discount": -700},
        {"name": "加购冰咖啡", "price_cents": 1000, "delta_after_discount": -1000},
    ]
    cands = [o for o in options
             if o["price_cents"] >= gap and o["price_cents"] <= max_topup_cents]
    if not cands:
        return None
    # 净收益 = 券抵扣 - 加购成本，只有净收益 > 0 才建议
    gain = coupon.face_value_cents - min(cands, key=lambda o: o["price_cents"])["price_cents"]
    return min(cands, key=lambda o: o["price_cents"]) if gain > 0 else None
```

**净收益门槛**：加购成本 ≥ 券面额时**不建议凑单**，直接提示"当前方案已是最优，不建议凑单"。

---

## 5. 推荐结果输出格式

每个方案用统一结构，**必须显式展示计算过程**（让用户看得懂钱省在哪）：

```
【方案 B · 均衡之选】★ 推荐
人数：3 人 | 渠道：到店 | 时段：午餐
────────────────────────────────
原价合计          ¥87.00
├ 巨无霸套餐          ¥39.00
├ 麦辣鸡腿堡套餐      ¥29.00
└ 中可乐 × 1          ¥19.00  ← 原价 ¥19.00，券减 ¥9.00

优惠明细
├ 满50减15券         -¥15.00   （已叠加）
└ 大杯可乐券          -¥9.00
────────────────────────────────
应付合计            ¥63.00
人均                ¥21.00
共省                ¥24.00（27.6%）

📌 说明：满减按已折后金额判定，故大杯券先于满减券计算。
⚠️ 券有效期至 2026-10-12 14:00，下单前请确认。
```

---

## 6. 硬性计算纪律

1. **禁止浮点运算**：全部走整数分，任何环节出现 float 立即转换。
2. **禁止负数应付**：折扣后金额 `max(0, total - discount)`。
3. **禁止超额抵扣**：折扣总额不得超过 `base_cents`。
4. **禁止隐式四舍五入**：展示层保留 2 位，内部不丢失精度。
5. **券的门槛按已折后金额判定**（除非 MCP 明确返回 `threshold_base="original"` 则按原价）——这是最容易算错的地方。
6. **所有金额都要能追溯**：输出的每个数字都必须对应 MCP 返回的某个字段，不得出现估算值。