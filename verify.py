# -*- coding: utf-8 -*-
"""verify.py —— 战报宏观/行情数字的自动交叉校验

为什么需要它
------------
日报里的宏观数字原先只有一个来源，单源数据无法自证对错；靠人工逐个核对不现实。
本脚本用**独立权威源**自动比对，把「数字准不准」变成每日一次、无需人参与的检查。

校验原理（关键）
----------------
主源的 `prev` 字段 = **最近一个已完成交易日的收盘**。
官方源给出的也是同一交易日的官方值 —— 两者本应相等。
**用「前值是否吻合」证明这条 feed 可信；feed 可信则同一 feed 的实时值也可信。**

⚠️ 必须按日期对齐（2026-10-16 修）
----------------------------------
各源发布节奏不同：CBOE 当日有值、财政部滞后约 1 个交易日、FRED 有时滞后 2 天。
**绝不能默认「各源最新一行」是同一天** —— 必须先定下基准日 `REF_DATE`，
再回每个源里查那一天；查不到就判 `skip` 并写明「源滞后」。
（此前就是因为拿 CNBC 的 10/15 前值去比 FRED 的 10/14 值，产生了假警报。）

独立校验源
----------
  ① CBOE 官方收盘（经 obb_fetch.py 落到 obb_macro.json）—— 最强，当日即有效
  ② 美国财政部官方日度国债收益率曲线（par yield）—— 2Y/5Y/10Y/30Y
  ③ FRED（圣路易斯联储）—— VIXCLS / SP500
  ④ CBOE 官方历史行情 —— 持仓与标的的前收盘

输出：verify_report.json
  {"as_of": "...", "ref_date": "...", "items": [...], "summary": {...}}

运行：<python> verify.py
"""
import datetime
import io
import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'verify_report.json')

UA_BROWSER = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
UA_CURL = 'curl/8.4.0'      # ⚠️ FRED 拒绝浏览器 UA，必须用 curl 型 UA

TREASURY = ('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
            'daily-treasury-rates.csv/%s/all?type=daily_treasury_yield_curve'
            '&field_tdr_date_value=%s&page&_format=csv')
FRED = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s'
CBOE_HIST = 'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/%s.json'

# 收益率容差 ±3bp：财政部公布的是 par yield（在券票面利率口径），
# 与市场在券基准/CNBC 报价的正常差异可达 2–3bp，尤其短端（2Y/5Y）更明显。
TOL = {'yield': 0.030, 'vix': 0.06, 'index': 0.0006, 'price': 0.004}

MON = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
       'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}


def get(url, ua=UA_BROWSER, retries=3, timeout=35):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua,
                                                       'Cache-Control': 'no-cache'})
            return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')
        except Exception as e:
            last = e
            time.sleep(1.0 + i * 0.8)
    raise last


def jload(name, default=None):
    try:
        with io.open(os.path.join(HERE, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def ref_date(quotes):
    """推断「最近一个已完成交易日」。

    - 已收盘（closed）：as_of 就是刚结束的那个交易日
    - 盘中（open）/ 未到 16:00：as_of 是今天且尚未收盘 → 基准日 = 上一工作日
    """
    q = quotes.get('SPYM@etf') or {}
    as_of = q.get('as_of') or ''
    state = (q.get('market_state') or '').lower()
    m = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})', as_of)
    if not m:
        d0 = (jload('daily_data.json', {}).get('data') or {}).get('SPYM') or {}
        as_of = d0.get('as_of') or ''
        m = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})', as_of)
    if not m:
        return None, None
    d = datetime.date(int(m.group(3)), MON.get(m.group(1), 1), int(m.group(2)))
    hh = re.search(r'(\d{1,2}):\d{2}\s*(AM|PM)', as_of)
    h = None
    if hh:
        h = int(hh.group(1)) % 12 + (12 if hh.group(2) == 'PM' else 0)
    if state == 'open' or (h is not None and h < 16):
        for _ in range(10):
            d -= datetime.timedelta(days=1)
            if d.weekday() < 5:
                break
    return d.strftime('%Y-%m-%d'), as_of


def fetch_treasury():
    """{'YYYY-MM-DD': {'10 Yr': 5.19, ...}}"""
    yr = time.strftime('%Y')
    txt = get(TREASURY % (yr, yr))
    lines = [l for l in txt.strip().split('\n') if l.strip()]
    if len(lines) < 2:
        return {}
    hdr = [h.strip().strip('"') for h in lines[0].split(',')]
    out = {}
    for ln in lines[1:]:
        f = [x.strip().strip('"') for x in ln.split(',')]
        if len(f) != len(hdr):
            continue
        rec, date = {}, None
        for k, v in zip(hdr, f):
            if k == 'Date':
                date = v
                continue
            try:
                rec[k] = float(v)
            except Exception:
                pass
        if date and rec:
            mm, dd, yy = date.split('/')
            out['%s-%02d-%02d' % (yy, int(mm), int(dd))] = rec
    return out


def fetch_fred(series):
    """{'YYYY-MM-DD': float}"""
    txt = get(FRED % series, ua=UA_CURL)
    out = {}
    for ln in txt.strip().split('\n')[1:]:
        p = ln.split(',')
        if len(p) < 2:
            continue
        try:
            out[p[0].strip()] = float(p[1])
        except Exception:
            continue
    return out


def fetch_cboe_close(sym):
    """{'YYYY-MM-DD': 收盘}。

    ⚠️ CBOE charts/historical 返回的是**字典列表**（date/open/high/low/close），不是数组；
    该端点会 307 重定向到 cdn-api.cboe.com（urllib 自动跟随）。
    """
    j = json.loads(get(CBOE_HIST % sym))
    out = {}
    for b in (j.get('data') or []):
        try:
            out[str(b['date'])] = float(b['close'])
        except Exception:
            pass
    return out


def main():
    cnbc = jload('cnbc_macro.json', {})
    quotes = jload('live_quotes.json', {})
    obb = jload('obb_macro.json', {})

    REF, ref_src = ref_date(quotes)

    # ⚠️ 取主源的哪个字段，取决于「这一行报价是不是 REF 那天收盘」：
    #   · 已收盘且 as_of 的日期 == REF → 取 last（当日收盘）
    #   · 盘中/盘前 → 取 prev（上一交易日收盘）
    # 【2026-10-19 修】原来只看 market_state，但**盘前 market_state 仍是 closed**，
    # 于是 VIX 被取到盘前期货实时值 18.68 去比 10/16 官方收盘 19.99，天天假警报。
    # 新规则：逐条看 as_of —— 带完整日期且 == REF 才用 last；只写 "6:27 AM EDT"
    # 这种无日期的就是实时值，必须退回 prev。
    _STATE0 = ((quotes.get('SPYM@etf') or {}).get('market_state') or '').lower()
    _USE_LAST = _STATE0 in ('closed', 'post', 'after', 'afterhours', 'postmarket')
    _SRC_A = 'CNBC 当日收盘' if _USE_LAST else 'CNBC 前值'

    def _row_is_ref(r):
        """该行报价是否就是 REF 那天收盘。"""
        as_of = str(r.get('as_of') or '')
        m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', as_of)
        if m:
            yy = int(m.group(3))
            yy += 2000 if yy < 100 else 0
            try:
                return datetime.date(yy, int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d') == REF
            except Exception:
                return False
        m2 = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})', as_of)
        if m2:
            try:
                return datetime.date(int(m2.group(3)), MON.get(m2.group(1), 1),
                                     int(m2.group(2))).strftime('%Y-%m-%d') == REF
            except Exception:
                return False
        # 【2026-11-01 修】债券行的 as_of 只有时间没有日期（"4:05 PM EST"），
        # 旧逻辑一律当"非基准日"退回 prev → 拿周四收盘去比周五官方收盘，5 项假警报。
        # 判据：收盘快照的时点一定 >= 16:00；盘前/期货是 6:27 AM 这类。
        m3 = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', as_of)
        if m3:
            hh = int(m3.group(1)) % 12 + (12 if m3.group(3).upper() == 'PM' else 0)
            return hh >= 16          # 16:05 PM → 当日收盘；6:27 AM → 盘前，仍用 prev
        return False

    def cn_val(nk):
        r = cnbc.get(nk) or {}
        v = r.get('last') if (r.get('last') is not None and _row_is_ref(r)) else r.get('prev')
        return v
    if not REF:
        print('⛔ 无法确定基准日，退出')
        return 1
    print('基准日 REF_DATE = %s   （来自 %s）' % (REF, ref_src))

    items = []

    def add(key, label, kind, a, sa, b, sb, day, skip_note=None):
        if skip_note:
            items.append(dict(key=key, label=label, day=day, a=a, b=None,
                              verdict='skip', note=skip_note, src_a=sa, src_b=sb))
            return
        if a is None or b is None:
            items.append(dict(key=key, label=label, day=day, a=a, b=b,
                              verdict='skip', note='该日至少一方无值',
                              src_a=sa, src_b=sb))
            return
        if kind == 'yield':
            diff = round((a - b) * 100, 2)
            tol, txt = TOL['yield'] * 100, '%+.2fbp' % ((a - b) * 100)
        elif kind == 'index':
            diff = round((a / b - 1) * 100, 3)
            tol, txt = TOL['index'] * 100, '%+.3f%%' % ((a / b - 1) * 100)
        else:
            diff = round(a - b, 4)
            tol, txt = TOL.get(kind, 0.06), '%+.4f' % (a - b)
        ok = abs(diff) <= tol
        items.append(dict(key=key, label=label, day=day, a=a, b=b, diff=diff,
                          diff_txt=txt, tol=tol, src_a=sa, src_b=sb,
                          verdict='ok' if ok else 'warn',
                          note='一致' if ok else '**不一致，需人工复核**'))

    # ---------- A. CBOE 官方收盘（最强，当日即有效）----------
    CBOE_MAP = {'TNX': ('US10Y', '10年期美债', 'yield', 'value_pct'),
                'TYX': ('US30Y', '30年期美债', 'yield', 'value_pct'),
                'FVX': ('US5Y', '5年期美债', 'yield', 'value_pct'),
                'VIX': ('.VIX', 'VIX 恐慌指数', 'vix', 'close'),
                'SPX': ('.SPX', '标普500', 'index', 'close')}
    for ck, (nk, label, kind, field) in CBOE_MAP.items():
        r = obb.get(ck) or {}
        if not r.get('ok'):
            continue
        if r.get('date') != REF:
            items.append(dict(key=ck, label=label + ' vs CBOE', day=REF,
                              a=cn_val(nk),
                              src_a=_SRC_A, src_b='CBOE 官方', verdict='skip',
                              note='CBOE 该日无值（其最新为 %s）' % r.get('date')))
            continue
        add(ck, label + ' vs CBOE', kind, cn_val(nk),
            _SRC_A, r.get(field), 'CBOE 官方', REF)

    # ---------- B. 美债收益率：CNBC 前值 vs 财政部官方 ----------
    try:
        trs = fetch_treasury()
    except Exception as e:
        trs = {}
        items.append(dict(key='_treasury', label='财政部收益率曲线', verdict='skip',
                          note='抓取失败：%s' % str(e)[:70]))
    tr_latest = max(trs) if trs else '—'
    for cnbc_key, tcol, label in (('US2Y', '2 Yr', '2年期美债'),
                                  ('US5Y', '5 Yr', '5年期美债'),
                                  ('US10Y', '10 Yr', '10年期美债'),
                                  ('US30Y', '30 Yr', '30年期美债')):
        a = cn_val(cnbc_key)
        t = trs.get(REF)
        if t is None:
            add(cnbc_key, label + ' vs 财政部', 'yield', a, _SRC_A, None,
                '财政部官方', REF,
                skip_note='财政部该日尚未发布（其最新为 %s，约滞后 1 个交易日）' % tr_latest)
        else:
            add(cnbc_key, label + ' vs 财政部', 'yield', a, _SRC_A,
                t.get(tcol), '财政部官方', REF)

    # ---------- C. VIX / 标普：CNBC 前值 vs FRED ----------
    for series, nk, label, kind in (('VIXCLS', '.VIX', 'VIX 恐慌指数', 'vix'),
                                    ('SP500', '.SPX', '标普500', 'index')):
        try:
            fr = fetch_fred(series)
        except Exception as e:
            items.append(dict(key=series, label=label + ' vs FRED', verdict='skip',
                              note='FRED 抓取失败：%s' % str(e)[:60]))
            continue
        fr_latest = max(fr) if fr else '—'
        a = cn_val(nk)
        if fr.get(REF) is None:
            add(series, label + ' vs FRED', kind, a, _SRC_A, None,
                'FRED ' + series, REF,
                skip_note='FRED 该日尚未发布（其最新为 %s，偶滞后 2 天）' % fr_latest)
        else:
            add(series, label + ' vs FRED', kind, a, _SRC_A,
                fr.get(REF), 'FRED ' + series, REF)

    # ---------- D. 持仓/标的：stockanalysis vs CBOE 官方收盘 ----------
    # 优先用 obb_fetch.py 的交叉校验结果 —— 它是 CBOE **当日**官方收盘（自带时间戳），
    # 比滞后一天的 charts/historical 强得多。
    # 取哪个字段取决于时段：已收盘 → price 就是 REF 收盘；盘中 → prev_close 才是 REF 收盘。
    open_now = (quotes.get('SPYM@etf') or {}).get('market_state', '').lower() == 'open'
    KMAP = {'SPYM': 'etf', 'QQQM': 'etf', 'VXUS': 'etf', 'METU': 'etf',
            'MU': 'stocks', 'META': 'stocks', 'RAM': 'etf'}
    cc = {}
    for r in (jload('obb_crosscheck.json', []) or []):
        if isinstance(r, dict) and r.get('symbol'):
            cc[r['symbol']] = r
    for sym, kind in KMAP.items():
        q = quotes.get('%s@%s' % (sym, kind)) or {}
        val = q.get('prev_close') if open_now else q.get('price')
        field = 'prev_close' if open_now else 'price'
        if not val:
            continue
        r = cc.get(sym) or {}
        ts = str(r.get('cboe_last_ts') or '')[:10]
        if r.get('ok') and ts == REF and r.get('cboe_close') is not None:
            add(sym, sym + ' vs CBOE', 'price', val, 'stockanalysis ' + field,
                r.get('cboe_close'), 'CBOE 官方收盘', REF)
            continue
        # 回退：CBOE 历史端点（常滞后一天）
        try:
            cboe = fetch_cboe_close(sym)
        except Exception as e:
            items.append(dict(key=sym, label=sym, verdict='skip',
                              note='CBOE 抓取失败：%s' % str(e)[:50]))
            continue
        if cboe.get(REF) is None:
            items.append(dict(key=sym, label=sym + ' vs CBOE', day=REF, a=val,
                              src_a='stockanalysis ' + field, src_b='CBOE 官方', verdict='skip',
                              note='CBOE 该日无值（历史端点最新为 %s，常滞后一天）'
                                   % (max(cboe) if cboe else '—')))
            continue
        add(sym, sym + ' vs CBOE', 'price', val, 'stockanalysis ' + field,
            cboe[REF], 'CBOE 收盘', REF)

    # ---------- 汇总 ----------
    ok = sum(1 for i in items if i['verdict'] == 'ok')
    warn = sum(1 for i in items if i['verdict'] == 'warn')
    skip = sum(1 for i in items if i['verdict'] == 'skip')
    rep = dict(as_of=time.strftime('%Y-%m-%d %H:%M'), ref_date=REF, items=items,
               summary=dict(ok=ok, warn=warn, skip=skip, total=len(items)))
    with io.open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)

    mark = {'ok': '✓', 'warn': '⚠', 'skip': '·'}
    print('\n=== 交叉校验（基准日 %s）===' % REF)
    print('  %-2s %-24s %-12s %-12s %-12s %s'
          % ('', '指标', '基准日', '主源', '校验源', '结果'))
    for i in items:
        if i['verdict'] == 'skip' and str(i.get('key', '')).startswith('_'):
            print('  ·  %s：%s' % (i['label'], i.get('note', '')))
            continue
        print('  %-2s %-24s %-12s %-12s %-12s %s'
              % (mark[i['verdict']], i.get('label', ''), i.get('day', '') or '—',
                 str(i.get('a') if i.get('a') is not None else '—'),
                 str(i.get('b') if i.get('b') is not None else '—'),
                 i.get('diff_txt') or i.get('note', '')))
    print()
    print('  一致 %d ｜ 不一致 %d ｜ 跳过 %d   -> verify_report.json' % (ok, warn, skip))
    if warn:
        print('  ⚠️ 有不一致项，请人工复核后再发布战报。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
