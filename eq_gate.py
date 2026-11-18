# -*- coding: utf-8 -*-
"""
eq_gate.py — 个股准入判定（2026-11-17 起，取消 NAV > $2,500 门槛）

用户裁定：「购入哪些个股我们只考虑其可能的收益与其他的对比」。
所以这里不再有任何"净值规模"门槛，只有三条可算的硬指标：

  ① 装得下   1 股金额 ≤ 允许仓位（= NAV × 2% ÷ 现实止损幅度，现实止损 = 2.5 × ATR%）
             —— 这不是"买不起"，是"一股就超了风险预算"，只能等价格或净值。
  ② 赔率     (回前高空间) ÷ (现实止损幅度) ≥ 2.0
             上行用「52 周高 × 0.97」而非分析师目标价 —— 免费源拿不到一致预期，
             而且"回到前高"是不含预测的、最保守的目标位。
  ③ 趋势     价 > MA200 且 MA50 > MA200
             这不是规模门槛，是"不接飞刀"。跌得越多赔率越好看，但趋势破位的票
             涨回前高的概率远低于宽基 —— 这一条就是给赔率口径踩刹车的。
  ④ 相对     单位波动赔率（赔率 ÷ ATR%）不低于 SPYM
             ① ② 是门槛；③ 只作参考、不作门槛 —— 宽基波动低、赔率天然低，
             拿它当门槛会失去约束力（SPYM 自己只有 0.4 左右），
             所以这里只单列出来看"每 1% 波动换来多少赔率"，由人判断。

用法：
    python eq_gate.py NVDA MU          # 指定标的
    python eq_gate.py                  # 默认 NVDA + MU + 基准 SPYM/QQQM

输出 eq_gate.json，并在终端打印表格。
"""

import json
import os
import sys
import time
import urllib.request

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json,*/*",
    "Cache-Control": "no-cache",
}

BASE = "https://stockanalysis.com/api/symbol/%s/%s/history?range=2Y"
FEE = 1.88             # 券商平台费 / 笔（0 佣金 + 固定平台费）
RISK_PCT = 0.02        # 单笔风险预算 = 净值 2%
STOP_MULT = 2.5        # 现实止损 = 2.5 × ATR（不是教科书 1×ATR）


def grab(url, tries=4):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url + "&t=%d" % int(time.time() * 1000), headers=HDR)
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(1.0)
    raise last


def bars(sym, kind="s"):
    """返回 [(date, o, h, l, c)] 升序。stockanalysis 返回降序，这里翻转。"""
    raw = json.loads(grab(BASE % (kind, sym)))
    rows = raw.get("data") or raw.get("bars") or []
    out = []
    for r in rows:
        try:
            if isinstance(r, dict):
                d = str(r.get("t") or r.get("date") or r.get("time"))[:10]
                out.append((d, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])))
            else:
                d = str(r[0])[:10]
                out.append((d, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def atr_pct(b, n=14):
    trs = []
    for i in range(1, len(b)):
        h, l, pc = b[i][2], b[i][3], b[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:                       # Wilder 平滑
        a = (a * (n - 1) + t) / n
    return a / b[-1][4] * 100


def sma(b, n, key=4):
    if len(b) < n:
        return None
    return sum(x[key] for x in b[-n:]) / n


def judge(sym, nav, kind="s"):
    b = bars(sym, kind)
    if len(b) < 60:
        return {"sym": sym, "err": "历史数据不足（%d 根）" % len(b)}
    px = b[-1][4]
    atr = atr_pct(b)
    hi = max(x[2] for x in b[-252:]) if len(b) >= 252 else max(x[2] for x in b)
    hi_d = [x[0] for x in b if x[2] == hi][-1]
    ma50, ma200 = sma(b, 50), sma(b, 200)

    stop = STOP_MULT * atr                      # 现实止损幅度 %
    seat = nav * RISK_PCT / (stop / 100)        # 允许仓位 $
    one = px                                   # 1 股金额
    tgt = hi * 0.97                             # 回前高（保守目标）
    up = (tgt / px - 1) * 100
    odds = up / stop

    # 手续费拖累（单边）—— 一笔 $150 的往返成本 ≈ 2.5%，这里按 1 股金额算
    fee_drag = FEE / one * 100 * 2

    ok_seat = one <= seat
    ok_odds = odds >= 2.0
    ok_trend = bool(ma50 and ma200 and px > ma200 and ma50 > ma200)
    eff = odds / atr if atr else None            # 单位波动赔率（参考，不作门槛）

    return {
        "sym": sym, "date": b[-1][0], "px": round(px, 2),
        "atr": round(atr, 2), "stop": round(stop, 2),
        "ma50": round(ma50, 2) if ma50 else None,
        "ma200": round(ma200, 2) if ma200 else None,
        "hi252": round(hi, 2), "hi_date": hi_d,
        "from_hi": round((px / hi - 1) * 100, 2),
        "tgt": round(tgt, 2), "up": round(up, 2),
        "odds": round(odds, 2),
        "seat": round(seat, 2), "one_share": round(one, 2),
        "one_pct": round(one / nav * 100, 2),
        "fee_drag": round(fee_drag, 2),
        "ok_seat": ok_seat, "ok_odds": ok_odds, "ok_trend": ok_trend,
        "vs_ma200": round((px / ma200 - 1) * 100, 2) if ma200 else None,
        "eff": round(eff, 3) if eff else None,
        "above200": (px > ma200) if ma200 else None,
    }


def main():
    args = [a.upper() for a in sys.argv[1:]]
    watch = args or ["NVDA", "MU"]
    base = ["SPYM", "QQQM"]

    D = json.load(open("daily_data.json", encoding="utf-8"))
    nav = D["nav"]

    res = {"nav": round(nav, 2), "items": [], "base": {}}
    for s in watch:
        try:
            r = judge(s, nav, "s")
        except Exception as e:                  # noqa: BLE001
            r = {"sym": s, "err": "%s: %s" % (type(e).__name__, e)}
        res["items"].append(r)
        time.sleep(0.4)
    for s in base:
        try:
            res["base"][s] = judge(s, nav, "e")
        except Exception as e:                  # noqa: BLE001
            res["base"][s] = {"sym": s, "err": str(e)}
        time.sleep(0.4)

    bp = (res["base"].get("SPYM") or {}).get("eff")
    print("\nNAV = $%.2f   单笔风险预算 = %.0f%%（$%.2f）   现实止损 = %.1f × ATR"
          % (nav, RISK_PCT * 100, nav * RISK_PCT, STOP_MULT))
    print("=" * 96)
    print("%-6s %8s %7s %8s %8s %7s %7s %8s %8s %5s" %
          ("标的", "现价", "ATR%", "止损%", "回高%", "赔率", "距MA200", "占净值", "允许仓位", "结论"))
    print("-" * 96)
    for r in res["items"] + [res["base"].get("SPYM")] + [res["base"].get("QQQM")]:
        if not r or r.get("err"):
            print("%-6s  ! %s" % (r.get("sym") if r else "?", (r or {}).get("err")))
            continue
        if not r["ok_seat"]:
            verdict = "装不下"
        elif not r["ok_odds"]:
            verdict = "赔率低"
        elif not r["ok_trend"]:
            verdict = "趋势破位"
        else:
            verdict = "通过"
        print("%-6s %8.2f %7.2f %8.2f %8.2f %7.2f %7.1f%% %7.2f%% %9.2f %5s" %
              (r["sym"], r["px"], r["atr"], r["stop"], r["up"], r["odds"],
               r.get("vs_ma200") or 0, r["one_pct"], r["seat"], verdict))
    print("-" * 96)
    print("门槛：① 装得下（1 股 ≤ 允许仓位）  ② 赔率 ≥ 2.0  ③ 趋势（价>MA200 且 MA50>MA200）")
    print("参考：效率 = 赔率 ÷ ATR%%，每 1%% 波动换多少赔率（宽基基准 SPYM %.2f）" % (bp or 0))
    print("※ 上行用「52 周高 × 0.97」，不是分析师目标价 —— 不含预测，最保守口径。")

    json.dump(res, open("eq_gate.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n→ eq_gate.json")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
    main()
