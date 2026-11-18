# -*- coding: utf-8 -*-
"""板块 / 宽度 / 风险偏好代理 —— 单次抓取，输出 sector_data.json

数据源：stockanalysis.com（与 fetch_live.py 同源，免密钥）
抓取量：11 个 SPDR 板块 + 8 个宽度/风格代理 = 19 个标的
用途：
  · 板块热力（当日涨跌 + 相对标普超额）
  · 宽度代理（等权 vs 市值权 / 小盘参与度 / 半导体相对强度）
  · 风险偏好（美元 UUP、比特币 IBIT）

⚠️ 全部是「当日」口径 —— 5 日 / 20 日相对强度需要历史序列，
   走 CBOE（kline_fetch.py），这里不做，避免重复抓取。
"""
import json
import re
import time
import urllib.request

HDR = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Cache-Control': 'no-cache',
}

SECTORS = [
    ('XLK', '信息技术'), ('XLC', '通信服务'), ('XLY', '可选消费'),
    ('XLF', '金融'), ('XLI', '工业'), ('XLV', '医疗保健'),
    ('XLP', '必需消费'), ('XLE', '能源'), ('XLU', '公用事业'),
    ('XLB', '材料'), ('XLRE', '房地产'),
]

PROXY = [
    ('RSP', '等权标普'), ('SPY', '标普500'), ('QQQ', '纳指100'),
    ('IWM', '罗素2000'), ('SMH', '半导体'), ('IGV', '软件'),
    ('UUP', '美元指数'), ('IBIT', '比特币'),
    # 防守三件套：写"轮动到防守"这类判断时必须有当天的真实涨跌，不能靠记忆
    ('TLT', '20年美债'), ('GLD', '黄金'), ('XBI', '生物科技'),
    # 指数代理的三个缺口：道指（DIA）与费半（SOXX）原来只存在 _idx_proxy.json 里，
    # 而那个文件没人刷新 → 每天的指数涨跌会停在上一份盘中快照（2026-10-23 踩）
    ('DIA', '道指代理'), ('SOXX', '费半代理'),
]


def grab(url, tries=3, timeout=15):
    for i in range(tries):
        try:
            req = urllib.request.Request(f'{url}?t={int(time.time() * 1000)}', headers=HDR)
            return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')
        except Exception:
            time.sleep(1.0 + i * 0.5)
    return ''


def parse(html):
    i = html.find('quote:{')
    if i < 0:
        return None
    seg = html[i:i + 1600]
    d = {}
    for k, f in (('p', 'price'), ('c', 'change'), ('cp', 'chg_pct'),
                 ('cl', 'prev_close'), ('h', 'day_high'), ('l', 'day_low'),
                 ('v', 'volume')):
        m = re.search(r'\b%s:(-?\.?\d+\.?\d*)' % k, seg)
        if m:
            d[f] = float(m.group(1))
    m = re.search(r'\bms:"([^"]*)"', seg)
    if m:
        d['market_state'] = m.group(1)
    m = re.search(r'\bu:"([^"]*)"', seg)
    if m:
        d['as_of'] = m.group(1)
    return d


def main():
    out = {'rows': {}, 'generated': time.strftime('%Y-%m-%d %H:%M:%S')}
    as_of = ''
    for sym, cn in SECTORS + PROXY:
        q = parse(grab(f'https://stockanalysis.com/etf/{sym.lower()}/'))
        if q and q.get('price'):
            out['rows'][sym] = {
                'cn': cn, 'price': q.get('price'), 'chg_pct': q.get('chg_pct'),
                'prev': q.get('prev_close'),
            }
            as_of = as_of or (q.get('as_of') or '')
            print(f'  {sym:5s} {cn:6s} {q.get("price"):>9} {q.get("chg_pct"):+6.2f}%')
        else:
            print(f'  {sym:5s} 抓取失败')
        time.sleep(0.25)
    out['as_of'] = as_of
    json.dump(out, open('sector_data.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'-> sector_data.json  {len(out["rows"])} 个标的  {as_of}')


if __name__ == '__main__':
    main()
