# -*- coding: utf-8 -*-
"""美股每日战报生成器 —— 抓取 stockanalysis.com 实时+历史数据，重算均线与走势图。"""
import urllib.request, re, json, time, datetime

HDR = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36'}
# ---- 账户数据统一从 portfolio.json 读（2026-11-18 抽离，便于脱敏/开源）----
try:
    _PF = json.load(open('portfolio.json', encoding='utf-8'))
except Exception:
    _PF = {}
POS = dict(_PF.get('positions') or {'SPYM': 0, 'QQQM': 0, 'VXUS': 0})
POS.setdefault('METU', 0)                      # 兼容清仓后为 0 的槽位
CASH = float(_PF.get('cash', 0) or 0)
FEE_PER_TRADE = float(_PF.get('fee_per_trade', 1.88) or 1.88)
NDAYS = 21


def grab(url, n=4):
    """带缓存破坏 + 重试：stockanalysis.com 的 CDN 按 URL 缓存，
    盘中/盘前重复请求同一 URL 会静默返回上一交易日收盘价。"""
    for i in range(n):
        try:
            req = urllib.request.Request(f'{url}?t={int(time.time() * 1000)}', headers=HDR)
            return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')
        except Exception:
            time.sleep(1.0 + i * 0.5)
    return ''


MON = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
       'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}


def history(sym, kind='etf'):
    """返回 [(label, close), ...] 时间降序，含今日盘中"""
    h = grab(f'https://stockanalysis.com/{kind}/{sym.lower()}/history/')
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S):
        tds = [re.sub(r'<[^>]+>', '', x).strip() for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S)]
        tds = [t for t in tds if t and 'svelte' not in t]
        if len(tds) >= 5 and re.match(r'^[A-Z][a-z]{2} \d{1,2}, \d{4}$', tds[0]):
            try:
                out.append((tds[0], float(tds[4])))
            except ValueError:
                pass
        if len(out) >= NDAYS:
            break
    return out


def live(sym, kind='etf'):
    h = grab(f'https://stockanalysis.com/{kind}/{sym.lower()}/')
    i = h.find('quote:{')
    seg = h[i:i + 700] if i > 0 else ''
    d = {}
    for k, name in (('p', 'price'), ('c', 'chg'), ('cp', 'pct'), ('cl', 'prev'),
                    ('h', 'hi'), ('l', 'lo'), ('o', 'op')):
        m = re.search(r'\b%s:(-?\.?\d+\.?\d*)' % k, seg)
        if m:
            d[name] = float(m.group(1))
    m = re.search(r'u:"([^"]*)"', seg)
    d['as_of'] = m.group(1) if m else ''
    return d


print('抓取实时 + 历史数据 ...')
data = {}
for s in POS:
    lv = live(s, 'etf')
    hist = history(s, 'etf')
    hist[0] = (hist[0][0], lv['price'])          # 首行替换为实时价
    closes = [c for _, c in hist]
    labels = [lab for lab, _ in hist]
    data[s] = {**lv, 'closes': closes, 'labels': labels,
               'ma5': sum(closes[:5]) / 5, 'ma10': sum(closes[:10]) / 10,
               'ma20': sum(closes[:20]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)}
    print(f"  {s:5s} ${lv['price']:>8.2f} {lv['pct']:>+6.2f}%  MA5 {data[s]['ma5']:.2f} "
          f"MA10 {data[s]['ma10']:.2f} MA20 {data[s]['ma20']:.2f}  @ {lv['as_of']}")
    time.sleep(0.3)

tsll = live('TSLL', 'etf')
print(f"  TSLL  ${tsll['price']:.2f} {tsll['pct']:+.2f}%（已清仓）")

# ---- 账户 ----
vals = {k: POS[k] * data[k]['price'] for k in POS}
SEC = sum(vals.values())
NAV = SEC + CASH
LEV = vals['METU']   # E 槽已关闭（RAM 10/13 清仓）
CORE = vals['SPYM'] + vals['QQQM'] + vals['VXUS']
print(f"\n净值 ${NAV:,.2f} | 杠杆 {LEV/NAV*100:.2f}% | 核心 {CORE/NAV*100:.2f}% | 现金 {CASH/NAV*100:.2f}%")

json.dump({'data': data, 'tsll': tsll, 'nav': NAV, 'vals': vals, 'cash': CASH},
          open('daily_data.json', 'w', encoding='utf-8'), indent=1)

# ---- 走势图 ----
def short(lab):
    m = re.match(r'([A-Z][a-z]{2}) (\d{1,2}), (\d{4})', lab)
    return f'{MON[m.group(1)]}/{int(m.group(2))}'


labs = [short(l) for l in data['SPYM']['labels']][::-1]   # 时间正序


def norm(sym):
    c = data[sym]['closes'][::-1]
    return [(v / c[0] - 1) * 100 for v in c]


def svg_chart():
    X0, X1, W = 58, 640, 640
    n = len(labs)
    px = lambda i: X0 + (X1 - X0) * i / (n - 1)
    core = {k: norm(k) for k in ('SPYM', 'QQQM', 'VXUS')}
    bet = {k: norm(k) for k in ('METU',)}
    allc = [v for s in core.values() for v in s]
    allb = [v for s in bet.values() for v in s]
    ca = max(6, max(abs(min(allc)), abs(max(allc))) * 1.15)
    ba_lo, ba_hi = min(-25, min(allb) * 1.15), max(50, max(allb) * 1.1)

    # 面板A
    A0, A1 = 42.9, 128.0
    pa = lambda v: (A0 + A1) / 2 - v / ca * (A1 - A0) / 2
    # 面板B
    B0, B1 = 166.3, 258.0
    pb = lambda v: B1 - (v - ba_lo) / (ba_hi - ba_lo) * (B1 - B0)

    s = [f'<svg aria-label="近{n}日归一化走势" role="img" viewBox="0 0 760 300" width="100%">']
    s.append(f'  <text x="14" y="34" font-size="11.5" font-weight="600" fill="#5F5E5A">核心仓</text>')
    s.append(f'  <text x="14" y="50" font-size="10.5" fill="#98a0aa">纵轴 ±{ca:.0f}%</text>')
    for v, lb in ((ca, f'+{ca:.0f}%'), (0, '0%'), (-ca, f'−{ca:.0f}%')):
        y = pa(v)
        s.append(f'  <line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="'
                 f'{"#c9d0d8" if v==0 else "#edf0f3"}" stroke-width="1"'
                 f'{"" if v==0 else ""}{" stroke-dasharray=\'3 3\'" if v==0 else ""}/>')
        s.append(f'  <text x="52" y="{y+3.5:.1f}" font-size="10" fill="#a8afb8" text-anchor="end">{lb}</text>')
    COL = {'SPYM': '#185FA5', 'QQQM': '#378ADD', 'VXUS': '#0F6E56', 'METU': '#854F0B'}
    tagY = {}
    for k, ser in core.items():
        pts = ' '.join(f'{px(i):.1f},{pa(v):.1f}' for i, v in enumerate(ser))
        s.append(f'  <polyline fill="none" stroke="{COL[k]}" stroke-width="1.8" points="{pts}"/>')
        s.append(f'  <circle cx="{px(n-1):.1f}" cy="{pa(ser[-1]):.1f}" r="3.2" fill="{COL[k]}"/>')
        tagY[k] = pa(ser[-1])
    for k in ('SPYM', 'QQQM', 'VXUS'):
        y = tagY[k]
        y = min(max(y, 40), 122)
        s.append(f'  <text x="646" y="{y+3:.1f}" font-size="10" fill="{COL[k]}">{k} {core[k][-1]:+.1f}%</text>')

    s.append(f'  <text x="14" y="158" font-size="11.5" font-weight="600" fill="#5F5E5A">赌注仓（2倍杠杆）</text>')
    s.append(f'  <text x="14" y="174" font-size="10.5" fill="#98a0aa">纵轴 {ba_hi:.0f}%~{ba_lo:.0f}%，刻度约核心仓的 {abs(ba_hi/ca):.0f} 倍</text>')
    for v in (ba_hi, 0, ba_lo):
        y = pb(v)
        s.append(f'  <line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="'
                 f'{"#c9d0d8" if v==0 else "#edf0f3"}" stroke-width="1"'
                 f'{" stroke-dasharray=\'3 3\'" if v==0 else ""}/>')
        s.append(f'  <text x="52" y="{y+3.5:.1f}" font-size="10" fill="#a8afb8" text-anchor="end">'
                 f'{"0%" if v==0 else f"{v:+.0f}%"}</text>')
    tagY2 = {}
    for k, ser in bet.items():
        pts = ' '.join(f'{px(i):.1f},{pb(v):.1f}' for i, v in enumerate(ser))
        s.append(f'  <polyline fill="none" stroke="{COL[k]}" stroke-width="1.8" points="{pts}"/>')
        s.append(f'  <circle cx="{px(n-1):.1f}" cy="{pb(ser[-1]):.1f}" r="3.2" fill="{COL[k]}"/>')
        tagY2[k] = pb(ser[-1])
    for k in ('METU',):
        y = min(max(tagY2[k], 162), 252)
        s.append(f'  <text x="646" y="{y+3:.1f}" font-size="10" fill="{COL[k]}">{k} {bet[k][-1]:+.1f}%</text>')

    s.append(f'  <line x1="{X0}" y1="278" x2="{X1}" y2="278" stroke="#e8ebee" stroke-width="1"/>')
    for i, lb in ((0, labs[0]), (n // 3, labs[n // 3]), (2 * n // 3, labs[2 * n // 3]),
                  (n - 2, labs[n - 2]), (n - 1, labs[n - 1])):
        fw = '600' if i == n - 1 else '400'
        fc = '#5F5E5A' if i == n - 1 else '#a8afb8'
        s.append(f'  <text x="{px(i):.1f}" y="292" font-size="10" font-weight="{fw}" fill="{fc}" '
                 f'text-anchor="middle">{lb}</text>')
    s.append('</svg>')
    return '\n'.join(s)


open('chart.svg', 'w', encoding='utf-8').write(svg_chart())
print('走势图已生成 chart.svg，点数', len(labs), labs[0], '→', labs[-1])
