"""
deal_optimizer.py — 优惠计算与组合推荐引擎

职责：
1. 券抵扣计算与叠加选择（单券最优 vs 叠加组合，取更优者）
2. 候选组合生成与打分排序（省钱 / 均衡 / 预算贴合 三策略）
3. 凑单引擎（低于券门槛时判断是否值得补单）
4. 结果渲染（可直接展示给用户的方案对比表）

铁律（与 references/pricing-rules.md 第 6 节一致）：
- 全程整数分，禁止浮点金额
- 折扣总额不得超过原价
- 应付金额不为负
- 所有金额必须可追溯到输入数据，不做估算

CLI：
    python deal_optimizer.py --demo
    python deal_optimizer.py --probe
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from mcd_client import (
        Coupon, MenuItem, Store, McdClient, McdError, McdUnavailable,
        fmt_cents, now_cst, resolve_be_type, BE_TYPE_TO_ORDER_TYPE,
        BE_TYPE_PICKUP, BE_TYPE_DELIVERY, BE_TYPE_DRIVE_THRU, BE_TYPE_CATERING,
    )
except ImportError:  # 允许以模块方式被上层导入
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcd_client import (  # type: ignore
        Coupon, MenuItem, Store, McdClient, McdError, McdUnavailable,
        fmt_cents, now_cst, resolve_be_type, BE_TYPE_TO_ORDER_TYPE,
        BE_TYPE_PICKUP, BE_TYPE_DELIVERY, BE_TYPE_DRIVE_THRU, BE_TYPE_CATERING,
    )


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
MAX_STACK = 2                 # 单笔订单最多应用 2 张券
MAX_TOPUP_CENTS = 2000        # 凑单上限 20 元
OVER_BUDGET_TOLERANCE = 0     # 超预算容忍度 0%，即不超
BUDGET_SATISFIED_LOW = 0.85   # 花到预算 85% 以上视为"吃得饱"

# 人数 -> 套餐策略
PARTY_STRATEGY_HINT = {
    1: "单人：优先随心配/单品+券，不推双人套餐（更贵且吃不完）",
    2: "双人：优先双人套餐或 1+1 组合",
    3: "3-4 人：优先超值套餐/欢聚餐，或单人套餐×N + 家庭桶",
}


@dataclass
class LineItem:
    """方案里的一条：某个商品 + 数量。"""
    item: MenuItem
    qty: int = 1

    @property
    def subtotal_cents(self) -> int:
        return (self.item.price_cents or 0) * self.qty


@dataclass
class AppliedCoupon:
    """已应用的券 + 它实际抵扣了多少。"""
    coupon: Coupon
    discount_cents: int
    note: str = ""


@dataclass
class Plan:
    """一个推荐方案。"""
    strategy: str                       # cheapest / balanced / budget_fit
    title: str
    lines: List[LineItem]
    applied: List[AppliedCoupon] = field(default_factory=list)
    original_cents: int = 0              # 划线价合计（真正原价）
    built_in_discount_cents: int = 0     # 商品自带优惠（促销/随单购）
    coupon_discount_cents: int = 0       # 券抵扣合计
    payable_cents: int = 0
    score: float = 0.0
    waste_risk: int = 0
    data_complete: bool = True
    warnings: List[str] = field(default_factory=list)
    topup_hint: Optional[Dict[str, Any]] = None
    verified: bool = False               # 是否已用 calculate-price 权威试算

    @property
    def discount_cents(self) -> int:
        """总优惠 = 内置优惠 + 券抵扣。"""
        return self.built_in_discount_cents + self.coupon_discount_cents

    @property
    def list_price_cents(self) -> int:
        """菜单现价合计（用户看到的加粗价）。"""
        return sum(l.item.price_cents or 0 for l in self.lines)

    @property
    def saving_rate(self) -> float:
        if self.original_cents <= 0:
            return 0.0
        return self.discount_cents / self.original_cents

    def per_head_cents(self, party_size: int) -> int:
        return self.payable_cents // max(party_size, 1)

    def to_dict(self, party_size: int = 1) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "title": self.title,
            "items": [{"name": l.item.name, "qty": l.qty,
                       "unit_cents": l.item.price_cents,
                       "list_cents": l.item.price_cents,
                       "original_cents": l.item.original_price_cents,
                       "built_in_discount_cents": l.item.built_in_discount_cents,
                       "subtotal_cents": l.subtotal_cents,
                       "can_with_order": l.item.can_with_order} for l in self.lines],
            "original_cents": self.original_cents,
            "list_price_cents": self.list_price_cents,
            "built_in_discount_cents": self.built_in_discount_cents,
            "coupon_discount_cents": self.coupon_discount_cents,
            "discount_cents": self.discount_cents,
            "payable_cents": self.payable_cents,
            "saving_rate": round(self.saving_rate, 4),
            "per_head_cents": self.per_head_cents(party_size),
            "coupons": [{"title": a.coupon.title, "discount_cents": a.discount_cents,
                         "note": a.note,
                         "coupon_id": a.coupon.coupon_id,
                         "coupon_codes": a.coupon.coupon_codes} for a in self.applied],
            "score": round(self.score, 3),
            "waste_risk": self.waste_risk,
            "data_complete": self.data_complete,
            "verified": self.verified,
            "warnings": self.warnings,
            "topup_hint": self.topup_hint,
        }


@dataclass
class Intent:
    """识别出的用户需求。"""
    party_size: int = 1
    budget_cents: Optional[int] = None
    taste_tags: List[str] = field(default_factory=list)
    meal_time: Optional[str] = None
    channel_pref: Optional[str] = None
    store: Optional[Store] = None
    assumptions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "party_size": self.party_size,
            "budget_cents": self.budget_cents,
            "taste_tags": self.taste_tags,
            "meal_time": self.meal_time,
            "channel_pref": self.channel_pref,
            "store": self.store.name if self.store else None,
            "assumptions": self.assumptions,
        }


# --------------------------------------------------------------------------
# 券抵扣计算
# --------------------------------------------------------------------------
def _scope_match(coupon: Coupon, lines: Sequence[LineItem]) -> bool:
    """券适用范围是否命中当前购物车。

    mcd-mcp 的 query-store-coupons 用 products[] 指定适用商品，
    这里支持三种 scope：
      - all              全场通用
      - item:<code,...>  限定 productCode（可能多个，逗号分隔）
      - name:<名称,...>  只有名称没有编码时按商品名匹配
    """
    if coupon.scope == "all":
        return True
    if coupon.scope.startswith("item:"):
        targets = {t.strip() for t in coupon.scope.split(":", 1)[1].split(",") if t.strip()}
        if not targets:
            return True
        return any(l.item.item_id in targets for l in lines)
    if coupon.scope.startswith("name:"):
        targets = [t.strip() for t in coupon.scope.split(":", 1)[1].split(",") if t.strip()]
        if not targets:
            return True
        for l in lines:
            for t in targets:
                if t and (t in (l.item.name or "") or t == (l.item.name or "")):
                    return True
        return False
    if coupon.scope.startswith("category:"):
        target = coupon.scope.split(":", 1)[1]
        return any(target in (l.item.category or "") for l in lines)
    return True


def _channel_ok(coupon: Coupon, channel: Optional[str]) -> bool:
    if not coupon.channel:
        return True
    if not channel:
        return False
    return coupon.channel.lower() in (channel or "").lower()


def coupon_discount(coupon: Coupon, base_cents: int, lines: Sequence[LineItem],
                    channel: Optional[str] = None) -> int:
    """单券对当前购物车的抵扣金额（分）。不可用返回 0。

    注意：门槛按传入的 base_cents 判定。调用方需决定 base 是原价还是已折后价，
    默认按已折后价（多数实际规则如此），除非券明确标注 threshold_base="original"。
    """
    if base_cents <= 0:
        return 0
    if not coupon.is_usable():
        return 0
    if base_cents < coupon.threshold_cents:
        return 0
    if not _scope_match(coupon, lines):
        return 0
    if not _channel_ok(coupon, channel):
        return 0

    if coupon.type == "percent":
        cut = base_cents - int(base_cents * coupon.percent)
        return max(0, min(cut, base_cents))
    if coupon.type in ("cash", "delivery_fee", "combo"):
        return max(0, min(coupon.face_value_cents, base_cents))
    return 0


def best_coupon_combo(coupons: Sequence[Coupon], lines: Sequence[LineItem],
                      channel: Optional[str] = None) -> List[AppliedCoupon]:
    """在所有券中选出抵扣最多的合法组合（单券或 ≤MAX_STACK 张叠加）。"""
    original = sum(l.subtotal_cents for l in lines)
    if original <= 0:
        return []

    usable = [c for c in coupons if c.is_usable() and _channel_ok(c, channel)]
    if not usable:
        return []

    def total_of(combo: Sequence[AppliedCoupon]) -> int:
        return sum(a.discount_cents for a in combo)

    best: List[AppliedCoupon] = []
    best_sum = 0

    # 维度 1：单张最优
    for c in usable:
        d = coupon_discount(c, original, lines, channel)
        if d > best_sum:
            best_sum, best = d, [AppliedCoupon(c, d)]

    # 维度 2：可叠加券的贪心组合
    # 门槛按已折后金额判定 -> 低门槛券先用，避免高门槛券失效
    stackable = [c for c in usable if c.stackable]
    # 同类型折扣券不可叠加（percent 与 percent 互斥）
    greedy = sorted(stackable, key=lambda c: (c.threshold_cents, -c.face_value_cents))
    applied: List[AppliedCoupon] = []
    cur = original
    used_types: List[str] = []
    for c in greedy:
        if len(applied) >= MAX_STACK:
            break
        if c.type == "percent" and "percent" in used_types:
            continue
        d = coupon_discount(c, cur, lines, channel)
        if d <= 0:
            continue
        applied.append(AppliedCoupon(c, d))
        used_types.append(c.type)
        cur -= d
    if applied and total_of(applied) > best_sum:
        note = "已叠加" if len(applied) > 1 else "单券"
        for a in applied:
            a.note = note
        best, best_sum = applied, total_of(applied)
    elif best:
        best[0].note = "单券最优"

    # 不变量：总抵扣不得超过原价
    if best_sum > original:
        best = [AppliedCoupon(best[0].coupon, original, "封顶")]
        best_sum = original
    return best


def filter_out_of_window(coupons: Sequence[Coupon], meal_time: Optional[str]) -> Tuple[List[Coupon], List[Coupon]]:
    """按时段过滤券。返回 (可用, 被剔除)。

    meal_time 形如 '早餐' / '午餐' / '下午茶' / '晚餐' / '夜宵'，
    与券的 valid_hours 做区间重叠判断；无法判断时保守放行。
    """
    if not meal_time:
        return list(coupons), []
    bands = {
        "早餐": (6, 10), "午餐": (10, 15), "下午茶": (14, 18),
        "晚餐": (17, 21), "夜宵": (21, 24),
    }
    band = bands.get(meal_time)
    if not band:
        return list(coupons), []
    keep, drop = [], []
    for c in coupons:
        if not c.is_usable():
            drop.append(c)
            continue
        vh = c.valid_hours or ""
        m = None
        import re as _re
        mm = _re.search(r"(\d{1,2})[:：]?\s*[-~至]\s*(\d{1,2})", str(vh))
        if mm:
            m = (int(mm.group(1)), int(mm.group(2)))
        if m is None:
            keep.append(c)          # 时段未知，保守放行
            continue
        if m[0] < band[1] and m[1] > band[0]:
            keep.append(c)
        else:
            drop.append(c)
    return keep, drop


# --------------------------------------------------------------------------
# 候选组合生成
# --------------------------------------------------------------------------
def _taste_ok(item: MenuItem, taste_tags: Sequence[str]) -> bool:
    """口味过滤：用户明确说「不吃X」时，排除带 X 标签的项。"""
    if not taste_tags:
        return True
    blob = " ".join(filter(None, [
        item.name or "", item.category or "", " ".join(item.tags or []),
        " ".join(str(c.get("name", "")) for c in (item.combo_items or [])),
    ]))
    for t in taste_tags:
        neg = t.lstrip("不无别忌")
        if neg and neg in blob:
            return False
    return True


def generate_candidates(menu: Sequence[MenuItem], intent: Intent,
                        limit: int = 400) -> List[List[LineItem]]:
    """生成候选购物车：套餐组合 + 单品组合。"""
    n = max(1, min(intent.party_size, 12))
    usable = [i for i in menu if i.price_complete and i.price_cents > 0
              and _taste_ok(i, intent.taste_tags)]
    combos = [i for i in usable if i.is_combo]
    singles = [i for i in usable if not i.is_combo]

    cands: List[List[LineItem]] = []

    # 1) 纯套餐组合：1~n 个套餐混搭
    for k in range(1, n + 1):
        if not combos:
            break
        stack: List[MenuItem] = []
        for _ in range(k):
            stack.append(combos[_ % len(combos)])
        cands.append([LineItem(it, 1) for it in stack])
        if len(combos) > 1 and k < n:
            # 换一种套餐组合
            alt = [combos[(i + k) % len(combos)] for i in range(k)]
            cands.append([LineItem(it, 1) for it in alt])

    # 2) 单个套餐 + 单品补齐（避免与套餐内容重复：不再叠加同类汉堡）
    if combos and singles:
        for c in combos:
            cands.append([LineItem(c, 1)])
            if n >= 2:
                # 只用饮料/小食类单品补齐，不重复点主食
                sides = [s for s in singles
                         if (s.category or "") in ("饮料", "小食", "甜品")]
                if sides:
                    cands.append([LineItem(c, 1), LineItem(sides[0], n - 1)])
        # 1 套餐 + n-1 个单份主食（不同款）
        mains_single = [s for s in singles
                        if (s.category or "") not in ("饮料", "小食", "甜品")]
        if mains_single and n >= 2:
            cands.append([LineItem(combos[0], 1)]
                         + [LineItem(mains_single[i % len(mains_single)], 1)
                            for i in range(n - 1)])

    # 3) 纯单品组合：一人一份，或全部同款
    if singles:
        # 3a) 每人一份主食（轮转覆盖不同单品）
        mains_pool = [s for s in singles
                      if (s.category or "") not in ("饮料", "小食", "甜品", "汤")]
        rot_src = mains_pool or singles
        rot = [LineItem(rot_src[i % len(rot_src)], 1) for i in range(n)]
        cands.append(_merge(rot))

        # 3b) 单品 × n
        for s in singles[:6]:
            cands.append([LineItem(s, n)])

        # 3c) 主食 + 饮品 + 小食（最贴近真实点单结构）
        drink = [s for s in singles if (s.category or "") in ("饮料", "汤")]
        snack = [s for s in singles if (s.category or "") in ("小食", "甜品")]
        if rot_src:
            base = [LineItem(rot_src[i % len(rot_src)], 1) for i in range(n)]
            if drink:
                base.append(LineItem(drink[0], n))
            if snack:
                base.append(LineItem(snack[0], n))
            cands.append(_merge(base))
            # 3d) 主食 + 饮品（不加小食，控制份量）
            if drink:
                cands.append(_merge([LineItem(rot_src[i % len(rot_src)], 1)
                                    for i in range(n)] + [LineItem(drink[0], n)]))
        # 3e) 套餐 + 饮品（单点套餐通常不含饮料，或想加饮料）
        if drink:
            for c in combos[:3]:
                cands.append([LineItem(c, 1), LineItem(drink[0], n)])

    # 去重 + 过滤明显不合理的方案
    seen, out = set(), []
    for c in cands:
        key = tuple(sorted((l.item.item_id or l.item.name or "", l.qty) for l in c))
        if key in seen or not c:
            continue
        seen.add(key)
        total = sum(l.subtotal_cents for l in c)
        if total <= 0:
            continue
        # 剔除明显超量（waste_risk=2）与吃不饱的方案，避免无意义推荐
        if _waste_risk(c, n) >= 2 or _is_underfed(c, n):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _merge(lines: Sequence[LineItem]) -> List[LineItem]:
    """合并相同 item 的数量。"""
    agg: Dict[Any, LineItem] = {}
    order: List[Any] = []
    for l in lines:
        k = l.item.item_id or l.item.name
        if k in agg:
            agg[k].qty += l.qty
        else:
            agg[k] = LineItem(l.item, l.qty)
            order.append(k)
    return [agg[k] for k in order]


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------
def _waste_risk(lines: Sequence[LineItem], party_size: int) -> int:
    """吃不完风险：0 恰好 / 1 偏多 / 2 明显超量。

    同时看两个维度：
      - 主餐份量（汉堡/套餐/炸鸡）是否超过人数
      - 总件数是否明显超过人数（3 人点 9 件即使全是小食也吃不完）
    """
    n = max(party_size, 1)
    main_cats = ("汉堡", "主餐", "套餐", "炸鸡")
    mains = sum(l.qty for l in lines
                if (l.item.category or "") in main_cats or l.item.is_combo)
    total_pieces = sum(l.qty for l in lines)

    main_ratio = mains / n
    piece_ratio = total_pieces / (n * 2.0)     # 每人 2 件为基准

    if main_ratio <= 1.2 and piece_ratio <= 1.2:
        return 0
    if main_ratio <= 1.5 and piece_ratio <= 1.5:
        return 1
    return 2


def _is_underfed(lines: Sequence[LineItem], party_size: int) -> bool:
    """是否明显吃不饱（没有任何主餐类）。

    用于避免「极致省钱」退化成只买一杯饮料这种无意义方案。
    """
    n = max(party_size, 1)
    mains = sum(l.qty for l in lines
                if (l.item.category or "") in ("汉堡", "主餐", "套餐", "炸鸡") or l.item.is_combo)
    return mains < n


def score_plan(original: int, payable: int, budget: Optional[int],
               is_combo: bool, waste_risk: int, underfed: bool = False) -> float:
    if original <= 0:
        return 0.0
    saving_rate = (original - payable) / original

    if budget:
        if payable > budget:
            penalty = (payable - budget) / budget * 100.0
        elif payable >= budget * BUDGET_SATISFIED_LOW:
            penalty = 0.0
        else:
            penalty = (budget * BUDGET_SATISFIED_LOW - payable) / budget * 20.0
    else:
        penalty = 0.0

    waste_penalty = {0: 0.0, 1: 3.0, 2: 10.0}.get(waste_risk, 0.0)
    # 吃不饱重罚：再便宜也不能只买饮料，省钱的前提是能吃饱
    underfed_penalty = 25.0 if underfed else 0.0

    return (saving_rate * 100.0 - penalty - waste_penalty - underfed_penalty
            + (2.0 if is_combo else 0.0))


def evaluate(lines: Sequence[LineItem], coupons: Sequence[Coupon],
             intent: Intent, strategy: str) -> Plan:
    """把一个候选购物车算成完整方案。

    金额基准（对齐 mcd-mcp 真实数据）：
      - original_cents  = 划线价合计（originalPrice），即真正原价
      - list_price      = 现价合计（currentPrice），用户实际看到的价格
      - 内置优惠        = 划线价 - 现价（促销优惠/随单购麦金卡价等）
      - 券抵扣基于现价计算（券作用在当前购物车金额上）
    """
    list_price = sum(l.subtotal_cents for l in lines)
    # 划线价缺失时退化为现价（即视为无折扣）
    original = sum(((l.item.original_price_cents if l.item.original_price_cents is not None
                     else l.item.price_cents) or 0) * l.qty for l in lines)
    built_in = sum(l.item.built_in_discount_cents * l.qty for l in lines)

    applied = best_coupon_combo(coupons, lines, intent.channel_pref)
    coupon_disc = sum(a.discount_cents for a in applied)
    payable = max(0, list_price - coupon_disc)

    waste = _waste_risk(lines, intent.party_size)
    underfed = _is_underfed(lines, intent.party_size)
    is_combo = any(l.item.is_combo for l in lines)
    # 节省率以划线价为基准，这样内置促销与券的贡献都能体现
    sc = score_plan(original or list_price, payable, intent.budget_cents,
                    is_combo, waste, underfed)

    data_ok = all(l.item.price_complete for l in lines)
    warnings: List[str] = []
    if not data_ok:
        warnings.append("部分单品价格未获取到，本方案数据不完整，仅供参考")

    if intent.budget_cents and payable > intent.budget_cents:
        warnings.append(f"超出预算 {fmt_cents(payable - intent.budget_cents)}")

    # 随单购提醒：该商品现价已是随单购到手价，下单时必须同步选随单购
    wo = [l for l in lines if l.item.can_with_order]
    if wo:
        warnings.append("含随单购商品，下单时需同步选择随单购卡，否则按原价购买")

    return Plan(
        strategy=strategy,
        title="",
        lines=list(lines),
        applied=applied,
        original_cents=original,
        built_in_discount_cents=built_in,
        coupon_discount_cents=coupon_disc,
        payable_cents=payable,
        score=sc,
        waste_risk=waste,
        data_complete=data_ok,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 凑单引擎
# --------------------------------------------------------------------------
TOPUP_POOL = [
    {"item_id": "TOPUP_FRIES", "name": "中薯条", "price_cents": 900},
    {"item_id": "TOPUP_COKE", "name": "中可乐", "price_cents": 700},
    {"item_id": "TOPUP_COFFEE", "name": "中冰咖啡", "price_cents": 1000},
    {"item_id": "TOPUP_SHAKE", "name": "中奶昔", "price_cents": 1200},
]


def suggest_topup(lines: Sequence[LineItem], coupons: Sequence[Coupon],
                  budget: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """当前金额够不到某张券门槛时，判断补单是否划算。

    仅当「券抵扣 > 加购成本」才建议凑单。
    """
    cur = sum(l.subtotal_cents for l in lines)
    best: Optional[Dict[str, Any]] = None

    for c in coupons:
        if not c.is_usable() or c.threshold_cents <= 0:
            continue
        if c.type == "percent":
            continue          # 折扣券无门槛概念，凑单无意义
        gap = c.threshold_cents - cur
        if gap <= 0:
            continue
        for opt in TOPUP_POOL:
            price = opt["price_cents"]
            if price < gap or price > MAX_TOPUP_CENTS:
                continue
            if budget is not None and cur + price > budget:
                continue
            # 补单后能省多少：券面额 - 加购成本
            gain = c.face_value_cents - price
            if gain <= 0:
                continue
            cand = {
                "coupon_title": c.title,
                "add": opt["name"],
                "add_cents": price,
                "gain_cents": gain,
                "detail": (f"加购{opt['name']} {fmt_cents(price)}，"
                           f"触发「{c.title}」减 {fmt_cents(c.face_value_cents)}，"
                           f"净省 {fmt_cents(gain)}"),
            }
            if best is None or cand["gain_cents"] > best["gain_cents"]:
                best = cand
    return best


def to_calc_items(plan: Plan) -> List[Dict[str, Any]]:
    """把方案转成 calculate-price 的 items 入参。

    券只能挂在**其适用范围命中**的商品上：
      - scope=all 的券挂在金额最大的商品（避免重复占用）
      - scope=item:<code> 的券只挂在对应商品
    每张券只用一次（用 set 去重），防止同一券码被挂到多个商品导致服务端报错。
    """
    used = {}
    for a in plan.applied:
        if a.discount_cents > 0 and a.coupon.coupon_id:
            used[a.coupon.coupon_id] = a.coupon

    out: List[Dict[str, Any]] = []
    for l in plan.lines:
        if not l.item.item_id:
            continue
        out.append({"productCode": l.item.item_id, "quantity": l.qty})

    # 按适用范围挂券，每张券只挂一次
    for cid, c in used.items():
        targets: set = set()
        if c.scope.startswith("item:"):
            targets = {t.strip() for t in c.scope.split(":", 1)[1].split(",") if t.strip()}
            # 券里的商品不在当前购物车，则跳过该券
            if not targets & {i["productCode"] for i in out}:
                continue
        # 优先挂到适用商品；否则（scope=all）挂金额最大的商品
        hit = next((i for i in out if i["productCode"] in targets), None) if targets else None
        if hit is None and not targets:
            hit = max(out, key=lambda i: i["quantity"], default=None) if out else None
        if hit is None:
            hit = out[0] if out else None
        if hit is None:
            continue
        hit["couponId"] = c.coupon_id
        if c.coupon_codes:
            hit["couponCode"] = c.coupon_codes[0]
    return out


def verify_with_calculate_price(client, store_code: str, plan: Plan,
                                be_type: int = 1, be_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """用 calculate-price 做权威试算，返回权威金额。

    这是**唯一可信的价格来源**：菜单的 currentPrice 只是标价，
    真实应付价要由服务端计算（可能含额外促销、随单购、配送费等）。
    返回 None 表示该方案未验证。
    """
    if not client.probe().bindings.get("order_price_preview"):
        return None
    items = to_calc_items(plan)
    if not items:
        return None
    try:
        data = client.preview_price(store_code, items, be_type=be_type, be_code=be_code)
    except McdError:
        return None
    if not data:
        return None

    price = pick_int(data, "price")
    product_price = pick_int(data, "product_price")
    original = pick_int(data, "original_price")
    discount = pick_int(data, "discount")
    if price is None:
        return None
    plan.verified = True

    # 服务端口径（实测 mcd-mcp calculate-price）：
    #   productPrice  = 商品现价合计
    #   originalPrice = 商品划线价合计
    #   discount      = originalPrice - productPrice  （仅商品侧优惠：内置促销 + 券抵扣）
    #   price         = 实付（含运费 / 餐具费）
    # 所以「共省」以 originalPrice - price 为准，可能大于商品侧 discount（差值即运费）。
    plan.payable_cents = price
    if original is not None:
        plan.original_cents = original

    if discount is not None:
        goods_discount = discount
    elif original is not None and product_price is not None:
        goods_discount = max(0, original - product_price)
    else:
        goods_discount = plan.discount_cents

    # 券抵扣不得超过服务端给出的商品侧优惠总额；剩余归为内置促销
    coupon_part = min(plan.coupon_discount_cents, goods_discount)
    plan.coupon_discount_cents = coupon_part
    plan.built_in_discount_cents = max(0, goods_discount - coupon_part)

    # 自洽性保证：共省 = 划线价 - 实付（含运费），与明细拆分可能相差运费额
    server_saved = (original - price) if original is not None else None
    return {
        "authoritative_price_cents": price,
        "authoritative_original_cents": original,
        "server_product_price_cents": product_price,
        "server_goods_discount_cents": goods_discount,
        "server_saved_cents": server_saved,
        "delivery_price_cents": pick_int(data, "delivery_price"),
        "tableware_price_cents": pick_int(data, "tableware_price"),
    }


def pick_int(d: Dict[str, Any], *names: str) -> Optional[int]:
    """从返回中取整数分值字段。"""
    for n in names:
        if n in d and d[n] is not None:
            try:
                return int(d[n])
            except (TypeError, ValueError):
                return None
    return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def recommend(menu: Sequence[MenuItem], coupons: Sequence[Coupon],
              intent: Intent, top_n: int = 3) -> Dict[str, Any]:
    """产出 Top-N 方案，覆盖三种策略。"""
    usable, dropped_window = filter_out_of_window(coupons, intent.meal_time)
    candidates = generate_candidates(menu, intent)

    if not candidates:
        return {
            "ok": False,
            "code": "F3",
            "message": "没有满足条件的可点商品（可能全部缺价或被口味过滤）",
            "intent": intent.to_dict(),
        }

    plans: List[Plan] = [evaluate(c, usable, intent, "cand") for c in candidates]
    plans.sort(key=lambda p: p.score, reverse=True)

    picked: List[Plan] = []
    used_sig: List[str] = []

    def sig(p: Plan) -> str:
        return ",".join(sorted((l.item.item_id or l.item.name or "") for l in p.lines))

    def take(pred, *, strict: bool = True) -> Optional[Plan]:
        """挑第一个满足 pred 且未重复的方案。strict=False 时放宽条件。"""
        for p in plans:
            if p in picked or sig(p) in used_sig:
                continue
            if pred(p):
                picked.append(p)
                used_sig.append(sig(p))
                return p
        if strict:
            for p in plans:
                if p in picked or sig(p) in used_sig:
                    continue
                picked.append(p)
                used_sig.append(sig(p))
                return p
        return None

    # 策略 A：极致省钱（人均最低，但不超量、不吃不饱）
    a = take(lambda p: p.waste_risk == 0)
    if a:
        a.strategy, a.title = "cheapest", "极致省钱"
    # 策略 B：均衡之选（默认推荐）——有实质优惠 + 不浪费
    b = take(lambda p: p.saving_rate >= 0.10 and p.waste_risk == 0)
    if b:
        b.strategy, b.title = "balanced", "均衡之选"
    # 策略 C：预算贴合
    if intent.budget_cents:
        c = take(lambda p: p.payable_cents <= intent.budget_cents and p.waste_risk == 0)
    else:
        c = take(lambda p: p.waste_risk == 0)
    if c:
        c.strategy, c.title = "budget_fit", "预算贴合"

    # 不足 top_n 时，用剩余高分方案补齐
    for p in plans:
        if len(picked) >= top_n:
            break
        if p not in picked and sig(p) not in used_sig:
            p.strategy, p.title = "alt", "备选方案"
            picked.append(p)
            used_sig.append(sig(p))

    picked = picked[:max(top_n, 1)]

    # 凑单建议挂到主推方案上
    if picked:
        main = next((p for p in picked if p.strategy == "balanced"), picked[0])
        main.topup_hint = suggest_topup(main.lines, usable, intent.budget_cents)

    # 超预算判定
    min_payable = min((p.payable_cents for p in picked), default=0)
    over_budget = bool(intent.budget_cents and min_payable > intent.budget_cents)

    return {
        "ok": True,
        "intent": intent.to_dict(),
        "plans": [p.to_dict(intent.party_size) for p in picked],
        "_plan_objs": picked,          # 供权威试算直接复用，避免 to_dict 后信息丢失
        "usable_coupons": [{"title": c.title, "type": c.type,
                            "face_value_cents": c.face_value_cents,
                            "threshold_cents": c.threshold_cents,
                            "percent": c.percent, "stackable": c.stackable,
                            "expires_at": c.expires_at.isoformat() if c.expires_at else None}
                           for c in usable],
        "dropped_coupons": [{"title": c.title, "reason": c.unusable_reason or "时段不匹配"}
                            for c in dropped_window],
        "over_budget": over_budget,
        "min_payable_cents": min_payable,
        "data_complete": all(p.data_complete for p in picked),
    }


def run(client: McdClient, intent: Intent) -> Dict[str, Any]:
    """端到端：探店 -> 拉菜单 -> 拉券 -> 推荐 -> 权威试算。

    对齐 mcd-mcp 真实契约：门店用 storeCode/beCode，菜单与试算需 orderType+beType。
    """
    try:
        rep = client.probe()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "F1", "message": f"能力探测失败：{e}"}

    if rep.level == "L0_NO_MCP":
        return {
            "ok": False, "code": "F1",
            "message": "未检测到麦当劳 MCP 通道，无法获取实时菜单与优惠券。",
            "capability": rep.to_dict(),
            "hint": "在连接器管理中绑定麦当劳连接器（mcd-mcp），或在 ~/.workbuddy/mcp.json 配置后点「信任」。",
        }

    be_type = resolve_be_type(None, intent.channel_pref)
    order_type = BE_TYPE_TO_ORDER_TYPE[be_type]

    # 门店由调用方传入，或先用 find_stores 定位
    if intent.store is None:
        return {"ok": False, "code": "F2",
                "message": "未确定门店，无法拉取该店菜单（价格随门店而异）。"}

    store_code = intent.store.store_code or intent.store.store_id
    be_code = intent.store.be_code

    try:
        menu = client.get_menu(store_code, be_type=be_type, be_code=be_code,
                               order_type=order_type)
    except McdError as e:
        return {"ok": False, "code": e.code, "message": e.message,
                "store": intent.store.name}

    coupons: List[Coupon] = []
    try:
        coupons = client.get_coupons(store_code, be_type=be_type, be_code=be_code,
                                     channel=intent.channel_pref, order_type=order_type)
    except McdError:
        coupons = []

    result = recommend(menu, coupons, intent)

    # 权威试算主推方案（直接复用 Plan 对象，避免 to_dict 往返丢失券信息）
    plan_objs: List[Plan] = result.get("_plan_objs") or []
    if result.get("ok") and plan_objs and rep.bindings.get("order_price_preview"):
        main_obj = next((p for p in plan_objs if p.strategy == "balanced"),
                        plan_objs[0])
        v = verify_with_calculate_price(client, store_code, main_obj, be_type, be_code)
        if v:
            idx = next((i for i, p in enumerate(plan_objs) if p is main_obj), 0)
            result["plans"][idx] = main_obj.to_dict(intent.party_size)
            result["plans"][idx]["verification"] = v

    # 内部字段不对外暴露
    result.pop("_plan_objs", None)

    result["store"] = {"name": intent.store.name, "address": intent.store.address,
                       "store_code": store_code, "be_code": be_code,
                       "be_type": be_type, "order_type": order_type,
                       "supports_reservation": intent.store.supports_reservation,
                       "reservation_slots": intent.store.reservation_slots}
    result["capability"] = rep.to_dict()
    return result




# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render(result: Dict[str, Any]) -> str:
    """把 recommend() 的结果渲染成可直接展示的文本。"""
    L: List[str] = []
    if not result.get("ok"):
        L.append(f"⚠️ {result.get('message', '未知错误')}")
        if result.get("hint"):
            L.append(f"\n{result['hint']}")
        return "\n".join(L)

    it = result["intent"]
    store = result.get("store") or {}
    if store.get("name"):
        L.append(f"📍 {store['name']}" + (f" · {store['address']}" if store.get("address") else ""))
    head = f"👥 {it['party_size']} 人"
    if it.get("budget_cents"):
        head += f" · 预算 {fmt_cents(it['budget_cents'])}"
    if it.get("meal_time"):
        head += f" · {it['meal_time']}"
    if it.get("taste_tags"):
        head += f" · 口味: {','.join(it['taste_tags'])}"
    L.append(head)

    dropped = result.get("dropped_coupons") or []
    if dropped:
        L.append(f"\n🎫 已剔除 {len(dropped)} 张不可用券：")
        for d in dropped:
            L.append(f"   - {d['title']}（{d['reason']}）")

    L.append("")
    for i, p in enumerate(result["plans"]):
        star = "★ 推荐" if p["strategy"] == "balanced" else ""
        L.append(f"【{chr(65+i)} · {p['title']}】{star}")
        for it2 in p["items"]:
            qty = f" × {it2['qty']}" if it2["qty"] > 1 else ""
            orig = it2.get("original_cents")
            # 划线价与现价不一致时标注，方便用户看清内置优惠
            tail = ""
            if orig and it2.get("list_cents") and orig != it2["list_cents"]:
                tail = f"  (划线 {fmt_cents(orig)}，省 {fmt_cents(orig - it2['list_cents'])})"
            wo = "  [随单购]" if it2.get("can_with_order") else ""
            L.append(f"  · {it2['name']}{qty}  {fmt_cents(it2['subtotal_cents'])}{tail}{wo}")

        L.append(f"  划线价合计      {fmt_cents(p['original_cents'])}")
        if p.get("built_in_discount_cents"):
            L.append(f"  ├ 商品促销/随单购 -{fmt_cents(p['built_in_discount_cents'])}")
        for c in p["coupons"]:
            L.append(f"  ├ {c['title']}  -{fmt_cents(c['discount_cents'])}  ({c['note']})")
        if not p.get("built_in_discount_cents") and not p["coupons"]:
            L.append(f"  （无可用优惠）")
        L.append(f"  实付            {fmt_cents(p['payable_cents'])}")
        v = p.get("verification") or {}
        saved = p["discount_cents"]
        if p.get("verified") and v and v.get("server_saved_cents") is not None:
            saved = v["server_saved_cents"]      # 共省 = 划线价 - 实付（含运费）
            extra = (v.get("delivery_price_cents") or 0) + (v.get("tableware_price_cents") or 0)
            if extra:
                L.append(f"  ├ 含配送/餐具费  {fmt_cents(extra)}")
            L.append(f"  ✅ 官方 calculate-price 权威价：{fmt_cents(v.get('authoritative_price_cents'))}")
        rate = (saved / p["original_cents"]) if p.get("original_cents") else 0
        L.append(f"  人均            {fmt_cents(p['per_head_cents'])}"
                 f"   共省 {fmt_cents(saved)}（{rate*100:.1f}%）")
        if p.get("topup_hint"):
            L.append(f"  💡 {p['topup_hint']['detail']}")
        for w in p.get("warnings") or []:
            L.append(f"  ⚠️ {w}")
        L.append("")
        if p.get("topup_hint"):
            L.append(f"  💡 {p['topup_hint']['detail']}")
        for w in p.get("warnings") or []:
            L.append(f"  ⚠️ {w}")
        L.append("")

    if result.get("over_budget"):
        L.append(f"💰 最低可行价 {fmt_cents(result['min_payable_cents'])}，已超预算。"
                 f"可选择提预算或放宽口味约束。")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------
def _demo() -> int:
    from mcd_client import mock_invoke, mock_probe, MOCK_MENU, MOCK_COUPONS
    from mcd_client import normalize_menu_item, normalize_coupon, extract_list

    client = McdClient(invoker=mock_invoke, probe_fn=mock_probe)
    store = client.find_stores(latitude=30.5928, longitude=114.3055)[0]
    menu = client.get_menu(store.store_id or "")
    coupons = [normalize_coupon(x) for x in
               extract_list(MOCK_COUPONS, "coupons", "list")]

    scenarios = [
        ("3 人 / 80 元 / 不吃辣", Intent(3, 8000, ["辣"], "午餐", "堂食", store)),
        ("1 人 / 无预算", Intent(1, None, [], None, "堂食", store)),
        ("2 人 / 50 元", Intent(2, 5000, [], None, "外送", store)),
    ]
    for label, intent in scenarios:
        print("=" * 62)
        print(f"场景：{label}")
        print("=" * 62)
        print(render(recommend(menu, coupons, intent)))
        print()
    return 0


def _probe_cli() -> int:
    c = McdClient()
    rep = c.probe(force=True)
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="麦当劳省钱助手 - 优惠计算引擎")
    ap.add_argument("--demo", action="store_true", help="用内置 mock 数据演示")
    ap.add_argument("--probe", action="store_true", help="探测 MCP 能力")
    args = ap.parse_args()
    if args.probe:
        sys.exit(_probe_cli())
    else:
        sys.exit(_demo())