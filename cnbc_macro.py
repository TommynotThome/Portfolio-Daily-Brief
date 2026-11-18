# -*- coding: utf-8 -*-
"""
cnbc_macro.py —— 实时指数 / 利率 / 波动率（免密钥）

为什么需要它：
  2026-10-15 起 CBOE 的 delayed_quotes 报价端点返回 AccessDenied（被封），
  而 charts/historical 只覆盖股票/ETF、且滞后一天 —— 实时利率与指数出现缺口。
  实测 CNBC 的 restQuote 接口免密钥、免注册、可一次批量拿全部宏观标的。

覆盖：10Y/30Y/5Y/2Y 美债收益率、VIX、标普500、纳斯达克综合、道琼斯、罗素2000、费城半导体、WTI

另含：汇率块（frankfurter.app，ECB 参考汇率，免密钥）—— USD/CNY、USD/HKD，写入同一文件的 "_FX" 键。

输出：cnbc_macro.json   形如
  {"US10Y": {"label": "...", "last": 5.058, "chg_bp": -13.2, "prev": 5.19, "as_of": "...", ...}, ...}
  利率类的 last/prev 已转成百分数（5.058 = 5.058%），chg_bp 为基点变动。

运行：<python> cnbc_macro.py
"""
import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cnbc_macro.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# CNBC 符号 -> (中文名, 是否利率类[bps])
SYMS = {
    "US10Y": ("10年期美债", True),
    "US30Y": ("30年期美债", True),
    "US5Y":  ("5年期美债", True),
    "US2Y":  ("2年期美债", True),
    ".VIX":  ("VIX 恐慌指数", False),
    ".SPX":  ("标普500", False),
    ".IXIC": ("纳斯达克综合", False),
    ".DJI":  ("道琼斯", False),
    ".RUT":  ("罗素2000", False),
    ".SOX":  ("费城半导体", False),
    "USO":   ("WTI 原油 ETF", False),
}

URL = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
       "?symbols=%s&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1"
       "&output=json&events=1")


def _num(s):
    """'5.058%' / '7,702.75' / '-0.132' -> float"""
    if s is None:
        return None
    try:
        return float(re.sub(r"[^0-9eE+\-.]", "", str(s)))
    except Exception:
        return None


def fetch(retries=3):
    url = URL % "|".join(SYMS)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url + "&t=%d" % int(time.time() * 1000),
                                         headers={"User-Agent": UA, "Cache-Control": "no-cache"})
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception as e:
            last = e
            time.sleep(1.0 + i * 0.6)
    raise last


FX = ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY,HKD")


def fetch_fx(retries=3):
    """frankfurter.app（ECB 参考汇率，免密钥）。注意 api.frankfurter.app 会 301 → 用 .dev/v1。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(FX, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            j = json.loads(raw)
            return {"date": j.get("date"), "base": j.get("base"),
                    "rates": j.get("rates") or {}}
        except Exception as e:
            last = e
            time.sleep(1.0 + i * 0.6)
    return {"error": str(last)[:80]} if last else {}


def build():
    d = fetch()
    out = {}
    for it in d["FormattedQuoteResult"]["FormattedQuote"]:
        s = it.get("symbol")
        if s not in SYMS:
            continue
        label, is_rate = SYMS[s]
        last = _num(it.get("last"))
        prev = _num(it.get("previous_day_closing"))
        open_ = _num(it.get("open"))
        hi = _num(it.get("high"))
        lo = _num(it.get("low"))

        rec = {
            "label": label,
            "last": last,
            "prev": prev,
            "open": open_,
            "high": hi,
            "low": lo,
            "is_rate": is_rate,
            "as_of": it.get("last_timedate"),
        }
        if last is not None and prev is not None:
            if is_rate:
                rec["chg_bp"] = round((last - prev) * 100, 1)   # 基点
            else:
                rec["chg_pct"] = round((last / prev - 1) * 100, 2)
        out[s] = rec
    out["_FX"] = fetch_fx()
    return out


if __name__ == "__main__":
    res = build()
    fx = res.pop("_FX", {})
    for s, r in res.items():
        if r["is_rate"]:
            print("  %-8s %-12s %8.3f%%   %+6.1fbp   (前值 %.3f%%)  %s"
                  % (s, r["label"], r["last"], r.get("chg_bp", 0), r["prev"], r["as_of"]))
        else:
            print("  %-8s %-12s %12s   %+6.2f%%   (前值 %s)  %s"
                  % (s, r["label"], format(r["last"], ","),
                     r.get("chg_pct", 0), format(r["prev"], ","), r["as_of"]))
    if fx.get("rates"):
        print("  %-8s %-12s USD/CNY %.4f ｜ USD/HKD %.4f   (ECB 参考汇率 %s)"
              % ("_FX", "汇率", fx["rates"].get("CNY", 0), fx["rates"].get("HKD", 0), fx.get("date")))
        res["_FX"] = fx
    else:
        print("  [汇率降级] frankfurter 未返回:", fx.get("error", "?"))
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n成功 %d / %d + 汇率 -> cnbc_macro.json" % (len([k for k in res if k != "_FX"]), len(SYMS)))
