"""
mcd_client.py — 麦当劳 MCP 适配层

职责：
1. 能力探测（probe）：扫描真实可用的 MCP 工具，映射到内部语义键
2. 参数适配（shim）：不同 MCP 的参数命名收敛
3. 字段收敛（normalize）：统一内部数据结构，金额统一为「分」
4. 错误分类：把底层异常映射为 F1~F11 兜底代码

设计原则：
- 不依赖任何具体 MCP 工具名，全部通过别名表 + 运行时探测
- 无 MCP 时抛出 McdUnavailable，由调用方走 fallback.md 的 F1 分支
- 金额一律 int(分)，杜绝浮点误差

CLI 自测：
    python mcd_client.py --probe          # 探测当前环境能力
    python mcd_client.py --self-test      # 用内置 mock 数据跑通归一化
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 兜底错误码（与 references/fallback.md 一一对应）
# --------------------------------------------------------------------------
F_NO_MCP = "F1"
F_NO_STORE = "F2"
F_NO_MENU = "F3"
F_NO_COUPON = "F4"
F_COUPON_SOLD_OUT = "F5"
F_PROMO_EXPIRED = "F6"
F_CLAIM_FAILED = "F7"
F_STACK_REJECTED = "F8"
F_OVER_BUDGET = "F9"
F_TIME_CONFLICT = "F10"
F_FIELD_MISSING = "F11"


class McdError(Exception):
    """带兜底代码的业务异常。"""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class McdUnavailable(McdError):
    """MCP 通道完全不可用。"""

    def __init__(self, message: str = "未检测到麦当劳 MCP 通道"):
        super().__init__(F_NO_MCP, message)


# --------------------------------------------------------------------------
# 别名表：语义键 -> 候选工具名（按优先级排列，先精确后模糊）
# 增删别名只改这里，不动任何逻辑代码
# --------------------------------------------------------------------------
CAPABILITY_ALIASES: Dict[str, List[str]] = {
    "store_locate": [
        # 真实：到店场景（必传 beType；searchType=2 时必填 city+keyword）
        "mcp__mcd-mcp__query-nearby-stores",
        "query_nearby_stores",
        # 真实：外送场景（需先经 delivery-query-address 拿 addressId）
        "mcp__mcd-mcp__delivery-query-stores",
        "delivery_query_stores",
        # 兼容第三方封装
        "mcp__mcdonalds__find_store",
        "find_store",
        "search_nearby_stores",
        "get_store_by_location",
    ],
    "menu_browse": [
        "mcp__mcd-mcp__query-meals",            # 真实：必传 storeCode/orderType/beType
        "query_meals",
        "mcp__mcd__get_menu",
        "get_menu",
        "list_menu_items",
    ],
    "coupon_available": [
        # 真实：按门店+订单类型查可用券（推荐，口径最准）
        "mcp__mcd-mcp__query-store-coupons",
        "query_store_coupons",
        # 真实：全国可领券列表（不含门店校验，返回 Markdown 文本）
        "mcp__mcd-mcp__available-coupons",
        "available_coupons",
        "get_available_coupons",
        "list_available_coupons",
    ],
    "coupon_claim": [
        "mcp__mcd-mcp__auto-bind-coupons",      # 真实：一键领全部（无需 couponId）
        "auto_bind_coupons",
        "mcp__mcdonalds__claim_coupon",
        "claim_coupon",
    ],
    "coupon_owned": [
        "mcp__mcd-mcp__query-my-coupons",      # 真实：卡包（注意：不校验门店可用性）
        "query_my_coupons",
        "list_my_coupons",
        "get_my_coupons",
        "user_coupon_wallet",
        "my_coupons",
    ],
    "combo_inspect": [
        "mcp__mcd-mcp__query-meal-detail",     # 真实：餐品详情/套餐组成
        "query_meal_detail",
        "mcp__mcdonalds__get_combo",
        "get_combo",
        "get_combo_detail",
        "list_combos",
        "combo_detail",
        "combo_inspect",
    ],
    "order_price_preview": [
        "mcp__mcd-mcp__calculate-price",       # 真实：金额单位为「分」
        "calculate_price",
        "mcp__mcdonalds__preview_order",
        "calc_order_price",
        "estimate_order_total",
    ],
    "promo_activity": [
        "mcp__mcd-mcp__campaign-calendar",     # 真实：营销活动日历（全国，非门店级）
        "campaign_calendar",
        "mcp__mcd-mcp__query-promotions",      # 真实：仅企业团餐场景(beType=6)
        "query_promotions",
        "mcp__mcdonalds__list_promotions",
        "get_active_promotions",
        "list_activities",
        "list_promotions",
    ],
}

# 最低可用能力集：缺任一则整体不可用
REQUIRED_CAPS = ("store_locate", "menu_browse")

# --------------------------------------------------------------------------
# beType / orderType 业务类型常量（mcd-mcp 强制要求，必传）
# --------------------------------------------------------------------------
BE_TYPE_PICKUP = 1        # 到店自取
BE_TYPE_DELIVERY = 2      # 麦乐送到家
BE_TYPE_DRIVE_THRU = 5    # 得来速车道取餐
BE_TYPE_CATERING = 6      # 企业团餐

ORDER_TYPE_IN_STORE = 1   # 到店（含自取 + 得来速）
ORDER_TYPE_DELIVERY = 2   # 外送（含麦乐送 + 团餐）

BE_TYPE_TO_ORDER_TYPE = {
    BE_TYPE_PICKUP: ORDER_TYPE_IN_STORE,
    BE_TYPE_DRIVE_THRU: ORDER_TYPE_IN_STORE,
    BE_TYPE_DELIVERY: ORDER_TYPE_DELIVERY,
    BE_TYPE_CATERING: ORDER_TYPE_DELIVERY,
}


def resolve_be_type(be_type: Optional[int], channel: Optional[str] = None) -> int:
    """把用户口语化的渠道偏好映射为 beType 枚举。默认到店自取。"""
    if be_type in BE_TYPE_TO_ORDER_TYPE:
        return be_type
    if channel:
        c = channel.strip().lower()
        if any(k in c for k in ("外送", "外带", "配送", "送", "delivery")):
            return BE_TYPE_DELIVERY
        if any(k in c for k in ("得来速", "车道", "drive", "免下车")):
            return BE_TYPE_DRIVE_THRU
        if any(k in c for k in ("团餐", "企业", "会议")):
            return BE_TYPE_CATERING
    return BE_TYPE_PICKUP


# --------------------------------------------------------------------------
# 工具别名归一
# --------------------------------------------------------------------------
def _norm_key(k: str) -> str:
    """把 key 归一成小写去下划线，便于模糊匹配。"""
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _camel_to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def normalize_field_map(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把任意命名风格（snake / camel / 中文）的字典统一成 snake_case key。

    同时保留原 key 作为别名入口：`_alias` 字段存 {原key: 规范key}。
    """
    out: Dict[str, Any] = {}
    alias: Dict[str, str] = {}
    for k, v in raw.items():
        nk = _camel_to_snake(str(k))
        out[nk] = v
        if nk != str(k):
            alias[str(k)] = nk
    if alias:
        out["_alias"] = alias
    return out


def pick(d: Dict[str, Any], *names: str, default: Any = None) -> Any:
    """按候选名依次取值，支持 snake/camel/大小写混排。"""
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d:
            return d[n]
    normed = {_norm_key(k): v for k, v in d.items() if k != "_alias"}
    for n in names:
        nk = _norm_key(n)
        if nk in normed:
            return normed[nk]
    return default


def to_cents(value: Any, *, already_cents: bool = False, default: Any = None) -> Optional[int]:
    """金额统一为「分」。

    处理三种输入：
      - already_cents=True：数值本身即分
      - int/float：视为「元」，×100
      - 字符串 "¥12.50" / "12.5元"：清洗后 ×100
    无法解析时返回 default（注意：显式传 0 会与「解析失败」混淆，
    调用方应自行用 `is None` 判断，而非依赖真值）。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            s = re.sub(r"[^\d.\-]", "", value)
            if not s:
                return default
            f = float(s)
        else:
            f = float(value)
    except (TypeError, ValueError):
        return default
    if already_cents:
        return int(round(f))
    return int(round(f * 100))


def parse_time(value: Any) -> Optional[datetime]:
    """宽松解析各种时间格式为 aware datetime（naive 视为 UTC+8）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_CST)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:      # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_CST)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    # Unix 秒/毫秒字符串
    if re.fullmatch(r"\d{10}|\d{13}", s):
        return parse_time(int(s))
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%Y%m%d%H%M%S", "%Y%m%d",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_CST)
        except ValueError:
            continue
    try:  # ISO8601
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=_CST)
    except ValueError:
        return None


_CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    return datetime.now(_CST)


def fmt_cents(c: Optional[int]) -> str:
    """分 -> 展示字符串。"""
    if c is None:
        return "未获取到"
    sign = "-" if c < 0 else ""
    c = abs(int(c))
    return f"{sign}¥{c // 100}.{c % 100:02d}"


def parse_json_loose(text: Any) -> Optional[Any]:
    """从混杂了说明文字的返回里提取 JSON。

    mcd-mcp 的返回常在 JSON 前后拼接自然语言描述（如字段说明表、输出格式要求），
    直接 json.loads 会失败。这里定位第一个平衡的 {...} 或 [...] 片段并解析。
    """
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass

    # 扫描所有可能的 JSON 起点，取第一个能成功解析的平衡片段
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        for i, ch in enumerate(s):
            if ch != start_ch:
                continue
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(s)):
                c = s[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == start_ch:
                    depth += 1
                elif c == end_ch:
                    depth -= 1
                    if depth == 0:
                        frag = s[i:j + 1]
                        try:
                            return json.loads(frag)
                        except (json.JSONDecodeError, TypeError):
                            break
    return None


def mcd_data(payload: Any) -> Optional[Any]:
    """提取 mcd-mcp 标准响应中的 data 字段，并校验 success 标志。"""
    obj = parse_json_loose(payload)
    if isinstance(obj, dict):
        if obj.get("success") is False:
            raise McdError(F_FIELD_MISSING,
                           f"接口返回失败：{obj.get('message', '未知错误')}",
                           {"code": obj.get("code"), "traceId": obj.get("traceId")})
        return obj.get("data", obj)
    return obj


# --------------------------------------------------------------------------
# 统一数据模型
# --------------------------------------------------------------------------
@dataclass
class Store:
    store_id: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    distance_m: Optional[int] = None
    open_hours: Optional[str] = None
    channels: List[str] = field(default_factory=list)
    # --- mcd-mcp 特有 ---
    store_code: Optional[str] = None       # 门店编码，后续工具入参
    be_code: Optional[str] = None          # 业务编码（得来速/外送必传）
    supports_reservation: bool = False
    reservation_slots: List[str] = field(default_factory=list)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open_likely(self) -> bool:
        """营业时间为自由文本时不做推断，返回 True 表示未知不阻塞。"""
        return True

    @property
    def needs_be_code(self) -> bool:
        """得来速/外送/团餐必须带 beCode。"""
        return bool(self.be_code)


@dataclass
class MenuItem:
    item_id: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    price_cents: Optional[int] = None
    is_combo: bool = False
    combo_items: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    available: bool = True
    # --- mcd-mcp 特有 ---
    original_price_cents: Optional[int] = None   # 划线价
    discount_type: Optional[str] = None          # 促销优惠 / 麦金卡优惠 / 早餐卡优惠
    can_with_order: bool = False                  # 是否可享随单购价
    with_order: Dict[str, Any] = field(default_factory=dict)  # 随单购卡参数
    image: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def price_complete(self) -> bool:
        return self.price_cents is not None

    @property
    def built_in_discount_cents(self) -> int:
        """商品自带的优惠额（划线价 - 现价），不依赖任何券。"""
        if self.original_price_cents is None or self.price_cents is None:
            return 0
        return max(0, self.original_price_cents - self.price_cents)

    @property
    def effective_price_cents(self) -> int:
        """实际可成交价：随单购商品优先取现价（现价已是随单购到手价）。"""
        return self.price_cents or 0


@dataclass
class Coupon:
    coupon_id: Optional[str] = None
    title: Optional[str] = None
    type: str = "cash"                 # cash / percent / delivery_fee / combo
    face_value_cents: int = 0
    percent: float = 1.0               # 0.85 == 85折
    threshold_cents: int = 0
    scope: str = "all"                 # all / category:<x> / item:<id>
    stackable: bool = False
    expires_at: Optional[datetime] = None
    claimed: bool = False
    remain: Optional[int] = None
    valid_hours: Optional[str] = None
    channel: Optional[str] = None
    # --- mcd-mcp 特有：一券多码、按名称匹配的适用范围 ---
    coupon_codes: List[str] = field(default_factory=list)
    product_names: List[str] = field(default_factory=list)
    product_codes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---- 状态判定 ----
    def is_expired(self, ref: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return (ref or now_cst()) > self.expires_at

    def is_sold_out(self) -> bool:
        return self.remain is not None and self.remain <= 0

    @property
    def has_amount_info(self) -> bool:
        """是否含可计算的金额信息。

        available-coupons 返回的券只有标题，没有面额/折扣，
        这类券只能用于提示「有券可领」，绝不能进入金额计算。
        """
        if self.type == "unknown":
            return False
        if self.type == "percent":
            return 0 < self.percent < 1
        return self.face_value_cents > 0

    def is_usable(self, ref: Optional[datetime] = None) -> bool:
        return (not self.claimed and not self.is_expired(ref)
                and not self.is_sold_out() and self.has_amount_info)

    @property
    def unusable_reason(self) -> Optional[str]:
        if self.claimed:
            return "已领取"
        if self.is_expired():
            return "已过期"
        if self.is_sold_out():
            return "已领完"
        if not self.has_amount_info:
            return "无金额信息（需领券后到门店查询）"
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return d


@dataclass
class CapabilityReport:
    """能力探测结果。"""
    bindings: Dict[str, Optional[str]] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    schemas: Dict[str, Any] = field(default_factory=dict)

    @property
    def level(self) -> str:
        """返回当前降级级别（fallback.md 第 3 节）。"""
        b = self.bindings
        has_core = all(b.get(c) for c in REQUIRED_CAPS)
        if not has_core:
            return "L0_NO_MCP"
        if not b.get("coupon_available"):
            return "L4_NO_COUPON"
        if not b.get("combo_inspect"):
            return "L3_ITEM_ONLY"
        if not b.get("coupon_claim"):
            return "L2_NO_CLAIM"
        return "L1_FULL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "bindings": self.bindings,
            "missing": self.missing,
        }


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def extract_list(payload: Any, *keys: str) -> List[Any]:
    """从各种包裹结构里挖出 list。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in list(payload.keys()) + [_camel_to_snake(k) for k in payload.keys()]:
            if _norm_key(k) in {_norm_key(x) for x in keys}:
                v = payload[k]
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    # 二次包裹，如 {"data": {"list": [...]}}
                    for kk, vv in v.items():
                        if isinstance(vv, list) and _norm_key(kk) in {"list", "items", "records", "rows", "content"}:
                            return vv
        # 通用：找第一个 list 字段
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in payload.values():
            if isinstance(v, dict):
                got = extract_list(v, *keys)
                if got:
                    return got
    return []


def normalize_store(raw: Any) -> Store:
    d = raw if isinstance(raw, dict) else {}
    d = normalize_field_map(d)
    dist = pick(d, "distance_m", "distance", "dist")
    dist_i: Optional[int] = None
    if dist is not None:
        try:
            dist_i = int(round(float(dist)))
            if _norm_key(pick(d, "distance_unit", default="")) in {"km", "kilometre", "kilometer"}:
                dist_i *= 1000
            elif 0 < float(dist) < 100 and float(dist) != int(float(dist)):
                dist_i = int(round(float(dist) * 1000))
        except (TypeError, ValueError):
            dist_i = None
    ch = pick(d, "channels", "service_types", "serviceTypes", "services", default=[])
    if isinstance(ch, str):
        ch = [c.strip() for c in re.split(r"[,，/|、]", ch) if c.strip()]

    # 预约时段：mcd-mcp 用 reservationTimeOptions（每个日期一条，含各时段文本）
    slots: List[str] = []
    rto = pick(d, "reservation_time_options", default=[]) or []
    if isinstance(rto, list):
        for opt in rto:
            if not isinstance(opt, dict):
                continue
            date = pick(opt, "date", default="")
            txt = pick(opt, "reservation_option_text", "reservationOptionText", default="")
            today = pick(opt, "today", default=False)
            if txt:
                prefix = f"{date} " + ("(今天) " if today else "")
                slots.append(prefix + str(txt).replace("\n", ""))

    res = pick(d, "reservation", "supports_reservation", "support_reservation", default=False)
    return Store(
        store_id=str(pick(d, "store_code", "storeCode", "store_id", "id", default="") or "") or None,
        store_code=str(pick(d, "store_code", "storeCode", default="") or "") or None,
        be_code=str(pick(d, "be_code", "beCode", default="") or "") or None,
        name=pick(d, "store_name", "storeName", "name", "title"),
        address=pick(d, "address", "addr"),
        distance_m=dist_i,
        open_hours=pick(d, "open_hours", "openHours", "business_hours", "businessHours"),
        channels=list(ch or []),
        supports_reservation=bool(res),
        reservation_slots=slots,
        latitude=pick(d, "latitude", "lat"),
        longitude=pick(d, "longitude", "lng", "lon"),
        raw=d,
    )


def normalize_menu_item(raw: Any, *, category: Optional[str] = None,
                       tags: Optional[List[str]] = None) -> MenuItem:
    d = raw if isinstance(raw, dict) else {}
    d = normalize_field_map(d)

    # mcd-mcp：currentPrice / originalPrice 为字符串元（如 "33.5"）
    price_raw = pick(d, "price_cents", "priceCents")
    if price_raw is not None:
        price = to_cents(price_raw, already_cents=True)
    else:
        price = to_cents(pick(d, "current_price", "currentPrice", "price", "sale_price"))
    orig = to_cents(pick(d, "original_price", "originalPrice"))

    combo = pick(d, "combo_items", "comboItems", "contents", "items", "includes", default=[])
    if isinstance(combo, dict):
        combo = [combo]
    tag_list = list(tags or [])
    extra_tags = pick(d, "tags", "labels", "label", default=[])
    if isinstance(extra_tags, str):
        extra_tags = [t.strip() for t in re.split(r"[,，/|、\s]+", extra_tags) if t.strip()]
    for t in (extra_tags or []):
        if t and t not in tag_list:
            tag_list.append(t)

    name = _clean_label(pick(d, "name", "item_name", "itemName", "product_name", "title"))
    is_combo = bool(pick(d, "is_combo", "isCombo", default=False)) or bool(combo) or \
        any(k in name for k in ("套餐", "三件套", "四件套", "五件套", "随心配", "餐")) or \
        "套餐" in tag_list

    avail = pick(d, "available", "is_available", "in_stock", default=True)
    return MenuItem(
        item_id=str(pick(d, "code", "product_code", "productCode", "item_id", "itemId", "id",
                         default="") or "") or None,
        name=name,
        category=category,
        price_cents=price,
        original_price_cents=orig,
        is_combo=is_combo,
        combo_items=list(combo or []),
        tags=tag_list,
        available=bool(avail if avail is not None else True),
        discount_type=pick(d, "discount_type", "discountType"),
        can_with_order=bool(pick(d, "can_with_order", "canWithOrder", default=False)),
        with_order=dict(pick(d, "with_order", "withOrder", default={}) or {}),
        image=pick(d, "image", "img", "image_url"),
        raw=d,
    )


def _clean_label(v: Any) -> str:
    """清洗分类名等展示文本。

    mcd-mcp 的分类名含真实换行（如 "随心配\\n1+1"、"小食甜品\\n/其他"），
    且可能是字面 backslash-n 两种形态，需一并处理。
    """
    s = str(v or "")
    s = s.replace("\\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" /")


# --------------------------------------------------------------------------
# 券名解析：真实 query-store-coupons 不返回面额，只能从券名反推
# --------------------------------------------------------------------------
# 例：「满50减15」-> threshold=5000 face=1500；「立减6」-> face=600
_NAME_RE = [
    (re.compile(r"满\s*(\d+(?:\.\d+)?)\s*减\s*(\d+(?:\.\d+)?)"), "fulldiscount"),
    (re.compile(r"满\s*(\d+(?:\.\d+)?)\s*折"), "threshold_percent"),
    (re.compile(r"(?:立减|减)\s*(\d+(?:\.\d+)?)\s*元?"), "reduce"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元\s*券"), "face"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*折"), "percent"),
    (re.compile(r"第\s*二\s*件\s*半价|第二份半价|买\s*一\s*送\s*一"), "bogo"),
]


def parse_amount_from_title(title: Optional[str]) -> Dict[str, Any]:
    """从券名反推面额/折扣/门槛。

    真实接口的 query-store-coupons 只给标题+适用商品，**不给金额**，
    但麦当劳券命名高度规整（「满50减15」「立减6」「8折」），可解析后参与计算。

    解析不出来时返回空 dict —— 此时券仍可用于「提示」，但不参与金额计算。
    """
    out: Dict[str, Any] = {}
    if not title:
        return out
    s = str(title).replace(" ", "")
    for rx, kind in _NAME_RE:
        m = rx.search(s)
        if not m:
            continue
        if kind == "fulldiscount":
            th = to_cents(m.group(1))
            fc = to_cents(m.group(2))
            if th and fc:
                out = {"threshold_cents": th, "face_value_cents": fc, "type": "cash"}
        elif kind == "threshold_percent":
            th = to_cents(m.group(1))
            try:
                pv = float(m.group(1))
                while pv > 1:      # "85折" -> 0.85
                    pv /= 10.0
                out = {"threshold_cents": th, "percent": pv, "type": "percent"}
            except ValueError:
                pass
        elif kind == "reduce":
            fc = to_cents(m.group(1))
            if fc:
                out = {"face_value_cents": fc, "type": "cash"}
        elif kind == "face":
            fc = to_cents(m.group(1))
            if fc:
                out = {"face_value_cents": fc, "type": "cash"}
        elif kind == "percent":
            try:
                pv = float(m.group(1))
                while pv > 1:
                    pv /= 10.0
                if 0 < pv < 1:
                    out = {"percent": pv, "type": "percent"}
            except ValueError:
                pass
        elif kind == "bogo":
            out = {"type": "bogo"}
        if out:
            out["_from_title"] = True
            break
    return out


def parse_mcd_menu(payload: Any) -> List[MenuItem]:
    """解析 mcd-mcp query-meals 的两级菜单结构。

    真实结构：
      data.categories[] = {name, meals: [{code, tags}]}     # 索引层（只有 code + tags）
      data.meals        = { "<code>": {name, currentPrice, ...} }  # 详情层
      data.frequent     = {code, tags}                        # 常点

    同一 code 可能出现在多个分类下（如薯条同时属于"小食"和"饮品"边角），
    这里按 code 去重，合并分类与标签。
    """
    data = mcd_data(payload)
    if not isinstance(data, dict):
        raise McdError(F_NO_MENU, "菜单返回结构异常", {"type": str(type(data))})

    details = data.get("meals") or {}
    if not isinstance(details, dict):
        details = {}
    # 兼容单层结构（第三方 MCP 直接给 list）
    if not details and isinstance(data.get("list"), list):
        return [normalize_menu_item(x) for x in data["list"]]

    merged: Dict[str, MenuItem] = {}
    cats = data.get("categories") or []
    if isinstance(cats, list):
        for cat in cats:
            if not isinstance(cat, dict):
                continue
            cat_name = _clean_label(cat.get("name"))
            for entry in (cat.get("meals") or []):
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("code") or "").strip()
                if not code:
                    continue
                tags = entry.get("tags") or []
                detail = details.get(code) or {}
                item = normalize_menu_item(detail, category=cat_name, tags=list(tags))
                if not item.item_id:
                    item.item_id = code
                if not item.name:
                    continue          # 无详情的索引项直接跳过，避免生成空名商品
                if item.item_id in merged:
                    prev = merged[item.item_id]
                    # 合并分类（同一商品可能横跨多个分类）
                    if cat_name and cat_name != prev.category:
                        prev.category = f"{prev.category}/{cat_name}" if prev.category else cat_name
                    for t in tags:
                        if t and t not in prev.tags:
                            prev.tags.append(t)
                else:
                    merged[item.item_id] = item

    items = list(merged.values())
    if not items:
        raise McdError(F_NO_MENU, "菜单解析结果为空", {"categories": len(cats or [])})
    return items


_CTYPE_MAP = {
    "cash": "cash", "amount": "cash", "reduction": "cash", "满减": "cash", "代金": "cash",
    "percent": "percent", "discount": "percent", "rate": "percent", "折扣": "percent",
    "delivery": "delivery_fee", "delivery_fee": "delivery_fee", "shipping": "delivery_fee", "配送": "delivery_fee",
    "combo": "combo", "meal": "combo", "套餐": "combo",
    "unknown": "unknown",
}


def normalize_coupon(raw: Any) -> Coupon:
    d = raw if isinstance(raw, dict) else {}
    d = normalize_field_map(d)

    # mcd-mcp：同一 couponId 可能对应多个 couponCode，按券ID归并
    codes = pick(d, "coupon_codes", default=None)
    if codes is None:
        one = pick(d, "coupon_code", "couponCode")
        codes = [one] if one else []
    if isinstance(codes, str):
        codes = [codes]
    codes = [str(c) for c in (codes or []) if c]

    # 适用范围：products[] = [{productName, productCode}]
    scope = str(pick(d, "scope", "applicable_range", "applicableRange", default="all") or "all")
    products = pick(d, "products", default=[]) or []
    if isinstance(products, dict):
        products = [products]
    product_names: List[str] = []
    product_codes: List[str] = []
    if isinstance(products, list):
        for p in products:
            if not isinstance(p, dict):
                continue
            nm = pick(p, "product_name", "productName", "name")
            cd = pick(p, "product_code", "productCode", "code")
            if nm:
                product_names.append(str(nm))
            if cd:
                product_codes.append(str(cd))
        if product_codes:
            scope = "item:" + ",".join(product_codes)
        elif product_names:
            scope = "name:" + ",".join(product_names)

    t_raw = str(pick(d, "type", "coupon_type", "couponType", default="cash") or "cash").lower()
    ctype = _CTYPE_MAP.get(t_raw, "cash")
    title = pick(d, "title", "name", "coupon_name", "couponName")

    fv_raw = pick(d, "face_value_cents", "faceValueCents")
    if fv_raw is not None:
        face = to_cents(fv_raw, already_cents=True)
    else:
        face = to_cents(pick(d, "amount", "discount", "value", "face_value", "price"))
    if face is None:
        face = 0

    pct = pick(d, "percent", "discount_rate", "discountRate", "rate")
    if pct is None:
        pct = pick(d, "discount", "reduction_ratio")
    percent = 1.0
    if pct is not None:
        try:
            f = float(pct)
            if f > 1.0:
                f = f / 100.0 if f <= 100 else f / 10.0
            percent = f if 0 < f <= 1 else 1.0
        except (TypeError, ValueError):
            percent = 1.0

    th_raw = pick(d, "threshold_cents", "thresholdCents")
    if th_raw is not None:
        threshold = to_cents(th_raw, already_cents=True)
    else:
        threshold = to_cents(pick(d, "threshold", "min_amount", "minAmount",
                                  "min_order_amount", "minOrderAmount", "condition"))
    if threshold is None:
        threshold = 0

    # 接口未给金额时，从券名反推（麦当劳券命名规整，如「满50减15」「立减6」「8折」）
    if face == 0 and threshold == 0 and percent == 1.0:
        g = parse_amount_from_title(title)
        if g:
            face = g.get("face_value_cents") or face
            threshold = g.get("threshold_cents") or threshold
            percent = g.get("percent", percent)
            gt = g.get("type")
            if gt == "bogo":
                # 买一送一/第二份半价：按第二件半价折算，精确值以 calculate-price 为准
                percent, ctype = 0.5, "percent"
            elif gt:
                ctype = gt

    if not scope or scope == "all":
        scope = "all"

    stackable = pick(d, "stackable", "can_stack", "canStack", "is_stackable", default=False)
    claimed = pick(d, "claimed", "is_claimed", "received", default=False)
    if claimed is None:
        claimed = False
    status = str(pick(d, "status", "state", default="") or "").lower()
    if status in {"claimed", "used", "已领取", "已核销", "received"}:
        claimed = True
    elif status in {"expired", "已过期", "invalid"}:
        claimed = True

    remain = pick(d, "remain", "remain_count", "remainCount", "stock", "left", "quantity")
    try:
        remain = int(remain) if remain is not None else None
    except (TypeError, ValueError):
        remain = None

    c = Coupon(
        coupon_id=str(pick(d, "coupon_id", "couponId", "id", default="") or "") or None,
        title=pick(d, "title", "name", "coupon_name", "couponName"),
        type=ctype,
        face_value_cents=face,
        percent=percent,
        threshold_cents=threshold,
        scope=scope,
        stackable=bool(stackable if stackable is not None else False),
        expires_at=parse_time(pick(d, "trade_date_time", "tradeDateTime", "expires_at",
                                   "expire_time", "expireTime", "valid_until", "end_time")),
        claimed=bool(claimed),
        remain=remain,
        valid_hours=pick(d, "valid_hours", "validHours", "time_period"),
        channel=pick(d, "channel", "scene", "usage_channel"),
        raw=d,
    )
    c.coupon_codes = codes
    c.product_names = product_names
    c.product_codes = product_codes
    return c


def parse_available_coupons_markdown(text: str) -> List[Coupon]:
    """解析 available-coupons 的 Markdown 返回。

    真实返回形如：
      - 优惠券标题：人气麦旋风买一送一 \\
        状态：可领取 \\
        优惠券图片：<img ...>

    该接口**不含 couponId / 面额 / 门槛**，因此只能生成「待确认」券：
    金额类字段一律留 0，计算时不可依赖它，只能用于提示用户去领。
    """
    out: List[Coupon] = []
    if not isinstance(text, str):
        return out
    # 以 "- 优惠券标题：" 为条目起点切块
    blocks = re.split(r"-\s*优惠券标题[:：]", text)
    for blk in blocks[1:]:
        title = blk.split("\\")[0].strip()
        status = ""
        ms = re.search(r"状态[:：]\s*([^\\\n]+)", blk)
        if ms:
            status = ms.group(1).strip()
        if not title:
            continue
        c = Coupon(
            title=title,
            # 无金额信息 -> 标记为 unknown，引擎遇到时必须跳过金额计算
            type="unknown",
            face_value_cents=0,
            percent=1.0,
            threshold_cents=0,
            scope="all",
            stackable=False,
            claimed=(status == "已领取"),
        )
        c.valid_hours = None
        c.raw = {"status_text": status, "source": "available-coupons"}
        out.append(c)
    return out


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class McdClient:
    """麦当劳 MCP 客户端。

    MCP 工具通过 ToolSearch + DeferExecuteTool 调用（WorkBuddy 运行时提供），
    本类负责发现、参数适配、归一化与错误分类，不直接依赖任何工具名。
    """

    MAX_RETRY = 1          # 领取类写操作最多重试 1 次
    BACKOFF_SEC = 0.6

    def __init__(self, invoker=None, probe_fn=None):
        """
        Args:
            invoker: (tool_name, params) -> 原始返回 的可调用对象。
                     缺省时在 probe() / call() 里按需从运行时获取。
            probe_fn: () -> {tool_name: schema} 的可调用对象，用于能力探测。
        """
        self._invoker = invoker
        self._probe_fn = probe_fn
        self._cap: Optional[CapabilityReport] = None
        self._cache: Dict[str, Any] = {}

    # ---------- 运行时工具桥接 ----------
    def _resolve_invoker(self):
        if self._invoker is not None:
            return self._invoker

        def _default(tool_name: str, params: Dict[str, Any]):
            # 延迟导入，避免在无 WorkBuddy 运行时环境下 import 失败
            from workbuddy_bridge import call_tool  # type: ignore
            return call_tool(tool_name, params)

        self._invoker = _default
        return self._invoker

    # ---------- 能力探测 ----------
    def probe(self, force: bool = False) -> CapabilityReport:
        """探测可用能力，结果缓存。"""
        if self._cap is not None and not force:
            return self._cap

        available: Dict[str, Any] = {}
        if self._probe_fn is not None:
            available = dict(self._probe_fn() or {})
        else:
            try:
                from workbuddy_bridge import list_tools  # type: ignore
                available = dict(list_tools() or {})
            except Exception:
                available = self._probe_via_search()

        bindings: Dict[str, Optional[str]] = {}
        schemas: Dict[str, Any] = {}
        missing: List[str] = []

        for cap, aliases in CAPABILITY_ALIASES.items():
            hit = None
            for name in aliases:                      # 精确名优先
                if name in available:
                    hit = name
                    break
            if hit is None:                            # 再按归一化模糊匹配
                inv = {_norm_key(k): k for k in available}
                for name in aliases:
                    k = _norm_key(name)
                    if k in inv:
                        hit = inv[k]
                        break
            bindings[cap] = hit
            if hit:
                schemas[cap] = available.get(hit)
            else:
                missing.append(cap)

        self._cap = CapabilityReport(bindings=bindings, missing=missing, schemas=schemas)
        return self._cap

    def _probe_via_search(self) -> Dict[str, Any]:
        """回退：用 ToolSearch 全量拉取别名 schema。"""
        found: Dict[str, Any] = {}
        try:
            from workbuddy_bridge import search_tools  # type: ignore
        except Exception:
            return found
        for cap, aliases in CAPABILITY_ALIASES.items():
            try:
                res = search_tools(aliases) or []
            except Exception:
                continue
            for item in res:
                if isinstance(item, dict) and item.get("name"):
                    found.setdefault(item["name"], item)
        return found

    def require(self, *caps: str) -> None:
        """确保指定能力可用，否则抛对应兜底异常。"""
        rep = self.probe()
        if rep.level == "L0_NO_MCP":
            raise McdUnavailable()
        for c in caps:
            if not rep.bindings.get(c):
                raise McdError(F_NO_MENU if c == "menu_browse" else F_NO_STORE,
                               f"能力不可用：{c}", rep.to_dict())

    # ---------- 参数适配 ----------
    @staticmethod
    def _shim(cap: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按优先级返回候选参数组合，第一个即最优。"""
        lat = params.get("latitude") or params.get("lat")
        lng = params.get("longitude") or params.get("lng") or params.get("lon")
        city = params.get("city")
        kw = params.get("keyword") or params.get("address")
        sid = params.get("store_id") or params.get("storeId") or params.get("store")
        uid = params.get("user_id") or params.get("userId")
        ch = params.get("channel") or params.get("scene")

        if cap == "store_locate":
            # mcd-mcp query-nearby-stores：beType 与 searchType 必传
            be_type = resolve_be_type(params.get("be_type"), ch)
            out: List[Dict[str, Any]] = []
            if city and kw:
                out.append({"searchType": 2, "city": city, "keyword": kw, "beType": be_type})
                out.append({"searchType": 2, "city": city, "keyword": kw, "beType": be_type})
            if kw:
                out.append({"searchType": 2, "keyword": kw, "city": city or "", "beType": be_type})
            if city:
                out.append({"searchType": 2, "city": city, "keyword": city, "beType": be_type})
            # searchType=1 搜索收藏餐厅
            out.append({"searchType": 1, "beType": be_type})
            # 外送场景：delivery-query-stores（需 addressId）
            addr = params.get("address_id") or params.get("addressId")
            if addr:
                out.append({"addressId": addr, "beType": be_type})
            return out

        if cap == "menu_browse":
            # mcd-mcp query-meals：storeCode / orderType / beType 必传
            be_type = resolve_be_type(params.get("be_type"), ch)
            base = {
                "storeCode": params.get("store_code") or sid,
                "orderType": params.get("order_type") or BE_TYPE_TO_ORDER_TYPE[be_type],
                "beType": be_type,
            }
            be_code = params.get("be_code") or params.get("beCode")
            if be_code:
                b2 = dict(base)
                b2["beCode"] = be_code
                return [b2, base]
            return [base]

        if cap in ("coupon_available", "coupon_owned"):
            # query-store-coupons：storeCode / orderType / beType 必传
            be_type = resolve_be_type(params.get("be_type"), ch)
            base = {
                "storeCode": params.get("store_code") or sid,
                "orderType": params.get("order_type") or BE_TYPE_TO_ORDER_TYPE[be_type],
                "beType": be_type,
            }
            be_code = params.get("be_code") or params.get("beCode")
            if be_code:
                b2 = dict(base)
                b2["beCode"] = be_code
                return [b2, base]
            # query-my-coupons / available-coupons：无参
            return [{}]

        if cap == "coupon_claim":
            # auto-bind-coupons：无参，一键领全部
            cid = params.get("coupon_id") or params.get("couponId")
            return [{}, {"couponId": cid}] if cid else [{}]

        if cap == "combo_inspect":
            # query-meal-detail：storeCode / orderType / beType / code 必传
            be_type = resolve_be_type(params.get("be_type"), ch)
            p = {
                "storeCode": params.get("store_code") or sid,
                "orderType": params.get("order_type") or BE_TYPE_TO_ORDER_TYPE[be_type],
                "beType": be_type,
                "code": params.get("code") or params.get("item_id"),
            }
            be_code = params.get("be_code") or params.get("beCode")
            if be_code:
                p["beCode"] = be_code
            return [p]

        if cap == "order_price_preview":
            # calculate-price：金额单位为「分」，items[].productCode 必填
            be_type = resolve_be_type(params.get("be_type"), ch)
            p = {
                "storeCode": params.get("store_code") or sid,
                "orderType": params.get("order_type") or BE_TYPE_TO_ORDER_TYPE[be_type],
                "beType": be_type,
                "items": params.get("items", []),
            }
            be_code = params.get("be_code") or params.get("beCode")
            if be_code:
                p["beCode"] = be_code
            wo = params.get("with_order")
            if wo:
                p["withOrder"] = wo
            if params.get("need_tableware") is not None:
                p["needTableware"] = params["need_tableware"]
            return [p]

        if cap == "promo_activity":
            # campaign-calendar：无参或仅 specifiedDate
            return [{}, {"specifiedDate": params.get("date", "")}]

        return [params]

    # ---------- 调用 ----------
    def call(self, cap: str, params: Dict[str, Any], *, retry: Optional[int] = None) -> Any:
        """按语义键调用 MCP，自动做参数适配与降级重试。"""
        rep = self.probe()
        tool = rep.bindings.get(cap)
        if not tool:
            if rep.level == "L0_NO_MCP":
                raise McdUnavailable()
            raise McdError(F_FIELD_MISSING, f"能力不可用：{cap}", rep.to_dict())

        invoke = self._resolve_invoker()
        attempts = self.MAX_RETRY if retry is None else retry
        last_err: Optional[Exception] = None

        for p in self._shim(cap, params):
            for i in range(attempts + 1):
                try:
                    return invoke(tool, p)
                except Exception as e:      # noqa: BLE001
                    last_err = e
                    msg = str(e).lower()
                    # 参数不匹配 → 立刻换下一组参数，不重试
                    if any(k in msg for k in ("invalid", "required", "parameter", "argument", "unexpected")):
                        break
                    if i < attempts:
                        time.sleep(self.BACKOFF_SEC * (i + 1))
        raise McdError(self._classify(last_err), f"调用 {cap} 失败：{last_err}", {"cap": cap, "tool": tool})

    @staticmethod
    def _classify(err: Optional[Exception]) -> str:
        if err is None:
            return F_FIELD_MISSING
        m = str(err).lower()
        if any(k in m for k in ("timeout", "network", "connection", "unreachable")):
            return F_NO_MENU
        if any(k in m for k in ("auth", "unauthorized", "login", "token", "登录", "授权")):
            return F_NO_COUPON
        if any(k in m for k in ("expire", "sold out", "已领完", "过期", "结束")):
            return F_PROMO_EXPIRED
        if any(k in m for k in ("stack", "叠加", "不可用")):
            return F_STACK_REJECTED
        return F_FIELD_MISSING

    # ---------- 语义方法（对齐 mcd-mcp 真实契约） ----------
    def find_stores(self, city: Optional[str] = None, keyword: Optional[str] = None,
                    be_type: int = BE_TYPE_PICKUP, address_id: Optional[str] = None,
                    **kw) -> List[Store]:
        raw = self.call("store_locate", {
            "city": city, "keyword": keyword, "be_type": be_type,
            "address_id": address_id, **kw})
        data = mcd_data(raw)
        items = data if isinstance(data, list) else extract_list(data, "stores", "list", "items")
        stores = [normalize_store(x) for x in items if isinstance(x, dict)]
        if not stores:
            raise McdError(F_NO_STORE, "未找到符合条件的门店",
                           {"city": city, "keyword": keyword, "beType": be_type})
        # 距离升序；收藏/搜索返回的 distance 常为 0，此时保持原顺序
        with_dist = [s for s in stores if s.distance_m]
        without = [s for s in stores if not s.distance_m]
        with_dist.sort(key=lambda s: s.distance_m or 0)
        return with_dist + without

    def get_menu(self, store_code: str, be_type: int = BE_TYPE_PICKUP,
                 be_code: Optional[str] = None,
                 order_type: Optional[int] = None, **kw) -> List[MenuItem]:
        ck = f"{store_code}:{be_type}:{be_code or ''}"
        if ck in self._cache:
            return self._cache[ck]
        raw = self.call("menu_browse", {
            "store_code": store_code, "be_type": be_type, "be_code": be_code,
            "order_type": order_type, **kw})
        items = parse_mcd_menu(raw)
        items = [i for i in items if i.available and i.price_cents]
        self._cache[ck] = items
        return items

    def get_coupons(self, store_code: Optional[str] = None,
                    be_type: int = BE_TYPE_PICKUP, be_code: Optional[str] = None,
                    channel: Optional[str] = None,
                    order_type: Optional[int] = None) -> List[Coupon]:
        """取券。优先门店可用券（口径最准），失败时退回卡包。

        注意：query-my-coupons 返回的是「我拥有的券」，MCP 明确声明
        **不校验门店/渠道可用性**，因此其结果只作补充，不可单独作为可用性依据。
        """
        out: List[Coupon] = []
        # 1) 门店可用券（含金额，可参与计算）
        if store_code:
            try:
                raw = self.call("coupon_available", {
                    "store_code": store_code, "be_type": be_type,
                    "be_code": be_code, "order_type": order_type, "channel": channel})
                data = mcd_data(raw)
                items = data if isinstance(data, list) else extract_list(data, "coupons", "list", "items")
                seen_ids = set()
                for x in items:
                    if not isinstance(x, dict):
                        continue
                    c = normalize_coupon(x)
                    if c.coupon_id and c.coupon_id in seen_ids:
                        continue          # 同一券ID多行（多券码）只取一次
                    if c.coupon_id:
                        seen_ids.add(c.coupon_id)
                    out.append(c)
            except McdError:
                pass

        # 2) 卡包券（补充，但标注未校验门店）
        if self.probe().bindings.get("coupon_owned"):
            try:
                raw = self.call("coupon_owned", {})
                data = mcd_data(raw)
                items = data if isinstance(data, list) else extract_list(data, "coupons", "list", "items")
                for x in items:
                    if isinstance(x, dict):
                        c = normalize_coupon(x)
                        c.raw["_source"] = "my_coupons"
                        out.append(c)
            except McdError:
                pass
        return out

    def get_claimable_coupons_markdown(self) -> List[Coupon]:
        """全国可领券（Markdown 文本，无金额信息）。

        仅用于「提示用户有券可领」，不可进入金额计算。
        """
        for cap in ("coupon_available",):
            if not self.probe().bindings.get(cap):
                continue
            try:
                raw = self.call(cap, {})
            except McdError:
                continue
            if isinstance(raw, str):
                return parse_available_coupons_markdown(raw)
        return []

    def claim_all_coupons(self) -> bool:
        """一键领取全部可用券（auto-bind-coupons）。

        写操作：**调用前必须获得用户明确确认**。
        """
        try:
            self.call("coupon_claim", {}, retry=1)
            return True
        except McdError:
            return False

    def preview_price(self, store_code: str, items: List[Dict[str, Any]],
                      be_type: int = BE_TYPE_PICKUP, be_code: Optional[str] = None,
                      order_type: Optional[int] = None,
                      with_order: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用 calculate-price 试算（权威价格）。金额单位为「分」。"""
        raw = self.call("order_price_preview", {
            "store_code": store_code, "be_type": be_type, "be_code": be_code,
            "order_type": order_type, "items": items, "with_order": with_order})
        data = mcd_data(raw)
        return data if isinstance(data, dict) else {}

    def get_combos(self, store_code: str, be_type: int = BE_TYPE_PICKUP,
                   be_code: Optional[str] = None) -> List[MenuItem]:
        """从菜单里筛出套餐。

        mcd-mcp 的 query-meals 已通过 tags 标注「套餐」，无需额外调
        query-meal-detail 逐个查详情（那会显著增加调用次数）。
        """
        menu = self.get_menu(store_code, be_type, be_code)
        combos = [i for i in menu if i.is_combo]
        if combos:
            return combos
        combo_kw = ("套餐", "三件套", "四件套", "五件套", "随心配", "超值", "组合", "欢聚")
        return [i for i in menu if any(k in (i.name or "") for k in combo_kw)]

    def get_promotions(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """营销活动日历（全国维度，非门店级）。

        注意：query-promotions 仅在企业团餐(beType=6)下可用，
        日常到店场景应使用 campaign-calendar。
        """
        if not self.probe().bindings.get("promo_activity"):
            return []
        try:
            raw = self.call("promo_activity", {"date": date})
        except McdError:
            return []
        data = mcd_data(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return extract_list(data, "promotions", "list", "items", "data")


# --------------------------------------------------------------------------
# CLI 自测
# --------------------------------------------------------------------------
MOCK_MENU = {
    "code": 0,
    "data": {"list": [
        {"id": "I1", "name": "巨无霸套餐", "category": "套餐", "price": 39.0,
         "type": "combo", "contents": [{"name": "巨无霸"}, {"name": "中薯"}, {"name": "中可乐"}]},
        {"id": "I2", "name": "麦辣鸡腿堡套餐", "category": "套餐", "price": 29.0, "isCombo": True},
        {"id": "I3", "name": "巨无霸", "category": "汉堡", "price": 24.0, "tags": ["牛肉"]},
        {"id": "I4", "name": "麦香鱼", "category": "汉堡", "price": 22.0, "tags": ["鱼", "辣"]},
        {"id": "I5", "name": "中薯条", "category": "小食", "price": 9.0},
        {"id": "I6", "name": "中可乐", "category": "饮料", "price": 10.0},
        {"id": "I7", "name": "麦乐鸡(5块)", "category": "小食", "price": 22.0},
    ]},
}

MOCK_COUPONS = {
    "result": {"coupons": [
        {"couponId": "C1", "name": "满50减15", "type": "cash", "amount": 15.0,
         "threshold": 50.0, "stackable": True, "expireTime": "2026-12-31 23:59:59", "remainCount": 100},
        {"couponId": "C2", "name": "大杯可乐8折", "type": "discount", "discountRate": 0.8,
         "scope": "item:I6", "canStack": True, "expireTime": "2026-10-20 23:59:59", "stock": 5},
        {"couponId": "C3", "name": "免配送费", "type": "delivery_fee", "amount": 6.0,
         "channel": "delivery", "expireTime": "2026-10-15 23:59:59"},
        {"couponId": "C4", "name": "过期券", "type": "cash", "amount": 30.0,
         "threshold": 10.0, "expireTime": "2020-01-01 00:00:00"},
        {"couponId": "C5", "name": "抢光的券", "type": "cash", "amount": 20.0,
         "remainCount": 0, "expireTime": "2026-12-31 23:00:00"},
    ]},
}


def mock_probe() -> Dict[str, Any]:
    return {
        "find_store": {"name": "find_store"},
        "get_menu": {"name": "get_menu"},
        "get_available_coupons": {"name": "get_available_coupons"},
        "claim_coupon": {"name": "claim_coupon"},
    }


def mock_invoke(tool: str, params: Dict[str, Any]):
    if tool == "find_store":
        return {"data": {"stores": [
            {"id": "S1", "storeName": "光谷广场店", "address": "洪山区珞喻路",
             "distance": 1.2, "openHours": "07:00-22:00", "channels": ["堂食", "外送"]},
            {"id": "S2", "name": "街道口店", "address": "珞珈山", "distance": 3200,
             "businessHours": "06:30-23:00"},
        ]}}
    if tool == "get_menu":
        return MOCK_MENU
    if tool == "get_available_coupons":
        return MOCK_COUPONS
    if tool == "claim_coupon":
        return {"success": True, "coupon": {"couponId": params.get("coupon_id"), "name": "已领取"}}
    raise RuntimeError(f"unknown tool {tool}")


def _self_test() -> int:
    c = McdClient(invoker=mock_invoke, probe_fn=mock_probe)
    rep = c.probe()
    print("能力探测:", json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))

    stores = c.find_stores(latitude=30.5928, longitude=114.3055, radius=3000)
    print(f"\n门店({len(stores)}):")
    for s in stores:
        dist = f"{s.distance_m}m" if s.distance_m is not None else "距离未知"
        print(f"  - {s.name}({s.store_id}) {dist} | 渠道={s.channels} | 营业={s.open_hours}")

    menu = c.get_menu("S1")
    print(f"\n菜单({len(menu)}):")
    for m in menu:
        print(f"  - {m.name} {fmt_cents(m.price_cents)} combo={m.is_combo} tags={m.tags}")

    combos = c.get_combos("S1")
    print(f"\n套餐({len(combos)}): {[m.name for m in combos]}")

    coupons = c.get_coupons("U8888", "S1")
    print(f"\n券({len(coupons)}):")
    for cp in coupons:
        usable = cp.is_usable()
        reason = cp.unusable_reason or "可用"
        print(f"  - {cp.title} [{cp.type}] 面额={fmt_cents(cp.face_value_cents)} "
              f"折扣={cp.percent} 门槛={fmt_cents(cp.threshold_cents)} "
              f"可叠={cp.stackable} → {reason}")

    usable = [cp for cp in coupons if cp.is_usable()]
    print(f"\n可用券 {len(usable)} 张；已剔除 {len(coupons) - len(usable)} 张（过期/领完）")

    # 关键不变量：过期券与抢光的券绝不能进入计算
    assert all(not cp.is_expired() for cp in usable), "过期券泄漏"
    assert all(not cp.is_sold_out() for cp in usable), "领完券泄漏"
    assert all(not cp.claimed for cp in usable), "已领券泄漏"
    print("✓ 不变量校验通过：无过期/领完/已领券进入可用集合")
    return 0


def _probe_cli() -> int:
    c = McdClient()
    rep = c.probe(force=True)
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    if rep.level == "L0_NO_MCP":
        print("\n→ 当前为 L0_NO_MCP，按 fallback.md 的 F1 分支处理：不给任何套餐推荐。")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--self-test"
    if arg == "--probe":
        sys.exit(_probe_cli())
    else:
        sys.exit(_self_test())