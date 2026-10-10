"""
mock_mcp_server.py — 本地 mock 麦当劳 MCP（严格对齐 mcd-mcp 真实契约）

数据结构、字段名、必填参数均复制自 mcd-mcp 实测返回，用于无 MCP 环境下的
开发与回归测试。真实契约要点：

1. 所有响应外层为 {"success":true,"code":200,"data":{...}}，且常在 JSON 前后
   拼接自然语言描述（字段说明表等），必须用宽松 JSON 解析。
2. query-meals 为两级结构：categories[].meals[].code（索引） + meals{}（详情）。
3. 门店用 storeCode / beCode；beType 必传（1到店自取/2麦乐送/5得来速/6团餐）。
4. calculate-price 返回金额单位为「分」。
5. available-coupons 返回 Markdown 文本，不含 couponId/面额/门槛。
6. query-my-coupons 不校验门店可用性（接口文档明确声明）。

用法：
    python mock_mcp_server.py --scenario normal
    python mock_mcp_server.py --run-regression
    python mock_mcp_server.py --list-scenarios
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

SCENARIOS = ["normal", "no_coupon", "coupon_sold_out", "coupon_expired",
             "time_conflict", "unstackable", "no_menu", "no_store", "no_mcp"]


def _ok(data: Any, **extra: Any) -> str:
    """构造 mcd-mcp 标准响应（JSON 前后带描述文本，模拟真实情况）。"""
    body = {"success": True, "code": 200, "message": "请求成功",
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": data}
    body.update(extra)
    return "字段说明：如需展示 storeCode 请完整输出。\n" + json.dumps(body, ensure_ascii=False) + "\n## 展示结果时，请完整展示。"


def _iso(days: int = 7) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 真实结构数据（字段名与 mcd-mcp 实测一致）
# --------------------------------------------------------------------------
_MENU: Dict[str, Any] = {
    "categories": [
        {"name": "随心配\n1+1", "meals": [
            {"code": "9900013304", "tags": ["超值百种组合随心配"]},
            {"code": "9900015568", "tags": ["超值百种组合随心配"]},
        ]},
        {"name": "精选单人餐", "meals": [
            {"code": "9900004064", "tags": ["经典精选", "爆款"]},
            {"code": "9900014974", "tags": ["经典精选", "全球爆款"]},
        ]},
        {"name": "巨无霸\n牛鱼肉堡", "meals": [
            {"code": "1100", "tags": ["单品", "100%纯牛肉"]},
            {"code": "9900005466", "tags": ["套餐", "100%纯牛肉"]},
            {"code": "9900005468", "tags": ["套餐", "100%纯牛肉"]},
        ]},
        {"name": "鸡肉汉堡\n/卷", "meals": [
            {"code": "1440", "tags": ["单品", "人气经典"]},
            {"code": "9900005456", "tags": ["套餐", "新升级"]},
        ]},
        {"name": "小食甜品\n/其他", "meals": [
            {"code": "4810", "tags": ["小食"]},
            {"code": "1401", "tags": ["单品", "人气"]},
        ]},
        {"name": "饮品", "meals": [
            {"code": "9900008751", "tags": ["冷饮"]},
            {"code": "3010", "tags": ["冷饮"]},
        ]},
        {"name": "麦咖啡™", "meals": [
            {"code": "507411", "tags": ["麦咖啡™美式"]},
            {"code": "510479", "tags": ["麦金卡"]},
        ]},
    ],
    "meals": {
        "9900013304": {"name": "人气经典随心配", "currentPrice": "14.9",
                        "originalPrice": "14.9", "canWithOrder": False},
        "9900015568": {"name": "精选超值随心配", "currentPrice": "13.9",
                        "originalPrice": "13.9", "canWithOrder": False},
        "9900004064": {"name": "精选单人餐三件套", "currentPrice": "34.5",
                        "originalPrice": "46.5", "canWithOrder": False},
        "9900014974": {"name": "枫糖风味厚松饼猪柳堡三件套", "currentPrice": "23.9",
                        "originalPrice": "36", "canWithOrder": False},
        "1100": {"name": "巨无霸", "currentPrice": "26.5", "originalPrice": "26.5",
                 "canWithOrder": False},
        "9900005466": {"name": "巨无霸三件套", "currentPrice": "37.5",
                        "originalPrice": "50.5", "discountType": "促销优惠",
                        "canWithOrder": False},
        "9900005468": {"name": "培根蔬萃双层牛堡三件套", "currentPrice": "35.5",
                        "originalPrice": "48.5", "canWithOrder": False},
        "1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "22.5", "originalPrice": "22.5",
                 "canWithOrder": False},
        "9900005456": {"name": "麦辣鸡腿汉堡三件套", "currentPrice": "33.5",
                        "originalPrice": "46.5", "canWithOrder": False},
        "4810": {"name": "薯条", "currentPrice": "14", "originalPrice": "14",
                 "canWithOrder": False},
        "1401": {"name": "麦乐鸡", "currentPrice": "14.5", "originalPrice": "14.5",
                 "canWithOrder": False},
        "9900008751": {"name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5",
                        "canWithOrder": False},
        "3010": {"name": "雪碧", "currentPrice": "9.5", "originalPrice": "9.5",
                  "canWithOrder": False},
        "507411": {"name": "麦咖啡™美式", "currentPrice": "11.9",
                    "originalPrice": "19", "discountType": "促销优惠",
                    "canWithOrder": False},
        # 随单购麦金卡价：现价已是到手价，下单必须同步选随单购
        "510479": {"name": "燕麦奶铁", "currentPrice": "9.9",
                    "originalPrice": "23", "discountType": "随单购麦金卡优惠",
                    "withOrder": {"membershipCode": "106", "specId": "870"},
                    "canWithOrder": True},
    },
    "frequent": {"code": "9900013304", "tags": ["我的常点"]},
}

_STORES = [
    {"storeCode": "1960198", "storeName": "麦当劳武汉万福国际广场餐厅",
     "address": "鹦鹉大道530-3号万福国际广场", "distance": 850,
     "longitude": 114.29, "latitude": 30.58, "reservation": True,
     "reservationTimeOptions": [
         {"date": "2026-10-10", "today": True,
          "reservationOptionText": "下午茶(14:44至16:45)，夜市(17:14至21:45)"},
         {"date": "2026-10-11", "today": False,
          "reservationOptionText": "早餐(07:14至10:15)，午餐(10:44至14:15)"},
     ]},
    {"storeCode": "1960286", "storeName": "麦当劳武汉玉龙路华美达得来速餐厅",
     "address": "玉龙路华美达酒店1层", "distance": 2600, "longitude": 0.0,
     "latitude": 0.0, "reservation": True,
     "reservationTimeOptions": [
         {"date": "2026-10-10", "today": True,
          "reservationOptionText": "下午茶(14:44至16:45)，夜市(17:14至21:45)"},
     ]},
]


def _coupons(scenario: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = [
        {"couponId": "CP01", "couponCode": "CP01-A", "title": "满50减15",
         "tradeDateTime": _iso(days=5), "remainCount": 500,
         "products": [{"productName": "全场商品", "productCode": "ALL"}]},
        # 同一 couponId 对应两个 couponCode（真实结构）
        {"couponId": "CP02", "couponCode": "CP02-A", "title": "可乐类券",
         "tradeDateTime": _iso(days=3), "stock": 88,
         "products": [{"productName": "可乐", "productCode": "9900008751"}]},
        {"couponId": "CP02", "couponCode": "CP02-B", "title": "可乐类券",
         "tradeDateTime": _iso(days=3), "stock": 20,
         "products": [{"productName": "可乐", "productCode": "9900008751"}]},
        {"couponId": "CP03", "couponCode": "CP03-A", "title": "随心配专属立减3",
         "tradeDateTime": _iso(days=2), "stock": 999,
         "products": [{"productName": "人气经典随心配", "productCode": "9900013304"}]},
        {"couponId": "CP04", "couponCode": "CP04-A", "title": "早餐专属立减6",
         "tradeDateTime": _iso(days=1), "validHours": "06:00-10:00", "stock": 50,
         "products": [{"productName": "麦咖啡™美式", "productCode": "507411"}]},
    ]
    if scenario == "no_coupon":
        return []
    if scenario == "coupon_sold_out":
        for c in items:
            if c["couponId"] in ("CP01", "CP03"):
                c["remainCount"] = 0
                c.pop("stock", None)
    elif scenario == "coupon_expired":
        for c in items:
            c["tradeDateTime"] = "2024-01-01 00:00:00"
    elif scenario == "time_conflict":
        for c in items:
            c["validHours"] = "06:00-10:00"
    elif scenario == "unstackable":
        for c in items:
            c.pop("remainCount", None)
    return items


_AVAILABLE_MD = """### 麦麦省优惠券列表：
- 优惠券标题：人气麦旋风买一送一 \\\\
  状态：可领取 \\\\
  优惠券图片：<img src="a.png">
- 优惠券标题：免费脆薯饼 \\\\
  状态：可领取 \\\\
  优惠券图片：<img src="b.png">
- 优惠券标题：巧克力味厚松饼猪柳蛋套餐 \\\\
  状态：已领取 \\\\
  优惠券图片：<img src="c.png">"""


def make_server(scenario: str = "normal"):
    """返回 (probe_fn, invoke_fn, tool_schemas)。"""
    if scenario == "no_mcp":
        return (lambda: {}), (lambda t, p: {}), {}

    tools: Dict[str, Any] = {
        "mcp__mcd-mcp__query-nearby-stores": {"required": ["beType", "searchType"]},
        "mcp__mcd-mcp__delivery-query-stores": {"required": ["beType", "addressId"]},
        "mcp__mcd-mcp__query-meals": {"required": ["storeCode", "orderType", "beType"]},
        "mcp__mcd-mcp__query-store-coupons": {"required": ["storeCode", "orderType", "beType"]},
        "mcp__mcd-mcp__available-coupons": {"required": []},
        "mcp__mcd-mcp__query-my-coupons": {"required": []},
        "mcp__mcd-mcp__auto-bind-coupons": {"required": []},
        "mcp__mcd-mcp__query-meal-detail": {"required": ["storeCode", "orderType", "beType", "code"]},
        "mcp__mcd-mcp__calculate-price": {"required": ["storeCode", "orderType", "beType", "items"]},
        "mcp__mcd-mcp__campaign-calendar": {"required": []},
    }

    def probe_fn() -> Dict[str, Any]:
        d = dict(tools)
        if scenario == "no_store":
            d.pop("mcp__mcd-mcp__query-nearby-stores")
            d.pop("mcp__mcd-mcp__delivery-query-stores")
        if scenario == "no_menu":
            d.pop("mcp__mcd-mcp__query-meals")
        return d

    def invoke(tool: str, params: Dict[str, Any]) -> Any:
        if tool == "mcp__mcd-mcp__query-nearby-stores":
            if scenario == "no_store":
                return _ok([])
            be_type = params.get("beType", 1)
            stores = [dict(s) for s in _STORES]
            if be_type == 5:      # 得来速：必带 beCode
                for s in stores:
                    s["beCode"] = f"BE-{s['storeCode']}"
            return _ok(stores)

        if tool == "mcp__mcd-mcp__delivery-query-stores":
            return _ok([{"storeCode": "1960198", "beCode": "BE-DEL-001",
                         "storeName": "麦当劳万福国际广场餐厅(麦乐送)", "distance": 850}])

        if tool == "mcp__mcd-mcp__query-meals":
            if scenario == "no_menu":
                return json.dumps({"success": False, "code": 500,
                                   "message": "menu service unavailable"}, ensure_ascii=False)
            return _ok(_MENU)

        if tool == "mcp__mcd-mcp__query-store-coupons":
            return _ok(_coupons(scenario))

        if tool == "mcp__mcd-mcp__available-coupons":
            return _AVAILABLE_MD

        if tool == "mcp__mcd-mcp__query-my-coupons":
            return _ok({"list": [{"couponId": "MY01", "couponCode": "MY01-A",
                                  "title": "我的9元券", "tradeDateTime": _iso(days=4),
                                  "products": [{"productName": "全场商品", "productCode": "ALL"}]}]})

        if tool == "mcp__mcd-mcp__auto-bind-coupons":
            return _ok({"claimed": True, "count": 3})

        if tool == "mcp__mcd-mcp__query-meal-detail":
            detail = _MENU["meals"].get(params.get("code"))
            if not detail:
                return json.dumps({"success": False, "code": 404, "message": "not found"},
                                  ensure_ascii=False)
            return _ok({"supportModify": False, "choice": {"supportModify": False}})

        if tool == "mcp__mcd-mcp__calculate-price":
            items = params.get("items", [])
            details = _MENU["meals"]
            price_sum = orig_sum = 0
            plist = []
            for it in items:
                code, qty = it.get("productCode"), int(it.get("quantity", 1))
                d = details.get(code)
                if not d:
                    continue
                cur = int(round(float(d["currentPrice"]) * 100))
                org = int(round(float(d["originalPrice"]) * 100))
                price_sum += cur * qty
                orig_sum += org * qty
                plist.append({"productCode": code, "productName": d["name"],
                              "quantity": qty, "originalSubtotal": org * qty,
                              "subtotal": cur * qty})
            coupon_cut = 1500 if price_sum >= 5000 else 0      # 满50减15
            payable = price_sum - coupon_cut
            return _ok({
                "productOriginalPrice": orig_sum, "productPrice": price_sum,
                "deliveryOriginalPrice": 0, "deliveryPrice": 0,
                "packingOriginalPrice": 0, "packingPrice": 0, "tablewarePrice": 0,
                "originalPrice": orig_sum,
                "discount": orig_sum - payable, "price": payable,
                "productList": plist,
                "takeWayList": [{"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
                                {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"}],
            })

        if tool == "mcp__mcd-mcp__campaign-calendar":
            return _ok({"list": [{"id": "P1", "name": "第二份半价", "expireTime": _iso(days=6)}]})

        raise RuntimeError(f"unknown tool: {tool}")

    return probe_fn, invoke, tools


def main() -> int:
    ap = argparse.ArgumentParser(description="麦当劳 MCP Mock Server（对齐 mcd-mcp 真实契约）")
    ap.add_argument("--scenario", default="normal", choices=SCENARIOS)
    ap.add_argument("--list-scenarios", action="store_true")
    ap.add_argument("--run-regression", action="store_true")
    args = ap.parse_args()

    if args.list_scenarios:
        print("可用场景：")
        for s in SCENARIOS:
            print(f"  - {s}")
        return 0
    if args.run_regression:
        return _regression()

    probe_fn, invoke, tools = make_server(args.scenario)
    print(json.dumps({"scenario": args.scenario, "tools": list(tools.keys())},
                     ensure_ascii=False, indent=2))
    return 0


def _regression() -> int:
    import mcd_client as mc
    import deal_optimizer as do

    cases = [("normal", True), ("no_coupon", True), ("coupon_sold_out", True),
             ("coupon_expired", True), ("time_conflict", True), ("unstackable", True),
             ("no_menu", False), ("no_store", False), ("no_mcp", False)]
    ok = True
    print(f"{'场景':<18}{'期望':<12}{'结果':<8}备注")
    print("-" * 58)
    for scen, exp_ok in cases:
        probe_fn, invoke, tools = make_server(scen)
        c = mc.McdClient(invoker=invoke, probe_fn=probe_fn)
        store = None
        try:
            store = c.find_stores(be_type=1, searchType=1)[0]
        except mc.McdError:
            pass
        res = do.run(c, do.Intent(3, 12000, [], "午餐", "堂食", store))
        got_ok = bool(res.get("ok"))
        passed = got_ok == exp_ok
        ok = ok and passed
        print(f"{scen:<18}{'可推荐' if exp_ok else '应停摆':<12}"
              f"{'PASS' if passed else 'FAIL':<8}code={res.get('code', '-')}")

    print("\n不变量检查：")
    probe_fn, invoke, _ = make_server("normal")
    c = mc.McdClient(invoker=invoke, probe_fn=probe_fn)
    st = c.find_stores(be_type=1, searchType=1)[0]
    menu = c.get_menu(st.store_code, be_type=1)
    coupons = c.get_coupons(st.store_code, be_type=1)

    bad_neg = bad_over = 0
    for n in range(1, 7):
        r = do.recommend(menu, coupons, do.Intent(n, None, [], None, "堂食", st))
        for p in r.get("plans", []):
            if p["payable_cents"] < 0:
                bad_neg += 1
            if p["coupon_discount_cents"] > p["list_price_cents"]:
                bad_over += 1
    print(f"  负数应付        : {bad_neg} {'✓' if bad_neg == 0 else '✗'}")
    print(f"  券抵扣超现价    : {bad_over} {'✓' if bad_over == 0 else '✗'}")
    ok = ok and bad_neg == 0 and bad_over == 0

    woo = [m for m in menu if m.can_with_order]
    print(f"  随单购商品识别  : {len(woo)} 个 {[m.name for m in woo]} {'✓' if woo else '✗'}")
    ok = ok and bool(woo)

    md = c.get_claimable_coupons_markdown()
    leak = [x.title for x in md if x.is_usable()]
    print(f"  无金额券泄漏    : {len(leak)} {'✓' if not leak else '✗'}")
    ok = ok and not leak

    dedup = [cp.coupon_id for cp in coupons]
    print(f"  券ID去重        : {len(dedup)} 张（{len(set(dedup))} 唯一）"
          f" {'✓' if len(dedup) == len(set(dedup)) else '✗'}")
    ok = ok and len(dedup) == len(set(dedup))

    print(f"\n{'全部通过 ✓' if ok else '存在失败 ✗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())