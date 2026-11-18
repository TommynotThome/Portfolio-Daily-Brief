import urllib.request, re, json, time

HDR = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
       'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
FIELDS = {'p': 'price', 'c': 'change', 'cp': 'chg_pct', 'cl': 'prev_close',
          'h': 'day_high', 'l': 'day_low', 'o': 'open', 'v': 'volume',
          'ms': 'market_state', 'u': 'as_of',
          'ep': 'ext_price', 'ec': 'ext_change', 'ecp': 'ext_chg_pct', 'epv': 'ext_volume'}


def grab(url):
    """带缓存破坏 + 重试。
    坑：stockanalysis.com 的 CDN 会按 URL 缓存，盘中重复请求同一 URL 会拿回
    上一交易日的收盘价（曾导致 RAM/METU/QQQM 全部返回 10/9 的旧值）。
    必须给每个 URL 加唯一查询参数强制回源。"""
    for attempt in range(4):
        try:
            req = urllib.request.Request(f'{url}?t={int(time.time() * 1000)}', headers=HDR)
            return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')
        except Exception:
            time.sleep(1.0 + attempt * 0.5)
    return ''


def parse_quote(html):
    i = html.find('quote:{')
    if i < 0:
        return None
    seg = html[i:i + 1400]
    d = {}
    for k, name in FIELDS.items():
        if k == 'u':
            m = re.search(r'\bu:"([^"]*)"', seg)
        elif k == 'ms':
            m = re.search(r'\bms:"([^"]*)"', seg)
        elif k == 'es':
            m = re.search(r'\bes:"([^"]*)"', seg)
        elif k == 'eu':
            m = re.search(r'\beu:"([^"]*)"', seg)
        else:
            m = re.search(r'\b%s:(-?\.?\d+\.?\d*)' % k, seg)
        if m:
            raw = m.group(1)
            d[name] = raw if k in ('u', 'ms') else float(raw)
    # 盘前/盘后标签与时间
    m = re.search(r'\bes:"([^"]*)"', seg)
    if m:
        d['ext_state'] = m.group(1)
    m = re.search(r'\beu:"([^"]*)"', seg)
    if m:
        d['ext_as_of'] = m.group(1)
    # p 可能缺失时用 pd（前收盘后价）
    if 'price' not in d:
        m = re.search(r'\bpd:(-?\.?\d+\.?\d*)', seg)
        if m:
            d['price'] = float(m.group(1))
    return d or None


TARGETS = [
    ('SPYM', 'etf'), ('QQQM', 'etf'), ('VXUS', 'etf'), ('METU', 'etf'),
    ('TSLL', 'etf'), ('SGOV', 'etf'),
    ('RAM', 'etf'), ('RAM', 'stocks'),
    ('MU', 'stocks'), ('META', 'stocks'), ('TSLA', 'stocks'),
    ('WDC', 'stocks'), ('STX', 'stocks'), ('SNDK', 'stocks'), ('NVDA', 'stocks'),
    ('SPY', 'etf'), ('QQQ', 'etf'),
    # 候选标的池（筛选用）
    ('SPLG', 'etf'), ('VOO', 'etf'), ('IVV', 'etf'), ('VEU', 'etf'), ('IXUS', 'etf'),
    ('SMH', 'etf'), ('SOXX', 'etf'), ('XLV', 'etf'), ('XLE', 'etf'), ('XLU', 'etf'),
    ('GRID', 'etf'), ('GLD', 'etf'),
    ('NVDL', 'etf'), ('GGLL', 'etf'), ('MSFU', 'etf'), ('AAPU', 'etf'), ('AMZU', 'etf'),
    # 头部科技股（回调参与评估）：Mag7 + 算力硬件链
    ('MSFT', 'stocks'), ('GOOGL', 'stocks'), ('AMZN', 'stocks'), ('AAPL', 'stocks'),
    ('AVGO', 'stocks'), ('TSM', 'stocks'), ('AMD', 'stocks'), ('ORCL', 'stocks'), ('PLTR', 'stocks'),
]

out = {}
for sym, kind in TARGETS:
    key = f'{sym}@{kind}'
    try:
        q = parse_quote(grab(f'https://stockanalysis.com/{kind}/{sym.lower()}/'))
        if q and 'price' in q:
            out[key] = q
            line = (f"{sym:6s} ${q.get('price'):>8.2f}  {q.get('change'):>+6.2f} ({q.get('chg_pct'):>+5.2f}%)  "
                    f"prev ${q.get('prev_close')}  H/L {q.get('day_high')}/{q.get('day_low')}  "
                    f"{q.get('market_state')}  @ {q.get('as_of')}")
            if 'ext_price' in q:
                line += (f"\n       {q.get('ext_state') or 'EXT'}: ${q['ext_price']:.2f} "
                         f"({q.get('ext_chg_pct', 0):+.2f}%)  @ {q.get('ext_as_of')}")
            print(line)
        else:
            print(f'{sym:6s} [{kind}] NO QUOTE')
    except Exception as e:
        print(f'{sym:6s} [{kind}] ERR {type(e).__name__}: {e}')
    time.sleep(0.3)

json.dump(out, open('live_quotes.json', 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
print('\nsaved -> live_quotes.json')
