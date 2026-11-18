# -*- coding: utf-8 -*-
"""
K 线数据抓取：CBOE 免密钥历史行情 -> 日/周/月/年四周期 OHLC。

数据源：https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/<SYMBOL>.json
  - 免 API Key，官方延迟报价（日线收盘价与 stockanalysis 完全一致，已交叉验证 6/6）。
  - 该端点返回 307 重定向，必须跟随重定向（urllib 默认跟随）。
  - 覆盖范围：ETF 与个股都有，深度不一（SPYM 自 2017、MU 自 2010、METU 自 2024 成立起）。

输出：kline_data.json
  { "SPYM": { "name": "标普500", "d": [[date,o,h,l,c],...], "w": [...], "m": [...], "y": [...] } }
  date 用紧凑格式：日/周 = "YYMMDD"，月 = "YYYYMM"，年 = "YYYY"
"""
import json
import os
import time
import urllib.request
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "kline_data.json")

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/%s.json"

# 标的 -> (中文名, 说明)
TARGETS = OrderedDict([
    ("SPYM",   ("标普500", "SPDR 标普500 ETF")),
    ("QQQM",   ("纳指100", "Invesco 纳斯达克100")),
    ("VXUS",   ("非美全球", "先锋非美全球股票")),
    ("METU",   ("2倍 META", "Direxion 2x 做多 META")),
    ("RAM",    ("2倍 DRAM", "Roundhill 2x 存储篮子")),
    ("MU",     ("美光", "DRAM 原厂")),
    ("SNDK",   ("闪迪", "NAND 原厂")),
    ("WDC",    ("西部数据", "HDD + 闪存")),
    ("STX",    ("希捷", "HDD")),
    # 候选池 / 观察名单（10/15 用户指定加入）
    ("NBIS",   ("Nebius", "AI 云 / GPU 基建")),
    ("SMH",    ("半导体ETF", "VanEck 半导体")),
    ("SPCX",   ("SpaceX", "2026-06 上市")),
    # 母公司（10/19 加）：METU 的底层，META 财报 10/21 盘后，需要看母股形态
    ("META",   ("Meta", "METU 的底层标的")),
])

# 各周期保留的 bar 数（近端）
BARS = {"d": 150, "w": 120, "m": 72, "y": 18}

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json,*/*",
    "Cache-Control": "no-cache",
}


def grab(url, tries=4):
    """带重试的抓取。CBOE 按 URL 缓存，拼时间戳强制回源（同 fetch_live.py 的教训）。"""
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url + "?t=%d" % int(time.time() * 1000), headers=HDR)
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(1.2)
    raise last


def _rk(x):
    return round(float(x), 2)


def aggregate(daily):
    """把日线合成周/月/年。输入 [(date, o, h, l, c)] 升序。"""
    def key_w(d):
        y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
        # ISO 周（周一起算）
        import datetime
        dt = datetime.date(y, m, dd)
        iy, iw, _ = dt.isocalendar()
        return "%04dW%02d" % (iy, iw)

    def key_m(d):
        return d[:7]

    def key_y(d):
        return d[:4]

    out = {}
    for tag, kf in (("w", key_w), ("m", key_m), ("y", key_y)):
        buf = OrderedDict()
        for d, o, h, l, c in daily:
            k = kf(d)
            if k not in buf:
                buf[k] = [d, o, h, l, c]      # [0]日期 [1]开 [2]高 [3]低 [4]收
            else:
                b = buf[k]
                b[0] = d                      # 周期内最后一个交易日
                b[2] = max(b[2], h)           # 高 = 期内最高
                b[3] = min(b[3], l)           # 低 = 期内最低
                b[4] = c                      # 收 = 期内末收（开保持期内首开）
        out[tag] = list(buf.values())
    return out


def short_date(d, p):
    y, m, dd = d[:4], d[5:7], d[8:10]
    if p == "d" or p == "w":
        return y[2:] + m + dd
    if p == "m":
        return y + m
    return y


def fmt_date_iso(d, p):
    y, m, dd = d[:4], d[5:7], d[8:10]
    if p == "d" or p == "w":
        return "%s-%s-%s" % (y, m, dd)
    if p == "m":
        return "%s-%s-01" % (y, m)
    return "%s-01-01" % y


def main():
    result = {}
    ok = fail = 0
    for sym, (name, desc) in TARGETS.items():
        rec = {"name": name, "desc": desc}
        try:
            j = json.loads(grab(BASE % sym))
            rows = j.get("data") or []
            daily = []
            for r in rows:
                try:
                    daily.append((r["date"], _rk(r["open"]), _rk(r["high"]),
                                  _rk(r["low"]), _rk(r["close"])))
                except Exception:       # noqa: BLE001
                    continue
            daily.sort(key=lambda x: x[0])
            if not daily:
                raise ValueError("empty")
            rec["d"] = daily[-BARS["d"]:]
            agg = aggregate(daily)
            for tag in ("w", "m", "y"):
                rec[tag] = agg[tag][-BARS[tag]:]
            # 紧凑化：日期字段单独转短码，OHLC 保留两位
            packed = {"name": name, "desc": desc}
            for tag in ("d", "w", "m", "y"):
                packed[tag] = [[short_date(b[0], tag), b[1], b[2], b[3], b[4]]
                               for b in rec[tag]]
            # 只存紧凑短码日期，显示用的完整日期由 JS 从短码还原，省体积
            result[sym] = packed
            ok += 1
            print("  %-5s %-9s 日%3d 周%3d 月%3d 年%3d | 最新 %s %.2f"
                  % (sym, name, len(packed["d"]), len(packed["w"]),
                     len(packed["m"]), len(packed["y"]),
                     daily[-1][0], daily[-1][4]))
        except Exception as e:          # noqa: BLE001
            fail += 1
            print("  %-5s ERR %s: %s" % (sym, type(e).__name__, str(e)[:70]))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT)
    print("\n成功 %d / 失败 %d -> %s (%.1f KB)" % (ok, fail, os.path.basename(OUT), size / 1024))


if __name__ == "__main__":
    main()
