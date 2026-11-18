# -*- coding: utf-8 -*-
"""patch_pre.py —— 抓盘前/盘后价（ext_price / ext_chg_pct / ext_as_of）写回 live_quotes.json

为什么单独一个脚本：
  gen_daily.py 只抓「上一交易日收盘」（p），fetch_live.py 会在最后几个请求上卡住
  且不落盘。而盘前战报的价格列必须用 ext_price（ep），否则整页显示的是昨天的收盘。

字段（stockanalysis 的 quote 对象）：
  p   最新成交价      cl  前收盘      cp  涨跌幅
  ep  盘前/盘后价     ecp 盘前/盘后涨跌幅    eu  盘前/盘后时间戳
用法：<python> patch_pre.py [SYM1 SYM2 ...]   默认抓持仓 4 只（ETF）
"""
import json
import re
import sys
import time
import urllib.request

HDR = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Cache-Control': 'no-cache',
}
DEFAULT = [('SPYM', 'etf'), ('QQQM', 'etf'), ('VXUS', 'etf'), ('METU', 'etf')]


def grab(url, timeout=15):
    req = urllib.request.Request(url + '?t=%d' % int(time.time() * 1000), headers=HDR)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')


def parse_ext(html):
    i = html.find('quote:{')
    if i < 0:
        return None
    seg = html[i:i + 1600]
    d = {}
    for k, f in (('p', 'price'), ('cl', 'prev_close'), ('cp', 'chg_pct'),
                 ('ep', 'ext_price'), ('ecp', 'ext_chg_pct'),
                 ('h', 'day_high'), ('l', 'day_low')):
        m = re.search(r'\b%s:(-?\.?\d+\.?\d*)' % k, seg)
        if m:
            d[f] = float(m.group(1))
    m = re.search(r'\bms:"([^"]*)"', seg)
    if m:
        d['market_state'] = m.group(1)
    m = re.search(r'\bu:"([^"]*)"', seg)
    if m:
        d['as_of'] = m.group(1)
    m = re.search(r'\beu:"([^"]*)"', seg)
    if m:
        d['ext_as_of'] = m.group(1)
    # 盘前/盘后的状态标签：stockanalysis 在 quote 里给 es:"pre" / "post"
    m = re.search(r'\bes:"([^"]*)"', seg)
    if m:
        d['ext_state'] = m.group(1)
    return d


def main():
    args = sys.argv[1:]
    targets = [(a, 'etf') for a in args] if args else DEFAULT
    L = json.load(open('live_quotes.json', encoding='utf-8'))
    ok = 0
    for s, kind in targets:
        try:
            q = parse_ext(grab('https://stockanalysis.com/%s/%s/'
                               % (kind, s.lower())))
        except Exception as e:
            print('%-5s 抓取失败 %s' % (s, type(e).__name__))
            continue
        if not q:
            print('%-5s 解析失败' % s)
            continue
        for key in list(L):
            if key.split('@')[0] == s:
                L[key].update(q)
        ok += 1
        print('%-5s 收 %8.2f | 盘前 %s (%s) | state=%s %s'
              % (s, q.get('price') or 0,
                 ('%8.2f' % q['ext_price']) if q.get('ext_price') else '   —   ',
                 ('%+.2f%%' % q['ext_chg_pct']) if q.get('ext_chg_pct') is not None else '  —  ',
                 q.get('ext_state') or '-', q.get('ext_as_of') or '-'))
        time.sleep(0.3)
    json.dump(L, open('live_quotes.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('-> live_quotes.json  更新 %d / %d' % (ok, len(targets)))


if __name__ == '__main__':
    main()
