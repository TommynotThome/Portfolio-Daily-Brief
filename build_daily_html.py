# -*- coding: utf-8 -*-
"""用 daily_data.json + live_quotes.json + chart.svg 生成《美股每日战报》HTML

两层口径：
  · 收盘基准  = daily_data.json（上一交易日收盘，用于均线/21日走势/标准配置表）
  · 盘中实时  = live_quotes.json（fetch_live.py 抓取，用于当日净值与触发线判定）
"""
import json, io
import time
import datetime as _dt

D = json.load(open('daily_data.json', encoding='utf-8'))
d, NAV0, vals, CASH = D['data'], D['nav'], D['vals'], D['cash']
tsll = D['tsll']
chart = open('chart.svg', encoding='utf-8').read()

# ---- 账户数据统一从 portfolio.json 读（2026-11-18 抽离，便于脱敏与开源）----
#   没有这个文件时退回空持仓，脚本不会崩，但净值会是 0 —— 相当于"提示你还没填配置"。
try:
    _PF = json.load(open('portfolio.json', encoding='utf-8'))
except Exception:
    _PF = {}

POS = dict(_PF.get('positions') or {})
POS.setdefault('METU', 0)                      # 清仓后仍需占位，否则 MV['METU'] 崩

# ⚠️ POS_PREV / CASH_PREV 必须对应「NAV_PREV 要对比的那一天收盘时的持仓」。
#    盘中/收盘后：对比基准 = 上一交易日收盘
#    盘前（当日尚未成交）：对比基准 = 再前一天收盘
#    写错的后果：NAV_PREV 拿"今天的股数 × 昨天收盘价"，当日涨跌被严重低估。
POS_PREV = dict(_PF.get('positions_prev') or POS)
POS_PREV.setdefault('METU', 0)
CASH_PREV = float(_PF.get('cash_prev', _PF.get('cash', 0)) or 0)
FEE_PER_TRADE = float(_PF.get('fee_per_trade', 1.88) or 1.88)

# ---- 目标权重由 targets.json 提供（唯一来源）----
try:
    _TG = json.load(open('targets.json', encoding='utf-8'))['targets']
except Exception:
    _TG = {'SPYM': 74, 'QQQM': 15, 'VXUS': 5, 'CASH': 6}
T_SPYM, T_QQQM, T_VXUS, T_CASH = (_TG['SPYM'], _TG['QQQM'], _TG['VXUS'], _TG['CASH'])
T_CASH_F = T_CASH / 100.0      # 现金下限小数
T_CASH_HI = T_CASH + 3         # 实操上限：整股导致最后一档常买不满，下限 ~ 下限+3 都算正常
NAME = {'SPYM': '标普500', 'QQQM': '纳指100', 'VXUS': '非美全球', 'METU': '2倍META'}
# 【2026-11-03】目标权重必须与 targets.json 同源 —— 之前这里是写死的旧档
#   30/15/01/14，导致 01 节表格显示"目标 30%"，而同页文案写"目标 70"，自相矛盾。
TGT = {'SPYM': T_SPYM, 'QQQM': T_QQQM, 'VXUS': T_VXUS, 'METU': 0}
# ---- 已清仓与历史成交，同样来自 portfolio.json（代码里不写死任何个人数据）----
_CP = _PF.get('closed_positions') or {}
_RBM = _PF.get('rebalance') or {}
_MCP = _CP.get('METU') or {}
METU_EXIT = float(_MCP.get('exit', 0) or 0)          # 实际成交价
METU_EXIT_DATE = _MCP.get('exit_date') or ''         # 止损成交日（冷却闸门只覆盖这一天）
FEE = FEE_PER_TRADE                                   # 每笔平台费（券商 0 佣金）
STOP = {k: float((v or {}).get('stop', 0) or 0) for k, v in _CP.items()}
TAKE = {k: float((v or {}).get('take', 0) or 0) for k, v in _CP.items()}
COST = {k: float((v or {}).get('cost', 0) or 0) for k, v in _CP.items()}
CLOSED = {k: v for k, v in _CP.items() if k != 'METU'}
RB = _RBM or {'date': '', 'sym': '', 'qty': 0, 'px': 0, 'fee': 0,
              'cash_from': 0, 'cash_to': 0, 'shares_from': 0, 'shares_to': 0, 'close': 0}
RB_FILL = RB['qty'] * RB['px'] + RB.get('fee', 0)
RB_EXEC = RB['qty'] * (RB['px'] - RB['close']) + RB.get('fee', 0)
RCL = CLOSED.get('RAM') or {'qty': 0, 'cost': 0, 'exit': 0, 'fee': 0, 'date': ''}
RCL_RECV = RCL['qty'] * RCL['exit']
RCL_COST = RCL['qty'] * RCL['cost']
RCL_NET = RCL_COST - RCL_RECV + RCL['fee']
RCL_PCT = RCL['exit'] / RCL['cost'] - 1
# RAM 01/14 收盘事实（已不再持仓，历史常数，避免从 daily_data 取不到）
RAM_H = {'prev': 12.17, 'close': 19.20, 'pct': -8.55, 'hi': 11.59, 'lo': 11.00, 'ma20': 13.4325}

# ---------- K 线数据（kline_fetch.py 产出；任一文件缺失则整块功能自动降级）----------
try:
    KLINE = json.load(open('kline_data.json', encoding='utf-8'))
except Exception:
    KLINE = {}
try:
    KLINE_CSS = open('kline_style.css', encoding='utf-8').read()
    KLINE_TPL = open('kline_block.html', encoding='utf-8').read()
except Exception:
    KLINE_CSS = KLINE_TPL = ''

# ---------------- 回撤线：高水位制（2026-10-20 用户批准生效）----------------
# 旧规则：固定值 = 2026-10-05 高点 $3,578.09 × 0.9 / 0.85。
#        问题：入金会把 NAV 抬高，而固定线不动 → 线被越推越远。
#        01/03 实测：NAV 1,845 时要跌 23.1% 才触发 -10%，闸门形同虚设。
# 新规则：trailing drawdown —— HWM = 历史最高**收盘**净值（每创一次新高就上抬），
#        -10% / -15% 线 = HWM × 0.9 / HWM × 0.85。
#        · 入金会同时抬高 NAV 与 HWM，新钱自带 10% 缓冲，已实现的回撤不会被抹平；
#        · 水位只在**收盘创新高**时上移并落盘（hwm.json），避免盘中尖峰永久抬高闸门；
#        · 线只会向上走，永不下移 —— 净值跌、线跟着跌是逻辑错误。
HWM_FILE = 'hwm.json'
try:
    _HW = json.load(open(HWM_FILE, encoding='utf-8'))
except Exception:
    _HW = {'nav': 1578.09, 'date': '2026-10-05'}
HWM_SAVED = float(_HW.get('nav') or 0.0)
HWM_SAVED_DATE = _HW.get('date') or '2026-10-05'
BASE_ASOF = d['SPYM']['as_of']

# ---------------- 实时行情层 ----------------
try:
    LQ = json.load(open('live_quotes.json', encoding='utf-8'))
except Exception:
    LQ = {}


def lq(sym, kind='etf'):
    return LQ.get(f'{sym}@{kind}', {})


# 指数涨跌用 ETF 代理自算（DIA→道指 / SPY→标普 / QQQ→纳指100 / IWM→罗素 / SOXX→费半）
# ⚠️ CNBC 的 chg_pct 在收盘后稳定返回 0，不能引用；ETF 代理与指数高度同步，且可回溯。
try:
    IDX = json.load(open('_idx_proxy.json', encoding='utf-8'))
except Exception:
    IDX = {}

# ⚠️ _idx_proxy.json 没有任何脚本会刷新（2026-10-23 踩：里面还是上一交易日 10:05 的盘中快照）。
#    sector_data.json 每次链路都会重抓并带真实收盘价 → 优先用它覆盖，代理文件只兜底。
try:
    _SEC = json.load(open('sector_data.json', encoding='utf-8'))
    for _k, _v in (_SEC.get('rows') or {}).items():
        if not _v.get('price'):
            continue
        _d = IDX.setdefault(_k, {})
        _d['price'] = _v['price']
        _d['chg_pct'] = _v.get('chg_pct')
        _d['close_chg'] = _v.get('chg_pct')
        _d['prev_close'] = _v.get('prev')
        _d['as_of'] = _SEC.get('as_of')
        # ⚠️ 必须清掉代理文件里的 ext_chg_pct（它是上一份盘中快照的 0.0），
        #    否则 ixp() 在盘前会一直读它 → 全表 +0.00%
        _d['ext_chg_pct'] = None
except Exception as _e:
    print('  [板块快照跳过]', type(_e).__name__, str(_e)[:60])


def ix(sym, key='chg_pct'):
    v = (IDX.get(sym) or {}).get(key)
    return v


def ixc(sym, d=2):
    """上一交易日（01/03）收盘涨跌 —— 取 close_chg，避免与盘中涨跌混淆"""
    v = ix(sym, 'close_chg')
    if v is None:
        v = ix(sym)
    return ('%+.' + str(d) + 'f%%') % v if v is not None else '—'


def ixp(sym, d=2):
    """当前时段涨跌：盘中 → chg_pct；盘前 → ext_chg_pct（没有盘前价就退回上一交易日收盘涨跌）"""
    v = ix(sym, 'ext_chg_pct') if _PRE else ix(sym, 'chg_pct')
    if v is None:
        v = ix(sym, 'chg_pct')
    return ('%+.' + str(d) + 'f%%') % v if v is not None else '—'


def ixu(sym):
    v = ix(sym)
    return '#C0392B' if (v or 0) > 0 else ('#3B6D11' if (v or 0) < 0 else '#4a5058')


def ixvu(sym):
    """当前时段涨跌的上色"""
    v = (ix(sym, 'ext_chg_pct') if _PRE else ix(sym, 'chg_pct')) or 0
    return '#C0392B' if v > 0 else ('#3B6D11' if v < 0 else '#4a5058')


def cl(sym, d=2):
    """上一交易日收盘涨跌（**不随相位变化**）。
    盘前时 LCHG 拿的是盘前涨跌，讲「昨天发生了什么」必须用这个，
    否则文案会把「01/05 费半 -3.79%」写成盘前的 +3.44%。"""
    v = lq(sym).get('chg_pct')
    return ('%+.' + str(d) + 'f%%') % v if v is not None else '—'


# 主口径 = 盘中实时（若抓取失败则退回收盘基准）
LV = {k: lq(k).get('price') for k in POS}
LVOK = all(LV.values())

if LVOK:
    LVP = {k: lq(k).get('chg_pct') for k in POS}
    LVHI = {k: lq(k).get('day_high') for k in POS}
    LVLO = {k: lq(k).get('day_low') for k in POS}
    LVASOF = lq('SPYM').get('as_of', '')
    LVSTATE = lq('SPYM').get('market_state', '')
    MV = {k: POS[k] * LV[k] for k in POS}
    NAV = sum(MV.values()) + CASH
    LEV = MV['METU']
    CORE = MV['SPYM'] + MV['QQQM'] + MV['VXUS']
    NAV_STOP = NAV - 1.88
    CASH_STOP = CASH
else:
    LVP = {k: d[k]['pct'] for k in POS}
    LVHI = {k: d[k]['hi'] for k in POS}
    LVLO = {k: d[k]['lo'] for k in POS}
    LVASOF, LVSTATE = BASE_ASOF, '收盘'
    MV = dict(vals)
    NAV = NAV0
    LEV = vals['METU']
    CORE = vals['SPYM'] + vals['QQQM'] + vals['VXUS']
    NAV_STOP = NAV - 1.88
    CASH_STOP = CASH

LEV_STOP = MV['METU']

# ---------------- 次日盘前（01/15）----------------
PM = {k: lq(k).get('ext_price') for k in POS}
PMP = {k: lq(k).get('ext_chg_pct') for k in POS}
PMASOF = lq('METU').get('ext_as_of', '')
PMOK = all(PM.values())
PMMV = {k: POS[k] * (PM[k] or (MV[k] / POS[k] if POS[k] else 0)) for k in POS}
PMNAV = sum(PMMV.values()) + CASH
PMLEV = PMMV['METU']
DRIFT = MV['METU'] - PMMV['METU']
PREVP = {k: lq(k).get('prev_close') for k in POS}
PREVMV = {k: POS_PREV[k] * (PREVP[k] or LV[k]) for k in POS}
NAV_PREV = sum(PREVMV.values()) + CASH_PREV          # 盘前相对昨日收盘又蒸发掉的金额
RAM_PNL_CLOSE = RAM_PNL_PM = 0.0
_r = CLOSED.get('RAM')
if _r:
    RAM_PNL_CLOSE = _r['qty'] * (_r['exit'] - _r['cost']) - _r['fee']
    RAM_PNL_PM = RAM_PNL_CLOSE

# ---------------- 底层存储链 ----------------
BASKET = ['MU', 'SNDK', 'WDC', 'STX']
U = {k: lq(k, 'stocks') for k in BASKET + ['META', 'NVDA', 'TSLA']}
UOK = all(U[k].get('price') for k in BASKET)
B_AVG0 = (sum(U[k]['chg_pct'] for k in BASKET) / 4) if UOK else 0   # 已被自适应层覆盖
RAM_AMP = 0.0   # E 槽（3.5% 2 倍赌注）已于 01/15 随 RAM 清仓关闭

# ---------------- 盘中 / 盘前 自适应口径（日期全部数据驱动，禁止写死）----------------
INTRADAY = (LVSTATE == 'open')
# ⚠️ 必须区分「盘前 / 盘中 / 收盘后」三态：收盘后若按「盘前」处理，
#    会去取 ext_price/ext_chg_pct（盘后变动），把当日涨跌显示成盘后涨跌。
_MONN = {'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04', 'May': '05', 'Jun': '06',
         'Jul': '07', 'Aug': '08', 'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12'}


def _md(txt):
    """'Oct 13, 2026, 4:00 PM EDT' -> '01/15'"""
    try:
        p = (txt or '').replace(',', '').split()
        return '%s/%02d' % (_MONN[p[0]], int(p[1]))
    except Exception:
        return ''


def _k2md(t):
    """CBOE 短码 '261013' -> '01/15'"""
    return '%s/%s' % (t[2:4], t[4:6])


def _mdint(txt):
    """'Oct 19, 2026, 6:21 AM EDT' -> (10, 19)；解析失败 -> (0, 0)"""
    try:
        p = (txt or '').replace(',', '').split()
        return int(_MONN[p[0]]), int(p[1])
    except Exception:
        return 0, 0


# 🔴 盘前识别（2026-10-19 修）：**次日盘前时 stockanalysis 的 market_state 仍然是 closed**。
#    只看 market_state 会把「新交易日盘前」误判成「昨天收盘后」→ 整份战报停在上一交易日。
#    真正的判别信号：ext_as_of 的日期已经跨到 as_of 之后，且 ext_state 是 Pre-market。
_ASOF_D = _mdint(lq('SPYM').get('as_of'))
_EXT_D = _mdint(lq('SPYM').get('ext_as_of'))
_EXTS = (lq('SPYM').get('ext_state') or '').lower()
_PRE_NEWDAY = (_ASOF_D[0] > 0 and _EXT_D[0] > 0 and _EXT_D > _ASOF_D and 'pre' in _EXTS)
_POST = (LVSTATE in ('closed', 'post', 'after', 'afterhours', 'postmarket')) and not _PRE_NEWDAY
_PRE = (not INTRADAY) and (not _POST)
PHASE = '盘中' if INTRADAY else ('收盘' if _POST else '盘前')
# 【2026-11-01】周末跑时也保留「收盘」—— 数据口径本来就是上一交易日收盘，
# 改成「周末」会让「{D_CUR} {PHASE}：纳指 -3.47%」读成「10/30 周末」，反而错。
WEEKEND = _dt.date.today().weekday() >= 5


# 交易日序列：CBOE 日线日期 + gen_daily 抓到的最近收盘日（CBOE 常滞后一天，须补齐）
_KALL = [_k2md(b[0]) for b in (KLINE.get('SPYM', {}).get('d') or [])]
_DDAY = _md(d['SPYM'].get('as_of'))
# 当前交易时段所属日（盘中 = as_of 日期；盘前 = ext_as_of 日期）
_LIVEDAY = _md(lq('SPYM').get('as_of') if (INTRADAY or _POST) else lq('SPYM').get('ext_as_of'))
# ⚠️ 盘中时 gen_daily 的 as_of 就是"今天"，这一天还没收盘，绝不能当成已完成的交易日
# _DDAY = gen_daily 抓到的 as_of 日。
#   盘前：它是「上一交易日」→ 要补进序列
#   盘中/收盘后：它就是当日 → 不补（否则 D_PREV 会等于当日，表格两列重复）
if _DDAY and _DDAY != _LIVEDAY and (not _KALL or _KALL[-1] != _DDAY):
    _KALL.append(_DDAY)
if not _KALL:
    _KALL = [_DDAY or '']
D_PREV = _KALL[-1]                                   # 「上一交易日收盘」列 = 最近一个完整交易日
D_CUR = ((_md(lq('SPYM').get('as_of')) if (INTRADAY or _POST)
          else _md(lq('SPYM').get('ext_as_of'))) or D_PREV)
D_REF = (_KALL[-1] if (INTRADAY or _POST)
         else (_KALL[-2] if len(_KALL) > 1 else _KALL[-1]))
D_DAY = D_CUR if _POST else D_PREV                   # 最近收盘日（标题/正文用）
# ⚠️ 净值对比的标签要跟着相位走：盘中是「今天 盘中 较 昨天」，盘前是「昨天 较 前天」——
#    写死成 {D_PREV} 较 {D_REF} 会在盘中变成「10/23 较 10/23」（2026-10-26 踩）
NAV_CMP = (('%s %s 较 %s' % (D_CUR, '盘中' if INTRADAY else '收盘', D_PREV))
           if (INTRADAY or _POST) else ('%s 较 %s' % (D_PREV, D_REF)))
_KRAW = [b[0] for b in (KLINE.get('SPYM', {}).get('d') or [])]   # CBOE 短码，如 '261013'
# ⚠️ CBOE 常滞后一天，所以不能直接取 _KRAW[-2]。
# 正确做法：先定 D_B2 = 「上一交易日」的前一天，再回查 CBOE 里对应那一天的键。
D_B2 = _KALL[-2] if len(_KALL) > 1 else D_PREV       # 「两日前收盘」列的日期
_b2map = {_k2md(t): t for t in _KRAW}
B2_KEY = _b2map.get(D_B2) or (_KRAW[-1] if _KRAW else None)
# 按日期精确取该日收盘（不能用 d[k]['prev']：盘中时它已经变成上一交易日了）
B2CLOSE = {}
for _s2 in POS:
    _mm = {b[0]: b[4] for b in (KLINE.get(_s2, {}).get('d') or [])}
    B2CLOSE[_s2] = _mm.get(B2_KEY) or d[_s2].get('prev')

# ⚠️ 盘中时 stockanalysis 的 day_high/day_low 给的是「盘前区间」（会出现最高价 < 现价
#    的荒谬值）→ 一律改用 **CBOE 日线里 D_PREV 那根的真实日内高低**。
_pvmap = {_k2md(t): t for t in _KRAW}
PV_KEY = _pvmap.get(D_PREV) or (_KRAW[-1] if _KRAW else None)
PVHL = {}
for _s4 in POS:
    _m4 = {b[0]: b for b in (KLINE.get(_s4, {}).get('d') or [])}
    _bb = _m4.get(PV_KEY)
    PVHL[_s4] = (_bb[2], _bb[3]) if _bb else (None, None)
# （PVHL 的应用见下方 LHI/LLO 定义之后）
UB2CLOSE = {}
for _s3 in BASKET:
    _m3 = {b[0]: b[4] for b in (KLINE.get(_s3, {}).get('d') or [])}
    UB2CLOSE[_s3] = _m3.get(B2_KEY) or U[_s3].get('price')
# RAM 已清仓、不在 POS 里，但表里要展示历史两列 —— 同样按日期精确取值
_rk = [b for b in (KLINE.get('RAM', {}).get('d') or [])]
_rdates = [_k2md(b[0]) for b in _rk]
def _ramrow(dl):
    """按日期取 RAM 的 (收盘, 涨跌%, 高, 低)。

    ⚠️ CBOE 历史端点常滞后一天（当天只有前一交易日），所以「上一交易日」这一列
    在 kline 里查不到 —— 必须回退到 live_quotes 的字段，**不能落到写死的 RAM_H**
    （那会让 01/02 那列显示成 01/14 的 19.20）。
    """
    if dl in _rdates:
        _i = _rdates.index(dl)
        _b = _rk[_i]
        _pv = _rk[_i - 1][4] if _i > 0 else _b[1]
        return _b[4], (_b[4] / _pv - 1) * 100, _b[2], _b[3]
    _q = lq('RAM')
    if dl == D_PREV and _q.get('price'):
        return (_q['price'], (_q.get('chg_pct') or 0.0),
                (_q.get('day_high') or _q['price']), (_q.get('day_low') or _q['price']))
    if dl == D_B2 and _q.get('prev_close'):
        _pb = _rdates.index(D_B2) if D_B2 in _rdates else None
        _pct = 0.0
        if _pb is not None and _pb > 0:
            _pct = (_rk[_pb][4] / _rk[_pb - 1][4] - 1) * 100
        return _q['prev_close'], _pct, _q['prev_close'], _q['prev_close']
    return None
RAM_B2V = _ramrow(D_B2) or (RAM_H['close'], RAM_H['pct'], RAM_H['hi'], RAM_H['lo'])
RAM_PV = _ramrow(D_PREV) or (RAM_H['close'], RAM_H['pct'], RAM_H['hi'], RAM_H['lo'])
NAV_LABEL = ('%s 盘中' % D_CUR) if INTRADAY else (('%s 收盘' % D_CUR) if _POST else ('%s 收盘' % D_PREV))
# 「上一交易日收盘」列 / 「最新」列 / 「最新涨跌」
#   盘前：price=最近收盘(即 D_PREV)、ext_price=盘前价；盘中：price=实时、prev_close=上一收盘
LCLOSE = {k: ((lq(k).get('price') if _PRE else lq(k).get('prev_close')) or LV[k]) for k in POS}
LLAST = {k: ((lq(k).get('ext_price') or LV[k]) if _PRE else LV[k]) for k in POS}
LCHG = {k: ((lq(k).get('ext_chg_pct') if _PRE else lq(k).get('chg_pct')) or 0.0) for k in POS}
# ⚠️ 盘前时 LLAST 是盘前价、LCLOSE 才是「上一交易日收盘」——说"昨天收盘"必须用 LCLOSE
# ⚠️ 这是「止损成交日（02/20）那一根收盘」，是历史常数，不能跟着最新收盘漂 ——
#    否则隔几天就会写出"卖价高于 10/23 收盘"这种把两天混在一起的句子。
METU_CLOSE = 26.50
METU_CLOSE_PCT = -14.05
LHI = {k: (lq(k).get('day_high') or LLAST[k]) for k in POS}
LLO = {k: (lq(k).get('day_low') or LLAST[k]) for k in POS}
# 🔴 只在**盘中**覆盖：盘中 stockanalysis 的 day_high/low 是"当日进行中"的高低价；
#    而**盘前**时 price 仍是上一交易日收盘 → day_high/low 正是那一整天的真实高低，
#    此时若用 CBOE 覆盖，会因 CBOE 滞后一天而拿到更早一天的区间（表头写 10/16、数据却是 01/02）。
if INTRADAY:
    for _s5 in POS:
        if PVHL[_s5][0] and PVHL[_s5][1]:
            LHI[_s5], LLO[_s5] = PVHL[_s5][0], PVHL[_s5][1]
# 月市况开关：取 CBOE 序列里"上个月最后一个收盘"作基准
def _mast(k, n=20):
    """按最新价判断该标的相对 MA20 的位置（自动着色，避免写死文案出错）。

    ⚠️ 必须定义在 rows2 / _CMT 之前 —— 否则 NameError。
    """
    px, ma = LLAST.get(k) or LV.get(k), d[k].get('ma20')
    if not ma or not px:
        return ''
    if px >= ma:
        return '<span class="tag g">站上 MA20 %.2f</span>' % ma
    return '<span class="tag r">跌破 MA20 %.2f（%.1f%%）</span>' % (ma, (px / ma - 1) * 100)


try:
    _kd = [(_k2md(b[0]), b[4]) for b in (KLINE.get('SPYM', {}).get('d') or [])]
    _prev = [v for d, v in _kd if d[:2] != D_CUR[:2]]
    MTD_SPYM = ((LLAST['SPYM'] / _prev[-1] - 1) * 100) if _prev else 0.0
except Exception:
    MTD_SPYM = 0.0

# 底层存储链（MU/SNDK/WDC/STX）同口径
UCLOSE = {k: ((U[k].get('price') if _PRE else U[k].get('prev_close')) or 0) for k in BASKET}
ULAST = {k: (((U[k].get('ext_price') or U[k].get('price')) if _PRE else U[k].get('price')) or 0)
         for k in BASKET}
# 底层标的的涨跌一律用 chg_pct：patch_live.py 每次都会把它刷成「当时」的口径
# （盘中=实时，盘前=上一交易日收盘）。不要走 ext_* —— 那些字段没人刷新，
# 盘前会读到上周的旧值，整张表渲染成 +0.00%（11/10 踩过）。
UCHG = {k: (U[k].get('chg_pct') or 0.0) for k in BASKET}
UHI = {k: (U[k].get('day_high') or ULAST[k]) for k in BASKET}
ULO = {k: (U[k].get('day_low') or ULAST[k]) for k in BASKET}
B_AVG = sum(UCHG.values()) / 4 if UOK else 0.0        # 篮子均值（同相位口径）

# RAM 已清仓，用实时行情做「清仓是否躲对」的对照
RAMQ = lq('RAM')
RAM_CLOSE = (RAMQ.get('prev_close') if _PRE else RAMQ.get('price')) or RAM_H['close']
RAM_NOW = ((RAMQ.get('ext_price') or RAMQ.get('price')) if _PRE else RAMQ.get('price')) \
    or RAM_H['close']
RAM_NOW_CHG = ((RAMQ.get('ext_chg_pct') if _PRE else RAMQ.get('chg_pct')) or 0.0)
RAM_13 = {'close': RAMQ.get('price') or 0, 'pct': RAMQ.get('chg_pct') or 0.0,
          'hi': RAMQ.get('day_high') or 0, 'lo': RAMQ.get('day_low') or 0}   # 最近收盘日事实
AMP_NOW = (RAM_NOW_CHG / B_AVG) if B_AVG else 0.0
AMP_TXT = ('%.2f' % AMP_NOW) if AMP_NOW else '—'

# ---------------- 候选标的池 ----------------
try:
    STATS = json.load(open('screen_stats.json', encoding='utf-8'))
except Exception:
    STATS = {}

CN = {'SPYM': '标普500', 'SPLG': '标普500', 'VOO': '标普500', 'IVV': '标普500',
      'QQQM': '纳指100', 'QQQ': '纳指100', 'VXUS': '非美全球发达+新兴',
      'VEU': '非美全球', 'IXUS': '非美全球', 'SMH': '半导体', 'SOXX': '半导体',
      'XLV': '医疗保健', 'XLE': '能源', 'XLU': '公用事业', 'GRID': '电网/电力基建',
      'GLD': '黄金现货', 'MU': '美光 DRAM', 'WDC': '西部数据 HDD+闪存',
      'STX': '希捷 HDD', 'SNDK': '闪迪 NAND', 'RAM': '2x DRAM 篮子',
      'METU': '2x META', 'TSLL': '2x 特斯拉', 'NVDL': '2x 英伟达',
      'GGLL': '2x 谷歌', 'MSFU': '2x 微软', 'AAPU': '2x 苹果', 'AMZU': '2x 亚马逊'}

# (组, ticker, kind, 角色标签, 裁决标签, 理由)
SLOTS = [
    ('A 核心宽基 · 目标 74%', 'SPYM', 'etf', '现持', 'keep', '业内最低费率档，规模充足。<b>不改</b>'),
    ('A 核心宽基 · 目标 74%', 'VOO', 'etf', '候选', 'same', '规模最大（$1.06T）但费率 0.03%，比现持高。<b>不改</b>'),
    ('A 核心宽基 · 目标 74%', 'IVV', 'etf', '候选', 'same', '同类，费率 0.03%。<b>不改</b>'),
    ('A 核心宽基 · 目标 74%', 'SPLG', 'etf', '候选', 'chk', '与 SPYM 同基金另一份额，费率相同。<b>数据源两者返回一致，[需核实]</b>'),
    ('B 纳指100 · 目标 15%', 'QQQM', 'etf', '现持', 'keep', '0.15% ＜ QQQ 的 0.18%，同一指数。<b>不改</b>'),
    ('B 纳指100 · 目标 15%', 'QQQ', 'etf', '候选', 'same', '流动性和期权最好，但费率更高。<b>不改</b>'),
    ('C 非美全球 · 目标 5%', 'VXUS', 'etf', '现持', 'keep', '<b>11 月定投第一顺位</b>（唯一仍在 MA20 下方）。<b>不改</b>'),
    ('C 非美全球 · 目标 5%', 'VEU', 'etf', '候选', 'same', '费率 0.04% 微低，但成分少一半，跟踪差。<b>不改</b>'),
    ('C 非美全球 · 目标 5%', 'IXUS', 'etf', '候选', 'same', '估值最低（PE 16.5）、股息最高，费率 0.07%。<b>备选</b>'),
    ('D 存储 · 无配置槽位（100% 已分配）', 'MU', 'stocks', '候选', 'wait', '<b>无净值门槛</b>（2026-11-17 取消）。判定只看收益对比：现处"供给叙事"杀跌中，<b>风险收益比未达 2:1</b> → 等待，不是等净值'),
    ('D 存储 · 无配置槽位（100% 已分配）', 'WDC', 'stocks', '候选', 'no', 'HDD 逻辑 ≠ DRAM，不能替代 MU 的敞口'),
    ('D 存储 · 无配置槽位（100% 已分配）', 'STX', 'stocks', '候选', 'no', 'PE 56 倍最贵，且是 HDD 逻辑'),
    ('D 存储 · 无配置槽位（100% 已分配）', 'SNDK', 'stocks', '候选', 'no', 'NAND 逻辑，与 DRAM 不同周期'),
    ('观察名单 · 无配置槽位', 'SMH', 'etf', '观察', 'watch', '<b>01/02 用户加入</b>。半导体 ETF；若 D 槽（存储 10%）最终不建 MU，SMH 是更分散的替代。<b>无净值门槛</b>，与个股按同一套收益对比判'),
    ('观察名单 · 无配置槽位', 'NBIS', 'stocks', '观察', 'watch', '<b>01/02 用户加入</b>。<b>买不起</b>：ATR 8.1% → 现实止损需 20–25% → 允许仓位 $155 &lt; 1 股。年化波动 112.9%（比 METU 还高）'),
    ('观察名单 · 无配置槽位', 'SPCX', 'stocks', '观察', 'watch', '<b>01/02 用户加入</b>。SpaceX，2026-06 上市；无对应槽位（太空/国防），只做观察'),
    ('观察名单 · 无配置槽位', 'SOXX', 'etf', '观察', 'watch', '近一年 <b>+102%</b>，估值 PE 40 倍，追高风险大'),
    ('观察名单 · 无配置槽位', 'XLV', 'etf', '观察', 'watch', '防御属性好（β 0.56），但 5 个持仓已经够复杂'),
    ('观察名单 · 无配置槽位', 'XLE', 'etf', '观察', 'watch', '今日 +1.7%（油价涨）；中东局势受益，但无槽位'),
    ('观察名单 · 无配置槽位', 'XLU', 'etf', '观察', 'watch', '公用事业近一年 <b>-8.8%</b>，利率高企压制，不碰'),
    ('观察名单 · 无配置槽位', 'GRID', 'etf', '观察', 'watch', 'AI 用电主题，但费率 0.56% 偏高，无槽位'),
    ('观察名单 · 无配置槽位', 'GLD', 'etf', '观察', 'watch', '近一年仅 +3.6% 且费率 0.40%，与你的策略不搭'),
]

VTAG = {'keep': ('tag g', '✅ 保持'), 'stop': ('tag r', '🔴 止损中'), 'wait': ('tag o', '等待条件'),
        'no': ('tag gray', '✖ 不采用'), 'same': ('tag gray', '＝ 无差异'), 'chk': ('tag o', '[需核实]'),
        'done': ('tag gray', '已清仓'), 'watch': ('tag', '👁 观察')}


def cand_rows():
    out = []
    for grp, sym, kind, role, vt, why in SLOTS:
        s = STATS.get(sym, {})
        q = lq(sym, kind)
        px = q.get('price')
        cpp = q.get('chg_pct')
        px_s = mo(px) if px else '—'
        cp_s = (f'<span style="color:{up(cpp)}">{cpp:+.2f}%</span>' if cpp is not None else '—')

        def clean(v, dash):
            """过滤数据源返回的 JS 空值（void 0 / 0）"""
            if v is None or str(v).strip() in ('', 'void 0', '0', 'n/a'):
                return dash
            return v

        fee = clean(s.get('expenseRatio'), '—' if kind == 'stocks' else '[MISSING]')
        aum = clean(s.get('aum'), '—' if kind == 'stocks' else '[MISSING]')
        y1 = clean(s.get('ch1y'), '—')
        tc, tl = VTAG[vt]
        bg = ' style="background:#f6f8fa"' if vt in ('same', 'no', 'done', 'watch') else ''
        _k = (' class="krow" data-k="%s"' % sym) if sym in KLINE else ''
        _b = '<span class="kbug">K线</span>' if sym in KLINE else ''
        out.append(
            f'  <tr{bg}{_k}><td class="small">{grp}</td><td><b>{sym}</b> <span class="small">{CN.get(sym, "")}</span>{_b}</td>'
            f'<td>{role}</td><td class="n">{px_s}</td><td class="n">{cp_s}</td>'
            f'<td class="n">{fee}</td><td class="n">{aum}</td><td class="n">{y1}</td>'
            f'<td><span class="{tc}">{tl}</span> {why}</td></tr>')
    return out



# ---------------- 挂单区（新） ----------------
ORDERS = [
    # (标的, 中文, 类型, 挂单价, 金额, 标记)  flag='stale' → 建议撤单（已不在规则内的价）
    # 【2026-11-18】$85.60 那张单已按 $86.00 成交 5 股 → 移到 FILLED，挂单表清空

    # 【2026-11-09】用户确认「目前已经没有挂单」：$87.59 那笔已撤。
    #   挂单表清空的连带效果：挂单闸门转正常、free_cash 不再被在途占用、评分回升。
    # 01/03 的 VXUS $83.00 × 0.3855 股已于 02/20 成交 → 移到 FILLED，不能再留在挂单表
]
# 在途金额（挂单表里的钱）—— 模块级，供 06 节的 free_cash 与闸门共用
_inflight_amt = sum(_o[4] for _o in ORDERS)

# 已成交回执 —— 成交的单一律移到这里，不再出现在挂单表里
FILLED = [
    dict(sym='QQQM', cn='纳指100', kind='etf', px_ord=88.00,
         px=85.00, sh=0.25, amt=49.97, fee=1.88,
         when='01/02 开盘', note='成交价低于挂单价 -0.77%'),
    dict(sym='SPYM', cn='标普500', kind='etf', px_ord=81.90,
         px=81.90, sh=1.0, amt=81.90, fee=1.88,
         when='01/03 盘中', note='入金落地第一笔，市价单（1 整股，SPYM 不支持碎股）'),
    dict(sym='SPYM', cn='标普500', kind='etf', px_ord=82.00,
         px=82.00, sh=3.0, amt=160.00, fee=1.88,
         when='01/03 盘中', note='现金垫优化后加仓，补到 44%'),
    dict(sym='QQQM', cn='纳指100', kind='etf', px_ord=271.00,
         px=271.00, sh=0.2915, amt=78.99, fee=1.88,
         when='11/18 盘中', note='碎股按金额下单，开盘后市价成交，无滑点'),
    dict(sym='SPYM', cn='标普500', kind='etf', px_ord=85.60,
         px=86.00, sh=5.0, amt=430.00, fee=1.88,
         when='11/18 盘中', note='挂 $85.60、实际成交 $86.00（高于挂单 +0.47%），补到 70.7%'),
    dict(sym='VXUS', cn='非美全球', kind='etf', px_ord=83.00,
         px=83.00, sh=0.3855, amt=32.00, fee=1.88,
         when='02/20 盘中', note='挂单价与成交价一致，补到<b>当时</b>的 10% 目标（现已改为 5%）'),
]

# 今日可买股数（自适应价格）：可动用现金扣一笔手续费后能买几股 SPYM
#   ⚠️ 必须把 SPYM 与 QQQM 两笔手续费都算进去，否则剩余现金会算成 9.6%（超过 6–9% 区间）
_INVESTABLE = max(0.0, CASH - NAV * T_CASH_F)
_buy_sh = int(max(0.0, _INVESTABLE - 1.88) // LLAST['SPYM']) if LLAST.get('SPYM') else 0
_buy_amt = _buy_sh * LLAST['SPYM'] + 1.88 if _buy_sh else 0.0
_qqqm_amt = max(0.0, _INVESTABLE - _buy_amt - 1.88) if _buy_sh else max(0.0, _INVESTABLE - 1.88)
_post_cash = CASH - _buy_amt - (_qqqm_amt + 1.88 if _qqqm_amt > 0 else 0.0)
_post_cash_pct = _post_cash / NAV * 100
_post_spym_pct = (MV['SPYM'] + _buy_sh * LLAST['SPYM']) / NAV * 100

# 加码档位定位：SPYM 距 52 周高的跌幅（来自 eq_gate.json；缺文件则 None）
try:
    _EG0 = json.load(open('eq_gate.json', encoding='utf-8'))
    _SPYM_FROM_HI = (_EG0.get('base') or {}).get('SPYM', {}).get('from_hi')
except Exception:                                   # noqa: BLE001
    _SPYM_FROM_HI = None
_TIER = ('正常定投', 1.0, int(T_CASH))
if _SPYM_FROM_HI is not None:
    if -15 < _SPYM_FROM_HI <= -10:
        _TIER = ('便宜，加投 50%', 1.5, 5)
    elif -25 < _SPYM_FROM_HI <= -15:
        _TIER = ('明显便宜，加倍', 2.0, 4)
    elif -40 < _SPYM_FROM_HI <= -25:
        _TIER = ('恐慌区，投三月额度', 3.0, 3)
    elif _SPYM_FROM_HI <= -40:
        _TIER = ('系统性危机，退回正常定投', 1.0, int(T_CASH))

ORDER_BUDGET_TOTAL = 170.0     # 本月回调档专项总额
ORDER_BUDGET_USED = 80.0       # QQQM $50 + VXUS $30 已用掉


def eq_block():
    """个股准入判定表（2026-11-17 起取消 NAV>$2,500 门槛）。
    数据来自 eq_gate.py 的产出；没有文件就不渲染，不影响构建。"""
    try:
        G = json.load(open('eq_gate.json', encoding='utf-8'))
    except Exception:                                   # noqa: BLE001
        return ''
    items = [r for r in (G.get('items') or []) if not r.get('err')]
    if not items:
        return ''
    base = G.get('base') or {}
    rows = []
    for r in items + [base.get('SPYM'), base.get('QQQM')]:
        if not r or r.get('err'):
            continue
        if not r.get('ok_seat'):
            vt, cls = '装不下', 'tag r'
        elif not r.get('ok_odds'):
            vt, cls = '赔率不够', 'tag o'
        elif not r.get('ok_trend'):
            vt, cls = '趋势破位', 'tag r'
        else:
            vt, cls = '通过', 'tag g'
        is_base = r['sym'] in ('SPYM', 'QQQM')
        rows.append(''.join([
            '<tr style="background:#F7F9FB">' if is_base else '<tr>',
            '<td><b>', r['sym'], '</b>',
            '<span class="small"> 基准</span>' if is_base else '',
            '</td><td class="n">', mo(r['px']),
            '</td><td class="n">', '%.2f%%' % r['atr'],
            '</td><td class="n">', '%.1f%%' % r['stop'],
            '</td><td class="n">', '%+.1f%%' % r['up'],
            '</td><td class="n"><b>', '%.2f' % r['odds'],
            '</b></td><td class="n">', '%.3f' % (r.get('eff') or 0),
            '</td><td class="n">', '%+.1f%%' % (r.get('vs_ma200') or 0),
            '</td><td class="n">', '%.1f%%' % r['one_pct'],
            '</td><td class="n">', mo(r['seat']),
            '</td><td><span class="', cls, '">', vt, '</span></td></tr>',
        ]))
    head = (
        '<h2><span class="no">11</span>个股准入判定 · 不看净值规模，只看收益对比</h2>\n'
        '<p class="cap"><b>2026-11-17 取消「NAV &gt; $2,500 才能建个股」的门槛</b> —— '
        '那个门槛只能让账户"看起来不冒险"，并不能回答这笔钱放哪里更划算。'
        '<b>现在三道硬门槛 + 一个参考量：</b><br>'
        '① <b>装得下</b>：1 股 ≤ 允许仓位（净值 × 2% ÷ 现实止损，现实止损 = 2.5 × ATR）；<br>'
        '② <b>赔率 ≥ 2:1</b>：（回到 52 周高 × 0.97 的空间）÷ 现实止损；<br>'
        '③ <b>趋势</b>：价 &gt; MA200 且 MA50 &gt; MA200 —— <b>这条不是规模门槛，是给赔率踩刹车</b>：'
        '跌得越多赔率越好看，但趋势破位的票回到前高的<b>概率</b>远低于宽基；<br>'
        '④ <b>效率（参考，不作门槛）</b>：赔率 ÷ ATR%，每承担 1% 波动换多少赔率。</p>\n'
        '<table>\n'
        '  <tr><th>标的</th><th class="n">现价</th><th class="n">ATR</th>'
        '<th class="n">现实止损</th><th class="n">回前高空间</th><th class="n">赔率</th>'
        '<th class="n">效率</th><th class="n">距 MA200</th><th class="n">1 股占净值</th>'
        '<th class="n">允许仓位</th><th>结论</th></tr>\n  ' + '\n  '.join(rows) + '\n</table>\n')
    tail = (
        '<p class="cap"><b>口径的四条局限（必须知道）：</b><br>'
        '① 上行用「52 周高 × 0.97」<b>不是分析师目标价</b> —— 免费源拿不到一致预期，'
        '但这个口径<b>不含任何预测</b>；代价是贴近年线高点的标的（SPYM 距高 5.3%）赔率天然低，'
        '<b>这不代表它该被卖</b>；<br>'
        '② 赔率是<b>空间比</b>不是<b>概率加权期望</b> —— 回前高的<b>概率</b>没算进去，'
        '而个股回到前高通常比宽基难。<b>所以赔率高的票，往往正是趋势最坏的票</b> —— '
        '这就是③存在的理由；<br>'
        '③ 趋势关（价 &gt; MA200）的代价是<b>错过底部第一段</b>。但它挡住的是"接飞刀"，'
        '而接飞刀是这个账户唯一可能被一次打残的方式。<b>底部可以等确认，本金不能等。</b><br>'
        '④ 「赔率 ≥ SPYM」原本想当门槛，实测失去约束力（SPYM 自己只有 0.65），'
        '所以降为参考量。<b>门槛只留前三条，最后一步由人判断。</b></p>')
    return head + tail


def order_rows():
    out = []
    for sym, cn, kind, px_ord, amt, *_flag in ORDERS:
        _stale = bool(_flag and _flag[0] == 'stale')
        q = lq(sym, kind)
        cur = (q.get('ext_price') or q.get('price')) if _PRE else (q.get('price') or q.get('ext_price'))
        if not cur:
            continue
        gap = (px_ord / cur - 1) * 100
        if gap > 0:
            st = '<span class="tag r">挂单价已高于现价</span>'
            g = f'<b style="color:#C0392B">{gap:+.2f}%</b>'
            gd = '（股价已跌到挂单价之下）'
        else:
            st = ('<span class="tag r">建议撤单</span>' if _stale
                  else '<span class="tag o">等价格跌下来</span>')
            g = f'<b>{abs(gap):.2f}%</b>'
            gd = '（还需再跌）'
        out.append(
            f'  <tr class="krow" data-k="{sym}"><td><b>{sym}</b> <span class="small">{cn}</span>'
            f'<span class="kbug">K线</span></td>'
            f'<td class="n"><b style="color:#1F5FA8">${px_ord:,.2f}</b></td>'
            f'<td class="n"><b>${amt}</b></td>'
            f'<td class="n">{g} <span class="small">{gd}</span></td>'
            f'<td class="n">{mo(cur)}</td><td>{st}</td></tr>')
    return chr(10).join(out)


def filled_rows():
    out = []
    for f in FILLED:
        v = f['px'] * f['sh']
        out.append(
            f'  <tr class="krow" data-k="{f["sym"]}"><td><b>{f["sym"]}</b> '
            f'<span class="small">{f["cn"]}</span><span class="kbug">K线</span></td>'
            f'<td class="n">{f["when"]}</td>'
            f'<td class="n"><b>${f["px"]:,.2f}</b></td>'
            f'<td class="n"><b>{f["sh"]:.4f}</b> 股</td>'
            f'<td class="n">{mo(v + f["fee"])}</td>'
            f'<td><span class="tag g">已成交</span> <span class="small">{f["note"]}</span></td></tr>')
    return chr(10).join(out)


def order_block():
    """挂单区：剩余挂单 + 已成交回执 + 预算口径（全部按当日价格实时计算）"""
    left = ORDER_BUDGET_TOTAL - ORDER_BUDGET_USED
    n_ord = len(ORDERS)
    _cur_spym = LLAST.get('SPYM') or LV.get('SPYM') or 0   # 盘前必须用盘前价，否则挂单价算错口径
    _free = max(0.0, CASH - NAV * T_CASH_F)
    _rng = _cur_spym * 0.955
    if n_ord:
        _head = ('<h2><span class="no">02</span>挂单 · %d 张 <span class="small">'
                 '（QQQM 01/02、VXUS 02/20 已成交）</span></h2>' % n_ord)
        _tail = ('<div class="say"><b>📌 剩余 %d 张 GTC 限价单，成交前不动。</b>'
                 '<b>价格按「跟涨不跟跌」—— 只在股价上涨时上移，股价下跌时不下调</b>。</div>'
                 % n_ord)
    else:
        _head = ('<h2><span class="no">02</span>挂单 · 0 张 '
                 '<span class="small">（全部已撤销／已成交，账户首次无在途委托）</span></h2>')
        _tail = (
            '<div class="ok" style="font-size:14px">'
            '<b>✅ 挂单已清空 —— 这个账户第一次「没有任何在途委托」。</b><br>'
            '① <b>可自由动用 %s</b>（现金 %s − %d%% 下限 %s）；<br>'
            '② <b>够买 %d 股 SPYM</b>（盘前 %s）—— 入金到账后第一次真正有子弹；<br>'
            '③ <b>回撤不再触发卖出</b>，纪律评分不再因挂单或减仓扣分。</div>\n'
            '<div class="say"><b>📌 今天怎么挂单（财报已达标：Q4 指引 1300 亿 &gt; 1243 亿触发线）</b><br>'
            '· <b>SPYM %d 股，限价 %s</b> —— 盘前 %s，挂高约 0.4%% 保成交，<b>不追高、不用回调档价</b>；<br>'
            '· <b>QQQM %s 碎股，开盘后市价</b>（碎股必须开盘后成交）；<br>'
            '· <b>做完后现金约 %.1f%%</b>，落在 %d–%d%% 的操作区间内。<br>'
            '<span class="small"><b>⚠️ 这是再平衡，不是等回调。</b>回调档价现在是 %s（现价 × 0.955），'
            '那是"等跌 4.5%%"的价 —— 用它补缺口，大概率永远不成交。'
            '「补缺口按现价、加仓按回调价」，两种单不能混。</span></div>'
            % (mo(_free), mo(CASH), int(T_CASH), mo(NAV * T_CASH_F),
               _buy_sh, mo(_cur_spym),
               _buy_sh, mo(_cur_spym * 1.004), mo(_cur_spym),
               mo(_qqqm_amt), _post_cash_pct, int(T_CASH), int(T_CASH_HI),
               mo(_cur_spym * 0.955)))
    return """
<h2>__HEAD__</h2>
<p class="h2sub"><b>GTC 限价买单；SPYM 只能买整股（1 股 ≈ 净值 5%），QQQM/VXUS 可按金额买碎股</b>；<b>价格按「跟涨不跟跌」—— 只在股价上涨时上移，股价下跌时不下调</b>。点行名可看 K 线。</p>

<table>
  <tr><th>标的</th><th class="n">挂单价</th><th class="n">金额</th><th class="n">距离</th><th class="n">现价</th><th>状态</th></tr>
__ROWS__
</table>

<div class="ok" style="font-size:13.5px"><b>✅ 01/02 已成交：QQQM 0.25 股 @ $85.00</b>
<span class="small">（本金 $49.97 + 手续费 $1.88 = $51.85）—— 挂单价 $88.00，实际成交价 <b>低于挂单价 -0.77%</b>。
成交后 QQQM <b>0.7615 → 0.9301 股</b>、现金 $518.18 → <b>$466.33</b>。这是三张回调单里<b>第一张按设计触发</b>的，不是意外事件。</span>
<table style="margin:8px 0">
  <tr><th>标的</th><th class="n">成交时间</th><th class="n">成交价</th><th class="n">股数</th><th class="n">合计支出</th><th>状态</th></tr>
__FILLED__
</table></div>

__TAIL__
""".replace('__HEAD__', _head).replace('__ROWS__', order_rows()
                                       ).replace('__FILLED__', filled_rows()
                                                 ).replace('__TAIL__', _tail)


# ---------------- 今日事件窗口（盘前 / 盘中 / 盘后） ----------------


def evt_window():
    rows = ''.join(
        f'  <div class="evt-r"><span class="evt-t">{k}</span><span class="evt-c">{v}</span></div>'
        for k, v in EVT.items())
    return ('<div class="evt"><div class="evt-h">今日事件窗口 · %s</div>%s</div>'
            % (HERO_DATE, rows))


# ---------------- 展示辅助 ----------------
up = lambda x: '#C0392B' if x > 0 else ('#3B6D11' if x < 0 else '#6b7280')
pc = lambda x: f'{x:+.2f}%'
mo = lambda x: f'${x:,.2f}'
# 今日已成交（无成交时置 None）—— 盘中分支的文案不能默认「今天没有成交」
_TT = _PF.get('today_trades') or []
if _TT:
    TODAY_TRADE = ' ＋ '.join('%s %g 股 @ $%.2f' % (t.get('sym'), t.get('shares'), t.get('px'))
                              for t in _TT)
    TODAY_FEE = sum(float(t.get('fee', FEE_PER_TRADE)) for t in _TT)
    TODAY_SH = {t.get('sym'): t.get('shares') for t in _TT}
else:
    TODAY_TRADE = None
    TODAY_FEE = 0.0
    TODAY_SH = {}

# 英伟达现价（BASKET 里没有 NVDA，单独取，
#   之前写 ULAST["NVDA"] 直接 KeyError 崩掉构建）
_NVQ = lq('NVDA', 'stocks')
_NVPX = _NVQ.get('price') or 0.0
_NVPV = _NVQ.get('prev_close') or 0.0
_NVCHG = ((_NVPX / _NVPV - 1) * 100) if _NVPV else 0.0

EVT = {
    '盘前': ('<b>英伟达 Q4 指引 1300 亿（预期 1251 亿）—— 超过我们设的 1243 亿触发线，按昨天定的规则，今天该买。</b><br>'
             '营收 <b>1158 亿（+103%）</b>、数据中心 <b>1076 亿（+110%，首次破千亿）</b>、'
             'Non-GAAP EPS <b>2.68（预期 2.48）</b>、毛利率 <b>74.9%</b>，全面超预期；'
             '<b>FY2028 营收增长指引从 70% 上调到 75%</b>。盘后涨超 4%。<br>'
             '<b>但三条警告必须一起读：</b><br>'
             '① <b>Q4 毛利率指引 71–72%</b>，比 Q3 的 74.9% 掉近 3pp —— 增长在放缓质量；<br>'
             '② <b>董事会明确表示不打算让黄仁勋复职</b>，已聘海德思哲找正式 CEO，'
             '并新设「国家安全与出口管制监督委员会」；<br>'
             '③ <b>11 月以来至少 11 家券商下调评级</b>：Baird 从 500 砍到 200、Bernstein 400→200、'
             'Evercore 与汇丰各降 60% / 53%。<br>'
             '<b>所以这是「需求确认 + 治理折价」的组合 —— 业绩再好，CEO 空缺这个问题没有被业绩回答。</b>'),
    '盘中': (f'<b>今天的核心事实：财报超预期，但 NVDA 跌破昨收。</b>'
             f'Q3 营收 1158 亿（+103%）、Q4 指引 1300 亿（预期 1251 亿），盘后一度涨 4% —— '
             f'<b>开盘后全部吐回，现价 {mo(_NVPX)}（{pc(_NVCHG)}），比昨收还低。</b>'
             f'<b>业绩没有回答"CEO 空缺"这件事。</b>'
             f'这也解释了为什么 QQQM 只 {pc(LCHG["QQQM"])}、SPYM 几乎平盘：'
             f'<b>市场把财报读成"确认不是崩了"，而不是"重新给溢价"。</b><br>'
             f'<b>接下来看三件：</b><br>'
             f'① <b>NVDA 收盘能不能收回昨收 {mo(_NVPV)} 上方</b> —— '
             f'收不回，说明 $155 附近就是新的估值锚；<br>'
             f'② <b>费半能不能收复 10,000</b> —— 11/17 收 9,642（SOXX 代理 {ixp("SOXX")}），'
             f'11/05 低点 9,011，10,000 是"监管冲击已消化"的分水岭；<br>'
             f'③ <b>你那张 $85.60 的挂单</b>：现价 {mo(LLAST["SPYM"])}，今日最低 $85.98，'
             f'<b>还差 {abs(85.60 / LLAST["SPYM"] - 1) * 100:.2f}% 没成交</b> —— '
             f'收盘前不成交，明天按收盘价重算要不要上移。<br>'
             f'<span class="small"><b>⚠️ 这笔是再平衡，不是等回调。</b>补缺口按现价、加仓按回调价 —— '
             f'你挂的 $85.60 落在两者之间（比现价低 {abs(85.60 / LLAST["SPYM"] - 1) * 100:.2f}%，'
             f'比回调档价 {mo(LLAST["SPYM"] * 0.955)} 高），是一个折中价：<b>能成交就赚了，不成交也不会错。</b></span>'),
    '盘后': ('<b>日程（美东时间）：</b><br>'
             '<b>11/19（三）</b> <b>Anthropic IPO（代码 ANT）</b> —— 已获约 6 倍超额认购，'
             '定价区间 70–78 美元、最高估值 1.4 万亿，英伟达认购约 100 亿；<br>'
             '<b>11/20（四）</b> 费城联储 + 初请失业金；<br>'
             '<b>12/8–9（二/三）</b> FOMC + 点阵图。<br>'
             '<b>对你最实际的一条：今天把 5 股 SPYM 买掉，不要等 Anthropic 定价。</b>'
             '缺口 22.9pp 是结构问题，Anthropic 定什么价跟 SPYM 没有关系 —— '
             '<b>把两件事混在一起，是又一种「等一个更好的价」。</b>'),
}


def chg_tag(x):
    return f'<span class="tag g">多头</span>' if x else f'<span class="tag o">偏弱</span>'


# ---------------- 01 账户快照 ----------------
rows1 = []
for k in POS:
    if POS[k] <= 0:
        continue
    v, p = MV[k], LV[k]
    rows1.append(
        f'  <tr><td><b>{k}</b> {NAME[k]}</td><td class="n">{POS[k]}</td>'
        f'<td class="n">{mo(B2CLOSE.get(k) or d[k]["prev"])}</td>'
        f'<td class="n">{mo(LCLOSE.get(k) or d[k]["price"])}</td>'
        f'<td class="n"><b style="color:{up(LCHG[k])}">{mo(LLAST[k])}</b> <span class="small" style="color:{up(LCHG[k])}">{pc(LCHG[k])}</span></td>'
        f'<td class="n">{mo(v)}</td><td class="n"><b>{v/NAV*100:.1f}%</b></td>'
        f'<td class="n">{TGT[k]}%</td><td class="n">{v/NAV*100-TGT[k]:+.1f}</td></tr>')
rows1.append(
    f'  <tr><td>现金</td><td class="n">—</td><td class="n">—</td><td class="n">—</td><td class="n">—</td>'
    f'<td class="n"><b>{mo(CASH)}</b></td><td class="n"><b>{CASH/NAV*100:.1f}%</b></td>'
    f'<td class="n">{T_CASH:.0f}%</td><td class="n">{CASH/NAV*100-T_CASH:+.1f}</td></tr>')
rows1.append(
    f'  <tr style="background:#f6f8fa"><td colspan="5"><b>合计</b></td>'
    f'<td class="n"><b>{mo(NAV)}</b></td><td class="n"><b>100%</b></td>'
    f'<td class="n">—</td><td class="n">—</td></tr>')

# ================= 新增：板块 / 宽度 / 技术指标 / 利率阶梯 =================
try:
    SECTOR = json.load(open('sector_data.json', encoding='utf-8')).get('rows') or {}
except Exception:
    SECTOR = {}
SEC_ASOF = ''
try:
    SEC_ASOF = json.load(open('sector_data.json', encoding='utf-8')).get('as_of') or ''
except Exception:
    pass


def _sec(sym, key='chg_pct'):
    return (SECTOR.get(sym) or {}).get(key)


SPY_CHG = _sec('SPY') or 0.0


def _rsi(closes, n=14):
    """Wilder RSI。closes 为升序（旧→新）。数据不足返回 None。"""
    if not closes or len(closes) < n + 2:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            g += ch
        else:
            l -= ch
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _rsi_txt(v):
    return '—' if v is None else '%.0f' % v


def _rsi_clr(v):
    if v is None:
        return '#6b7280'
    if v >= 70:
        return '#C0392B'
    if v <= 30:
        return '#0F7B4F'
    return '#4a5058'


def _hi_lo(sym, n):
    """近 n 根日线的最高/最低价（用 CBOE OHLC，比 stockanalysis 的日内高低可靠）。"""
    bars = (KLINE.get(sym) or {}).get('d') or []
    if len(bars) < 5:
        return None, None
    b = bars[-n:]
    return max(x[2] for x in b), min(x[3] for x in b)


RSI = {k: _rsi(d[k].get('closes') or []) for k in POS}
HI20, LO20 = {}, {}
for k in POS:
    HI20[k], LO20[k] = _hi_lo(k, 20)

# ---- 11 个 SPDR 板块（按当日涨跌排序，算相对标普超额）----
_sec_rows = []
for _sym, _cn in [('XLK', '信息技术'), ('XLC', '通信服务'), ('XLY', '可选消费'),
                  ('XLF', '金融'), ('XLI', '工业'), ('XLV', '医疗保健'),
                  ('XLP', '必需消费'), ('XLE', '能源'), ('XLU', '公用事业'),
                  ('XLB', '材料'), ('XLRE', '房地产')]:
    _v = _sec(_sym)
    if _v is None:
        continue
    _sec_rows.append({'sym': _sym, 'cn': _cn, 'chg': _v, 'ex': _v - SPY_CHG})
_sec_rows.sort(key=lambda x: -x['chg'])
SEC_OK = len(_sec_rows) >= 8
SEC_MAX = max([abs(r['chg']) for r in _sec_rows] or [1.0])
_sec_top = _sec_rows[0] if _sec_rows else None      # 最强（已按涨幅降序）
_sec_bot = _sec_rows[-1] if _sec_rows else None     # 最弱

# 板块结论一句话：必须由数据推出，不能写死（2026-10-23 踩：写死的"+1.62%/-1.73%"已经过期两天）
_SECMAP = {r['sym']: r['chg'] for r in _sec_rows}
_SEC_CN = {r['sym']: r['cn'] for r in _sec_rows}


def _sg(k):
    return _SECMAP.get(k)


_DFN = [k for k in ('XLP', 'XLV', 'XLU') if _sg(k) is not None and _sg(k) > SPY_CHG]
_CYC = [k for k in ('XLE', 'XLB', 'XLI') if _sg(k) is not None and _sg(k) > SPY_CHG]
if _sg('XLP') is not None and _sg('XLE') is not None:
    _SEC_NOTE = ('<b>防守（%s）%s跑赢基准，能源 %s</b> —— %s'
                 % ('、'.join('%s %s' % (_SEC_CN[k], pc(_sg(k))) for k in ('XLP', 'XLV', 'XLU')
                             if _sg(k) is not None),
                    '集体' if len(_DFN) == 3 else '部分',
                    pc(_sg('XLE')),
                    '这是"避险"的形状，不是"再通胀"的形状。'
                    if len(_DFN) >= 2 and _sg('XLE') < SPY_CHG else
                    '这是"再通胀"的形状：能源也跑赢了基准。'))
else:
    _SEC_NOTE = ''

# ---- 宽度代理 ----
BR = {
    'rsp': _sec('RSP'), 'spy': _sec('SPY'), 'iwm': _sec('IWM'),
    'qqq': _sec('QQQ'), 'smh': _sec('SMH'), 'igv': _sec('IGV'),
    'uup': _sec('UUP'), 'ibit': _sec('IBIT'),
}
BR_OK = all(v is not None for v in (BR['rsp'], BR['spy'], BR['iwm'], BR['smh'], BR['qqq']))
BR_RSP = (BR['rsp'] - BR['spy']) if BR_OK else None      # 等权 − 市值权
BR_IWM = (BR['iwm'] - BR['spy']) if BR_OK else None      # 小盘 − 标普
BR_SMH = (BR['smh'] - BR['qqq']) if BR_OK else None      # 半导体 − 纳指


def br_cap():
    """宽度解读：措辞必须跟着符号走。
    ⚠️ 之前写死「等权跑赢…指数跌一个多点…半导体跑输」，涨跌一反向就成了反话。"""
    if not BR_OK:
        return '宽度数据未取到。'
    w = lambda v, a, b: a if v > 0 else b
    s1 = w(BR_RSP, '等权<b>跑赢</b>市值权', '等权<b>跑输</b>市值权')
    s2 = w(BR_IWM, '小盘<b>跑赢</b>标普', '小盘<b>跑输</b>标普')
    s3 = w(BR_SMH, '半导体<b>跑赢</b>纳指', '半导体<b>跑输</b>纳指')
    c1 = ('说明<b>涨跌面是宽的</b>，不是少数权重股在拉指数'
          if BR_RSP > 0 else
          '说明<b>指数靠少数权重股撑着</b>，多数股票没跟上')
    c2 = ('AI 硬件是今天的<b>领涨</b>方向' if BR_SMH > 0
          else '挨打的是"AI 硬件"这一条线')
    return ('%s <b>%+.2fpp</b>、%s <b>%+.2fpp</b> —— %s；而%s <b>%+.2fpp</b>，%s。'
            % (s1, BR_RSP, s2, BR_IWM, c1, s3, BR_SMH, c2))

# ---- 10Y 关键位阶梯 ----
KEY10Y = [4.50, 4.70, 5.00, 5.20, 5.50]
# ⚠️ _CNBC_MACRO 定义在 910 行，比这里晚 → 必须自己读文件，不能引用变量
try:
    _y10 = float((json.load(open('cnbc_macro.json', encoding='utf-8')).get('US10Y') or {}).get('last'))
except Exception:
    _y10 = None
_y10_lo = [x for x in KEY10Y if x <= (_y10 or 0)]
_Y10_BELOW = _y10_lo[-1] if _y10_lo else None
_y10_hi = [x for x in KEY10Y if x > (_y10 or 0)]
_Y10_ABOVE = _y10_hi[0] if _y10_hi else None

# ---- 财报记分卡（手工维护，见 earnings_card.json）----
try:
    ECARD = json.load(open('earnings_card.json', encoding='utf-8'))
except Exception:
    ECARD = {'week': '', 'rows': []}
EC_ROWS = ECARD.get('rows') or []


def _mv(v):
    return '—' if v is None else mo(v)


# ---- 开新仓候选池（读 newpos_scan.py 的产出，六道关卡就地复算）----
# ⚠️ 数字一律来自 newpos_scan.json，不许在这里写死 —— 否则第二天就过期
NEWPOS_ROWS, NEWPOS_WATCH = [], []
try:
    _NPJ = json.load(open('newpos_scan.json', encoding='utf-8'))
    _NP = _NPJ.get('items') or {}
    _NP_ASOF = (_NPJ.get('meta') or {}).get('as_of') or ''
except Exception:
    _NP, _NP_ASOF = {}, ''

# 关卡：与《开新仓方案》文档里的定义必须逐条一致
_NPG = dict(min_dv=20e6, oh=(-35.0, -3.0), vm20=(-10.0, 3.0), max_atr=3.5,
            max_corr=0.90, max_share=25.0)
for _s, _r in _NP.items():
    _px = _r.get('px_eff') or 0
    if not _px:
        continue
    _fail = []
    if (_r.get('dv20') or 0) < _NPG['min_dv']:
        _fail.append('流动性')
    if not _r.get('ma200') or _px <= _r['ma200'] or _r['ma50'] <= _r['ma200']:
        _fail.append('趋势')
    if _r.get('off_hi') is None or not (_NPG['oh'][0] <= _r['off_hi'] <= _NPG['oh'][1]):
        _fail.append('距高点')
    if not (_NPG['vm20'][0] <= (_r.get('vs_ma20') or 0) <= _NPG['vm20'][1]):
        _fail.append('距 MA20')
    if (_r.get('atrp') or 9) > _NPG['max_atr']:
        _fail.append('波动')
    if (_r.get('corr_spy') or 0) > _NPG['max_corr']:
        _fail.append('重叠')
    if _px / NAV * 100 > _NPG['max_share']:
        _fail.append('单价过高')
    # 「规则解禁价」= 它自己要跌到多少，才同时满足"距高 ≤ -3%"和"距 MA20 ≤ +3%"
    _unlock = None
    if _r.get('hi52') and _r.get('ma20'):
        _unlock = min(_r['hi52'] * 0.97, _r['ma20'] * 1.03)
        _unlock = _unlock if _px > _unlock else None
    _row = dict(sym=_s, cn=_r.get('cn') or '', px=_px, off_hi=_r.get('off_hi'),
                vs20=_r.get('vs_ma20'), rsi=_r.get('rsi14'), atr=_r.get('atrp'),
                corr=_r.get('corr_spy'), r60=_r.get('r60'), unlock=_unlock,
                fail=_fail)
    if not _fail:
        NEWPOS_ROWS.append(_row)
    else:
        NEWPOS_WATCH.append(_row)

# 过关的按「60 日超额收益 + 独立性」粗排（文档里有完整五因子，日报只给次序）
NEWPOS_ROWS.sort(key=lambda x: -(x['r60'] or 0) - 40 * (1 - (x['corr'] or 0)))
# 观察名单：只看"结构上真正缺的那几个方向"（能源/医疗/红利/金融/公用/小盘/原材料）
_WATCH_PICK = [w for w in NEWPOS_WATCH
               if w['sym'] in ('XLE', 'XLV', 'SCHD', 'XLF', 'XLP', 'XLU', 'IWM', 'XLB', 'XLI')
               and w['unlock'] and (1 - w['unlock'] / w['px']) >= 0.005]
_WATCH_PICK.sort(key=lambda x: -(1 - x['unlock'] / x['px']))


def newpos_block():
    if not _NP:
        return '<p class="cap">未取到 newpos_scan.json，本节跳过。</p>'
    h = ['<table>',
         '  <tr><th>标的</th><th class="n">现价</th><th class="n">距 52 周高</th>'
         '<th class="n">距 MA20</th><th class="n">RSI</th><th class="n">ATR%</th>'
         '<th class="n">与 SPY 相关</th><th>裁决</th></tr>']
    for r in NEWPOS_ROWS:
        h.append('  <tr><td><b>%s</b> <span class="small">%s</span></td>'
                 '<td class="n">%s</td><td class="n">%+.1f%%</td><td class="n">%+.1f%%</td>'
                 '<td class="n">%.1f</td><td class="n">%.2f%%</td><td class="n">%.2f</td>'
                 '<td><span class="tag g">六关全过</span></td></tr>'
                 % (r['sym'], r['cn'], mo(r['px']), r['off_hi'] or 0, r['vs20'] or 0,
                    r['rsi'] or 0, r['atr'] or 0, r['corr'] or 0))
    if not NEWPOS_ROWS:
        h.append('  <tr><td colspan="8">今天没有标的六关全过 —— <b>这本身就是结论</b></td></tr>')
    h.append('</table>')
    if _WATCH_PICK:
        h.append('<p class="cap" style="margin-top:14px"><b>排序依据：</b>过表格按「60 日超额收益 + 与持仓的独立性」粗排；'
                 '观察表按「离解禁价还差多远」由近到远。</p>')
        h.append('<h3 style="font-size:13.5px;margin:20px 0 4px">差一关的（结构上真正缺的）：'
                 '写清"跌到多少才允许买"</h3>')
        h.append('<table>\n  <tr><th>标的</th><th class="n">现价</th><th class="n">规则解禁价</th>'
                 '<th class="n">还需再跌</th><th>卡在哪一关</th></tr>')
        for w in _WATCH_PICK[:6]:
            h.append('  <tr><td><b>%s</b> <span class="small">%s</span></td><td class="n">%s</td>'
                     '<td class="n"><b>%s</b></td><td class="n">%.1f%%</td><td>%s</td></tr>'
                     % (w['sym'], w['cn'], mo(w['px']), mo(w['unlock']),
                        (1 - w['unlock'] / w['px']) * 100, '、'.join(w['fail'])))
        h.append('</table>')
    return '\n'.join(h)


# 候选池结论里要引用 XLF 的价格：优先用当日抓价，取不到回退扫描价
try:
    _NPXLF = (D['data'].get('XLF') or {}).get('price') or (_NP.get('XLF') or {}).get('px_eff') or 0.0
except Exception:
    _NPXLF = (_NP.get('XLF') or {}).get('px_eff') or 0.0

NEWPOS_HTML = newpos_block()


# ---- 板块热力 HTML（零轴居中条形）----
SEC_HTML = ''
if SEC_OK:
    _h = []
    for _r in _sec_rows:
        _w = max(1.5, abs(_r['chg']) / SEC_MAX * 47)
        _pos = 'left:50%' if _r['chg'] >= 0 else 'right:50%'
        _col = '#C0392B' if _r['chg'] >= 0 else '#0F7B6E'
        _ec = '#C0392B' if _r['ex'] >= 0 else '#0F7B6E'
        _h.append(
            '<div class="secrow"><span class="secn">%s</span>'
            '<span class="sect"><i style="%s;width:%.1f%%;background:%s"></i><b></b></span>'
            '<span class="secv" style="color:%s">%+.2f%%</span>'
            '<span class="sece" style="color:%s">%+.2fpp</span></div>'
            % (_r['cn'], _pos, _w, _col, _col, _r['chg'], _ec, _r['ex']))
    SEC_HTML = ''.join(_h)

# ---- 宽度代理 HTML ----
BR_HTML = ''
if BR_OK:
    def _br(_label, _val, _hint):
        _c = '#C0392B' if _val >= 0 else '#0F7B6E'
        return ('<div class="brrow"><span class="brk">%s</span>'
                '<span class="brv" style="color:%s">%+.2fpp</span>'
                '<span class="brt">%s</span></div>' % (_label, _c, _val, _hint))
    BR_HTML = (_br('等权 RSP − 标普', BR_RSP, '正数＝普涨；负数＝少数权重股在拉指数')
               + _br('小盘 IWM − 标普', BR_IWM, '正数＝小盘参与；负数＝风险偏好收缩')
               + _br('半导体 SMH − 纳指', BR_SMH, '正数＝芯片领涨；负数＝AI 硬件被卖'))

# ---- 10Y 关键位阶梯 ----
Y10_HTML = ''
if _y10:
    _parts = []
    for _i, _k in enumerate(KEY10Y):
        _on = (_Y10_BELOW == _k)
        _parts.append('<span class="ln%s">%.2f</span>' % (' on' if _on else '', _k))
        if _i < len(KEY10Y) - 1:
            _cls = 'll done' if (_y10 > KEY10Y[_i + 1]) else ('ll next' if _Y10_BELOW == _k else 'll')
            _parts.append('<span class="%s"></span>' % _cls)
    _gap = ('距 %.2f 还有 <b>%.0fbp</b>' % (_Y10_ABOVE, (_Y10_ABOVE - _y10) * 100)) if _Y10_ABOVE \
        else '已站上最高一档（%.2f）' % KEY10Y[-1]
    _back = ('，已越过 %.2f' % _Y10_BELOW) if _Y10_BELOW else ''
    Y10_HTML = ('<div class="lad">%s</div>'
                '<div class="ladnow">当前 <b>%.3f%%</b>%s；%s</div>'
                % (''.join(_parts), _y10, _back, _gap))

# ---- 财报记分卡 HTML ----
EC_HTML = ''
if EC_ROWS:
    _rows_html = []
    for _e in EC_ROWS:
        _chg = _e.get('chg')
        if _chg == 'live':
            _chg = (LQ.get('%s@stocks' % _e['sym']) or {}).get('chg_pct')
        _rev_s = (_e['rev'] / _e['rev_e'] - 1) * 100 if _e.get('rev_e') else None
        _eps_s = (_e['eps'] / _e['eps_e'] - 1) * 100 if _e.get('eps_e') else None

        def _cell(sur):
            if sur is None:
                return '<td class="n">—</td>'
            cls = 'beat' if sur >= 0 else 'miss'
            return '<td class="n"><span class="%s">%+.1f%%</span></td>' % (cls, sur)

        _chg_cell = ('<td class="n" style="color:%s"><b>%s</b></td>'
                     % (up(_chg or 0), pc(_chg) if _chg is not None else '—'))
        _rows_html.append(
            '<tr><td class="sym">%s <span class="small">%s</span></td><td class="n small">%s %s</td>'
            '<td class="n">$%.1f <span class="small">/ %.1f</span></td>%s'
            '<td class="n">$%.2f <span class="small">/ %.2f</span></td>%s'
            '%s<td class="note">%s</td></tr>'
            % (_e['sym'], _e.get('name', ''), _e.get('date', ''), _e.get('when', ''),
               _e['rev'], _e['rev_e'], _cell(_rev_s),
               _e['eps'], _e['eps_e'], _cell(_eps_s),
               _chg_cell, _e.get('note', '')))
    EC_HTML = ''.join(_rows_html)


# ---------------- 02 技术信号 ----------------
rows2 = []
for k in POS:
    if POS[k] <= 0:
        continue
    x = d[k]
    p = LLAST[k] if LLAST.get(k) else LV[k]      # 相位口径：盘前/盘中取最新价
    above5, above10, above20 = p > x['ma5'], p > x['ma10'], p > x['ma20']
    allabove = above5 and above10 and above20
    n20 = (p / x['closes'][min(20, len(x['closes']) - 1)] - 1) * 100
    if k == 'SPYM':
        note = '核心宽基，波动最小的一档；<b>%s</b>，贴 52 周高 $91.74' % _mast('SPYM')
    elif k == 'QQQM':
        note = ('<span class="tag g">回调单 01/02 成交</span> <b>%s</b>' % _mast('QQQM'))
    elif k == 'VXUS':
        note = '%s；<b>02/20 已按 $83.00 补仓 0.3855 股</b>（补到<b>当时</b>的 10%% 目标 —— 现目标已改 5%%，这笔成了超配，只降不补）' % _mast('VXUS')
    elif k == 'METU':
        note = ('母公司 META 01/06 <b>%s</b>、盘前 <b>%s</b> → METU 本时段 <b>%s</b>；<b>%s</b>，'
                '距止损 $25 <b>%+.1f%%</b>'
                % (pc(U['META'].get('chg_pct') or 0),
                   pc((lq('META', 'stocks').get('ext_chg_pct') if _PRE
                       else lq('META', 'stocks').get('chg_pct')) or 0), pc(LCHG['METU']),
                   _mast('METU'), (25 / p - 1) * 100))
    else:
        note = '—'
    _hc = ('<span class="small">均线按 {D_DAY} 收盘算，价格列为 {D_CUR} {PHASE}口径</span>'
           if (INTRADAY or _PRE) else '')
    rows2.append(
        f'  <tr><td><b>{k}</b></td><td class="n"><b>{mo(p)}</b></td>'
        f'<td class="n">{LVHI[k]:.2f}/{LVLO[k]:.2f}</td>'
        f'<td class="n">{x["ma5"]:.2f}</td><td class="n">{x["ma10"]:.2f}</td>'
        f'<td class="n"><b>{x["ma20"]:.2f}</b></td>'
        f'<td class="n" style="color:{up(n20)}">{n20:+.1f}%</td>'
        f'<td class="n" style="color:{_rsi_clr(RSI.get(k))}"><b>{_rsi_txt(RSI.get(k))}</b></td>'
        f'<td class="n small">{mo(LO20.get(k))} / {mo(HI20.get(k))}</td>'
        f'<td>{chg_tag(allabove)} {note}</td></tr>')


# ---------------- 高水位与回撤线（必须排在 NAV / D_CUR / _POST 之后）----------------
HWM = HWM_SAVED
HWM_JUST_RAISED = False
# ⚠️ 能否上移取决于「NAV 是不是一个已确认的收盘」：
#   · 收盘后（_POST）：price = 当日官方收盘 → 可确认
#   · 盘前（_PRE）  ：price 仍是**上一交易日的官方收盘** → 同样是已确认的收盘，可确认
#                      （2026-10-22 踩：01/06 收盘 $3,849.67 创新高，但当天没跑收盘版，
#                        盘前跑时若不允许上移，这个新高就永久丢了）
#   · 盘中（INTRADAY）：price = 实时价 → 绝不上移，否则一次盘中尖峰会把闸门永久抬高
HWM_CAN_RAISE = (not INTRADAY) and NAV > HWM_SAVED + 0.005
if HWM_CAN_RAISE:
    HWM = round(NAV, 2)
    HWM_SAVED_DATE = _md(lq('SPYM').get('as_of')) or (D_CUR if _POST else D_PREV)
    HWM_JUST_RAISED = True
    try:
        json.dump({'nav': HWM, 'date': HWM_SAVED_DATE},
                  open(HWM_FILE, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:
        pass
HIGH = HWM                    # 兼容旧变量名：全篇 HIGH 即「高水位」
DD10, DD15 = HWM * 0.9, HWM * 0.85

# ---------------- 08 触发线 ----------------
rows3 = []
for k in ('METU',):
    p = LLAST[k]
    ds, dt = (STOP[k] / p - 1) * 100, (TAKE[k] / p - 1) * 100
    if POS.get(k, 0) <= 0:
        # ⚠️ 已清仓就不能再显示「距止损 +5.0%」——那条线对它已经失效了
        rows3.append(
            f'  <tr><td><b>{k}</b></td><td class="n">{mo(p)}</td>'
            f'<td class="n">—</td><td class="n">—</td><td class="n">—</td><td class="n">—</td>'
            f'<td><span class="tag g">✅ {METU_EXIT_DATE} 已清仓</span> 成交 {mo(METU_EXIT)}（10 股），'
            f'已实现亏损 {mo((COST[k] - METU_EXIT) * 6 + 1.88)}（含费）；止损线 $25 随之失效（详第 06 节）</td></tr>')
        continue
    elif ds > 0:
        st = (f'<span class="tag r">🔴 已跌破止损 {ds:+.1f}%</span> '
              f'<b>现价 {mo(p)} ≤ ${STOP[k]:g} —— 清仓 {POS[k]:g} 股，不等反弹</b>（详第 06 节）')
    elif k == 'METU':
        st = (f'<span class="tag g">仍持 {POS[k]:g} 股，止损未触发</span> $34 单已于 01/15 到期作废。'
              f'当前距 $34 需 {((34/p)-1)*100:+.1f}%，<b>不建议重挂</b>（详第 06 节）；'
              f'$25 止损为纪律底线，距现价 {ds:+.1f}%')
    else:
        st = f'<span class="tag o">距止损 {ds:.1f}%</span> 盯收盘价'
    rows3.append(
        f'  <tr><td><b>{k}</b></td><td class="n"><b>{mo(p)}</b></td><td class="n"><b>{mo(STOP[k])}</b></td>'
        f'<td class="n" style="color:{"#C0392B" if ds > 0 else "#3B6D11"}">'
        f'{"+" if ds > 0 else ""}{ds:.1f}%{" ⚠" if ds > 0 else ""}</td>'
        f'<td class="n"><b>{mo(TAKE[k])}</b></td>'
        f'<td class="n" style="color:{up(dt)}"><b>{dt:+.1f}%</b></td><td>{st}</td></tr>')
rows3.append(
    f'  <tr><td><b>组合回撤 -10%</b></td><td class="n">{mo(NAV)}</td>'
    f'<td class="n"><b>{mo(DD10)}</b></td><td class="n" style="color:#3B6D11">{(DD10/NAV-1)*100:+.1f}%</td>'
    f'<td class="n">—</td><td class="n">—</td><td><b>不卖</b> · 观察信号 + 加码刻度</td></tr>')
rows3.append(
    f'  <tr><td><b>组合回撤 -15%</b></td><td class="n">{mo(NAV)}</td>'
    f'<td class="n"><b>{mo(DD15)}</b></td><td class="n" style="color:#3B6D11">{(DD15/NAV-1)*100:+.1f}%</td>'
    f'<td class="n">—</td><td class="n">—</td><td><b>不卖</b> · 加码档位再升一级</td></tr>')

# ---------------- 回调档 ----------------
NEW = {k: LV[k] * 0.955 for k in ('SPYM', 'QQQM', 'VXUS')}
OLD = {k: LV[k] for k in ('SPYM', 'QQQM', 'VXUS')}      # 参考基准 = 当日收盘，公开版不写死历史价
CAP = {k: LV[k] * 1.05 for k in ('SPYM', 'QQQM', 'VXUS')}  # 上限 = 首档价 × 1.05
AMT = {'SPYM': 90, 'QQQM': 50, 'VXUS': 30}

# 存储链表格
urows = []
for k in BASKET:
    q = U[k]
    urows.append(
        f'  <tr><td>{"<b>美光 MU</b>" if k=="MU" else CN[k]}</td>'
        f'<td class="n">{mo(q.get("prev_close", 0))}</td><td class="n"><b>{mo(q["price"])}</b></td>'
        f'<td class="n" style="color:{up(q["chg_pct"])}"><b>{pc(q["chg_pct"])}</b></td>'
        f'<td class="n">{mo(q.get("day_low", 0))}</td></tr>')



# ============ P1 / Step2 / Step3：宏观数据层（OpenBB-CBOE + FRED，均免密钥） ============
import os as _os
MACRO_V2 = _os.environ.get('MACRO_V2', '0') == '1'
BRIEF_OUT = _os.environ.get('BRIEF_OUT', 'daily-brief.html')


def _jload(name, default):
    try:
        return json.load(open(name, encoding='utf-8'))
    except Exception:
        return default


OBB_MACRO = _jload('obb_macro.json', {})
# CNBC 实时宏观（2026-10-15 新增：CBOE 报价端点被封后的替代源）
_CNBC_MACRO = _jload('cnbc_macro.json', {})
FRED = _jload('fred_macro.json', {})
XCHK = _jload('obb_crosscheck.json', [])
_FI = {it['key']: it for it in FRED.get('items', [])}


def _delta(x, d=2):
    # 涨跌着色：红涨绿跌（中文市场惯例）。仅表示方向，不代表好坏。
    if x is None:
        return '<span style="color:#8b929c">—</span>'
    if abs(x) < 0.005:
        return '<span style="color:#8b929c">0.00</span>'
    c = '#C0392B' if x > 0 else '#3B6D11'
    txt = f'{x:+.{d}f}'.rstrip('0').rstrip('.')
    return f'<span style="color:{c}">{txt}</span>'


def _v1_block():
    """P1 宏观速览。

    2026-10-15 起 CBOE 的 delayed_quotes 报价端点返回 AccessDenied（被封），
    而 charts/historical 只覆盖股票/ETF 且滞后一天 —— 实时利率与指数出现缺口。
    改用 CNBC restQuote（免密钥、可一次批量取全，见 cnbc_macro.py），CBOE 留作回退。
    """
    cm = _CNBC_MACRO
    if cm:
        def chip(r, decimals, pct=False):
            v = r.get('last')
            if v is None:
                return ''
            if pct:
                body = '<b>%s</b>' % format(v, ',.%df' % decimals)
                d = r.get('chg_pct')
                tag = ('<span class="tag %s">%+.2f%%</span>'
                       % ('r' if (d or 0) > 0 else 'g', d or 0))
            else:
                body = '<b>%.*f%%</b>' % (decimals, v)
                d = r.get('chg_bp')
                tag = ('<span class="tag %s">%+.1fbp</span>'
                       % ('r' if (d or 0) > 0 else 'g', d or 0))
            return '%s %s %s' % (r.get('label', ''), body, tag)

        def chip_idx(proxy, key, decimals):
            """指数 chip：点位取 CNBC 官方值，涨跌取 ETF 代理
            ⚠️ CNBC 的 chg_pct 在收盘后稳定返回 0 → 直接引用会让整条变成 +0.00%"""
            r = cm.get(key) or {}
            v = r.get('last')
            if v is None:
                return ''
            d = ix(proxy, 'chg_pct')
            tag = ('<span class="tag %s">%+.2f%%</span>'
                   % ('r' if (d or 0) > 0 else 'g', d or 0)) if d is not None else ''
            return '%s <b>%s</b> %s' % (r.get('label', ''), format(v, ',.%df' % decimals), tag)

        parts = [x for x in (
            chip(cm.get('US10Y') or {}, 3), chip(cm.get('US30Y') or {}, 3),
            chip(cm.get('US5Y') or {}, 3), chip(cm.get('.VIX') or {}, 2, True),
            chip_idx('SPY', '.SPX', 2), chip_idx('QQQ', '.IXIC', 2),
            chip_idx('DIA', '.DJI', 2), chip_idx('SOXX', '.SOX', 2),
        ) if x]
        if parts:
            asof = ((cm.get('US10Y') or {}).get('as_of')
                    or (cm.get('.SPX') or {}).get('as_of') or '—')
            return (
                '<div class="say"><b>📊 宏观速览（P1 · CNBC 实时 · ' + str(asof) + '）</b><br>'
                + ' ｜ '.join(parts)
                + '<br><span class="small">这一层只解决一件事：<b>把实时利率与指数补上</b> —— '
                  '2026-10-15 起 CBOE 的 <code>delayed_quotes</code> 报价端点返回 AccessDenied，'
                  '而 <code>charts/historical</code> 滞后一天。CNBC <code>restQuote</code> 免密钥、可一次批量取全'
                  '（脚本 <code>cnbc_macro.py</code>）。给的是<b>盘中快照</b>，没有历史序列。</span></div>'
            )
    # ---- 回退：CBOE 当日快照 ----
    tnx = OBB_MACRO.get('TNX', {})
    vix = OBB_MACRO.get('VIX', {})
    spx = OBB_MACRO.get('SPX', {})
    parts = []
    if tnx.get('ok'):
        prev = tnx.get('prev_close')
        prevtxt = f'（前值 {prev / 10:.3f}%）' if prev else ''
        parts.append(f"10 年期美债 <b>{tnx['value_pct']:.3f}%</b>{prevtxt}")
    if vix.get('ok'):
        parts.append(f"VIX <b>{vix['close']}</b>")
    if spx.get('ok'):
        parts.append(f"标普 500 <b>{spx['close']:,.2f}</b>")
    return (
        '<div class="say"><b>📊 宏观速览（P1 · CBOE 免密钥 · '
        + str(tnx.get('date', '—')) + '）</b><br>' + ' ｜ '.join(parts)
        + '<br><span class="small">⚠️ 这是<b>回退数据源</b>：CBOE 报价端点在 01/02 已被封，'
          '此处为最近一次可取得的快照，可能滞后一天。</span></div>'
    )


def _macro_long_block():
    # P1 + Step2 + Step3：FRED 全序列 + 报价交叉校验。

    def row(key, label=None, extra=''):
        it = _FI.get(key)
        if not it:
            return ''
        if it.get('unit') == '%':
            val = '%.2f%%' % it['value']
        else:
            val = '%g %s' % (it['value'], it.get('unit', ''))
        dt = it.get('date', '')
        if it.get('freq') == '月':
            dt = dt[:7]
        return ('<tr><td><b>%s</b></td><td class="n"><b>%s</b></td>'
                '<td class="n">%s</td><td>%s%s</td>'
                '<td class="n small">%s</td></tr>'
                % (label or it['label'], val, _delta(it.get('chg')),
                   it.get('note', ''), extra, dt))

    def inf_row(key, label):
        it = _FI.get(key)
        if not it:
            return ''
        mom = it.get('mom')
        momtxt = ('<span class="tag o">环比 %+.2f%%</span>' % mom) if mom is not None else ''
        return ('<tr><td><b>%s</b></td><td class="n"><b>%s%%</b></td>'
                '<td class="n">%s</td><td>%s</td>'
                '<td class="n small">%s</td></tr>'
                % (label or it['label'], it['value'], momtxt,
                   it.get('note', ''), it.get('date', '')[:7]))

    def card(k, v, d1, d2):
        return ('<div class="card"><div class="k">%s</div><div class="v">%s</div>'
                '<div class="d">%s</div><div class="d">%s</div></div>' % (k, v, d1, d2))

    ff = _FI.get('DFF', {})
    pce = _FI.get('PCEPILFE', {})
    un = _FI.get('UNRATE', {})
    d10 = _FI.get('DGS10', {})
    core = _FI.get('CPILFESL', {})
    pay = _FI.get('PAYEMS', {})
    t10 = OBB_MACRO.get('TNX', {})

    cards = '<div class="cards">' + ''.join([
        card('政策利率 (EFFR)', '%s%%' % ff.get('value', '—'), '美联储当前档位',
             '距 2% 通胀目标仍远 → 没有快速降息空间'),
        card('10 年美债', '%s%%' % t10.get('value_pct', '—'),
             'CBOE %s（当日）' % t10.get('date', '—'),
             'FRED 同口径 %s%%（%s，滞后 1–2 日）' % (d10.get('value', '—'), d10.get('date', '—'))),
        card('核心 PCE 同比', '%s%%' % pce.get('value', '—'), '美联储法定参考口径',
             '仍显著高于 2% 目标'),
        card('失业率', '%s%%' % un.get('value', '—'),
             '较上月 %+g pp' % un.get('chg', 0), '就业开始降温，但还没到衰退'),
    ]) + '</div>'

    ok = [r for r in XCHK if r.get('ok') and r.get('match')]
    bad = [r['symbol'] for r in XCHK if r.get('ok') and r.get('live_price') and not r.get('match')]
    badge = ('<div class="ok"><b>✅ 报价交叉校验：%d/%d 完全一致</b>'
             '（CBOE 官方收盘价 vs 本地 stockanalysis 抓取值，容差 0.05%%）%s'
             '<br><span class="small">这是防「CDN 缓存静默返回上一交易日收盘价」那个 bug 的自动哨兵 '
             '—— 今后本地价与官方价一旦偏离超过 0.05%% 就会被点名。</span></div>'
             % (len(ok), len(XCHK), ('<br>⚠️ 不一致：%s' % ', '.join(bad)) if bad else ''))

    gen = FRED.get('generated', '—')
    return (
        '<details class="fold"><summary>宏观长周期 · 利率 / 通胀 / 就业（FRED 免密钥）'
        '<span class="sm">核心 PCE %s%% ／ 失业率 %s%% ／ 实际利率 %s%% · 数据抓取于 %s</span>'
        '</summary><div class="fin">'
        '\n<h3 style="font-size:15px;margin:6px 0 2px">宏观仪表盘 · 利率 / 通胀 / 就业</h3>\n'
        '<p class="h2sub">数据源：FRED 公开序列（<b>免 API Key</b>）· 抓取于 %s · '
        '日频序列滞后 1–2 个交易日属正常</p>\n%s\n'
        '<table>\n'
        '  <tr><th>利率与资金成本</th><th class="n">数值</th><th class="n">日变动</th>'
        '<th>为什么看它</th><th class="n">数据日</th></tr>\n'
        '  %s\n  %s\n  %s\n  %s\n  %s\n  %s\n  %s\n</table>\n'
        '<table>\n'
        '  <tr><th>通胀</th><th class="n">同比</th><th class="n">环比</th>'
        '<th>备注</th><th class="n">数据月</th></tr>\n'
        '  %s\n  %s\n  %s\n</table>\n'
        '<table>\n'
        '  <tr><th>就业</th><th class="n">数值</th><th class="n">变化</th>'
        '<th>解读</th><th class="n">数据月</th></tr>\n'
        '  %s\n  %s\n</table>\n%s\n'
        '<div class="warn"><b>这 12 个数字连起来，讲的是 V1 版本根本讲不出来的故事：</b><br>'
        '① <b>通胀还没被打败</b> —— 核心 PCE 同比 <b>%s%%</b>，8 月核心 CPI 环比 '
        '<b>+%s%%</b>，已经高于"美联储可以按兵不动"的 0.2%% 线。<br>'
        '② <b>就业却在快速降温</b> —— 失业率升到 <b>%s%%</b>，非农月增从前值 %s 千'
        '掉到 <b>%s 千</b>。<br>'
        '③ 这两件事同时发生，就是最难受的组合：<b>想降息，通胀不允许；不降息，就业撑不住。</b><br>'
        '④ 再看资金成本：10 年期实际利率 <b>%s%%</b>、30 年期 <b>%s%%</b> —— '
        '<b>长端与真实利率都还很高，这才是压着成长股估值的根因</b>（不是"AI 故事讲完了"）。<br>'
        '<b>⑤ 本月两份通胀数据都已落地</b>（CPI 核心环比 +0.3%%、PPI 同比 +6.3%%），都落在'
        '"重燃加息争论"那一档 —— <b>回调单会更容易成交，但那也说明这不是"便宜的下跌"。</b><br>'
        '<b>⑥ 对持仓的直接含义：</b>这种"滞胀雏形 + 高真实利率"的环境，'
        '恰恰是 <b>2 倍杠杆品种最不友好的</b>（方向不明 + 波动率抬升 = 波动损耗最大化）。'
        '<b>这也正是 METU 只给 12%% 仓位、而且不因为跌了就加仓的原因。</b></div>\n'
        '</div></details>\n'
        % (pce.get('value', '—'), un.get('value', '—'),
           _FI.get('DFII10', {}).get('value', '—'), gen,
           gen, cards,
           row('DFF'), row('DGS2'),
           row('DGS10'),
           row('DGS30'),
           row('T10Y2Y', None, '　<span class="tag g">已脱离倒挂</span>'),
           row('DFII10'),
           row('T10YIE'),
           inf_row('CPIAUCSL', 'CPI 同比'),
           inf_row('CPILFESL', '核心 CPI 同比'),
           inf_row('PCEPILFE', '核心 PCE 同比'),
           row('UNRATE'),
           row('PAYEMS', '非农就业月增',
               '　<span class="tag r">前值 %g 千 → 本月骤降</span>' % pay.get('prev', 0)),
           badge,
           pce.get('value', '—'), core.get('mom', '—'), un.get('value', '—'),
           pay.get('prev', '—'), pay.get('value', '—'),
           _FI.get('DFII10', {}).get('value', '—'), _FI.get('DGS30', {}).get('value', '—'))
    )


def _v2_iv_block():
    """Step2/3 独有：用期权隐含波动率给"事件"标价。"""
    IV = _jload('obb_iv.json', {})
    tk_map = IV.get('tickers', {})
    if not tk_map:
        return ''

    def rows_for(sym, name):
        r = tk_map.get(sym, {})
        if not r.get('ok'):
            return ''
        html = ''
        for e in r['expiries'][:4]:
            tag = ' <span class="tag r">含财报</span>' if e.get('has_earnings') else ''
            html += ('<tr><td><b>%s</b></td><td class="n">$%s</td>'
                     '<td>%s%s</td><td class="n">%d</td>'
                     '<td class="n"><b>%.1f%%</b></td>'
                     '<td class="n">±$%.2f <span class="small">(±%.1f%%)</span></td></tr>'
                     % (name, r['spot'], e['expiration'], tag, e['dte'],
                        e['iv'] * 100, e['move'], e['move_pct']))
        return html

    body = rows_for('METU', 'METU（2倍META）') + rows_for('QQQM', 'QQQM（纳指100）')

    ev_html = ''
    ev = tk_map.get('METU', {}).get('event')
    if ev:
        shares = POS.get('METU', 0)
        pnl = shares * ev['implied_event_move_usd']
        lev = 2.0
        body_usd = ev['implied_event_move_usd']
        nav_pct = pnl / NAV * 100 if NAV else 0
        ev_html = (
            '<div class="warn"><b>★ 把财报单独剥出来：期权市场给 10/28 META 财报的定价是 ±%.1f%%</b><br>'
            '算法是方差分解 —— 用「不含财报的 %s（DTE %d，IV %.1f%%）」作基线，'
            '减去「含财报的 %s（DTE %d，IV %.1f%%）」，剩下的就是财报自己的方差。<br>'
            '<b>IV 台阶 %+.1f 个百分点，财报单独定价 ±%.1f%%（≈ $%.2f/股）。</b><br>'
            '换算到 META 本体（METU 是 2 倍）：<b>±%.1f%%</b> —— 而 META 近四次财报后的实际单日波动是 '
            '<b>-8.70%% / -8.51%% / +10.34%% / -10.80%%</b>。'
            '<b>期权市场既没高估也没低估，它是准的。</b><br>'
            '对你账户意味着什么：%d 股 × $%.2f = <b>±$%.2f ≈ 净值的 ±%.1f%%</b>。'
            '这就是"本月唯一实质敞口"的真实尺寸 —— 不是形容词，是可以提前算出来的数字。<br>'
            '<span class="small">附：QQQM 的 ATM IV 只有 19–21%%，是"平静"读数 —— '
            '意味着你的 QQQM 回调单不会因为短期抖动被击中，只能靠趋势性下跌成交。</span></div>'
            % (ev['implied_event_move_pct'], ev['baseline_exp'], ev['baseline_dte'],
               ev['baseline_iv'] * 100, ev['event_exp'], ev['event_dte'],
               ev['event_iv'] * 100, ev['iv_step_pp'], ev['implied_event_move_pct'],
               body_usd, ev['implied_event_move_pct'] / lev,
               shares, body_usd, pnl, nav_pct))

    return (
        '\n<h3 style="font-size:15px;margin:26px 0 2px">期权市场在给什么定价（CBOE 期权链）</h3>\n'
        '<p class="h2sub">隐含波动率是"真金白银买出来的预期"，比任何观点都硬 · '
        '抓取于 ' + str(IV.get('generated', '—')) + '</p>\n'
        '<table>\n'
        '  <tr><th>标的</th><th class="n">现价</th><th>到期日</th><th class="n">DTE</th>'
        '<th class="n">ATM 隐含波动率</th><th class="n">该期间的 1σ 隐含波动</th></tr>\n  '
        + body + '</table>\n' + ev_html
    )


def _cn(k, d=2):
    """从 CNBC 实时宏观里取「最新值」；缺失返回 —。"""
    v = ((_CNBC_MACRO or {}).get(k) or {}).get('last')
    return ('%.*f' % (d, v)) if v is not None else '—'


def _cnc(k, d=2):
    """取涨跌幅 %%（指数类）；缺失返回 —。"""
    v = ((_CNBC_MACRO or {}).get(k) or {}).get('chg_pct')
    return ('%+.*f%%' % (d, v)) if v is not None else '—'


def _cnv(k):
    """取涨跌幅的数值（用于 up() 上色）；缺失返回 0。"""
    return (((_CNBC_MACRO or {}).get(k) or {}).get('chg_pct') or 0)


def _cnb(k, d=1):
    """取基点变动（利率类）；缺失返回 —。"""
    v = ((_CNBC_MACRO or {}).get(k) or {}).get('chg_bp')
    return ('%+.*fbp' % (d, v)) if v is not None else '—'


def _cnprev(k, d=2):
    """取「上一交易日」的值：VIX 这类指标盘前会给期货价，不能拿它当昨天的收盘。"""
    v = ((_CNBC_MACRO or {}).get(k) or {}).get('prev')
    return ('%.*f' % (d, v)) if v is not None else '—'


def _cna(k):
    """取 as_of 时间（只留 HH:MM）。"""
    t = str(((_CNBC_MACRO or {}).get(k) or {}).get('as_of') or '')
    return t.replace(' EDT', '').replace(' EST', '')

def _lqq(sym):
    """取实时行情（自动识别 etf / stocks，避免踩 kind 默认值的坑）"""
    r = lq(sym)
    if not r:
        r = lq(sym, 'stocks')
    return r or {}


def _lp(sym):
    """取实时价"""
    return (_lqq(sym).get('price') or 0)


def _lc(sym):
    """取实时涨跌 %%"""
    return (_lqq(sym).get('chg_pct') or 0.0)


def _lcp(sym, kind='stocks'):
    """取**当前时段**涨跌 %%：盘前读 ext_chg_pct，盘中/收盘读 chg_pct。
    ⚠️ 盘前直接用 _lc() 会拿到上一交易日的收盘涨跌（02/20 踩：META 盘前 -1.74% 被显示成 +0.47%）。"""
    d = lq(sym, kind)
    if _PRE and d.get('ext_chg_pct') is not None:
        return d.get('ext_chg_pct') or 0.0
    return d.get('chg_pct') or 0.0


def _lpp(sym, kind='stocks'):
    """取**当前时段**价格：盘前读 ext_price。"""
    d = lq(sym, kind)
    if _PRE and d.get('ext_price'):
        return d.get('ext_price')
    return d.get('price')


def _idx_up_n():
    """今天有几个指数是涨的（0~5），用于判断"普涨 / 普跌 / 分化"。"""
    return sum(1 for k in ('DIA', 'IWM', 'SPY', 'QQQ', 'SOXX')
               if (ix(k, 'chg_pct') or 0) > 0)


def _rel(sym):
    """相对标普的表现短语。
    ⚠️ 只陈述事实：同一时段两个 ETF 的涨跌差。不写"资金流向"这类叙事 ——
    那类句子被方向变化打脸过三次（02/20 轮动、10/27 科技独涨、10/29 科技下跌）。"""
    a = ix(sym, 'chg_pct')
    b = ix('SPY', 'chg_pct')
    if a is None or b is None:
        return '—'
    d = a - b
    if d > 0.3:
        return '跑赢标普 <b>%+.2fpp</b>' % d
    if d < -0.3:
        return '跑输标普 <b>%+.2fpp</b>' % d
    return '与标普同步（差 %+.2fpp）' % d


def _sec_up_n():
    """11 个 SPDR 板块里有几个在涨（数据缺失 → None）。
    ⚠️ 只看指数会把"权重股拖累"误判成"普跌"：01/05 指数 1/5 上涨，
       但板块 7/11 上涨 —— 那是轮动，不是普跌。"""
    ks = ('XLK', 'XLC', 'XLY', 'XLF', 'XLI', 'XLV',
          'XLP', 'XLE', 'XLU', 'XLB', 'XLRE')
    vs = [(SECTOR.get(k) or {}).get('chg_pct') for k in ks]
    if all(v is None for v in vs):
        return None
    return sum(1 for v in vs if (v or 0) > 0)


def _idx_head():
    """04 节的标题句：方向由数据决定，不写死。日期已由 _idx_tag() 给出，这里不重复。"""
    n = _idx_up_n()
    s = _sec_up_n()
    if n >= 4:
        return ('<b>普涨</b>（%d/5 上涨）—— 上周那波"AI 敞口被单独抛售"的杀跌<b>暂时停了</b>，'
                '但费半仍<b>比 6/22 的纪录高点低两成以上</b> —— 反弹不等于修复。' % n)
    if n <= 1:
        if s is not None and s >= 6:
            return ('<b>指数在跌、内部在涨</b>（指数只有 %d/5 上涨，但 11 个板块有 <b>%d 个</b>在涨）'
                    '—— 这是<b>权重股拖累指数</b>，不是普跌。<b>看板块，别看指数。</b>' % (n, s))
        return ('<b>普跌</b>（只有 %d/5 上涨）。<b>这时看的是"谁跌得少"</b>，而不是"谁在涨"。' % n)
    return ('<b>分化</b>（%d/5 上涨%s）—— 资金在<b>换赛道</b>而不是离场，'
            '这才是"轮动 vs 普跌"最该盯的形态。'
            % (n, ('、板块 %d/11 上涨' % s) if s is not None else ''))


def _idx_tag():
    """指数涨跌的时点：盘前没有指数期货源，只能给「上一交易日收盘」；其余给当前时段。"""
    return ('%s 收盘' % D_PREV) if _PRE else ('%s %s' % (D_CUR, PHASE))


def _stk_tag():
    return '%s %s' % (D_CUR, PHASE)


def _pct_now(sym, kind='etf'):
    """当前时段涨跌（%）：盘前读 ext_chg_pct，盘中/收盘读 chg_pct；没有该标的返回 None。
    ⚠️ 不要直接写 lq(...)['ext_chg_pct'] —— 盘中它是 None，会渲染成 +0.00%。"""
    d = lq(sym, kind)
    if not d:
        return None
    return d.get('ext_chg_pct') if _PRE else d.get('chg_pct')


def _pcn(sym, kind='etf'):
    v = _pct_now(sym, kind)
    return pc(v) if v is not None else '—'


def _pclr(sym, kind='etf'):
    return up(_pct_now(sym, kind) or 0)


def _clr(v):
    return '#C0392B' if (v or 0) > 0 else ('#3B6D11' if (v or 0) < 0 else '#6b7280')


# 汇率（cnbc_macro.py 写入的 _FX 键；缺失则整段降级）
_FXR = (_CNBC_MACRO.get('_FX') or {}).get('rates') or {}
_FXD = (_CNBC_MACRO.get('_FX') or {}).get('date') or ''
USDC = _FXR.get('CNY')      # 1 USD = ? CNY
USDH = _FXR.get('HKD')      # 1 USD = ? HKD

# 宏观区（2026-10-16 起归一）：正文用「实时速览」，FRED 长周期仪表盘收进折叠区。
# 原 _v2_block()/_v2_iv_block() 仍保留定义（供将来手动调用），但默认不再产出第二份文件。
MACRO_BLOCK = _v1_block()
MACRO_LONG = _macro_long_block()
IV_BLOCK = ''

# ---------- 今日盘中合成一根日 K（CBOE 历史行情不含当日）----------
_MON = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
        'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}


def _asof_key(txt):
    try:
        p = (txt or '').replace(',', '').split()
        return '%02d%02d%02d' % (int(p[2]) % 100, _MON[p[0]], int(p[1]))
    except Exception:
        return None


def _live_bar(sym_):
    if sym_ in POS and LV.get(sym_):
        q = lq(sym_)
        return (q.get('open') or LV[sym_], LVHI.get(sym_) or LV[sym_],
                LVLO.get(sym_) or LV[sym_], LV[sym_])
    if sym_ in U and U[sym_].get('price'):
        q = U[sym_]
        return (q.get('open') or q['price'], q.get('day_high') or q['price'],
                q.get('day_low') or q['price'], q['price'])
    q = lq(sym_)
    if q.get('price'):
        return (q.get('open') or q['price'], q.get('day_high') or q['price'],
                q.get('day_low') or q['price'], q['price'])
    return None


_kkey = _asof_key(LVASOF) or _asof_key(BASE_ASOF)
if _kkey and KLINE:
    for _s in list(KLINE.keys()):
        _bar = _live_bar(_s)
        _seq = KLINE[_s].get('d')
        if not _bar or not _seq:
            continue
        if _kkey > _seq[-1][0]:
            _seq.append([_kkey] + [round(_v, 2) for _v in _bar])
            KLINE[_s]['live'] = True

# 首屏日期：从 D_CUR 推导（原来写死，过一天就错）
_WD = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
try:
    _m_, _d_ = [int(x) for x in D_CUR.split('/')]
    _DD = _dt.date(int(time.strftime('%Y')), _m_, _d_)
    HERO_DATE = '%d 年 %d 月 %d 日（%s）' % (_DD.year, _DD.month, _DD.day, _WD[_DD.weekday()])
except Exception:
    HERO_DATE = D_CUR


# ==================== 设计层 v3（样式 / 归一 / 折叠） ====================
def _apply_workbench(html):
    """工作台外壳 v1：左侧导航 + 顶部状态条 + 概览页（KPI / 六道闸门 / 纪律评分）。

    meta 全部在 Python 端算好再注入 JSON —— 不让浏览器去猜数字，
    页面上每一个数都能回到这里的某一行。
    """
    try:
        _css = open('workbench.css', encoding='utf-8').read()
        _js = open('workbench.js', encoding='utf-8').read()
    except Exception as _e:
        print('  [工作台跳过]', type(_e).__name__, str(_e)[:60])
        return html

    # ---------- 账户口径 ----------
    lev_pct = LEV / NAV * 100 if NAV else 0.0
    cash_pct = CASH / NAV * 100 if NAV else 0.0
    core_pct = CORE / NAV * 100 if NAV else 0.0
    dd_buf = abs(DD10 / NAV - 1) * 100 if NAV else 0.0

    risk_budget = NAV * 0.02                                   # 单笔风险预算 = 净值 2%
    risk_used = sum(max(0.0, LLAST[k] - STOP[k]) * POS.get(k, 0)
                    for k in STOP if k in POS)                 # 只算「有止损」的仓位
    risk_used = round(risk_used, 2)
    risk_free = round(max(0.0, risk_budget - risk_used), 2)
    # ⚠️ 未成交挂单要扣（成交后自动归零）；$90 是「回调档专项」下限，两者取大，不能相加 ——
    #    相加等于把同一笔 $90 扣两次（2026-10-23 踩：free_cash 被算成 127.61）
    _inflight = sum(_o[4] for _o in ORDERS)
    # 现金下限只含「整股零头 + 手续费 + 应急」；回调档弹药已于 2026-11-17 废止，不再单独扣
    free_cash = max(0.0, CASH - NAV * T_CASH_F - _inflight)
    cash_floor_amt = NAV * T_CASH_F                                  # 10% 硬下限（含回调档与手续费）

    metu_px = LLAST['METU']
    # METU 已清仓 → 没有在仓止损位，不能再说"距止损 +5%"
    _metu_sh = POS.get('METU', 0)
    _risk_note = ('无在仓止损 · METU 已清仓 %s' % mo(METU_EXIT)) if _metu_sh <= 0 else \
        (('止损已触发 · 待实现亏损 %s'
          % mo(abs((COST.get('METU', metu_px) - metu_px) * _metu_sh)))
         if (metu_px <= STOP.get('METU', 0)) else
         ('已占用 %s（%.0f%%）' % (mo(risk_used), (risk_used / risk_budget * 100) if risk_budget else 0)))

    # ⚠️ 止损纪律不能写死成「未触发」：盘中真的跌破就必须红掉并扣分
    _hit = [k for k in STOP if POS.get(k, 0) > 0 and LLAST[k] <= STOP[k]]
    _stop_hit = bool(_hit)
    # 止损成交日 = METU_EXIT_DATE；冷却只覆盖「成交当天」，隔一个交易日即解除（不写死 True）
    STOP_EXECUTED = True
    COOLDOWN = (D_CUR == METU_EXIT_DATE)
    vxus_px = LLAST['VXUS']
    vxus_gap = abs(83.0 / vxus_px - 1) * 100 if vxus_px else 0.0

    # ---------- 数据校验读数 ----------
    _vok, _vtot, _vskip = 0, 0, 0
    try:
        _vr = json.load(open('verify_report.json', encoding='utf-8')).get('summary') or {}
        _vok = int(_vr.get('ok') or 0)
        _vtot = int(_vr.get('total') or 0)
        _vskip = int(_vr.get('skip') or 0)
    except Exception:
        pass
    # ⚠️ skip 不是失败：CBOE 滞后一天，那几项本来就没有对照值
    _vpass = (_vok > 0 and (_vok + _vskip) >= _vtot)

    # ---------- 再平衡缺口（相对 targets.json）----------
    _tgt = {'SPYM': T_SPYM, 'QQQM': T_QQQM, 'VXUS': T_VXUS}
    _gap = {k: _tgt[k] - MV[k] / NAV * 100 for k in _tgt}
    _under = max(_gap, key=lambda k: _gap[k])          # 缺口最大的那个
    _under_gap = _gap[_under]
    _px = LLAST[_under]
    _deploy = max(0.0, CASH - NAV * T_CASH_F)          # 可投到现金下限
    _frac_ok = (_under != 'SPYM')                      # QQQM/VXUS 可碎股
    _sh = (_deploy / _px) if (_frac_ok and _px) else (int(_deploy // _px) if _px else 0)
    _amt = _sh * _px
    _fee_pct = FEE / _amt * 100 if _amt else 99.0
    _spym_ord = ORDERS[0][3] if ORDERS else None
    _spym_gap = (LLAST['SPYM'] / _spym_ord - 1) * 100 if _spym_ord else 0.0
    _cash_ok = (cash_pct > T_CASH + 0.3) and (_sh >= 1)

    # ---------- 七道闸门（现实里的否决顺序，不是比喻）----------
    gates = [
        {'k': '铁律', 's': 'on' if LEV <= 0 else 'off',
         't': ('已无杠杆仓位' if LEV <= 0 else '2 倍杠杆只减不加'),
         'd': ('METU 已清仓，杠杆归零、且不再重建' if LEV <= 0 else '任何价格都不加仓，只允许卖出')},
        {'k': '冷却', 's': 'off' if COOLDOWN else 'on', 't': '止损后 24 小时',
         'd': ('今日已执行止损，不新建仓' if COOLDOWN
               else '已隔 %s 成交，冷却解除' % METU_EXIT_DATE)},
        {'k': '现金', 's': 'on' if _cash_ok else 'off',
         't': '可投 %s（超下限 %.1fpp）' % (mo(_deploy), cash_pct - T_CASH),
         'd': ('现金 %s = %.2f%%，下限 %g%% = %s；可投额度 = 现金 − 下限'
               % (mo(CASH), cash_pct, T_CASH, mo(cash_floor_amt)))
              if _cash_ok else
              ('现金 %.2f%% 已在下限 %g%%（%s）以内，或不足 1 股' % (cash_pct, T_CASH, mo(cash_floor_amt)))},
        {'k': '手续费', 's': 'idle' if _amt <= 0 else ('on' if _fee_pct <= 1.0 else 'off'),
         't': ('无待下单' if _amt <= 0 else '单笔 %s 费 %.2f%%' % (mo(_amt), _fee_pct)),
         'd': ('可投额度不足 1 股，今天没有可执行的买盘'
               % ()) if _amt <= 0 else
              (('$1.88 ÷ %s = %.2f%%，低于 1%% 的门槛（再平衡按实际下单金额算）'
                % (mo(_amt), _fee_pct)) if _fee_pct <= 1.0 else
               ('$1.88 ÷ %s = %.2f%%，单笔太小，费用吃掉收益' % (mo(_amt), _fee_pct)))},
        {'k': '槽位', 's': 'off' if _under_gap >= 5 else 'on',
         't': '%s 缺口 %.1fpp' % (_under, _under_gap),
         'd': ('目标 %g%% / 实际 %.2f%% —— 超出 ±%gpp 容差，需要补'
               % (_tgt[_under], MV[_under] / NAV * 100, 5))
              if _under_gap >= 5 else
              ('三个仓位都在 ±5pp 容差内（SPYM %g / QQQM %g / VXUS %g）'
               % (T_SPYM, T_QQQM, T_VXUS))},
        {'k': '风控', 's': 'hit' if _stop_hit else 'on',
         't': ('METU 已跌破止损（现 $%.2f）' % metu_px) if _stop_hit else '无在仓止损位',
         'd': ('止损已触发 → 清仓，不讨论、不等反弹' if _stop_hit
               else '在仓三个宽基没有止损线（核心仓不设）；个股止损在建仓前写死')},
        {'k': '挂单', 's': 'warn' if _spym_ord else 'idle',
         't': ('旧单 $%.2f 建议撤销' % _spym_ord) if _spym_ord else '无挂单',
         'd': ('现价 %s 仍比它高 %.2f%%，且近 60 个交易日从未触及 —— 它等不到；'
               '再平衡要用"现在能成交的价"' % (mo(LLAST['SPYM']), _spym_gap))
              if _spym_ord else '—'},
    ]

    # ---------- 纪律评分（只看规则达成度，与盈亏无关）----------
    items = [
        {'k': '杠杆合规', 'v': '%.2f%% ≤ 0%%' % lev_pct, 'ok': lev_pct <= 0.1, 'w': 20},
        {'k': '现金下限', 'v': '%.2f%% ≥ %g%%' % (cash_pct, T_CASH), 'ok': cash_pct >= T_CASH, 'w': 20},
        {'k': '回撤缓冲', 'v': '%.1fpp ≥ 5pp' % dd_buf, 'ok': dd_buf >= 5, 'w': 20},
        {'k': '止损纪律',
         'v': ('已执行：METU @ $%.2f（%s）' % (METU_EXIT, METU_EXIT_DATE)) if STOP_EXECUTED else '未触发',
         'ok': True, 'w': 20},
        {'k': '挂单纪律',
         'v': ('1 张待撤销' if _spym_ord else '无失效单'), 'ok': not _spym_ord, 'w': 10},
        {'k': '数据校验',
         'v': ('%d 项一致 · %d 跳过' % (_vok, _vskip)) if _vtot else '—',
         'ok': _vpass, 'w': 10},
    ]
    score_total = sum(i['w'] for i in items if i['ok'])

    # ---------- 槽位（目标来自 targets.json）----------
    slots = [
        {'n': 'SPYM', 'w': MV['SPYM'] / NAV * 100, 't': T_SPYM},
        {'n': 'QQQM', 'w': MV['QQQM'] / NAV * 100, 't': T_QQQM},
        {'n': 'VXUS', 'w': MV['VXUS'] / NAV * 100, 't': T_VXUS},
        {'n': '现金', 'w': cash_pct, 't': T_CASH},
    ]

    # ---------- 今日动作 ----------
    if _stop_hit:
        actions = [
            {'t': '<b>卖出 %s %g 股（市价）</b> —— 已跌破止损，触发即执行，不等反弹、不改价格'
                  % (_hit[0], POS[_hit[0]]), 'do': True},
            {'t': '<b>卖出后现金约 %s</b>，账户回到纯核心仓' % mo(CASH + LLAST[_hit[0]] * POS[_hit[0]] - 1.88),
             'do': False},
        ]
    elif _cash_ok and _fee_pct <= 1.0:
        actions = [
            {'t': '<b>买入 %s %s 股</b>，限价 = <b>开盘价 × 1.002</b>（约 %s）—— 再平衡，'
                  '把现金从 %.1f%% 降到 %g%% 附近（%s 缺口 %.1fpp）'
                  % (_under, ('%.4f' % _sh) if _frac_ok else int(_sh),
                     mo(_amt), cash_pct, T_CASH, _under, _under_gap), 'do': True},
            {'t': '<b>成交后：%s 权重约 %.1f%%%s，现金 %.1f%%</b> —— 回到容差内'
                  % (_under, (MV[_under] + _amt) / NAV * 100,
                     '（含在途）' if _spym_ord else '', (CASH - _amt) / NAV * 100), 'do': False},
            {'t': '<b>不再拆单</b>：SPYM 整股、手续费按笔收，拆 3 张的 $5.64 等于多付 1.59% —— '
                  '再平衡看方向，不看分毫不差', 'do': False},
            {'t': '<b>成交后把价与股数告诉我</b>，我更新持仓并记进 trades.csv', 'do': False},
        ]
    else:
        actions = [
            {'t': '<b>现金 %.1f%% 已在 %g–%g%% 区间</b> —— 今天不需要再动手'
                  % (cash_pct, T_CASH, T_CASH_HI), 'do': False},
            {'t': ('<b>%s 还差 %.1fpp，但可投额度只剩 %s（不足 1 股）</b> —— '
                   '等 11 月入金到账后按月度开关再买；<b>不要为补齐缺口去动 QQQM 或 VXUS</b>'
                   % (_under, _under_gap, mo(max(0.0, CASH - NAV * T_CASH_F))))
                  if _under_gap >= 5 else
                  ('<b>三个仓位都在 ±5pp 容差内</b>，不做微调；等下一次入金或偏离超过 5pp 再说'),
             'do': False},
        ]

    # ---------- 今日结论（由缺口与闸门推出，不写死）----------
    _off = sum(1 for _g in gates if _g['s'] in ('off', 'warn'))
    _on = sum(1 for _g in gates if _g['s'] == 'on')
    if _stop_hit:
        _verdict = ('执行', '%s 已跌破止损 —— 清仓，这是规则里唯一不允许犹豫的动作' % _hit[0])
    elif _cash_ok and _fee_pct <= 1.0 and _under_gap >= 5:
        _verdict = ('执行', '再平衡：补 %s %s 股（%s），现金 %.1f%% → %.1f%%'
                    % (_under, ('%.4f' % _sh) if _frac_ok else int(_sh), mo(_amt),
                       cash_pct, (CASH - _amt) / NAV * 100))
    elif _under_gap >= 5:
        _verdict = ('等待', '%s 缺口 %.1fpp，但可投额度不足一手或费用过高' % (_under, _under_gap))
    else:
        _verdict = ('仅观察', '三个仓位都在 ±5pp 容差内，现金 %.1f%% 在 %g–%g%% 区间 —— 今天没有需要动手的事'
                    % (cash_pct, T_CASH, T_CASH_HI))

    # ---------- 星期 ----------
    _wd = ''
    try:
        import datetime as _dtm
        _mm, _dd = D_CUR.split('/')
        _wd = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][
            _dtm.date(_dtm.date.today().year, int(_mm), int(_dd)).weekday()]
    except Exception:
        pass

    meta = {
        'date': D_CUR, 'wd': _wd, 'phase': PHASE,
        'asof': lq('SPYM').get('ext_as_of') or LVASOF,
        'dayLabel': D_PREV,
        'nav': round(NAV, 2), 'navPrev': round(NAV_PREV, 2), 'navNow': round(PMNAV, 2),
        # ⚠️ 盘中 PMNAV == NAV，用 PMNAV-NAV 会得到 0.00 —— 当日盈亏必须对上一收盘
        'pnl': round(NAV - NAV_PREV, 2), 'pnlPct': round((NAV / NAV_PREV - 1) * 100, 2),
        'lev': round(lev_pct, 2), 'levCap': 15.5,
        'cash': round(CASH, 2), 'cashPct': round(cash_pct, 2), 'cashFloor': T_CASH,
        'core': round(core_pct, 2), 'coreTarget': T_SPYM + T_QQQM + T_VXUS,
        'hwm': round(HWM, 2), 'dd10': round(DD10, 2), 'dd15': round(DD15, 2),
        'ddBufPct': round(dd_buf, 1),
        'riskBudget': round(risk_budget, 2), 'riskUsed': risk_used, 'riskFree': risk_free,
        'riskNote': _risk_note,
        'freeCash': round(free_cash, 2), 'minTrade': 150, 'fee': 1.88,
        'gates': gates,
        'verdict': {'tag': _verdict[0], 'text': _verdict[1]},
        'score': {'total': score_total, 'items': items},
        'slots': slots,
        'actions': actions,
    }

    try:
        _blob = json.dumps(meta, ensure_ascii=False).replace('</', '<\\/')
    except Exception:
        _blob = '{}'

    html = html.replace('</style>', _css + '\n</style>', 1)
    html = html.replace('</body>',
                        '<script id="wb-meta" type="application/json">' + _blob + '</script>\n</body>', 1)
    html = html.replace('</body>', '<script>\n' + _js + '\n</script>\n</body>', 1)
    return html


def _apply_dashboard(html):
    """面板视图 v1：注入 dashboard.css / dashboard.js（DOM 重排在浏览器端完成）。

    为什么在浏览器端重排而不是 Python 端切分：
      · `_apply_design` 会把 08/10 包进 <details class="fold">，章节边界变成嵌套结构，
        Python 端按 <h2> 切片会把 details 的开/闭标签切错；
      · 浏览器端用 DOM 看「真实父节点」，嵌套由浏览器自己解释，天然正确；
      · 失败时原 DOM 完好无损 → 自动退回清单视图，不会白屏。
    """
    try:
        _css = open('dashboard.css', encoding='utf-8').read()
        _js = open('dashboard.js', encoding='utf-8').read()
    except Exception as _e:
        print('  [面板视图跳过]', type(_e).__name__, str(_e)[:60])
        return html

    # ① 尽早设定 data-view，避免「先按清单渲染、再跳成面板」的闪动
    boot = ('<script>try{var _s=JSON.parse(localStorage.getItem("pb.dash.v3")||"{}");'
            'document.documentElement.dataset.view=(_s.view==="list"?"list":(_s.view==="panel"?"panel":"work"));}'
            'catch(e){document.documentElement.dataset.view="panel";}</script>')
    if '<style>' in html:
        html = html.replace('<style>', boot + '\n<style>', 1)

    # ② 样式追加到 <style> 末尾（此时 KLINE_CSS 已在其中，面板样式最后生效）
    html = html.replace('</style>', _css + '\n</style>', 1)

    # ③ 外壳脚本放在 </body> 前（K 线脚本之后，openK 已就绪）
    html = html.replace('</body>', '<script>\n' + _js + '\n</script>\n</body>', 1)
    return html


# ---------------- 咨询报告外壳（2026-11-17 新增） ----------------
# 目标：像大摩 / 益普索那种 research note —— 封面 + 目录 + 分页矩形版面，
# 去掉左侧导航与三视图切换；目录带锚点，点击直接跳到对应章节。
_RPT_CSS = """
html{scroll-behavior:smooth}
body{margin:0;background:#eceef1;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
     color:#16202b;-webkit-font-smoothing:antialiased;font-size:14px;line-height:1.75}
.page{width:1180px;max-width:calc(100% - 28px);margin:26px auto;background:#fff;padding:60px 72px 72px;
      min-height:720px;position:relative;border:1px solid #e3e7ec}
.eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#8a95a3;margin:0 0 26px;
         border-bottom:1px solid #e3e7ec;padding-bottom:10px}
.cover{display:flex;flex-direction:column;justify-content:space-between}
.cover .kicker{font-size:12px;letter-spacing:.2em;color:#8a95a3;text-transform:uppercase}
.cover h1{font-size:46px;line-height:1.2;font-weight:600;margin:18px 0 10px;letter-spacing:-.5px}
.cover .lede{font-size:16px;color:#5d6a78;max-width:720px;margin:0}
.rule{height:3px;background:#123a5f;width:88px;margin:34px 0}
.cover-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border-top:1px solid #e3e7ec;margin-top:auto}
.cover-metrics div{padding:22px 24px 0 0;border-right:1px solid #e3e7ec}
.cover-metrics div:last-child{border-right:none}
.cover-metrics .k{font-size:11px;color:#8a95a3;letter-spacing:.1em;text-transform:uppercase}
.cover-metrics .v{font-size:28px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.cover-metrics .d{font-size:12px;color:#5d6a78;margin-top:2px}
.toc-h{font-size:12px;letter-spacing:.2em;color:#8a95a3;text-transform:uppercase;margin:0 0 26px}
.toc-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 56px}
.toc-item{display:flex;gap:16px;padding:15px 0;border-bottom:1px solid #eef1f4;text-decoration:none;color:inherit}
.toc-item:hover .toc-t{color:#123a5f}
.toc-no{font-size:12px;color:#9aa5b2;font-variant-numeric:tabular-nums;padding-top:3px;min-width:20px}
.toc-t{font-size:15px;font-weight:500}
.toc-d{font-size:12px;color:#8a95a3;margin-left:auto;padding-top:3px}
h2{font-size:23px;font-weight:600;margin:0 0 22px;padding-bottom:14px;border-bottom:2px solid #16202b;
   letter-spacing:-.2px;display:flex;align-items:baseline;gap:14px}
h2 .no,.h2 .no{font-size:13px;color:#9aa5b2;font-weight:400;letter-spacing:.1em;font-variant-numeric:tabular-nums}
h3{font-size:15px;font-weight:600;margin:28px 0 10px}
h2sub,.h2sub{display:block;color:#5d6a78;font-size:13.5px;margin:-8px 0 24px;line-height:1.7}
p{margin:0 0 14px}
b{font-weight:600}
table{width:100%;border-collapse:collapse;margin:18px 0 22px;font-size:13px;font-variant-numeric:tabular-nums}
th{background:#f7f9fb;font-weight:600;font-size:12px;letter-spacing:.03em;color:#48566a;
   text-align:left;padding:11px 12px;border-bottom:1.5px solid #cfd6de}
td{padding:11px 12px;border-bottom:1px solid #eef1f4;vertical-align:top}
tr:last-child td{border-bottom:1px solid #cfd6de}
.n{text-align:right;font-variant-numeric:tabular-nums}
.warn,.ok,.say{border-left:3px solid #d9dee4;background:#fafbfc;padding:18px 22px;margin:18px 0;font-size:13.5px}
.warn{border-left-color:#b4541e;background:#fdf8f4}
.ok{border-left-color:#16653f;background:#f5faf7}
.say{border-left-color:#123a5f;background:#f6f8fb}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:2px;background:#eef1f4;
     color:#48566a;letter-spacing:.02em;vertical-align:middle}
.tag.g{background:#e8f4ec;color:#16653f}.tag.r{background:#fbeceb;color:#a0291f}
.tag.o{background:#fcf3e3;color:#8a5a00}
.card{background:#f7f9fb;padding:16px 18px;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:18px 0 22px}
.card .k{font-size:11px;color:#8a95a3;letter-spacing:.08em;text-transform:uppercase}
.card .v{font-size:24px;font-weight:600;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.card .d{font-size:12px;color:#5d6a78}
.small,span.small{font-size:12px;color:#7f8a97}
.cap{font-size:12px;color:#7f8a97;margin:-8px 0 20px;line-height:1.65}
.khint{font-size:12px;color:#8a95a3;margin:6px 0 18px}
.krow{cursor:pointer}
.krow:hover td{background:#f7f9fb}
.bar{display:none}
.pfoot{position:absolute;left:72px;right:72px;bottom:26px;display:flex;justify-content:space-between;
       font-size:11px;color:#9aa5b2;letter-spacing:.06em;border-top:1px solid #eef1f4;padding-top:10px}
.kbug{display:none}
ol,ul{margin:0 0 16px;padding-left:22px}
li{margin:0 0 12px;line-height:1.7}
details.fold{border:1px solid #e3e7ec;padding:14px 18px;margin:18px 0;font-size:13px}
details.fold summary{cursor:pointer;font-weight:500;color:#48566a}
a{color:#123a5f}
@media print{
  body{background:#fff}
  .page{width:auto;max-width:none;margin:0;border:none;min-height:0;page-break-after:always;padding:24mm 18mm}
  .pfoot{display:none}
}
@media (max-width:900px){
  .page{padding:32px 22px}
  .cover h1{font-size:32px}
  .cover-metrics{grid-template-columns:1fr 1fr}
  .toc-grid{grid-template-columns:1fr}
}
"""


def _apply_report(html):
    import re as _re
    b0 = html.index('<body>') + len('<body>')
    b1 = html.rindex('</body>')
    body = html[b0:b1]
    kp = body.find('<div class="kmask"')
    tail = body[kp:] if kp > 0 else ''
    core = body[:kp] if kp > 0 else body

    m_h1 = _re.search(r'<h1[^>]*>(.*?)</h1>', core, _re.S)
    title = _re.sub(r'<[^>]+>', '', m_h1.group(1)).strip() if m_h1 else '美股每日战报'
    hero = ''
    mh = _re.search(r'<div class="hero">(.*?)</div>\s*(?=<div|<h2)', core, _re.S)
    if mh:
        hero = mh.group(0)

    # 首屏之前的引导块（hero 之后的第一个 warn/ok/say）
    intro = ''
    mi = _re.search(r'(<div class="(?:warn|ok|say)"[^>]*>.*?</div>)', core[:20000], _re.S)
    if mi:
        intro = mi.group(1)

    h2s = list(_re.finditer(r'<h2[^>]*>(.*?)</h2>', core, _re.S))
    secs = []
    for i, m in enumerate(h2s):
        inner = m.group(1)
        m2 = _re.search(r'class="no">([^<]+)<', inner)
        no = (m2.group(1).strip() if m2 else '%02d' % i)
        t = _re.sub(r'<[^>]+>', ' ', inner)
        t = t.replace(no, '', 1).strip()
        t = _re.sub(r'\s+', ' ', t)
        start = m.end()
        end = h2s[i + 1].start() if i + 1 < len(h2s) else len(core)
        secs.append((no, t, core[start:end]))

    # 目录
    toc = []
    for no, t, _c in secs:
        short = t if len(t) <= 26 else t[:25] + '…'
        toc.append('<a class="toc-item" href="#s%s"><span class="toc-no">%s</span>'
                   '<span class="toc-t">%s</span><span class="toc-d">→</span></a>'
                   % (no, no, short))
    toc_html = '<div class="toc-grid">%s</div>' % ''.join(toc)

    # 指标带
    _dd = ''
    if DEPOSIT:
        _dd = '含入金 %s' % mo(DEPOSIT)
    metrics = (
        '<div class="cover-metrics">'
        '<div><div class="k">净值</div><div class="v">%s</div><div class="d">%s</div></div>'
        '<div><div class="k">现金 / 杠杆</div><div class="v">%.1f%%</div><div class="d">杠杆 %.2f%%</div></div>'
        '<div><div class="k">核心三仓</div><div class="v">%.2f%%</div><div class="d">目标 %d%%</div></div>'
        '<div><div class="k">回撤缓冲</div><div class="v">%.1f%%</div><div class="d">-10%% 线 %s</div></div>'
        '</div>' % (mo(NAV), _dd or NAV_CMP, CASH / NAV * 100, LEV / NAV * 100,
                    CORE / NAV * 100, T_SPYM + T_QQQM + T_VXUS,
                    (1 - DD10 / NAV) * 100, mo(DD10)))

    kg = _re.search(r'<p class="meta">(.*?)</p>', core, _re.S)
    meta_line = _re.sub(r'<[^>]+>', ' ', kg.group(1)) if kg else ''
    meta_line = _re.sub(r'\s+', ' ', meta_line).strip()[:260]

    parts = []
    parts.append('<section class="page cover">')
    parts.append('<div><div class="kicker">Equity Research · Daily Note</div>')
    parts.append('<h1>%s</h1>' % title)
    parts.append('<p class="lede">目标配置 SPYM %d%% / QQQM %d%% / VXUS %d%% / 现金 %d%% &nbsp;·&nbsp; 杠杆 %.2f%%（上限 15.5%%） &nbsp;·&nbsp; 数据截至 %s</p>'
                 % (T_SPYM, T_QQQM, T_VXUS, T_CASH, LEV / NAV * 100, LVASOF))
    parts.append('<div class="rule"></div></div>')
    parts.append(metrics)
    parts.append('</section>')

    parts.append('<section class="page"><div class="eyebrow">目录 · Contents</div>'
                 '<div class="toc-h">点击标题可直接跳转</div>%s</section>' % toc_html)

    for no, t, c in secs:
        parts.append('<section class="page" id="s%s"><div class="eyebrow">%s · %s</div>'
                     '<h2><span class="no">%s</span>%s</h2>%s'
                     '<div class="pfoot"><span>美股每日战报 · %s</span><span>%s</span></div></section>'
                     % (no, title, HERO_DATE, no, t, c, HERO_DATE, no))

    out = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>%s · %s</title>%s<style>%s</style></head><body>%s%s</body></html>'
           % (title, HERO_DATE, html[html.index('<style>'):html.index('</style>') + 8],
              _RPT_CSS, ''.join(parts), tail))
    return out


def _apply_design(html):
    # ① 换样式
    try:
        _css = open('brief_style.css', encoding='utf-8').read()
    except Exception:
        _css = ''
    if _css:
        i0 = html.index('<style>') + len('<style>')
        i1 = html.index('</style>')
        html = html[:i0] + '\n' + _css + '\n' + html[i1:]

    # ② 配色归一（41 种 → 1 套调色板）
    PAL = [
        ('#3B6D11', '#0F7B4F'), ('#1D9E75', '#0F7B4F'), ('#0F6E56', '#0E6B54'),
        ('#185FA5', '#1F5FA8'), ('#378ADD', '#4A8BD6'), ('#854F0B', '#95590A'),
        ('#f6f8fa', '#F7F9FB'), ('#FDF3F2', '#FDF1EF'), ('#FCEBEB', '#FDF1EF'),
        ('#EFF8F1', '#E9F5EE'), ('#EAF3DE', '#E9F5EE'), ('#FFF8E6', '#FCF4E6'),
        ('#FAEEDA', '#FCF4E6'), ('#F1EFE8', '#F1F3F6'), ('#F5F6F8', '#F5F7FA'),
        ('#E6F1FB', '#EAF1F9'), ('#edf0f3', '#F0F3F6'), ('#f2f5f8', '#F2F5F8'),
        ('#8b929c', '#8B929C'), ('#98a0aa', '#98A0AA'), ('#a8afb8', '#A8AFB8'),
        ('#6b7280', '#5A626C'), ('#4a5058', '#5A626C'), ('#5b6470', '#5A626C'),
        ('#7d2b23', '#7C2A20'), ('#1a5c45', '#0B5A3B'), ('#fcfdfd', '#F7F9FB'),
    ]
    for a, b in PAL:
        html = html.replace(a, b)

    # ③ 章节重排（持仓 → 挂单 → 触发线 → …）
    try:
        html = _restructure(html)
    except Exception as _e:
        print('  [重排降级]', type(_e).__name__, str(_e)[:70])

    # ④ 分组分隔
    GRP = [('00', '概览'), ('01', '持仓与执行'), ('04', '市场'), ('06', '事件与动作'), ('08', '参考')]
    for no, name in GRP:
        tag = '<h2><span class="no">%s</span>' % no
        if tag in html:
            html = html.replace(tag, '<div class="grp">%s</div>\n%s' % (name, tag), 1)

    # ⑤ 折叠深度章节
    def _fold(no, title, note=''):
        nonlocal html
        a = html.find('<h2><span class="no">%s</span>' % no)
        if a < 0:
            return
        nxt = html.find('<h2><span class="no">', a + 10)
        b = nxt if nxt > 0 else html.find('<p class="foot">')
        if b < 0:
            return
        seg = html[a:b]
        seg = seg.replace('<h2><span class="no">%s</span>' % no, '<h2><span class="no">%s</span>' % no)
        head = '<details class="fold"><summary>%s<span class="sm">%s</span></summary><div class="fin">' % (title, note)
        html = html[:a] + head + seg + '</div></details>\n' + html[b:]

    _fold('08', '汇率与资金 · 账户结构', '现金拆解／入金计划')
    _fold('10', '候选标的池 与 回测 · backtesting.py', '槽位筛选 + 10 标的 × 3 策略')

    # 数据来源也折叠
    if '<p class="foot">' in html:
        a = html.find('<p class="foot">')
        # ⚠️ 边界必须停在 K 线弹层之前，否则弹层会被包进折叠区、关闭时整块不可见
        b = html.find('<div class="kmask"')
        if b < 0:
            b = html.find('</body>')
        seg = html[a:b]
        html = html[:a] + ('<details class="fold"><summary>数据来源与局限说明'
                           '<span class="sm">逐项标注来源</span></summary>'
                           '<div class="fin">%s</div></details>\n' % seg) + html[b:]

    return html


def _restructure(html):
    """把扁平 13 节重排为：概览 → 持仓 → 挂单 → 触发线 → 走势 → 市场 → 事件 → 动作 → 参考"""
    import re as _r
    a = html.index('<h2><span class="no">00</span>')
    b = html.index('<p class="foot">')
    head, body, tail = html[:a], html[a:b], html[b:]
    mk = [(int(m.group(1)), m.start()) for m in _r.finditer(r'<h2><span class="no">(\d+)</span>', body)]
    sec = {}
    for i, (no, st) in enumerate(mk):
        en = mk[i + 1][1] if i + 1 < len(mk) else len(body)
        sec[no] = body[st:en]

    def bare(frag):
        """去掉 section 的 h2 + 首行 h2sub（用于合并两节）"""
        frag = _r.sub(r'^<h2><span class="no">\d+</span>[^<]*</h2>\s*', '', frag, count=1)
        frag = _r.sub(r'^<p class="h2sub">.*?</p>\s*', '', frag, count=1, flags=_r.S)
        return frag

    def titled(frag, no, title, sub=None):
        frag = _r.sub(r'<h2><span class="no">\d+</span>[^<]*</h2>',
                      '<h2><span class="no">%s</span>%s</h2>' % (no, title), frag, count=1)
        if sub is not None:
            frag = _r.sub(r'<p class="h2sub">.*?</p>',
                          '<p class="h2sub">%s</p>' % sub, frag, count=1, flags=_r.S)
        return frag

    # 持仓 = 原 01（账户快照）+ 原 02（技术信号）
    hold = titled(sec[1], '01', '持仓',
                  '股数 / 收盘 / %s价 / 市值 / 占比 vs 目标，<b>点行名可看 K 线</b>' % PHASE)
    if 2 in sec:
        hold += '\n' + bare(sec[2])

    # 事件 = 原 05（大事件）+ 原 07（日历）+ 今日事件窗口
    raw5 = sec[5]
    evt = ('<h2><span class="no">06</span>今日事件</h2>'
           '<p class="h2sub">盘前 / 盘中 / 盘后一屏看完；<b>展开可看深度解读与未来 30 天催化剂日历</b></p>'
           + evt_window())
    _deep = bare(raw5)
    if 7 in sec:
        _deep += ('\n<h3 style="font-size:13.5px;font-weight:650;color:#5A626C;margin:22px 0 7px">'
                  '未来 30 天催化剂日历</h3>\n' + bare(sec[7]))
    evt += ('\n<details class="fold"><summary>深度解读与催化剂日历'
            '<span class="sm">台积电 Q3 ／ CPI 之后／PPI 前瞻／未来 30 天</span>'
            '</summary><div class="fin">' + _deep + '</div></details>')

    # 候选池与回测合并
    cand = titled(sec[11], '10', '候选标的池 与 回测',
                  '槽位筛选 + backtesting.py 回测；<b>点行名可看 K 线</b>')
    if 12 in sec:
        cand += '\n' + bare(sec[12])

    NEW = [hold, order_block(), sec[8], sec[3], sec[4], evt, sec[6], sec[9], sec[10],
           eq_block(), cand]

    # 概览（原 00）保留在最前
    out = sec[0] + '\n' + '\n'.join(NEW)
    # 统一重编号（原有各节编号已乱，按最终顺序 00..N 重排）
    _c = [0]

    def _rn(_m):
        v = '%02d' % _c[0]
        _c[0] += 1
        return '<h2><span class="no">%s</span>' % v

    out = _r.sub(r'<h2><span class="no">\d+</span>', _rn, out)
    return head + out + tail



def _hero(html, line, nums):
    """在 meta 之后插入决策条"""
    anchor = '</p>\n\n<div class="warn"'
    if anchor not in html:
        anchor = '</p>'
    box = ['<div class="hero"><div class="htop"><span class="hdate">%s</span>'
           '<span class="hph">%s</span></div><div class="hline">%s</div><div class="hnum">'
           % (HERO_DATE, PHASE, line)]
    for k, v, d in nums:
        box.append('<div class="hn"><div class="hk">%s</div><div class="hv">%s</div>'
                   '<div class="hd">%s</div></div>' % (k, v, d))
    box.append('</div></div>')
    return html.replace(anchor, '</p>' + ''.join(box) + '\n\n<div class="warn"', 1)


# ---------- 第 12 节：候选标的回测（bt_section.py，缺失自动降级）----------
try:
    import bt_section as _bts
    BT_BLOCK = _bts.build(_os.path.dirname(_os.path.abspath(__file__)))
except Exception as _e:
    BT_BLOCK = ''
    print('  [回测节降级]', type(_e).__name__, str(_e)[:60])

def _mast_old(k, n=20):
    """已废弃：_mast 已上移到 rows2 之前。（保留占位，避免引用顺序问题）"""
    return ''


# ---------------- 持仓表（动态生成：抗跌/最弱标签 + 相位中立措辞）----------------
# ⚠️ 只对「还在仓」的标的评最强/最弱 —— 已清仓的（POS=0）不参与，否则标签会落到空仓上
_HELD = [k for k in POS if POS.get(k, 0) > 0]
_bestk = max(_HELD, key=lambda k: LCHG[k])
_worstk = min(_HELD, key=lambda k: LCHG[k])
NOWW = '现' if INTRADAY else '收'

_CMT = {
    'SPYM': '核心宽基，波动最小的一档；<b>%s</b>' % _mast('SPYM'),
    'QQQM': '<span class="tag g">回调单 01/02 成交</span> 0.25 股 @ $85.00 → 持 <b>%.4f 股</b>，%s' % (POS['QQQM'], _mast('QQQM')),
    'VXUS': '%s；<b>01/03 挂单 0.3855 股 @ $83</b>' % _mast('VXUS'),
    'METU': '母公司 <b>META %s</b>，2 倍放大 → %s 贡献 <b>%s</b>；距 $34 需 <b>%+.2f%%</b>' % (
        pc(U['META']['chg_pct']), D_PREV, mo(MV['METU'] - PREVMV['METU']), (34 / LV['METU'] - 1) * 100),
}

_hrow = []
_UPDAY = max(LCHG.values()) > 0        # 全线上涨时不能用"抗跌/最弱"这类措辞
for k in _HELD:
    if k == _bestk:
        tag = '<span class="tag g">%s</span> ' % ('本时段最强' if _UPDAY else ('今日最抗跌' if _POST else '本时段最抗跌'))
    elif k == _worstk:
        tag = '<span class="tag r">%s</span> ' % ('本时段最弱' if (_UPDAY or not _POST) else '今日最弱')
    else:
        tag = ''
    _hrow.append(
        '  <tr class="krow" data-k="%s"><td><b>%s</b> <span class="small">%s</span><span class="kbug">K线</span></td>'
        '<td class="n">%s</td><td class="n">%s</td>'
        '<td class="n"><b style="color:%s">%s</b> <span class="small" style="color:%s">%s</span></td>'
        '<td class="n">%.2f/%.2f</td><td>%s%s</td></tr>'
        % (k, k, NAME[k], mo(B2CLOSE[k]), mo(LCLOSE[k]),
           up(LCHG[k]), mo(LLAST[k]), up(LCHG[k]), pc(LCHG[k]),
           LHI[k], LLO[k], tag, _CMT[k]))
_hrow.append(
    '  <tr class="krow" data-k="RAM" style="background:#F5F6F8"><td><b>RAM</b> '
    '<span class="small">2倍 DRAM</span><span class="kbug">K线</span></td>'
    '<td class="n">%s</td><td class="n">%s</td>'
    '<td class="n"><b style="color:%s">%s</b> <span class="small" style="color:%s">%s</span></td>'
    '<td class="n">%s/%s</td><td><span class="tag gray">已清仓</span> 卖出价 %s；%s 收盘 %s（%s）'
    '<b>本时段 %s 比卖价低 %s</b>（%s）</td></tr>'
    % (mo(RAM_B2V[0]), mo(RAM_PV[0]),
       up(RAM_NOW_CHG), mo(RAM_NOW), up(RAM_NOW_CHG), pc(RAM_NOW_CHG),
       mo(RAM_PV[2] or RAM_NOW), mo(RAM_PV[3] or RAM_NOW),
       mo(RCL['exit']), D_PREV, mo(RAM_PV[0]), pc(RAM_PV[1]),
       mo(RAM_NOW), ('%.1f%%' % ((1 - RAM_NOW / RCL['exit']) * 100)), pc(RAM_NOW_CHG)))

# ⚠️ 表头第 2/3 列是「上一交易日收盘 / 当前时段」—— 不能用 METU_CLOSE（那是 02/20 成交日收盘，
#    只属于叙事引用）。这里按日期取值，否则会出现「10/26 收盘 = 02/20 收盘」+ 涨跌用错基准。
_M_LC = LCLOSE.get('METU') or d['METU']['price']
_M_LCP = (LLAST['METU'] / _M_LC - 1) * 100 if _M_LC else 0.0

_hrow.append(
    '  <tr class="krow" data-k="METU" style="background:#F5F6F8"><td><b>METU</b> '
    '<span class="small">2倍 META</span><span class="kbug">K线</span></td>'
    '<td class="n">%s</td><td class="n">%s</td>'
    '<td class="n"><b style="color:%s">%s</b> <span class="small" style="color:%s">%s</span></td>'
    '<td class="n">%s/%s</td><td><span class="tag gray">已清仓</span> 卖出价 %s（%s，10 股）；'
    '%s 收盘 %s（%s）<b>卖出价比收盘高 %s</b>；已实现亏损 %s（含费）</td></tr>'
    % (mo(B2CLOSE['METU']), mo(_M_LC),
       up(_M_LCP), mo(LLAST['METU']), up(_M_LCP), pc(_M_LCP),
       mo(d['METU'].get('hi') or METU_CLOSE), mo(d['METU'].get('lo') or METU_CLOSE),
       mo(METU_EXIT), METU_EXIT_DATE, METU_EXIT_DATE, mo(METU_CLOSE), pc(METU_CLOSE_PCT),
       ('%.1f%%' % ((METU_EXIT / METU_CLOSE - 1) * 100)),
       mo(abs((COST['METU'] - METU_EXIT) * 6 + 1.88))))

HOLD_TABLE = ('<h3 style="font-size:15px;margin:22px 0 6px">三个在仓 + 两个清仓记录 · '
              '%s %s · 逐日对照</h3>\n<table>\n'
              '  <tr><th>标的</th><th class="n">%s 收盘</th><th class="n">%s 收盘</th>'
              '<th class="n">%s %s</th><th class="n">%s 高/低</th><th>一句话</th></tr>\n%s\n</table>'
              % (D_CUR, PHASE, D_B2, D_PREV, D_CUR, PHASE,
                 (D_CUR if _POST else D_PREV), chr(10).join(_hrow)))

USD_C = ('%.4f' % USDC) if USDC else '—'
USD_H = ('%.4f' % USDH) if USDH else '—'

# ---------------- 数据校验块（verify.py 产出，缺失自动降级）----------------
_VFY = _jload('verify_report.json', {})


def verify_block():
    """把 verify.py 的交叉校验结果渲染成一个可折叠区块。

    单源数据无法自证对错 —— 这个区块存在的意义是：
    把「宏观数字到底准不准」从人工核对变成每日自动比对。
    """
    items = _VFY.get('items') or []
    if not items:
        return ''
    sm = _VFY.get('summary') or {}
    n_ok, n_warn, n_skip = sm.get('ok', 0), sm.get('warn', 0), sm.get('skip', 0)

    def nf(x):
        return ('%.4f' % x) if isinstance(x, float) else (('—' if x is None else str(x)))

    rows = []
    for it in items:
        if it.get('verdict') == 'skip' and str(it.get('key', '')).startswith('_'):
            continue
        tag = {'ok': '<span class="tag g">一致</span>',
               'warn': '<span class="tag r">不一致</span>',
               'skip': '<span class="tag gray">跳过</span>'}.get(it.get('verdict'), '')
        rows.append(
            '  <tr><td><b>%s</b></td><td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td>%s %s</td></tr>'
            % (it.get('label', ''), it.get('day', '') or '—', nf(it.get('a')), nf(it.get('b')),
               it.get('diff_txt', '') or '—', it.get('src_b', '') or '—', tag,
               '' if it.get('verdict') == 'ok' else it.get('note', '')))

    head = ('<div class="ok"><b>%s 本期自动校验：%d 项一致、%d 项不一致、%d 项跳过</b>'
            '<span class="small">（校验时间 %s）</span><br>'
            '<span class="small">每个数字都用<b>独立权威源</b>复核过，不依赖人工核对。'
            '核对对象是<b>「上一交易日收盘」</b>（基准日 %s）：主源的前值若与官方源吻合，'
            '说明这条 feed 可信，同一 feed 的实时值也可信。</span></div>'
            % ('✅' if not n_warn else '⚠️', n_ok, n_warn, n_skip, _VFY.get('as_of', '—'),
               _VFY.get('ref_date', '—')))

    warn = ''
    if n_warn:
        warn = ('<div class="warn"><b>⚠️ 有 %d 项不一致，发布前需人工复核。</b>'
                '常见原因是比对日的源口径差异（如财政部 par yield 与在券基准差 1–2bp），'
                '或某一源当日尚未更新。</div>' % n_warn)

    return ('<details class="fold"><summary>数据校验 · 每个数字的独立复核'
            '<span class="sm">%d 项 · CBOE ／ 美国财政部 ／ FRED 四源交叉，按同一交易日对齐</span>'
            '</summary><div class="fin">%s%s'
            '<table style="margin:8px 0">'
            '<tr><th>指标</th><th class="n">比对日</th><th class="n">主源</th>'
            '<th class="n">校验源</th><th class="n">差值</th><th class="n">校验来源</th><th>判定</th></tr>'
            '%s</table>'
            '<p class="cap"><b>校验怎么做的：</b>把主源的<b>「上一交易日收盘」</b>与独立权威源在'
            '<b>同一交易日</b>的官方值逐一比对 —— 前值吻合即说明这条 feed 可信，'
            '同一 feed 的实时值也可信。<b>各源发布节奏不同（FRED 偶滞后 2 天、财政部滞后 1 天），'
            '所以必须先定基准日再回各源查那一天；查不到就标「跳过」，不拿不同日期的数字硬比。</b><br>'
            '<b>四个校验源：</b>① <b>CBOE 官方收盘</b>（当日即有效，最强）｜'
            '② <b>美国财政部官方日度收益率曲线</b>｜③ <b>FRED</b>（VIXCLS / SP500）｜'
            '④ <b>CBOE 历史行情</b>（持仓兜底）。<br>'
            '<b>容差：</b>收益率 ±2bp（不同源口径差异）｜VIX ±0.06｜指数 ±0.06%%｜报价 ±0.4%%。'
            '脚本 <code>verify.py</code>，在生成战报前自动运行。</p>'
            '</div></details>\n' % (len(rows), head, warn, chr(10).join(rows)))


BT_BLOCK_PLACEHOLDER_NOT_USED = None

# ---------------- 00 节文案：相位自适应 ----------------
# ⚠️ 旧单 $87.59 与现价的关系随行情变化，措辞必须跟方向走：
#    2026-11-06 SPYM 跌到 87.17，挂单价已高于现价 —— 若单还挂着就已经成交了。
if LLAST.get('SPYM'):
    _pg = (87.59 / LLAST['SPYM'] - 1) * 100
    _ORDWARN = ('<b>挂单价已高于现价 %.2f%%</b> —— 单子若还在，今天开盘就该成交了' % _pg
                if _pg > 0 else
                '距成交还需再跌 <b>%.2f%%</b>' % abs(_pg))
else:
    _ORDWARN = '现价缺失'
# 【入金日优先】2026-11-17 入金 $500.00 —— 净值跳升里绝大部分是本金，不是收益。
#   不加这个分支，首屏会显示「账户 +23.83%」，把入金当成行情，误导性极强。
# ⚠️ DEPOSIT 只表示「**当日**入金」，来自 portfolio.json 的 deposit_today。
#    写错会让首屏把入金说成收益（或把收益说成入金），这是最容易误导人的一处。
DEPOSIT = float(_PF.get('deposit_today', 0) or 0)
_NAVD = NAV - NAV_PREV
_NAVP = (NAV / NAV_PREV - 1) * 100
if DEPOSIT:
    _MKT = _NAVD - DEPOSIT                      # 剔除入金后的市价变动
    _MKT_PCT = (sum(MV.values()) / (NAV_PREV - CASH_PREV) - 1) * 100
    _T00H = ('入金 %s 到账 —— 现金 %.1f%% → 可部署 %s'
             % (mo(DEPOSIT), CASH / NAV * 100, mo(max(0, CASH - NAV * T_CASH_F))))
    _T00S = ('<b>净值今天 %+.2f，但这不是收益：其中 %s 是入金，市价变动只有 %+.2f。</b>'
             '<b>别把 %+.2f%% 当成行情 —— 持仓本身的加权涨跌是 %+.2f%%。</b>'
             '三条腿分别 <b>SPYM %s</b>、<b>QQQM %s</b>、<b>VXUS %s</b>。'
             '<b>现金从 %.1f%% 升到 %.1f%%，目标 %d%% —— 这就是要部署的部分。</b>'
             % (_NAVD, mo(DEPOSIT), _MKT, _NAVP, _MKT_PCT,
                pc(LCHG['SPYM']), pc(LCHG['QQQM']), pc(LCHG['VXUS']),
                CASH_PREV / NAV_PREV * 100, CASH / NAV * 100, int(T_CASH)))
    _OKN = '其中 %s 是入金、%s 是市价变动' % (mo(DEPOSIT), mo(_MKT))
    _OKN2 = ('今天净值 %+.2f 里 %s 是入金、%s 是三个持仓的市价变动'
             % (_NAVD, mo(DEPOSIT), mo(_MKT)))
elif INTRADAY or _POST:
    # 费半统一走 ixp()（= sector_data 的最新价），与 05 节保持同一时点
    _T00H = ('账户 %s %+.2f%%，费半 %s' % (PHASE, _NAVP, ixp('SOXX')))
    if TODAY_TRADE:
        _FLOAT2 = 0.2915 * (LLAST['QQQM'] - 271.0) + 5 * (LLAST['SPYM'] - 86.0)
        _T00S = ('<b>今天两笔全部成交：%s，手续费 %s。</b>净值 %s 里，<b>%s 是刚付的手续费</b>，'
                 '其余 %s 是市价变动。两笔成交价与现价几乎一致（合计浮动 %s）—— '
                 '<b>没有滑点，因为买的是宽基、用的是限价单</b>。'
                 '三条腿分别 <b>SPYM %s</b>、<b>QQQM %s</b>、<b>VXUS %s</b>，加权后账户 <b>%+.2f%%</b>。'
                 % (TODAY_TRADE, mo(TODAY_FEE), mo(_NAVD), mo(TODAY_FEE), mo(_NAVD + TODAY_FEE),
                    mo(_FLOAT2), pc(LCHG['SPYM']), pc(LCHG['QQQM']), pc(LCHG['VXUS']), _NAVP))
        _OKN = '今天这 %s 里 %s 是手续费、其余是市价变动' % (mo(_NAVD), mo(TODAY_FEE))
        _OKN2 = _OKN
    else:
        _T00S = ('<b>今天没有成交，这 %s 全部是市价变动。</b>三条腿分别 <b>SPYM %s</b>、<b>QQQM %s</b>、<b>VXUS %s</b>，'
                 '加权后账户 <b>%+.2f%%</b> —— %d%% 标普打底的结果，就是指数怎么走、账户就怎么走。'
                 % (mo(_NAVD), pc(LCHG['SPYM']), pc(LCHG['QQQM']), pc(LCHG['VXUS']), _NAVP,
                    round(MV['SPYM'] / NAV * 100)))
        _OKN = '今天没有成交，全部是市价变动'
        _OKN2 = '今天这 %s 全部来自三个持仓的市价变动' % mo(_NAVD)
else:
    _T00H = ('账户 %s 只跌 %.2f%%，费半却跌了 %.2f%%'
             % (D_PREV, abs(_NAVP), abs(lq('SOXX').get('chg_pct') or 0)))
    _T00S = ('<b>配置 %d/%d/%d/%d 落地后的第一个完整交易日。</b>三条腿分别 <b>SPYM %s</b>、<b>QQQM %s</b>、<b>VXUS %s</b>，'
             '加权后账户 <b>%+.2f%%</b>。拆开看：<b>%s 是买入执行成本</b>（%d 股买在 %s、收盘 %s，'
             '浮亏 %s + 手续费 %s），剩下 <b>%s</b> 才是纯市价变动 —— '
             '<b>执行成本占净值 %.2f%%，一次性，不会再发生</b>。'
             % (T_SPYM, T_QQQM, T_VXUS, T_CASH, cl('SPYM'), cl('QQQM'), cl('VXUS'), _NAVP,
                mo(RB_EXEC), RB['qty'], mo(RB['px']), mo(RB['close']),
                mo(RB['qty'] * (RB['px'] - RB['close'])), mo(RB['fee']),
                mo(abs(_NAVD) - RB_EXEC), RB_EXEC / NAV * 100))
    _OKN = ('其中 %s 是 %s 买入的执行成本（浮亏 %s + 手续费 %s），一次性'
            % (mo(RB_EXEC), RB['date'], mo(RB['qty'] * (RB['px'] - RB['close'])), mo(RB['fee'])))
    _OKN2 = ('今天净值少了 %s，其中 %s 是 %s 的买入执行成本、其余是三个持仓的市价变动'
             % (mo(abs(_NAVD)), mo(RB_EXEC), RB['date']))

HTML = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股每日战报 · 2026-10-22 盘中（METU 已止损清仓 @$27.00；账户杠杆归零，现金 30%）</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
         max-width: 1020px; margin: 0 auto; padding: 32px 20px 60px; background: #fff; color: #1f2328; line-height: 1.75; }}
  h1 {{ font-size: 26px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.3px; }}
  .meta {{ font-size: 12.5px; color: #8b929c; margin: 0 0 22px; }}
  h2 {{ font-size: 19px; font-weight: 600; margin: 38px 0 4px; display: flex; align-items: baseline; gap: 9px; }}
  h2 .no {{ font-size: 13px; font-weight: 500; color: #fff; background: #185FA5; border-radius: 6px;
           padding: 2px 9px; letter-spacing: 0.5px; }}
  .h2sub {{ font-size: 13px; color: #6b7280; margin: 0 0 14px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); gap: 12px; margin: 16px 0 8px; }}
  .card {{ background: #f6f8fa; border-radius: 10px; padding: 14px 16px; }}
  .card .k {{ font-size: 12px; color: #6b7280; }}
  .card .v {{ font-size: 22px; font-weight: 600; margin-top: 3px; font-variant-numeric: tabular-nums; }}
  .card .d {{ font-size: 11.5px; color: #8b929c; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }}
  th {{ background: #f2f5f8; text-align: left; padding: 9px 9px; font-weight: 600; border-bottom: 1px solid #dde2e7; white-space: nowrap; }}
  td {{ padding: 8px 9px; border-bottom: 1px solid #edf0f3; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .say {{ background: #f6f8fa; border-left: 3px solid #185FA5; border-radius: 0 8px 8px 0; padding: 12px 16px; margin: 14px 0; font-size: 14px; }}
  .warn {{ background: #FDF3F2; border-left: 3px solid #C0392B; border-radius: 0 8px 8px 0; padding: 12px 16px; margin: 14px 0; font-size: 13.5px; color: #7d2b23; }}
  .ok {{ background: #EFF8F1; border-left: 3px solid #1D9E75; border-radius: 0 8px 8px 0; padding: 12px 16px; margin: 14px 0; font-size: 13.5px; color: #1a5c45; }}
  ul {{ margin: 8px 0; padding-left: 21px; font-size: 14px; }}
  li {{ margin-bottom: 7px; }}
  ol {{ margin: 8px 0; padding-left: 21px; font-size: 14px; }}
  .tag {{ display: inline-block; font-size: 11.5px; padding: 1px 8px; border-radius: 10px; background: #E6F1FB; color: #185FA5; white-space: nowrap; }}
  .tag.o {{ background: #FAEEDA; color: #854F0B; }}
  .tag.r {{ background: #FCEBEB; color: #A32D2D; }}
  .tag.g {{ background: #EAF3DE; color: #3B6D11; }}
  .tag.gray {{ background: #F1EFE8; color: #5F5E5A; }}
  .fig {{ background: #fcfdfd; border: 1px solid #edf0f3; border-radius: 12px; padding: 18px 16px 12px; margin: 14px 0; }}
  .fig .cap {{ font-size: 12.5px; color: #6b7280; text-align: center; margin-top: 6px; }}
  .lg {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: #4a5058; justify-content: center; margin-top: 8px; }}
  .lg span {{ display: flex; align-items: center; gap: 5px; }}
  .sw {{ width: 11px; height: 11px; border-radius: 3px; display: inline-block; }}
  .foot {{ margin-top: 44px; padding-top: 14px; border-top: 1px solid #edf0f3; font-size: 11.5px; color: #98a0aa; }}
  code {{ background: #f2f5f8; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }}
  .small {{ font-size: 11px; color: #8b929c; }}
  .cap {{ font-size: 12px; color: #6b7280; margin: 8px 0 0; }}
</style>
</head>
<body>

<h1>美股每日战报</h1>
<p class="meta">{HERO_DATE}<b> {PHASE}</b> · <b>配置 {T_SPYM:.0f}/{T_QQQM:.0f}/{T_VXUS:.0f}/{T_CASH:.0f}</b>：当前 SPYM {MV["SPYM"]/NAV*100:.1f}%、现金 {CASH/NAV*100:.1f}%、杠杆 0｜{_idx_tag()}：纳指 <b>{ixp('QQQ')}</b>、费半 <b>{ixp('SOXX')}</b>（SOXX 代理）、标普 <b>{ixp('SPY')}</b>｜ 行情时点 <b>{lq("SPYM").get("as_of") or LVASOF}</b>（{NAV_LABEL}）｜ 生成时间：{time.strftime('%m/%d %H:%M')} 北京 ｜ 配套文档：<a href="https://www.workbuddy.cn/space/d/2q7vxWtaSKcFeIomp1n8hs">投资总纲</a> · <a href="https://www.workbuddy.cn/space/d/kaPK8ddyDnN9Wp35GPpmcK">开新仓方案</a></p>

<div class="warn" style="font-size:14px">
<b style="font-size:15px">🔴 {METU_EXIT_DATE} 执行了这套系统的第一次止损：METU 10 股 @ {mo(METU_EXIT)} —— 而那天费半 -6.7%、纳指创 6 月以来最大单日跌幅。</b><br>
<b>① 卖价 {mo(METU_EXIT)} 高于 {METU_EXIT_DATE} 收盘 {mo(METU_CLOSE)}</b>，多保住 <b>{mo((METU_EXIT-METU_CLOSE)*6)}</b>；
但触发线是 $25.00，<b>从 $25 到 $27.00 仍多付了 {mo((25-METU_EXIT)*6)}</b> —— 滑点是止损的固定成本，不是失误。<br>
<b>② 已实现亏损合计 {mo(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))}，占净值 {abs(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))/NAV*100:.2f}%</b>
（RAM {mo(abs(RCL_NET))} + METU {mo(abs((COST['METU']-METU_EXIT)*6+1.88))}）。<b>一个 {mo(NAV)} 的账户犯两个错、亏 {abs(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))/NAV*100:.1f}%，仍然活着。</b><br>
<b>③ 02/20 那天是轮动，不是普跌</b>：钱从 AI 硬件流向<b>必需消费、公用事业、医疗与长债</b>，
费半与软件/成长是唯一在被系统性抛售的一块。<b>所以"METU 卖错了"的叙事不成立 —— 它卖在一条正在被重定价的线上。</b><br>
<span class="small"><b>📌 今天要看的三件事：</b>
① <b>存储链{PHASE}整片反弹</b>：MU {pc(UCHG['MU'])}、SNDK {pc(UCHG['SNDK'])}、WDC {pc(UCHG['WDC'])}、STX {pc(UCHG['STX'])}，篮子均值 <b>{pc(B_AVG)}</b>；
已清仓的 RAM 同步 {pc(RAM_NOW_CHG)} —— <b>你不在车上，这是清仓的代价，也是 2 倍杠杆被关掉之后的常态</b>；
② <b>SPYM 缺口 {T_SPYM-MV['SPYM']/NAV*100:.1f}pp 不追</b>：现金只剩 {mo(CASH)}，再买 1 股就掉到 {(CASH-LLAST['SPYM'])/NAV*100:.1f}%、破 {T_CASH:.0f}% 下限 —— <b>缺口靠入金稀释解决，不为这 {T_SPYM-MV['SPYM']/NAV*100:.1f}pp 单独下单</b>；
③ <b>挂单已清空 —— 账户第一次没有任何在途委托。</b>可自由动用从 $0 变成 <b>{mo(max(0, CASH-NAV*T_CASH_F))}</b>，
但<b>仍买不了 1 股 SPYM（{mo(LLAST['SPYM'])}）</b>；下一张单等 11 月入金到账再挂。</span>
</div>

<div class="ok" style="font-size:14px">
<b style="font-size:15px">🟢 净值 {mo(NAV)}（{NAV_CMP} {NAV-NAV_PREV:+.2f} / {(NAV/NAV_PREV-1)*100:+.2f}%）—— {_OKN}。</b><br>
<b>SPYM {MV['SPYM']/NAV*100:.2f}%</b>（目标 {T_SPYM:.0f}）｜<b>QQQM {MV['QQQM']/NAV*100:.2f}%</b>（{T_QQQM:.0f}）｜<b>VXUS {MV['VXUS']/NAV*100:.2f}%</b>（{T_VXUS:.0f}）｜现金 <b>{CASH/NAV*100:.2f}%</b>（{mo(CASH)}）｜<b>杠杆 0.00%</b>。<br>
<span class="small"><b>核心三仓 {CORE/NAV*100:.2f}%（目标 {T_SPYM+T_QQQM+T_VXUS:.0f}%）</b> —— 差的是 <b>SPYM {T_SPYM-MV['SPYM']/NAV*100:.1f}pp 的缺口</b>，
既不是"超配"，也不是"该开新仓"。<b>现金 {CASH/NAV*100:.1f}% 已经贴着下限</b>（{T_CASH:.0f}% = {mo(NAV*T_CASH_F)}），
只多出 <b>{mo(CASH-NAV*T_CASH_F)}</b> —— <b>买不了 1 股 SPYM（{mo(LLAST['SPYM'])}），等于子弹已经打光</b>，剩下的缺口只能等 11 月入金。</span>
</div>

<div class="say"><b>📌 {RB['date']} 再平衡已完成：买入 SPYM {RB['qty']} 股 @ {mo(RB['px'])}。</b><br>
<b>① 成交明细</b>：{RB['qty']} 股 × {mo(RB['px'])} = <b>{mo(RB['qty']*RB['px'])}</b> + 平台费 {mo(RB['fee'])} = <b>{mo(RB_FILL)}</b>；
现金 <b>{mo(RB['cash_from'])} → {mo(RB['cash_to'])}</b>（30.4% → {CASH/NAV*100:.1f}%），SPYM <b>{RB['shares_from']} 股 → {RB['shares_to']} 股</b>（44.7% → {MV['SPYM']/NAV*100:.1f}%）。<br>
<b>② 成交价 = {RB['date']} 开盘价</b>：{mo(RB['px'])} 是当天开盘，<b>相对开盘零滑点</b>；但收盘回落到 {mo(RB['close'])}，所以现在浮亏 <b>{mo(RB['qty']*(RB['px']-RB['close']))}</b> —— <b>这是执行成本，不是判断错</b>。<br>
<b>③ QQQM / VXUS 不动</b>：QQQM {MV['QQQM']/NAV*100:.2f}%（目标 {T_QQQM:.0f}%，在容差内）；VXUS {MV['VXUS']/NAV*100:.2f}%（目标 {T_VXUS:.0f}%，<b>不卖、靠新钱不补慢慢漂</b>）。<br>
<span class="small"><b>④ 距 {T_SPYM:.0f}% 还差 {T_SPYM-MV['SPYM']/NAV*100:.1f}pp —— 这是整股约束，不是判断问题：</b>
1 股 ≈ 净值 {LLAST['SPYM']/NAV*100:.1f}%，从 {POS['SPYM']} 股到 {POS['SPYM']+1} 股就到 {(POS['SPYM']+1)*LLAST['SPYM']/NAV*100:.1f}% ——
<b>但现金只剩 {mo(CASH)}，买完立刻掉到 {(CASH-LLAST['SPYM'])/NAV*100:.1f}%，破 {T_CASH:.0f}% 下限</b>。
所以下一笔不是"择时"，是"等入金"：11 月新钱进来先补 SPYM。
<span class="bad">（$87.59 那张单已在 11/09 清理 —— 挂了 20 天、从未成交，最后一次检查时已经高于现价。）</span></span>
</div>

<h2><span class="no">00</span>{_T00H}</h2>
<p class="h2sub">{_T00S}
<b>表一是持仓的 {D_B2} / {D_PREV} / {D_CUR} 三日对照</b>。</p>

<div class="cards">
  <div class="card"><div class="k">净值（{NAV_LABEL}）</div><div class="v">{mo(NAV)}</div><div class="d">较 {D_REF} {NAV-NAV_PREV:+.2f}（{(NAV/NAV_PREV-1)*100:+.2f}%）</div></div>
  <div class="card"><div class="k">杠杆敞口</div><div class="v" style="color:#0F7B4F">{LEV/NAV*100:.2f}%</div><div class="d">上限 15.5% · 达标</div><div class="bar g"><i style="width:{min(LEV/NAV/0.155*100,100):.0f}%"></i><span class="tgt" style="left:100%"></span></div></div>
  <div class="card"><div class="k">核心三仓</div><div class="v" style="color:#854F0B">{CORE/NAV*100:.2f}%</div><div class="d">目标 {T_SPYM+T_QQQM+T_VXUS:.0f}%（{T_SPYM:.0f}/{T_QQQM:.0f}/{T_VXUS:.0f}）</div><div class="bar a"><i style="width:100%"></i><span class="tgt" style="left:{55/(CORE/NAV*100)*100:.0f}%"></span></div></div>
  <div class="card" style="background:#EFF8F1"><div class="k">现金</div><div class="v" style="color:#0F7B4F">{CASH/NAV*100:.2f}%</div><div class="d">{mo(CASH)} · 下限 {T_CASH:.0f}% / 实操 {T_CASH:.0f}–{T_CASH_HI:.0f}%</div><div class="bar g"><i style="width:{min(CASH/NAV/0.21*100,100):.0f}%"></i><span class="tgt" style="left:100%"></span></div></div>
  <div class="card" style="background:#FDF3F2"><div class="k">VIX（{D_CUR} {PHASE}）</div><div class="v" style="color:#C0392B">{_cn(".VIX", 2)}</div><div class="d">{_cnc(".VIX", 2)} · {D_PREV} 收 {_cnprev(".VIX", 2)} → 当前 {_cn(".VIX", 2)}</div></div>
</div>

{HOLD_TABLE}

<p class="khint">💡 <b>点击上面任意一行</b>，即可展开该标的的 K 线图（日 / 周 / 月 / 年四个周期可切换，鼠标悬停看每根数值）。</p>

<div class="warn"><b>把这张表和账户净值放在一起看，得到的是一个"闭环"：<b>同一个账户里，两笔已经走完的亏损，金额都小得可以承受。</b></b><br>
<b>▪ 已清仓的 RAM：{PHASE} {mo(RAM_NOW)}（{pc(RAM_NOW_CHG)}）</b> —— 你 $18.00 的卖价仍高出 <b>{abs(18.00/RAM_NOW-1)*100:.1f}%</b>。
<small>它这一周走完两轮来回（$7.94 → $8.52 → 现在 {mo(RAM_NOW)}），<b>三个方向的情绪你一次都没跟</b>。</small><br>
<b>▪ 已清仓的 METU：10 股 @ {mo(METU_EXIT)} 卖出，成本 $30.00</b> —— 已实现亏损 <b>{mo(abs((COST['METU']-METU_EXIT)*6+1.88))}</b>（含费）。
<small><b>这是这套规则运行以来第一次真正触发止损。</b>它不是"判断错了"，是"判断错了但代价被提前量好了"。
对照一下：若不卖，按 02/20 收盘 {mo(METU_CLOSE)} 算，账面还要再少 <b>{mo((METU_EXIT-METU_CLOSE)*6)}</b> —— <b>但这不是理由，只是巧合。</b></small><br>
<b>两笔已实现亏损合计 {mo(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))}，占净值 {abs(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))/NAV*100:.2f}%。</b>
<b>一个 {mo(NAV)} 的账户，犯两个错、亏掉 5%，仍然活着 —— 这就是仓位上限和止损在做的事。</b><br>
<span class="small"><b>📌 反过来看 02/20 最惨的那批：</b>费半 -6.72%、半导体（SMH 代理）-5.99%、存储链 +0.54%、META -6.98%（{D_PREV} 收盘）。
<b>你一个都没持有</b>（RAM 已清、MU 槽位空置）。<b>你付的学费，是为"参与过"付的，不是为"看错方向"付的。</b></span></div>

<h3 style="font-size:15px;margin:24px 0 6px">RAM 的对手盘：底层存储链 {D_PREV} 收盘 / {D_CUR} {PHASE}</h3>
<table>
  <tr><th>底层标的</th><th class="n">{D_B2} 收盘</th><th class="n">{D_PREV} 收盘</th><th class="n">{D_CUR} {PHASE}</th><th class="n">最新低</th></tr>
  <tr class="krow" data-k="MU"><td><b>美光 MU</b> <span class="small">DRAM</span><span class="kbug">K线</span></td><td class="n">{mo(UB2CLOSE['MU'])}</td><td class="n">{mo(UCLOSE['MU'])}</td><td class="n"><b style="color:{up(UCHG['MU'])}">{mo(ULAST['MU'])}</b> <span class="small" style="color:{up(UCHG['MU'])}">{pc(UCHG['MU'])}</span></td><td class="n">{mo(ULO['MU'])}</td></tr>
  <tr class="krow" data-k="SNDK"><td><b>闪迪 SNDK</b> <span class="small">NAND</span><span class="kbug">K线</span></td><td class="n">{mo(UB2CLOSE['SNDK'])}</td><td class="n">{mo(UCLOSE['SNDK'])}</td><td class="n"><b style="color:{up(UCHG['SNDK'])}">{mo(ULAST['SNDK'])}</b> <span class="small" style="color:{up(UCHG['SNDK'])}">{pc(UCHG['SNDK'])}</span></td><td class="n">{mo(ULO['SNDK'])}</td></tr>
  <tr class="krow" data-k="WDC"><td><b>西部数据 WDC</b> <span class="small">HDD+闪存</span><span class="kbug">K线</span></td><td class="n">{mo(UB2CLOSE['WDC'])}</td><td class="n">{mo(UCLOSE['WDC'])}</td><td class="n"><b style="color:{up(UCHG['WDC'])}">{mo(ULAST['WDC'])}</b> <span class="small" style="color:{up(UCHG['WDC'])}">{pc(UCHG['WDC'])}</span></td><td class="n">{mo(ULO['WDC'])}</td></tr>
  <tr class="krow" data-k="STX"><td><b>希捷 STX</b> <span class="small">HDD</span><span class="kbug">K线</span></td><td class="n">{mo(UB2CLOSE['STX'])}</td><td class="n">{mo(UCLOSE['STX'])}</td><td class="n"><b style="color:{up(UCHG['STX'])}">{mo(ULAST['STX'])}</b> <span class="small" style="color:{up(UCHG['STX'])}">{pc(UCHG['STX'])}</span></td><td class="n">{mo(ULO['STX'])}</td></tr>
  <tr style="background:#f6f8fa"><td><b>篮子均值（4 只）</b> <span class="small">{D_CUR} {PHASE}</span></td><td class="n">—</td><td class="n">—</td><td class="n" style="color:{up(B_AVG)}"><b>{pc(B_AVG)}</b></td><td class="n">—</td></tr>
  <tr style="background:#F5F6F8"><td><b>RAM（2 倍）</b> <span class="small">已清仓</span></td><td class="n">{mo(RAM_H['close'])}</td><td class="n">{mo(RAM_13['close'])}</td><td class="n"><b style="color:{up(RAM_NOW_CHG)}">{mo(RAM_NOW)}</b> <span class="small" style="color:{up(RAM_NOW_CHG)}">{pc(RAM_NOW_CHG)}</span></td><td class="n">—</td></tr>
</table>
<p class="cap">{D_CUR} {PHASE}放大倍数实测 <b>{AMP_TXT} 倍</b>（篮子均值 {pc(B_AVG)} → RAM {pc(RAM_NOW_CHG)}）—— <b>倍数不固定</b>：篮子内权重大的标的跌得越多，2 倍产品的实际跌幅就越大于 2 倍。RAM 已于 01/15 清仓，这张表留着它只为对照「同期篮子跌多少、2 倍产品跌多少」。</p>

<p class="cap" style="margin-top:14px">📌 <b>留档复盘（01/15 清仓那天）</b>：成交价 $18.00 低于前一日（01/14）收盘 $19.20，差 <b>{mo(4*RAM_H['close']-RCL_RECV)}</b>（净值 {(4*RAM_H['close']-RCL_RECV)/NAV*100:.2f}%）。<b>这个数字小到不值得自责，也小到没有理由再拖一天。</b>规则的价值在于闭环：条件满足 → 执行 → 不再讨论。</p>

<h2><span class="no">01</span>账户快照（{NAV_LABEL}）</h2>
<p class="h2sub">你的股数 × 最新价（<b>{LVASOF}</b>）｜ 净值 <b>{mo(NAV)}</b>（{PHASE}口径）｜ 市值按最新价重算，"{D_B2} / {D_PREV} 收盘"两列用于对照</p>

<table>
  <tr><th>持仓</th><th class="n">股数</th><th class="n">{D_B2}收盘</th><th class="n">{D_PREV}收盘</th><th class="n">{D_CUR} {PHASE}</th><th class="n">市值</th><th class="n">占比</th><th class="n">目标</th><th class="n">偏差</th></tr>
{chr(10).join(rows1)}
</table>

<div class="say"><b>这张表要读的不是"配置好不好"，而是"三个宽基的加权，怎么决定账户每天走多少"。</b><br>
<b>SPYM {MV['SPYM']/NAV*100:.2f}%</b>（目标 {T_SPYM:.0f}%，偏离 {MV['SPYM']/NAV*100-T_SPYM:+.1f}pp）｜QQQM {MV['QQQM']/NAV*100:.2f}%（{T_QQQM:.0f}）｜VXUS {MV['VXUS']/NAV*100:.2f}%（{T_VXUS:.0f}，只降不补）｜<b>METU 0%（{METU_EXIT_DATE} 清仓）</b>｜现金 {CASH/NAV*100:.2f}%。<br>
<b>SPYM {MV['SPYM']/NAV*100:.1f}%，距 {T_SPYM:.0f}% 目标差 {T_SPYM-MV['SPYM']/NAV*100:.1f}pp —— 这 {T_SPYM-MV['SPYM']/NAV*100:.1f}pp 用现有现金补不了：</b>
买 1 股（{mo(LLAST['SPYM'])}）现金就从 {CASH/NAV*100:.1f}% 掉到 {(CASH-LLAST['SPYM'])/NAV*100:.1f}%，<b>直接破 {T_CASH:.0f}% 下限</b>。
<b>所以缺口只能靠"入金稀释 + 逐笔买整股"解决</b>：1 股 ≈ 净值 {LLAST['SPYM']/NAV*100:.1f}%，每笔新钱先把 SPYM 补回来，再谈别的腿。<br>
<span class="small"><b>⚠️ {D_CUR} {PHASE}的算术很干净：</b>账户 <b>{(NAV/NAV_PREV-1)*100:+.2f}%</b>，而费半 {ixp('SOXX')}、纳指 {ixp('QQQ')}、标普 {ixp('SPY')}、道指 {ixp('DIA')}。
<b>{MV['SPYM']/NAV*100:.0f}% 标普 + {MV['QQQM']/NAV*100:.0f}% 纳指 + {MV['VXUS']/NAV*100:.0f}% 非美的加权结果，落在标普和纳指之间 —— 这就是分散的数学意义。</b><br>
<b>止损那 {mo(abs((COST['METU']-METU_EXIT)*6+1.88))} 已经落在 {METU_EXIT_DATE} 的净值里</b>（当天 -2.36%），
<b>{_OKN2}，与已实现亏损无关</b> —— 别再把它当成"还在亏"。</span></div>

<h2><span class="no">02</span>持仓行情与技术信号</h2>
<p class="h2sub">均线用近 20 个交易日收盘价计算（截至 {D_DAY}）；价格列为 {D_CUR} {PHASE}最新价</p>

<table>
  <tr><th>标的</th><th class="n">{D_CUR} {PHASE}</th><th class="n">{(D_CUR if _POST else D_PREV)} 高/低</th><th class="n">MA5</th><th class="n">MA10</th><th class="n">MA20</th><th class="n">距 20 日前</th><th class="n">RSI14</th><th class="n">支撑 / 压力（20日）</th><th>技术判断</th></tr>
{chr(10).join(rows2)}
</table>

<div class="say"><b>📌 技术面三件事（{D_CUR} {PHASE}价 vs 均线，均线截至 {D_DAY} 收盘）：</b><br>
<b>① SPYM</b> —— {mo(LLAST['SPYM'])}，MA20 {d['SPYM']['ma20']:.2f}，<b>{_mast('SPYM')}</b>。
<b>{MV['SPYM']/NAV*100:.1f}% 的压舱石本时段 {pc(LCHG['SPYM'])}，是三个持仓里波动最小的一档</b> —— 这正是它占 {MV['SPYM']/NAV*100:.0f}% 的原因：<b>它不需要你判断对，只需要你不卖。</b><br>
<b>② QQQM</b> —— {mo(LLAST['QQQM'])}，MA20 {d['QQQM']['ma20']:.2f}，距 MA20 {(LLAST['QQQM']/d['QQQM']['ma20']-1)*100:+.1f}%。<b>本时段 {pc(LCHG['QQQM'])}</b>，跟着纳指走，没有额外风险；<b>挂单</b>口径见 02 节。<br>
<b>③ VXUS</b> —— {mo(LLAST['VXUS'])}，<b>{_mast('VXUS')}</b>，本时段 {pc(LCHG['VXUS'])}。<b>占 {MV['VXUS']/NAV*100:.1f}%，比新目标 {T_VXUS:.0f}% 高 {MV['VXUS']/NAV*100-T_VXUS:+.1f}pp</b> —— 02/20 那笔 $83.00 买的是**旧**目标 10%，按 70/15/5 就是超配。<b>不卖、新钱不补，靠自然漂移回到 {T_VXUS:.0f}%</b>，这就是它唯一需要的处理。<br>
<span class="small"><b>METU 已在 02/20 清仓，不再占槽位</b> —— 这是它作为持仓的最后一份技术读数：卖出价 {mo(METU_EXIT)}，02/20 收盘 {mo(METU_CLOSE)}（{pc(METU_CLOSE_PCT)}），
<b>收盘低于 MA20 {(METU_CLOSE/d['METU']['ma20']-1)*100:.1f}%</b>、低于止损 {(25/METU_CLOSE-1)*100:+.1f}%。<b>均线在止损位失效之后就没有信息量了 —— 看止损线，不看均线。</b></span></div>

<h2><span class="no">03</span>近 21 个交易日走势（→ {D_DAY} 收盘）</h2>
<div class="fig">
{chart}
<div class="lg">
  <span><i class="sw" style="background:#185FA5"></i>SPYM</span>
  <span><i class="sw" style="background:#378ADD"></i>QQQM</span>
  <span><i class="sw" style="background:#0F6E56"></i>VXUS</span>
  <span><i class="sw" style="background:#854F0B"></i>METU</span>
</div>
<p class="cap">两个面板纵轴刻度不同（下方约为上方的 8 倍），起点统一设为区间首日收盘 = 0%。<b>图中末点是 {D_DAY} 收盘，尚未包含今日{PHASE}。</b>过去 21 个交易日里，SPYM / QQQM / VXUS 大部分时间贴着 0% 横着走，而棕色线 METU 走出了最大的波动区间 —— 先跟着 META 上冲，最近又被杠杆放大着回吐。<b>注意最后三根：METU 的反弹力度一次比一次弱，这正是 2 倍杠杆"跌时放大、涨时不还原"的形态。</b><b>这就是"核心宽基打底 + 单一主题进攻"的真实形态：账户的绝大部分波动都来自那一条棕色线，而它最高只占过净值 {LV['METU']*6/NAV_PREV*100:.1f}%。</b>
<b>它已经在 02/20 清仓（@ {mo(METU_EXIT)}），所以这条线从现在起只用来复盘 —— 不再是持仓。</b>把杠杆合计压在 15% 以内、并留一条止损线，就是为了让这条线放大时你还能睡着觉。</p>
</div>

<h2><span class="no">04</span>市场情绪仪表</h2>
<p class="h2sub">{_idx_tag()} · <b>道指 {ixp('DIA')}、罗素 {ixp('IWM')}、费半 {ixp('SOXX')}</b> —— {_idx_head()}</p>

<table>
  <tr><th>指标</th><th class="n">点位 / 价格</th><th class="n">涨跌</th><th>时点</th><th>解读</th></tr>
  <tr style="background:#EFF8F1"><td><b>道琼斯</b></td><td class="n"><b>{_cn(".DJI", 2)}</b></td>
    <td class="n" style="color:{ixvu('DIA')}"><b>{ixp('DIA')}</b></td>
    <td class="m">{_idx_tag()}</td>
    <td>道指里没有 AI 资本开支 —— 它是"轮动 vs 普跌"最好的对照组；{_rel('DIA')}</td></tr>
  <tr style="background:#EFF8F1"><td>罗素 2000</td><td class="n">{_cn(".RUT", 2)}</td>
    <td class="n" style="color:{ixvu('IWM')}">{ixp('IWM')}</td>
    <td class="m">{_idx_tag()}</td>
    <td>小盘对融资成本最敏感，是"信用端松紧"的读数；{_rel('IWM')}</td></tr>
  <tr><td>标普 500</td><td class="n">{_cn(".SPX", 2)}</td>
    <td class="n" style="color:{ixvu('SPY')}">{ixp('SPY')}</td>
    <td class="m">{_idx_tag()}</td>
    <td><b>你的 SPYM {MV['SPYM']/NAV*100:.0f}% 就吃这一档</b> —— 组合的基本盘</td></tr>
  <tr style="background:#FDF3F2"><td>纳斯达克（QQQ 代理）</td><td class="n">{_cn(".IXIC", 2)}</td>
    <td class="n" style="color:{ixvu('QQQ')}"><b>{ixp('QQQ')}</b></td>
    <td class="m">{_idx_tag()}</td>
    <td><b>QQQM 装的就是它</b>，{MV['QQQM']/NAV*100:.0f}% 的仓位直接对着这一行</td></tr>
  <tr style="background:#FDF3F2"><td><b>费城半导体</b></td><td class="n">{_cn(".SOX", 2)}</td>
    <td class="n" style="color:{ixvu('SOXX')}"><b>{ixp('SOXX')}</b></td>
    <td class="m">{_idx_tag()}</td>
    <td>费半 = AI 敞口的温度计；{_rel('SOXX')}。<b>仍比 6/22 的纪录高点低两成以上 —— 反弹不等于修复</b></td></tr>
  <tr style="background:#FDF3F2"><td><b>存储链 · {PHASE}</b></td><td class="n">—</td>
    <td class="n" style="color:{up(B_AVG)}"><b>{pc(B_AVG)}</b></td>
    <td class="m">{_stk_tag()}</td>
    <td><b>希捷 {pc(_lcp('STX'))}｜西数 {pc(_lcp('WDC'))}｜闪迪 {pc(_lcp('SNDK'))}｜美光 {pc(_lcp('MU'))}</b>
    —— 与你无关了（RAM 已清、MU 槽位空置），只作"AI 硬件链还在不在被卖"的读数</td></tr>
  <tr style="background:#FDF3F2"><td><b>Meta META</b></td><td class="n"><b>{mo(_lp('META'))}</b></td>
    <td class="n" style="color:{up(_lcp('META'))}"><b>{pc(_lcp('META'))}</b></td>
    <td class="m">{_stk_tag()}</td>
    <td><span class="tag r">财报全超 + 股价照跌</span> <b>市场在给它"资本开支执行者"的折价</b>，不是给"财报"的折价
    —— FCF 首度转负、5 年期 CDS 创纪录、未来承诺 8,250 亿</td></tr>
  <tr><td>特斯拉 TSLA</td><td class="n">{mo(_lp('TSLA'))}</td>
    <td class="n" style="color:{up(_lcp('TSLA'))}">{pc(_lcp('TSLA'))}</td>
    <td class="m">{_stk_tag()}</td>
    <td>它"砍了资本开支"（250 亿 → 约 200 亿）—— <b>市场在奖励"少花钱"，这本身是个信号</b></td></tr>
  <tr><td>英伟达 / AMD</td><td class="n">{mo(_lp('NVDA'))} / {mo(_lp('AMD'))}</td>
    <td class="n" style="color:{up(_lcp('NVDA'))}">{pc(_lcp('NVDA'))} / {pc(_lcp('AMD'))}</td>
    <td class="m">{_stk_tag()}</td>
    <td><b>越依赖"二线算力扩张"，波动越大</b> —— 这是整条 AI 链的杠杆位置</td></tr>
  <tr><td>谷歌 / 亚马逊</td><td class="n">{mo(_lp('GOOGL'))} / {mo(_lp('AMZN'))}</td>
    <td class="n" style="color:{up(_lcp('GOOGL'))}">{pc(_lcp('GOOGL'))} / {pc(_lcp('AMZN'))}</td>
    <td class="m">{_stk_tag()}</td>
    <td><b>10/30 两家财报的资本开支口径是下一个定价点</b></td></tr>
  <tr style="background:#FFF8E6"><td><b>VIX 恐慌指数</b></td><td class="n"><b>{_cn(".VIX", 2)}</b></td>
    <td class="n" style="color:#C0392B"><b>{_cnc(".VIX", 2)}</b></td>
    <td class="m">{_stk_tag()}</td>
    <td><span class="tag o">从低位抬头</span> {D_PREV} 收 <b>{_cnprev(".VIX", 2)}</b> → 当前 <b>{_cn(".VIX", 2)}</b>。
    <b>还没到 10/16 的 19.99</b>，说明这是"板块抛售"而不是"系统性避险"</td></tr>
  <tr style="background:#FFF8E6"><td><b>10 年期美债</b></td><td class="n"><b>{_cn("US10Y", 3)}%</b></td>
    <td class="n">{_cnb("US10Y")}</td>
    <td class="m">{_stk_tag()}</td>
    <td>30Y {_cn("US30Y", 3)}%（{_cnb("US30Y")}）、2Y {_cn("US2Y", 3)}%（{_cnb("US2Y")}）——
    <b>{D_PREV} 一天 -17bp 是 3 月以来最大单日降幅，资金在买债避险；今天是回吐</b></td></tr>
  <tr style="background:#FFF8E6"><td><b>原油</b></td><td class="n">WTI $99.47 / 布伦特 $113.17</td>
    <td class="n" style="color:#C0392B"><b>+2.4% / +4%</b></td>
    <td class="m">02/20 收盘</td>
    <td>布伦特 <b>5 月以来最高</b> —— 胡塞袭击沙特目标 + 美以被曝计划中期选举后再度打击伊朗，
    <b>油价成了"不能降息"的又一个理由</b></td></tr>
</table>
<p class="cap">指数涨跌一律用 <b>ETF 代理自算</b>（DIA / SPY / QQQ / IWM / SOXX，来自 <code>sector_data.json</code> 每次链路重抓）；
点位取 CNBC 官方快照。<b>「{D_CUR} {PHASE}」列就是当前时段口径 —— 盘前是上一交易日收盘涨跌，盘中是实时涨跌，不做混淆。</b></p>

<h3 style="font-size:13.5px;margin:24px 0 2px">板块热力 · 11 个 SPDR <span class="small">（条形＝当日涨跌；右列＝相对标普的超额）</span></h3>
<p class="h2sub" style="margin:2px 0 8px">基准 <b>标普 {pc(SPY_CHG)}</b>。绝对值告诉你"涨没涨"，<b>超额告诉你"钱是流进来了还是流走了"</b>。</p>
{SEC_HTML}
<p class="cap">最强 <b>{(_sec_top['cn'] + ' ' + pc(_sec_top['chg'])) if _sec_top else '—'}</b>｜最弱 <b>{(_sec_bot['cn'] + ' ' + pc(_sec_bot['chg'])) if _sec_bot else '—'}</b>。
{_SEC_NOTE}
<span class="small">⚠️ 如果能源与油价背离（油价涨、能源股跌），说明市场把油价上涨读成"成本压力"而不是"盈利改善" —— 那对消费和估值都是坏消息。</span></p>

<h3 style="font-size:13.5px;margin:22px 0 2px">市场宽度 · 三个比值 <span class="small">（当日，非 5/20 日）</span></h3>
<p class="h2sub" style="margin:2px 0 6px">指数是加权平均，会被最大的几家公司绑架。<b>下面三个差值回答"这是一次普跌，还是少数股票在拖"。</b></p>
{BR_HTML}
<p class="cap">{br_cap()}</p>

<h3 style="font-size:13.5px;margin:22px 0 2px">10 年期美债 · 关键位在哪</h3>
{Y10_HTML}
<p class="cap">关键位取整数关口 + 本轮高点。<b>利率每上一档，长久期资产（成长股、2 倍 ETF）的估值就再压一层</b> —— 这是"只减不加"的分母理由。</p>

<div class="warn"><b>{D_PREV} 真正的引爆点不是财报，是黄仁勋的一句话。</b>
<table style="margin:8px 0">
  <tr><th>时点</th><th>事件</th><th>为什么杀伤力大</th></tr>
  <tr style="background:#FDF3F2"><td><b>约 09:00 ET</b></td><td><b>黄仁勋在柏林 GTC 受访：</b>"neocloud 的需求爆表，但<b>中间缺的是营运资金</b> —— 资本出现了短缺。<b>资本的需求远大于资本的供给</b>，这个问题会得到解决。"</td>
  <td><b>他把"某家公司烧钱"升级成了"整个行业的融资约束"。</b>而且说话的人是卖方龙头 —— 等于承认<b>买家没钱了</b></td></tr>
  <tr style="background:#FDF3F2"><td><b>开盘后</b></td><td><b>富国银行交易台</b>直接写："这句话<b>吓到市场，再度点燃信贷端的忧虑</b> —— 当前风险偏好下，资本市场已经承压。"<b>CoreWeave 债券收益率与 CDS 继续高位，股价 -4%</b></td>
  <td>这是<b>信贷市场</b>的语言，不是股票市场的。AI 的瓶颈从"芯片"变成"钱"</td></tr>
  <tr style="background:#FFF8E6"><td><b>同日</b></td><td><b>Nebius 拿到花旗 35 亿美元贷款</b>（364 天，SOFR+2.75%）</td>
  <td><b>这是反面证据：钱荒有解，但要付利息。</b>融资能续上，只是成本更高 —— 所以估值要打折，不是生意要黄</td></tr>
</table>
<b>一句话概括 {D_PREV}：市场不是不信 AI，是不信"用别人的钱做 AI"能一直做下去。</b>
摩根大通的纪要说得更直白：<b>"本轮科技股反弹之后，市场没有足够强的做多共识、增量资金跟进。"</b><br>
<span class="small"><b>📌 这条对你的持仓意味着什么：</b>你没有任何"靠融资扩张"的仓位（没有 neocloud、没有二线算力、存储已清、METU 已清）。
<b>你只有 {MV['SPYM']/NAV*100:.0f}% 标普 + {MV['QQQM']/NAV*100:.0f}% 纳指 + {MV['VXUS']/NAV*100:.0f}% 非美 + {CASH/NAV*100:.0f}% 现金。</b>
{D_PREV} 道指 +0.04%、罗素 +0.07% —— <b>市场很清楚地区分了"有现金流"和"要融资"两类资产，而你的核心仓在前一类。</b></span></div>

<div class="say"><b>📌 对账户的意义：{METU_EXIT_DATE} 不是"配置失败"，是"一个 8% 的仓位按剧本走完了它的结局"。</b><br>
<p class="small" style="margin:2px 0 6px">下表全部是 <b>{METU_EXIT_DATE} 当天收盘</b>的固定读数（历史记录，不随今天盘中变化）：</p>
<table style="margin:8px 0">
  <tr><th>对照</th><th class="n">{METU_EXIT_DATE} 涨跌</th><th>说明</th></tr>
  <tr><td>费城半导体</td><td class="n"><b class="dn">-6.72%</b></td><td>当天最惨的一类（距 6/22 纪录高点约 -25%）</td></tr>
  <tr><td>纳斯达克</td><td class="n">-2.60%</td><td>你的 QQQM {MV['QQQM']/NAV*100:.1f}% 对着它</td></tr>
  <tr><td>标普 500</td><td class="n">-1.21%</td><td>你的 SPYM {MV['SPYM']/NAV*100:.1f}% 对着它</td></tr>
  <tr><td>必需消费 / 公用 / 长债</td><td class="n"><b class="up">+3.31% / +2.07% / +1.87%</b></td><td><b>钱没离场，换了赛道</b> —— 这三个你一个都没有，所以没吃到</td></tr>
  <tr style="background:#FDF3F2"><td><b>你的账户</b></td><td class="n"><b>-2.36%</b></td><td><b>落在标普与纳指之间 —— 这是 {MV['SPYM']/NAV*100:.0f}/{MV['QQQM']/NAV*100:.0f}/{MV['VXUS']/NAV*100:.0f} 的加权结果</b></td></tr>
</table>
<b>已实现亏损已经落袋：METU {mo(abs((COST['METU']-METU_EXIT)*6+1.88))}（含费）+ RAM {mo(abs(RCL_NET))} = {mo(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))}，净值 {abs(abs(RCL_NET)+abs((COST['METU']-METU_EXIT)*6+1.88))/NAV*100:.2f}%。</b>
<b>这笔钱换来的是"杠杆归零"和"不用再盯一个 2 倍产品" —— 两笔加起来不到净值的 5%，这就是"止损"和"扛单"的区别。</b><br>
<span class="small"><b>要提醒自己的事：</b>如果 METU 没卖，按 02/20 收盘 {mo(METU_CLOSE)} 算，账面还要再少 <b>{mo((METU_EXIT-METU_CLOSE)*6)}</b>。
<b>但这不是"卖对了"的证明 —— 只是这一次的路径恰好如此。</b>规则的价值在于它每次都执行，而不在于它每次都赚。</span></div>

{MACRO_BLOCK}
{MACRO_LONG}
{IV_BLOCK}

<h2><span class="no">05</span>事件复盘与存档（02/20–10/23）</h2>
<p class="h2sub"><b>这一节是历史记录，不是今日新闻</b> —— 记的是 02/20 那根阴线的成因（财报好、股价坏，中间隔着黄仁勋的一句话），
以及 10/23 的企稳。留着是为了<b>下个月回看"AI 融资约束"这条叙事有没有被验证</b>。</p>

<h3 style="font-size:13.5px;margin:16px 0 4px">财报记分卡 · 上一轮（本周的 GOOGL/MSFT/AMZN/INTC 见 06 节事件窗口） <span class="small">（{ECARD.get("week") or ""}）</span></h3>
<p class="h2sub" style="margin:2px 0 8px"><b>这张表要看的不是"超没超预期"，是"超了之后股价怎么走"。</b>三家全部 EPS 超预期，两家跌 —— 这就是当前市场的定价方式。</p>
<table class="ec">
  <tr><th>公司</th><th class="n">日期</th><th class="n">营收（实际 / 预期）</th><th class="n">营收超预期</th><th class="n">EPS（实际 / 预期）</th><th class="n">EPS 超预期</th><th class="n">财报后</th><th>关键点</th></tr>
{EC_HTML}
</table>
<p class="cap">营收单位为亿美元，EPS 为美元。<b>「财报后」列：盘后公布的取次日（今日）盘中涨跌。</b>
<span class="small">手工维护，数据来自公司财报与公开报道；如有出入以公司 IR 为准。</span></p>

<ul>
  <li style="background:#FDF3F2;padding:10px 12px;border-radius:6px"><b>① 🔴 Meta Q3 逐项对照：每一项都比预期好，股价 {pc(_lcp('META'))}</b><br>
    <table style="margin:8px 0">
      <tr><th>项目</th><th class="n">实际</th><th class="n">预期</th><th>判定</th></tr>
      <tr style="background:#EFF8F1"><td><b>营收</b></td><td class="n"><b>646 亿（+26%）</b></td><td class="n">633 亿</td><td><span class="tag g">超 2%</span></td></tr>
      <tr style="background:#EFF8F1"><td><b>Q4 营收指引</b></td><td class="n"><b>730–760 亿</b></td><td class="n">约 730 亿</td><td><span class="tag g">中值 745 亿，超</span></td></tr>
      <tr style="background:#EFF8F1"><td><b>调整后 EPS</b></td><td class="n"><b>7.40</b></td><td class="n">6.54</td><td><span class="tag g">超 13%</span>（GAAP 3.83 受 98.9 亿和解拖累）</td></tr>
      <tr style="background:#FDF3F2"><td><b>自由现金流</b></td><td class="n"><b>-21.2 亿</b></td><td class="n">-94 亿</td><td><span class="tag o">好于预期，但上市以来首次转负</span></td></tr>
      <tr style="background:#FDF3F2"><td><b>全年资本开支指引</b></td><td class="n"><b>1,350–1,450 亿</b></td><td class="n">1,300–1,450 亿</td><td><span class="tag r">下限上调 50 亿</span></td></tr>
      <tr><td>长期债务</td><td class="n">809.2 亿</td><td class="n">（2025 年末 587.4 亿）</td><td>半年增加 222 亿</td></tr>
    </table>
    <b>股价的三段式，比财报本身更有信息量：</b>盘后一度 <b>+6~7%</b> → 收窄到 +2~4% → 盘前 <b>-1.74%</b> → 开盘后 <b>{pc(_lcp('META'))}</b>。
<b>每一段都在跌，跌了一整天。</b><br>
    <span class="small"><b>⚖️ 对你的含义：</b>METU 已于 02/20 <b>{mo(METU_EXIT)} 清仓 10 股</b>（成本 $30.00，亏损 {mo((COST['METU']-METU_EXIT)*6+1.88)}）。
<b>"四超预期"没能救它，因为市场定价的不是财报，是"资本开支执行者"这个身份。</b></span></li>

  <li style="background:#FDF3F2"><b>② 🔴 今天真正的引爆点：黄仁勋说"资本短缺"</b><br>
    他在柏林 GTC 受访的原话：<b>"neocloud 的需求爆表……中间缺的是推动这些企业增长的营运资金。因为 AI 在各个领域的需求都很强，资本出现了短缺。资本的需求远大于资本的供给，这个问题会得到解决。"</b><br>
    <b>为什么这句话比财报杀伤力大：</b>说这话的人是<b>卖方龙头</b>，等于承认<b>买他芯片的人没钱了</b>。
    市场立刻翻译成：<b>AI 的瓶颈从"芯片产能"变成了"融资能力"</b>。<br>
    <span class="small"><b>富国银行交易台当天的原话：</b>"黄仁勋关于资本稀缺的表态<b>吓到市场，再度点燃信贷端的忧虑</b> —— 当前风险偏好下，资本市场已经承压。"
    <b>CoreWeave 债券收益率与 CDS 继续高位、股价 -4%</b>。<b>摩根大通：</b>"本轮科技股反弹之后，市场没有足够强的做多共识、增量资金跟进。"<br>
    <b>反面证据也要记：Nebius 同日拿到花旗 35 亿美元贷款</b>（364 天，SOFR+2.75%）。<b>钱荒有解，但要付利息 —— 所以是估值打折，不是生意黄了。</b></span></li>

  <li><b>③ 特斯拉：从盘前 -5.4% 到盘中转涨，因为它"砍了资本开支"</b><br>
    Q3 营收 280.5 亿（-0.2%，不及预期）、净利 7.87 亿（-43%）、调整后 EPS 0.48（超预期）、<b>自由现金流 +15.3 亿（预期 -49.9 亿）</b>、
    <b>资本开支只有 47.3 亿（预期 79.5 亿）</b>。盘中 <b>{pc(_lcp('TSLA'))}</b>。<br>
    <span class="small"><b>⚖️ 这条对你的直接敞口是零</b>（TSLL 已于 10/2 清仓），但它是今天最好的注脚：
    <b>META 加资本开支被卖，TSLA 砍资本开支被买</b> —— 同一个交易日、同一个行业，两个完全相反的裁决。<b>市场现在只给"少花钱"溢价。</b></span></li>

  <li style="background:#FFF8E6"><b>④ 存储链：连续第二天被卖，跌得比费半轻</b><br>
    盘中：<b>希捷 {pc(UCHG['STX'])}、西数 {pc(UCHG['WDC'])}、闪迪 {pc(UCHG['SNDK'])}、美光 {pc(UCHG['MU'])}</b>，篮子 <b>{pc(B_AVG)}</b>；
    费半 {ixp('SOXX')}。亚太同步：SK 海力士 -4%+、三星 -4%。<br>
    <span class="small"><b>⚖️ MU 槽位怎么判：</b><b>没有净值门槛，只看收益对比</b> —— 这条链的定价权现在在"买方还有没有钱"这一端，
    所以<b>它的风险收益比还算不出来（分母是未知数）</b>，不是价格不够低。算不出比例的标的，不进仓。</span></li>

  <li><b>⑤ 分母层面：油价 + 加息，两个都在压长久期资产</b><br>
    <b>WTI 站上 $99、布伦特破 $111</b>（美以被曝计划中期选举后重启对伊朗打击）；CME：<b>12 月加息 25bp 定价 77.3%</b>。
    10Y 今天 <b>{_cnb("US10Y")}</b> 至 {_cn("US10Y", 3)}% —— <b>但这是避险买盘，不是通胀降温。</b><br>
    <span class="small"><b>⚖️ 这就是"2 倍杠杆只减不加"的最硬理由：</b>油价上行 → 通胀预期 → 加息定价 → <b>长久期资产的估值天花板被压住</b>。
    <b>METU 就算 META 业绩再好，也要先过这一关 —— 而它 02/20 没过（已清仓）。</b></span></li>
</ul>

<h3 style="font-size:15px;margin:26px 0 6px">📁 存档 · 01/14–01/02：这一轮 AI 硬件杀跌的四条线（背景，非今日）</h3>

<ul>
  <li style="background:#FDF3F2;padding:8px 10px;border-radius:6px"><b>① 🔴 最重要的一条，而且它跟存储无关：AI 扩张的"融资瓶颈"被两个 CEO 同时点名</b><br>
    博通 CEO 陈福阳在新加坡一场活动上表示，在全行业争相建设 AI 基础设施之际，<b>融资可能成为比半导体、存储芯片或电力短缺更大的障碍</b>。同台的英特尔 CEO 陈立武也表达了对融资的担忧，指出<b>资本成本不断上升</b>，市场在质疑大举投资 AI 的企业最终能否获得足够回报。<br>
    <b>市场反应立竿见影，而且砸得最狠的正是依赖云厂商资本开支承诺的光通信：</b>Ciena -9.95%、Lumentum -9.78%、Coherent -7.87%、康宁 -6.00%。博通自身 -1.88%。<br>
    <span class="small"><b>⚖️ 这条为什么重要：</b>过去两年 AI 链的定价逻辑是"需求有多强"。这是第一次由两位产业链核心人物把话题转向"<b>钱从哪里来、成本多少</b>"。<b>如果 AI 融资真的成为瓶颈，整条链（包括 DRAM）的估值都要重算 —— 这是比任何技术竞争都更高一层的风险。</b>对你来说，它进一步支持"E 槽（3.5% 杠杆赌注）应该关掉"这个结论。</span></li>

  <li><b>② 长鑫存储（CXMT）4F² 架构落地时间表，把海外三巨头的技术代差预期往前推了两年</b><br>
    长鑫在第四届集成芯片和芯粒大会上公布：新一代 4F² 架构 DRAM 研发取得关键进展，<b>年底将推出融合 4F² 与晶圆级混合键合的 DDR5 RDIMM</b>，直奔服务器内存市场。相比主流 6F² 方案，单元面积压缩约三分之一，<b>同晶圆同制程下密度理论提升约五成</b>。<br>
    时间对比：SK 海力士 2025 年才公布 4F² 路线图，三星目标是 2028 年实现 10 纳米以下 DRAM 量产。<b>若长鑫年底如期交付，代差被拉大到两年左右。</b><br>
    <b>韩国股市当天直接反应：KOSPI -3.65%，收 6,384.24 点。</b>今晨（01/15）跌势加速 —— <b>KOSPI 再收跌 5.47% 至 6,035.34 点（8 月以来最低），SK 海力士单日跌超 10%，日经 225 -1.82%</b>。</li>

  <li><b>③ 供给纪律的"第二条裂缝"：东芝逆势扩产 HDD</b><br>
    东芝宣布投入约 <b>600 亿日元</b>扩建菲律宾 HDD 工厂，目标 <b>2027 财年将 AI 数据中心 HDD 产能较 2025 财年翻一番，市场份额从 10% 提升到 30%</b>。<br>
    <span class="small"><b>为什么这条很关键：</b>HDD 和 DRAM 不是同一个品类，但它打破了市场此前的核心假设 —— <b>"巨头会集体控产保价"</b>。一旦有人开始逆势扩产，涨价叙事就要打折。<b>这是"priced for perfection"风险的又一次兑现：不是需求没了，是供给端的稀缺故事松动了。</b></span></li>

  <li><b>④ 宏观加压：克利夫兰联储哈马克放鹰 + 油价创阶段新高</b><br>
    哈马克周一表示<b>可能需要收紧货币政策以降低通胀，当前政策的限制性还不够</b>，通胀回归速度过慢、高通胀风险仍在上升。<br>
    油价端：沙特利雅得国际机场周末遇袭，特朗普表示美军可能加入沙特的军事反击；沙特联军对胡塞武装多轮打击。<b>WTI +2.06% 至 $93.74，布伦特 +1.84% 至 $106.65。</b><br>
    <span class="small"><b>这两条叠加，直接构成 10/14 CPI 之后"通胀粘性"叙事的燃料。</b>美元指数同日创年内新高，黄金 -0.61% 至 $6,190.60。<b>CPI 前两天加息派重新发声，不是巧合。</b></span></li>

  <li><b>⑤ 受益方：微软自研芯片 + Meta 新模型，资金去了"AI 应用"而非"AI 硬件"</b><br>
    <b>微软</b>公布新一代自研 AI 芯片 <b>Maia 300</b> 及大规模部署计划，股价 +1.14%，<b>总市值重返 4 万亿美元上方</b>；<b>Meta</b> 因推出两款前沿 AI 模型大涨 <b>+3.38%</b>（你的 METU 因此 +6.75%）；苹果 +1.29%（市场等当天发布会）。<br>
    另一边：<b>英伟达 -1.29%、博通 -1.88%、台积电 -1.98%、特斯拉 -1.22%</b>，全部跑输大盘。<br>
    <span class="small"><b>这条印证了昨天你已经看到的现象：</b>扎克伯格公开说"AI 资源极度紧缺、要更多算力"，<b>这个对 DRAM 需求的直接利多并没有托住存储板块</b>。需求故事早已充分定价，<b>边际定价权在供给端和融资端。</b></span></li>

  <li><b>⑥ 板块极端分化 + 一个有趣的日期</b><br>
    当天恰好是<b>本轮美股牛市四周年</b>（2022/01/14 标普收 3,577.03，四年后 7,807.24）。在这样的纪念日上，市场给出的是一份<b>分化到极致</b>的成绩单：XLE 创新高、道指创新高，而费半创三个月新低。<br>
    成交量方面，因债市休市 + 大银行财报在次日，<b>交易所成交量低于 20 日平均水平</b>，属于清淡震荡。<span class="small">这一点要注意：<b>低成交量下的跌幅，代表性会打折 —— 但它同样意味着"没有多少人愿意在这个价位接存储"</b>，这对决定要不要抢反弹是负面证据。</span></li>
</ul>

<h3 style="font-size:15px;margin:24px 0 6px">📁 存档 · 台积电 Q3（01/02）与 PPI 对撞：「AI 需求硬证据」vs「融资质疑」</h3>
<p class="h2sub">台积电交出创纪录营收并大幅上调资本开支 —— 但股价当天仍跌。这就是当前市场的核心矛盾（<b>01/02 存档，今日续跌中</b>）</p>

<div class="warn" style="font-size:13.5px">
<b>① 台积电 Q3：需求端没有任何问题。</b><br>
<b>Q3 营收约新台币 1.4942 万亿（≈ $467 亿），季增 17.6%、创单季历史新高，超过公司财测上缘（$446–458 亿）和市场共识（$458 亿）；9 月单月同比 +54.6%；前九月累计同比 +41.1%。</b><br>
<b>资本开支从 $520–560 亿大幅上调至 $600–640 亿</b>；Q4 指引营收 $446–458 亿、毛利率 65–67%；<b>全年收入增速目标从"至少 30%"上调到 40%</b>。
外资目标价同步上调：高盛与摩根大通给到 3,300 新台币，摩根士丹利 3,088。<br>
<b>② 但市场反应是跌的。</b>TSM 10/14 收跌 <b>-3.40%</b>，次日盘前再跌 <b>-1.73%</b>。
<span class="small"><b>这不是"业绩不好"，而是"业绩好到没有惊喜"</b> —— 台积电每月披露营收，Q3 的数字市场早就知道了。真正被交易的是估值与久期，不是基本面。</span><br>
<b>③ 这恰好构成对你最有价值的一组对照实验：</b><br>
<b>AI 需求的"硬证据"（台积电营收、资本开支）在持续变强；但 AI 资产的估值在持续被压。</b>
<b>说明市场卖的不是"AI 需求见顶"，而是"AI 资产的久期与融资成本"。</b>
<span class="small">这也解释了为什么<u>同一周里</u>：甲骨文 -8.83%（负债最重）、存储链 -4~5%（供给端竞争+资本密集）、而苹果 +0.50%（低估值+现金流）。<b>三者的差别不在 AI 相关度，在资产负债表。</b></span><br>
<b>④ ✅ 01/02 已落地：9 月 PPI 同比 +6.3%，比 CPI 更"热"。</b>
<b>整体同比 +6.3%（预期 5.3%、前值 5.4%）、环比 +1.3%（预期 0.6%）—— 2022 年 12 月以来最大年增幅；核心同比 +5.0%（预期 4.4%）。能源环比 +10.8%、运输仓储 +3.4%、航空票价 +5%。</b>
按 10/14 的剧本这该把 10Y 再推高一次，<b>结果 10Y 反而跌 13.4bp 到 5.06%</b> —— 因为同一天 CoreWeave 的 CDS 突破 1000bp，避险买盘压过了通胀定价。
<b>这一条本身就是对"通胀数据决定利率"这个直觉的反证：当信用风险出现时，利率会先服从避险需求，而不是服从通胀。</b>
</div>

<div class="say"><b>📌 一个容易被忽略的细节：这次下跌里，"最贵"和"最便宜"的资产分得很干净。</b><br>
· <b>被卖</b>：甲骨文（PE 高、负债 $1,000 亿+）、存储链（资本密集、供给竞争）、无盈利的 AI 基建（如 NBIS 这类）。<br>
· <b>被买</b>：能源（现金流即时）、苹果（PE 30x 出头、现金 $600 亿+）、你的 SPYM（宽基、分散）。<br>
<span class="small"><b>这不是"risk-off"，是"quality-on"</b> —— 钱没有离场，只是从"讲故事"搬到"有现金流"。
<b>对你最重要的含义：你的核心三仓（SPYM/QQQM/VXUS）恰恰是"有现金流"这一侧 —— 01/15 到 01/02 这三天里，纳指跌了约 3%、费半跌了约 8.5%，而你的账户回撤 {(NAV/1564.62-1)*100:.2f}%。</b></span></div>

<table>
  <tr><th>近三日关键消息（存档）</th><th>内容</th><th>对你的含义</th></tr>
  <tr style="background:#FDF3F2"><td><b>Cathie Wood<br>点名存储</b></td>
    <td>ARK 的木头姐在香港对投资者表示，HBM 需求依然强劲、供给依然紧张，<b>但"趋势向上，周期却很残酷"</b>——如果中国再有几家竞争对手进场，<b>存储价格会变得更不稳定</b>。她因此回避传统存储股。</td>
    <td><span class="tag r">供给端论调</span> <b>这是"定价权在供给端"这个判断的华尔街注脚</b>，且出自最擅长抓成长股的人之口</td></tr>
  <tr style="background:#EFF8F1"><td><b>美光创纪录<br>回购授权</b></td>
    <td>美光董事会授权 <b>351.6 亿美元</b>股票回购计划，<b>12 月 9 日起生效</b>——公司史上最大规模。</td>
    <td><span class="tag g">基本面利多</span> 股东回报在改善，<b>但股价照跌 4%</b> → 再次印证"杀估值不是杀逻辑"</td></tr>
  <tr style="background:#EFF8F1"><td><b>闪迪评级<br>被上调</b></td>
    <td>S&amp;P 将闪迪信用评级从 BB+ 上调至 <b>BBB</b>（投资级），理由是现金流改善；预计其 <b>2027 财年营收增长 125%</b>、自由现金流超 <b>230 亿美元</b>（此前预估超 150 亿）。</td>
    <td><span class="tag g">基本面利多</span> 与股价走势完全背离 —— <b>连续两天，存储的"坏消息"全是供给/融资，而"好消息"全是自己的基本面</b></td></tr>
  <tr style="background:#FFF8E6"><td><b>油价与<br>牛津经济</b></td>
    <td>布伦特周一 <b>+1.8%</b>；牛津经济研究院上调预测：<b>布伦特到明年二季度均价将维持在 $100 以上</b>，之后才随供给正常化回落，<b>且回归长期均衡的速度比此前预期更慢</b>（红海出口设施遇袭 + 地缘溢价更顽固）。今晨 WTI 小幅回落至约 $93.25。</td>
    <td><span class="tag o">已兑现</span> 这是 CPI/PPI 双双超预期的<b>上行风险源</b>。<b>而这个窗口确实来了 —— QQQM 的 $88.00 回调单已在 01/02 开盘按设计成交。剩下 SPYM / VXUS 两张继续挂着。</b></td></tr>
  <tr><td><b>AI 信用<br>第一道裂缝</b></td>
    <td>CoreWeave 5 年期 CDS 突破 <b>1000bp</b>、2032 年债券跌到面值 77–81 美分、收益率 &gt;15%（隐含 5 年违约概率约 63%，一年前约 30%）。对照：软银 424bp、甲骨文 288bp、投资级指数约 60bp。</td>
    <td><span class="tag r">本轮下跌的真正驱动器</span> <b>市场开始给"AI 基建的债务"定价，而不只是给"AI 的故事"定价。</b>这也解释了为什么台积电业绩爆表（净利 +68.7%）股价仍跌 4.75%</td></tr>
  <tr><td><b>Gartner<br>预警</b></td>
    <td>Gartner 预警<b>超 3.5 万亿美元 AI 基础设施投资面临减值</b>，预计到 2030 年<b>超 80% 新云服务商难以独立运营</b>。</td>
    <td><span class="tag o">长期变量</span> 与 CDS 走势互相印证。<b>但这是"neocloud"的风险，不是你的持仓的风险 —— 你没有任何一只 AI 基建个股</b></td></tr>
  <tr><td><b>美元与<br>油价</b></td>
    <td>美元指数 10/14 收 <b>102.554</b>（18 个月新高）；WTI 10/14 收 <b>$95.09（+1.01%）</b>、布伦特 $106.06。<b>01/02 油价继续走高</b>（WTI 原油 ETF +1.15%）。</td>
    <td><span class="tag gray">与你相关</span> 美元强 = 非美资产（VXUS）承压，也解释了它为何始终在 MA20 下方；油价高 = 通胀粘性的燃料</td></tr>
</table>

<div class="say"><b>📌 今晨最值得记住的一句话（来自博通 CEO 陈福阳）：AI 行业目前最大的瓶颈，已经从"造不出芯片"变成"借不到钱"。</b><br>
昨天存储跌，可以说成"中国技术竞争"；今晨韩股崩 5.47%，说明市场认的是更宽的那一层 —— <b>整条 AI 链的估值，正在从"需求叙事"切换到"资金成本叙事"</b>。这不是一天能修复的事，也正好解释了为什么"关掉 E 槽那 3.5%"比"换一只 2 倍 ETF 继续赌"更合理。</div>

<h2><span class="no">06</span>今日操作指令</h2>
<p class="h2sub"><b>今天执行完毕：两笔全部成交，结构一次到位。</b>
{TODAY_TRADE}，手续费合计 {mo(TODAY_FEE)}。
现在 <b>SPYM {MV['SPYM']/NAV*100:.1f}% / QQQM {MV['QQQM']/NAV*100:.1f}% / VXUS {MV['VXUS']/NAV*100:.1f}% / 现金 {CASH/NAV*100:.1f}%</b>
—— 核心三仓 <b>{CORE/NAV*100:.2f}%</b>，距 94% 只差 <b>{94-CORE/NAV*100:.2f}pp</b>，差的那部分是 VXUS 超配 3pp 和 SPYM 整股零头。
<b>接下来零动作</b>：下一次决策点是 12/8–9 FOMC 与 12 月入金。</p>

<div class="ok" style="font-size:14px">
<b>✅ 上一交易日（{D_PREV}）成交回执</b>
<table style="margin:8px 0">
  <tr><th>动作</th><th class="n">价格</th><th class="n">数量</th><th class="n">金额</th><th>结果</th></tr>
  <tr style="background:#EFF8F1"><td><b>买入 SPYM</b></td><td class="n"><b>{mo(RB['px'])}</b></td><td class="n">{RB['qty']} 股</td><td class="n">{mo(RB_FILL)}</td><td><span class="tag g">再平衡</span> {RB['shares_from']} → <b>{RB['shares_to']} 股</b>，占比 {MV['SPYM']/NAV*100:.2f}%</td></tr>
  <tr><td>手续费</td><td class="n">—</td><td class="n">1 笔</td><td class="n">{mo(RB['fee'])}</td><td>平台费（美股 0 佣金）；券商"今日盈亏"不含这个</td></tr>
</table>
<b>现金 {mo(CASH)}（{CASH/NAV*100:.2f}%）</b>｜<b>杠杆 0.00%</b>｜核心三仓 {CORE/NAV*100:.2f}%｜净值 {mo(NAV)}。
</div>

<h3 style="font-size:14px;margin:26px 0 8px">📉 回撤加码档位（核心宽基只买不卖）</h3>
<p class="cap" style="margin-bottom:10px"><b>2026-11-17 起：「跌 10% 减半仓」废止。</b>核心三仓是宽基，没有"基本面变坏"这个卖出理由，
只有"价格变便宜"这个买入理由。<b>回撤线现在决定的是"多买多少"，不是"卖多少"。</b>
档位按 <b>SPYM 距 52 周高的跌幅</b>算（不是净值回撤 —— 净值含现金和入金，会被稀释）。</p>
<table>
  <tr><th>SPYM 距 52 周高</th><th class="n">当月投入</th><th class="n">现金可降至</th><th>状态</th></tr>
  <tr><td>&gt; −10%</td><td class="n">1 × 月额度</td><td class="n">6%</td><td>正常定投</td></tr>
  <tr><td>−10% ~ −15%</td><td class="n">1.5 ×</td><td class="n">5%</td><td>便宜，加投 50%</td></tr>
  <tr><td>−15% ~ −25%</td><td class="n">2 ×</td><td class="n">4%</td><td>明显便宜，加倍</td></tr>
  <tr><td>−25% ~ −40%</td><td class="n">3 ×</td><td class="n">3%</td><td>恐慌区，投三个月额度</td></tr>
  <tr><td>&lt; −40%</td><td class="n">1 ×</td><td class="n">6%</td><td>系统性危机，退回正常（弹药留到企稳）</td></tr>
</table>
<p class="cap" style="margin-top:-6px"><b>当前位置：SPYM 距 52 周高 {_SPYM_FROM_HI if _SPYM_FROM_HI is not None else "—"}% → {_TIER[0]}（当月投 {_TIER[1]}× 月额度，现金下限 {_TIER[2]}%）。</b>
额外投入的钱从现金缓冲里出；<b>现金见底就只投当月额度，不卖持仓</b>。跌幅超过 −40% 时停止加码 —— 不是看空，是不知道底在哪，弹药要留给能确认企稳的时候。</p>

<div class="say" style="font-size:14.5px">
<b>📌 冷却闸早已解除，但今天仍然不开新仓 —— 三个理由，都是硬的：</b><br>
<b>① 今晚是英伟达财报，不建仓。</b>可动用 <b>{mo(max(0, CASH - NAV*T_CASH_F - _inflight_amt))}</b>（现金 {mo(CASH)} − {T_CASH:.0f}% 下限 {mo(NAV*T_CASH_F)}）——
<b>这笔钱明天就要投出去，但今晚是二元事件</b>：NVDA 的 ATR 6.31% 意味着财报后单日 ±10% 是常态，<b>用新钱赌一个几小时后才知道的数字，不是投资</b>。<br>
<b>② 12/8–9 的 FOMC 是年内最后一票，12 月加息已定价 77.3%。</b>长久期资产（成长、2 倍 ETF）的估值天花板被压住，
<b>所以"2 倍杠杆只减不加"这条在 12 月前不放松</b> —— 现在槽位是空的，正好。<br>
<b>③ 新仓评估已暂停。</b>上一轮扫描（10/23，37 个标的、6 道关卡）的结论留在
<a href="https://www.workbuddy.cn/space/d/kaPK8ddyDnN9Wp35GPpmcK"><b>《开新仓方案》</b></a> 里，
<b>个股不再有净值门槛（2026-11-17 取消）</b>，只按<b>收益对比</b>判：这一笔买它，是否比买 SPYM / QQQM 更划算（含手续费）。
<b>现在 SPYM 缺口还在，所以"更划算"这一关它过不了</b> —— 这是相对收益的问题，不是净值大小的问题。<br>
<span class="small">这不是"看空"，是<b>优先级</b>：先把 70% 的 SPYM 补齐，再谈第四条腿。缺 5.3pp 时开新仓，是把结构做乱。</span>
</div>

<div class="warn"><b>接下来三个决策点：</b>
<table style="margin:8px 0">
  <tr><th>时间</th><th>事件</th><th>你会做什么</th></tr>
  <tr style="background:#FFF8E6"><td><b>11/3–11/4</b></td><td><b>中期选举</b>（11/3 已投票，计票中）</td><td><b>不交易选举结果。</b>今天涨的是"不确定性消退"（VIX −16.9%），不是政策利好；<b>结果只影响 11 月入金的买点</b></td></tr>
  <tr style="background:#EFF8F1"><td><b>11 月上旬</b></td><td>11 月入金 ¥1,500–2,000 ≈ $225–300</td><td><b>先补 SPYM 整股</b>（1 股 ≈ 5pp），补完再看现金是否回到 13%</td></tr>
  <tr style="background:#FFF8E6"><td><b>11/6（五）</b></td><td><b>10 月非农</b></td><td>只看，不动 —— 数据不改仓，改的是 12 月的概率</td></tr>
  <tr style="background:#FFF8E6"><td><b>12/8–9（二/三）</b></td><td><b>FOMC 议息 + 点阵图</b></td><td>年内最后一次。<b>在此之前 2 倍杠杆槽位保持空置</b></td></tr>
</table>
<b>三条"不要做"：</b><br>
<b>① 不要因为 METU 反弹就后悔。</b>止损的成本是"可能错过反弹"，收益是"永远不会扛一个无限亏损" —— <b>前者可逆，后者不可逆。</b><br>
<b>② 不要在这个位置加仓"跌透了"的科技股。</b>杀跌理由是<b>融资约束</b>（META FCF 转负 -21.2 亿、5 年期 CDS 创纪录、未来承诺 8,250 亿），
<b>融资约束不会因为股价跌 30% 而消失。</b><br>
<b>③ 现金不低于 {T_CASH:.0f}%（实操 {T_CASH:.0f}–{T_CASH_HI:.0f}%），但回撤不再触发卖出。</b>回撤线 <b>{mo(DD10)}</b>（高水位 {mo(HWM)} × 0.9）从"止损线"改成<b>"加码刻度"</b> —— 当前距它 <b>{abs(DD10/NAV-1)*100:.1f}%，跌到那里是加投的信号，不是卖出的信号。</b>
<span class="small">行情上的事实（{D_PREV} 收盘）：<b>道指 {ixc('DIA')}、罗素 {ixc('IWM')}、必需消费 {ixc('XLP')}、公用 {ixc('XLU')}、20 年美债 {ixc('TLT')}</b>；
<b>费半 {ixc('SOXX')}、半导体 {ixc('SMH')}、软件代理 {ixc('IGV')}</b>。
<b>市场没有抛售股票，它在抛售"需要融资的商业模式"。</b>这句话接下来三周会反复被验证或被推翻 —— 两种可能都留着。</span></div>

<h2><span class="no">07</span>催化剂日历（未来 30 天）</h2>
<p class="h2sub">标红的四天直接跟你的钱相关｜<b>本周主角：10/28 FOMC</b>；下周主角：四巨头财报（10/30）与中期选举（11/3）</p>

<table>
  <tr><th>日期</th><th>事件</th><th>影响</th><th>你要做什么</th></tr>
  <tr style="background:#F6F8FA"><td><b>01/03（二）</b></td><td><b>✅ 入金 $332.41 全部落地</b>（SPYM 10 股 → 9 股）｜Netflix Q3（盘后 -5.78%）</td><td><span class="tag gray">已完成</span></td><td>无</td></tr>
  <tr style="background:#F6F8FA"><td><b>01/06（三）</b></td><td><b>✅ META Q3 财报（盘后）</b> + 特斯拉（盘后）</td><td><span class="tag gray">已完成</span> META 营收 646 亿 +26%、Q4 指引超预期，但 <b>FCF 首次转负 -21.2 亿</b>、capex 下限上调到 1,350 亿</td><td>无</td></tr>
  <tr style="background:#F6F8FA"><td><b>02/20（四）</b></td><td><b>🔴 METU 跌破 $25 止损 → 清仓 10 股 @ {mo(METU_EXIT)}</b>｜奈飞/特斯拉财报次日杀跌｜WDC + 英特尔财报（盘后）· 初请 19.4 万</td><td><span class="tag gray">已完成</span> 纳指 -2.60%、费半 -6.72%、标普 -1.21%、道指 +0.04%（02/20 收盘）；<b>10 年美债收益率跌破 5%</b>（-17bp 至 4.97%）</td><td><b>已执行：卖出 METU 10 股</b></td></tr>
  <tr style="background:#F6F8FA"><td><b>10/23（五）</b></td><td>美国 10 月 PMI 初值 · 密歇根信心终值 · 日本 9 月 CPI</td><td><span class="tag gray">已完成</span> 纳指 <b class="dn">-0.01%</b>、标普 -0.52%、道指 -1.24%、<b>费半 +0.53%</b>（10/23 收盘）</td><td>无（当日无成交）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/24–25</b><br><span class="small">周末</span></td><td><b>巴西总统大选第二轮投票</b>（当地时间周日）</td><td><span class="tag r">黑天鹅</span> <b>卢拉 51.76% 逆转胜选</b> —— 市场此前押注右翼，结果公布后巴西资产全面重定价</td><td>无（周末，你无事可做）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/26（一）</b></td><td><b>🔴 巴西大选结果落地 → 巴西资产崩盘</b>：EWZ -20.5%（上市以来最大单日跌幅）、雷亚尔 -10%、巴西股指期货熔断、财政部取消 10/27 与 10/29 国债拍卖</td><td><span class="tag gray">已完成</span> 纳指 +1.11%、费半 +2.85%、标普 +0.71%、道指 +0.31% —— <b>美股全涨，只有巴西在崩：这是单一国家定价，不是全球 risk-off</b></td><td>无（当日无成交）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/27（二）<br>上一交易日</b></td><td><b>Anthropic 递交 IPO 申请（代码 ANT）</b> → AI 叙事回温，芯片股连涨第三天；<b>META 把史上最大股权融资扩大到 975 亿美元</b>、股价 -1.2%；波音 -9.7%；<b>能源最弱（XLE -2.05%）</b></td><td><span class="tag gray">已完成</span> 纳指 +1.27%（QQQ 代理）、标普 +0.56%、<b>道指基本持平</b>、<b>SOXX +3.91%</b>；<b>科技 vs 能源的分化比指数本身更重要</b></td><td>无（当日无成交）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/28（三）<br>上一交易日</b></td><td><b>🔴 FOMC：维持 3.75–4.00% 不变</b>，<b>10:2 通过</b> —— Cook、Hammack 投反对票、主张再加 25bp；声明改为"累积更多信息"，沃什拒绝给前瞻指引</td><td><span class="tag gray">已完成</span> <b>市场读成偏鸽</b>：10Y -6bp 至 4.99%、2Y -8bp 至 4.66%、VIX 16.77</td><td>无（当日无成交）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/29（四）<br>上一交易日</b></td><td><b>🔴 Alphabet Q3：营收低于预期、搜索广告连续第二季不及 → −12.6%</b>；微软 Azure +48%、FCF +186 亿仍小跌；<b>盘后亚马逊一度 −12%、英特尔 −23%</b></td><td><span class="tag r">已完成</span> 道指 −0.37%、标普 −0.30%、纳指 <b>−1.38%</b>、<b>罗素 +0.97%</b>、费半 −1.42% —— <b>8/11 板块上涨，跌幅只集中在科技</b></td><td>无（当日无成交）</td></tr>
  <tr style="background:#F6F8FA"><td><b>10/30（五）<br>上一交易日</b></td><td><b>🔴 英特尔财报后暴跌近 29%</b>；亚马逊 −12% 延续 → AI 硬件全线杀；同日公布 <b>Q3 GDP +3.4%</b>（预期 3.0%）、<b>9 月 PCE 3.6% / 核心 3.1%</b></td><td><span class="tag r">已完成</span> 纳指 <b>−2.00%</b>、标普 −0.57%、道指 −0.85%、<b>费半 −9.10%</b>；必需消费/能源/公用事业收涨</td><td>无（当日无成交）</td></tr>
  <tr style="background:#FFF8E6"><td><b>11/1（日）<br>今天</b></td><td><b>周末休市</b> · 本月最后一天的无交易日</td><td><span class="tag o">复盘</span> 本周主线：<b>AI 资本开支叙事第二次崩塌</b>（第一次是 02/20 META FCF）</td><td><b>不动。</b>下周只有一个动作：等入金</td></tr>
  <tr style="background:#EFF8F1"><td><b>11/2（一）<br>今天</b></td><td>ISM 制造业 PMI（预期 54.7 / 价格分项 77.2）· <b>苹果财报（盘后）</b>（预期 EPS $1.98、营收 +11%）· Palantir 盘后</td><td><span class="tag o">高</span> 苹果是 QQQM 的第一大权重；<b>价格分项是 11 月 CPI 的先行指标</b></td><td><b>执行再平衡：买 SPYM 10 股</b>（现金 30.4% → 10.4%）</td></tr>
  <tr style="background:#FFF8E6"><td><b>11/3（二）</b></td><td><b>🔴 美国中期选举</b> · 9 月 JOLTS · AMD / Shopify / 辉瑞财报</td><td><span class="tag r">波动源</span> <b>真正的新风险是"数据中心监管"</b>，不是税收；两院归属决定它会不会被摆上桌</td><td>不改仓位</td></tr>
  <tr><td><b>11/4（三）</b></td><td>ADP 就业 · <b>10 月 ISM 服务业 PMI</b> · <b>财政部季度再融资公告</b> · 高通 / Arm / 麦当劳</td><td><span class="tag o">高</span> 服务业<b>价格分项</b>决定 12 月加息的概率；再融资规模决定长端</td><td>观察，不动</td></tr>
  <tr><td><b>11/5（四）</b></td><td>Q3 生产率与单位劳动成本 · 英国央行 · Cloudflare / Airbnb / Lumentum</td><td><span class="tag gray">中</span> 单位劳动成本是"通胀粘性"最硬的一个读数</td><td>—</td></tr>
  <tr style="background:#FDF3F2"><td><b>11/6（五）</b></td><td><b>🔴 10 月非农就业 + 失业率</b>（预期约 +6 万、失业率持平）</td><td><span class="tag r">最高</span> 决定 12 月那一票 —— 市场现在给 <b>77%</b></td><td><b>不动。</b>数据出来也不改仓</td></tr>
  <tr><td>11 月上旬</td><td><b>11 月入金到账</b>（¥1,500–2,000）</td><td><span class="tag g">加仓窗口</span> 新仓首档执行窗口</td><td><b>按《开新仓方案》执行</b></td></tr>
  <tr><td>12/8–9</td><td>FOMC 议息会议（年内最后一次，带点阵图）</td><td>市场已给 12 月加息 25bp <b>77.3%</b> 的概率</td><td>届时再评估；<b>2 倍杠杆在 12 月前不放松</b></td></tr>
</table>

<h2><span class="no">08</span>触发线距离表（{D_CUR} {PHASE}口径）</h2>
<p class="h2sub">到价就执行，不讨论、不犹豫</p>

<table>
  <tr><th>标的</th><th class="n">{D_CUR} {PHASE}价</th><th class="n">止损</th><th class="n">距离</th><th class="n">止盈</th><th class="n">距离</th><th>状态</th></tr>
{chr(10).join(rows3)}
</table>
<p class="cap"><b>2026-11-17 规则变更：核心宽基只买不卖，回撤线从「止损线」降为「加码刻度」。</b>
高水位 = 历史最高<b>收盘</b>净值 <b>{mo(HWM)}</b>（{HWM_SAVED_DATE} 确认），当前 {mo(NAV)}（{(NAV/HWM-1)*100:+.2f}%）。
<b>原来的 -10% 减半仓 / -15% 只留核心，已废止</b> —— 理由有三条：① 与"跌了买入"直接冲突，同一次下跌不能既卖又买；
② 减仓后<b>没有买回条件</b>，账户会长期停在低仓位（按 6% 现金测算，减半仓后现金会瞬间变成约 39%，然后呢？）；
③ 长期回测显示宽基择时系统性跑输买入持有 <b>2–4pp/年</b>，20 年复利差距约两成。
<b>现在两条线只用来决定"跌到这个位置该加投多少"，不产生任何卖出动作。线本身仍只会向上，永不下移。</b></p>

<h2><span class="no">09</span>汇率与资金</h2>
<table>
  <tr><th>项目</th><th class="n">数值</th><th>说明</th></tr>
  <tr style="background:#EFF8F1"><td><b>上月入金（10 月）</b></td><td class="n"><b>$332.41</b></td><td><span class="tag g">已于 01/03 到账</span> 已计入现金 —— 比原计划（≈$200）多 $132。<b>11 月那笔还没到</b></td></tr>
  <tr><td>美元 / 人民币</td><td class="n"><b>{USD_C}</b></td><td>ECB 参考汇率（{_FXD}）—— 下月 ¥1,500–2,000 ≈ <b>{mo(1500/USDC) if USDC else "—"} ~ {mo(2000/USDC) if USDC else "—"}</b></td></tr>
  <tr><td>美元 / 港币</td><td class="n"><b>{USD_H}</b></td><td>入金到账后按此折算，是本账户"人民币 → 美股"的换算口径</td></tr>
  <tr><td><b>当前现金</b></td><td class="n"><b>{mo(CASH)}</b></td><td>占 {CASH/NAV*100:.2f}% —— <b>下限 {T_CASH:.0f}%（{mo(NAV*T_CASH_F)}），实操 {T_CASH:.0f}–{T_CASH_HI:.0f}%</b>（见下）</td></tr>
  <tr><td>├ 现金下限 {T_CASH:.0f}%</td><td class="n">{mo(NAV*T_CASH_F)}</td><td><b>10% → {T_CASH:.0f}%（2026-11-17 生效）</b>：SPYM 一手零头 ≈3.9% + 全年手续费 ≈0.9% + 应急 ≈1.2%；<b>原「回调档弹药 4%」已废止</b></td></tr>
  <tr><td>├ 为什么不再留 10%</td><td class="n">—</td><td>即使每次都完美抄在 −25% 底部，10% 现金 20 年仍净亏约 <b>$7.5 万</b>（拖累 −9.99 万 vs 抄底收益 +2.53 万）；且原来那条「跌 10% 减半仓」本身已在 2026-11-17 废止</td></tr>
  <tr><td>├ 手续费预留</td><td class="n">$20.00</td><td>约 10 笔 × $1.88</td></tr>
  <tr style="background:#EFF8F1"><td><b>└ 可自由动用</b></td><td class="n"><b>{mo(max(0, CASH - NAV*T_CASH_F - _inflight_amt))}</b></td><td><b>这就是今天要投出去的钱</b> —— 现金 {mo(CASH)} − {T_CASH:.0f}% 下限 {mo(NAV*T_CASH_F)} − 在途 {mo(_inflight_amt)}</td></tr>
  <tr><td>└ 市况开关读数</td><td class="n">横盘偏强</td><td>SPYM 自 9/30 {pc(MTD_SPYM)} → <b>名义可投 $260</b>（跌≥3% 投 $390 / 创新高投 $130）—— 11 月入金到账后按此决定投多少</td></tr>
  <tr style="background:#EFF8F1"><td><b>└ 两轮部署</b></td><td class="n"><b>{mo(81.90+160.00+32+RB_FILL)}</b></td><td><b>全部落地</b>：01/03 轮 {mo(81.90+160.00+32)}（SPYM 10 股 + VXUS 0.3855 股）、{RB['date']} 轮 {mo(RB_FILL)}（SPYM {RB['qty']} 股）→ <b>现金 30.4% → {CASH/NAV*100:.1f}%，SPYM {MV['SPYM']/NAV*100:.0f}%</b></td></tr>
  <tr><td>└ 下次入金</td><td class="n">11 月</td><td>¥1,500–2,000 ≈ {mo(1500/USDC) if USDC else "—"}–{mo(2000/USDC) if USDC else "—"} —— 届时按市况开关决定投多少</td></tr>
  <tr><td>└ 加码刻度（原回撤闸门）</td><td class="n"><b>{mo(DD10)}</b></td><td><b>高水位 {mo(HWM)} × 0.9</b>（{HWM_SAVED_DATE} 起自动上移）—— <b>跌到这里不是卖，是加投</b>；当前距 {abs(DD10/NAV-1)*100:.1f}%</td></tr>

</table>
<p class="cap"><b>停泊收益 vs 省手续费，别搞错重点：</b>现金躺一年少赚约 $13（且根本拿不到 —— 货基门槛 HK$50,000 ≈ $6,313，你还差 {mo(6313-NAV)}）；而把定投改成智能定投，一年省 <b>$22.56</b>。<b>该花力气的是省手续费，不是赚利息。</b></p>

<h2><span class="no">10</span>复盘存档 · 不随行情变动的结论</h2>
<ol>
  <li style="background:#FDF3F2"><b>🔴 第一优先：02/20 那次止损 —— "止损"这两个字第一次真正用了，复盘它比复盘行情重要</b><br>
    <b>事实链：</b>10/19 建仓时定下 $25 → 01/06 财报前我算过"最差情形 METU -22.7%，10 股亏 $36.6" → 02/20 盘中跌破 $25，<b>以 {mo(METU_EXIT)} 成交</b>。<br>
    <b>三个值得记住的点：</b><br>
    <b>① 触发价不是成交价。</b>$25 是触发线，实际成交 {mo(METU_EXIT)}（当日最低 {mo(LLO['METU'])}），<b>多付 {mo((25-METU_EXIT)*6)}</b>。<b>"止损保护你"是有成本的，不是精确的。</b><br>
    <b>② 财报不是变量，"身份"才是。</b>META 营收 646 亿 +26%、Q4 指引超预期 —— <b>全超，FCF 首次转负 -21.2 亿，股价 02/20 {pc(LCHG.get('META') or _lcp('META'))}</b>。
    市场卖的不是"财报差"，是"资本开支执行者"这个身份。<b>买 2 倍单股 ETF，本质是买那个身份，不是买那一季报表。</b><br>
    <b>③ 一个 {6*LV['METU']/NAV_PREV*100:.1f}% 的仓位，造成了这次亏损的绝大部分。</b>杠杆产品要么占很小，要么不要。<b>"只减不加"这条规则昨天证明了它的价值：你没有在下跌里摊平。</b><br>
    <span class="small"><b>下一步不是"要不要抄底 METU"，而是"METU 这个槽位以后还要不要"。</b>这个问题留到 11 月入金前再答 —— 现在没有杠杆的账户，正好可以从容地想。</span></li>

  <li><b>第二优先：财报季是"AI 信仰"的连续验证，而不是孤立事件</b><br>
    <b>01/06 特斯拉 + Meta → 02/20 英特尔 + 西部数据 → 10/30 微软 / Alphabet / 亚马逊 → 11/2 苹果。</b><br>
    <span class="small"><b>看点不是单家公司的好坏，而是"资本开支口径"能不能被四巨头互相印证。</b>META 已经把 2027 年的支出承诺从 7,000 亿抬到 <b>8,250 亿</b>，
    且用"发无投票权 C 类股"来为融资留口子。<b>如果微软/亚马逊 10/30 说同样的话，"AI 融资瓶颈"这条叙事会被重新定价 —— 向上还是向下，取决于市场那一刻更相信"需求"还是更相信"现金流"。</b>上周的答案是后者。</span></li>

  <li style="background:#FFF8E6"><b>🔴 第三优先：12 月加息已被定价 77.3% —— 这是"分母"层面最硬的一条约束</b><br>
    CME FedWatch：<b>10 月不动 83.9% / 加息 25bp 16.1%</b>；<b>12 月加息 25bp 77.3% / 加息 50bp 14.5% / 不动仅 8.2%</b>。9 月纪要：多数官员认为年内还需再加。<br>
    <span class="small"><b>为什么这条对你比对别人更重要：</b>你的组合里 2 倍杠杆已经降到 <b>0%（METU 清仓）</b>。
    但即使不加杠杆，<b>长久期资产（成长股、纳指、生物科技）也会被 10Y 5% 附近的天花板压住估值</b>。
    <b>在"市场在赌加息"的环境里，加仓的正确方向是短久期，不是长久期 —— 这不需要预测对错，只需要承认赔率变了。</b>
    所以「2 倍杠杆合计 ≤15%、只减不加」这条铁律，在 12 月之前不要放松。</span></li>

  <li style="background:#f6f8fa"><b>✅ 一条值得长期记住的规律：利率现在由"信用风险"定价，不由通胀数据定价</b><br>
    <b>9 月 CPI +3.7% 同比</b>（预期 3.6%）、核心 +2.6%；<b>9 月 PPI +6.3% 同比</b>（预期 5.3%）—— 2022 年 12 月以来最大年增幅。<br>
    但从 CPI 后的峰值 5.314% → 10/16 收 5.098% → <b>02/20 一天 -17bp 到 4.972%（3 月以来最大单日降幅）→ 现在 {_cn("US10Y", 3)}%</b>。<br>
    <span class="small"><b>读法：两份通胀报告都差，利率却回落 —— 说明当前利率更多由"避险需求 / 信用风险"驱动。</b>
    02/20 这条规律又验证了一次：<b>油价大涨（布伦特 +4% 到 $113）+ 股票大跌 → 资金反而买国债</b>。这不是通胀交易，是信用交易。</span></li>

  <li><b>✅ 已完成：RAM 清仓（01/15，$18.00，10 股）—— 复盘它的"两头"</b><br>
    01/14 收盘 $19.20 已低于 $19.00 阈值，01/15 成交 $18.00。<b>这是执行，不是决策 —— 规则闭环已走完。</b><br>
    上周五它 <b>-11.88% 至 $7.94</b>（当时看像"卖对了"），现在{PHASE} <b>{pc(RAM_NOW_CHG)} 至 {mo(RAM_NOW)}</b>（现在看又像"卖早了"）。<br>
    <span class="small"><b>两个方向都别下结论：卖价 $18.00 至今仍高出 {abs(18.00/RAM_NOW-1)*100:.1f}%。</b>
    真正的收获不是"逃过一次下跌"，而是<b>"清仓后就把它从决策范围里删掉"</b> —— 否则今天这个反弹会逼你做一个新决定，而那个决定本来不在你的策略里。</span></li>

  <li><b>把"盘前/盘中不写结论"这条写进流程</b><br>
    10/16 我在盘中写"结构性换仓"，收盘被推翻。<b>现在规则是：盘前/盘中只写"观察 + 待验证的三个问题"，结论一律等收盘版。</b><br>
    <span class="small">这也解决了一个实际问题：你人在国内，看盘时正好是美国深夜。<b>与其在深夜下判断，不如让盘前的我做"观察"，让收盘后的我做"结论" —— 分工清楚，两边都不越界。</b></span></li>

  <li><span class="tag">[需你确认]</span> <b>去 App 建一个"智能定投"计划试试</b>（哪怕设 $100 后暂停），确认它是否也受 HK$50,000 门槛限制。这是当前唯一能立刻省下真金白银的动作：<b>一年 $22.56，约占净值 {22.56/NAV*100:.2f}%。</b></li>
</ol>

<h2><span class="no">11</span>开新仓候选池（六道关卡实时复算）</h2>
<p class="h2sub">扫描 <b>{len(_NP)}</b> 个流动标的（11 个 SPDR + 8 个宽度/防守代理 + 19 个大盘股 + 3 个基准），
<b>扫描时点 {_NP_ASOF or '—'}</b>（不是本节的实时价；要看实时请点行名看 K 线），
<b>六道关卡逐条否决</b>。<b>完整推演、买点、止损与仓位算法在单独文档 <a href="https://www.workbuddy.cn/space/d/kaPK8ddyDnN9Wp35GPpmcK">《开新仓方案》</a>，本节只给结果。</b></p>

<div class="say"><b>先说规则，再看表 —— 六道关卡按"现实里的否决顺序"排列：</b><br>
<b>① 流动性</b> 20 日均美元成交额 ≥ $20M（买得进也卖得出）；<br>
<b>② 趋势</b> 价 &gt; MA200 且 MA50 &gt; MA200（<b>只在长期向上的资产里买</b>，不接左侧）；<br>
<b>③ 买点</b> 距 52 周高 <b>-35% ~ -3%</b>（不追新高）、距 MA20 <b>≥ -10%</b>（不接飞刀）；<br>
<b>④ 波动</b> ATR14 ≤ 3.5%（<b>$1.88/笔的小资金吃不起高波动</b>）；<br>
<b>⑤ 不重叠</b> 与 SPY 日收益相关性 ≤ 0.90（<b>重复下注不加分</b>）；<br>
<b>⑥ 装得下</b> 1 股 ≤ 允许仓位（净值×2% ÷ 现实止损，现实止损 = 2.5×ATR）。<b>2026-11-17 起废除原「1 股 ≤ 净值 25%」</b>—— 那是把"账户小"当理由，不是把"风险预算"当理由。<br>
<span class="small">这六条不是我新造的，全部从你现有的规则里推出来的：<b>"跟涨不跟跌"+"上限×1.05"就是③，"单笔=净值2%÷止损"就是④⑥，"核心三仓已无替代价值"就是⑤。</b></span></div>

{NEWPOS_HTML}

<div class="warn"><b>📌 日报口径的结论（完整版见 <a href="https://www.workbuddy.cn/space/d/kaPK8ddyDnN9Wp35GPpmcK">《开新仓方案》</a>）：</b><br>
<b>① 今天不开新仓。</b>可自由动用 {mo(max(0, CASH - NAV*T_CASH_F))} ——
一笔 $300 时往返费只有 {FEE*2/300*100:.2f}%、{FEE*2*365/(300*0.10):.0f} 天回本；
<b>但按现在的现金只能开 {mo(max(0, CASH - NAV*T_CASH_F))}，往返费升到 {FEE*2/max(1,CASH - NAV*0.12 - 90 - 20)*100:.2f}%、要 {FEE*2*365/max(1,CASH - NAV*0.12 - 90 - 20)/0.10:.0f} 天</b>。<br>
<b>② 过关的 8 个里，有两个"结构上真该买"：XLP 与 XLF。</b><b>XLP（必需消费）今天是第一次进入合规区</b>，
与 SPY 相关性 <b>-0.27</b>、ATR 1.50% —— <b>这是组合里唯一"防御 + 负相关"同时成立的资产</b>。
其余 6 个要么重复（IGV 与 QQQM 同源、EWJ 与 VXUS 相关 0.74、IWM 与 SPY 相关 0.76），
要么<b>你已经持有</b>（VXUS 占 10.1%，正好是目标 —— <b>过关 ≠ 该买</b>），要么单价太高（TSM 一股约 24%）、要么是长久期（XBI）。<br>
<b>③ 唯一还在"贴着 52 周高"的是能源（XLE 只差不到 1% 就到解禁价 $67.76）。</b>所以不是"不买"，是<b>"等它跌到规则允许的价位再买"</b> —— 上表已经写清每只的规则解禁价。<br>
<b>④ 执行时间表：10/28 FOMC 之后 + 11 月入金到账</b>，两件事都满足才动手；首笔目标 $300 左右（往返费 1.25%，回本降到 46 天）。</div>

{BT_BLOCK}

{verify_block()}

<p class="foot">
<b>数据来源：</b>价格与历史收盘价来自 stockanalysis.com（底层 S&amp;P Global Market Intelligence），<b>行情时点 {lq("SPYM").get("as_of") or LVASOF}</b>（{D_CUR} {PHASE}）；<b>利率 / VIX / 指数来自 CNBC restQuote 接口（免密钥）</b>；K 线与交叉校验来自 CBOE 官方行情；均线为近 20 个交易日自行计算；汇率来自 <code>frankfurter.dev</code>（ECB 参考汇率）。<br>
<b>10/19 收盘数据来源：</b>stockanalysis.com（收盘价，已与 CBOE 官方收盘逐一交叉验证 6/6 完全一致）｜CNBC restQuote（指数/利率/VIX，10/19 收盘）｜CBOE 官方历史行情（K 线）。
<b>指数涨跌由 10/16 与 10/19 两个官方收盘价自行计算</b>（CNBC 的 <code>chg_pct</code> 在收盘后常返回 0，不可直接引用）。<br>
<b>{D_CUR} {PHASE}数据来源：</b>stockanalysis.com（<code>quote</code> 对象取 <b>上一交易日官方收盘 + 当前时段报价</b>（盘中 <code>p</code> / 盘前盘后 <code>ep</code>）；历史日线走 <code>/api/symbol/e|s/&lt;SYM&gt;/history?range=2Y</code>）｜
CNBC restQuote（利率 / VIX / 指数点位）｜<b>指数与行业涨跌一律用 ETF 代理自算</b>（<code>sector_data.json</code>，每次链路重抓，含 DIA/SPY/QQQ/IWM/SMH/SOXX/XLP/XLU/XLV/XLE/TLT 等 22 个），因为 CNBC 的 <code>chg_pct</code> 在收盘后稳定返回 0｜
CBOE 官方历史行情（K 线）｜FRED CSV（长周期宏观）｜美国财政部收益率曲线｜<code>frankfurter.dev</code>（ECB 参考汇率）｜候选池扫描 <code>newpos_scan.py</code> → <code>newpos_scan.json</code>。<br>
<b>{D_CUR} 盘前消息来源：</b>CNBC Daily Open《AI spending fears haunt markets before Halloween》（META FCF -$21.2 亿、capex 1,350–1,450 亿、10Y -17bp 至 4.972%、布伦特 +4% 至 $113.17、WTI +2.4% 至 $99.47、美以拟中期选举后再度打击伊朗）；
路透 / The Straits Times《S&P 500, Nasdaq end sharply lower as revived AI doubts ignite tech selloff》（纳指 -2.58%、费半自 6/22 纪录高点回落约四分之一、初请近数十年低位、"市场已定价年内再加息一次"）；
第一财经《纳指重挫近 700 点！Meta 跌近 7%，芯片股遭抛售》（道指 +0.02%、纳指 -2.60%、标普 -1.21%、费半 -6.72%、AMZN -4.57%、NVDA -4.40%、AMD -8.10%、INTC -8.04%、TSM -4.52%、SpaceX -9.14%）；
澎湃新闻《Meta AI 支出暴增、美股科技股再遭抛售》（未来支出承诺 7,000 亿 → 8,250 亿、5 年期 CDS 创纪录、无投票权 C 类股 2027 年 1 月生效、CoreWeave 跌超 10%、Ken Griffin"几乎肯定已形成资本泡沫"、Larry Fink 警示数据中心延迟）；
FOMC 日历（federalreserve.gov：10/27–28 会议、10/28 14:00 ET 声明、无 SEP；12/8–9 会议）。<br>
<b>02/20 盘中消息来源：</b>每日经济新闻《纳指跌超 1%，AI 硬件股全线下挫》（开盘读数：道指 -0.44%、标普 -0.91%、纳指 -1.55%，费半日内 -4%，AMD -5.56%、泛林 -10.03%、英特尔 -6.03%、应用材料 -5.85%）；同花顺《美股三大指数集体低开》（META -4.55%、TSLA -3.07%、SPCX -4.94%）；凤凰网财经《存储芯片板块重挫》（闪迪、SK 海力士跌超 4%，初请 19.4 万 vs 预期 20 万）；新浪财经 / 环球市场播报《开盘：纳指跌 1.5% AI 担忧升温》（<b>黄仁勋"资本供给紧张"、富国银行"再度点燃信贷端忧虑"、摩根大通"没有足够做多共识与增量资金"</b>、10Y -9bp 至 5.048%、布伦特破 $111）；钜亨网《AI 云端股跌不停！黄仁勋喊需求爆表 为何市场仍不买单》；智通财经《Nebius 获花旗 35 亿美元贷款》（364 天、SOFR+2.75%）。<br>
<b>01/06 财报与盘前消息来源：</b>工商时报《Meta 营收狂飙 26% 也挡不住 AI 烧钱！自由现金流上市首日…上市以来首度转负 资本支出下限再加码》（营收 646 亿 / Q4 指引 730–760 亿 / EPS 3.83 与调整后 7.40 / 98.9 亿法律和解 / 全年 capex 1,350–1,450 亿 / FCF -22 亿）；
上海证券报《现金流转负 Meta 仍坚持加码 AI》（capex 349.1 亿 vs 经营现金流 327.9 亿 / FCF -21.2 亿 / C 类股 2027 年 1 月生效 / Muse 六周 1,000 万用户、约 25% 日活）；
腾讯新闻《Meta 的 Q3：一份"暴利"财报下的三重撕裂》（长期债务 587.44 亿 → 809.22 亿 / 成本费用 +68% 至 516.1 亿 / 营业利润率 40%→20% / 63 位分析师平均目标价约 $818 / 高盛 $860、BMO $580 持有）；
中国证券网《特斯拉，盘后跌超 3%》（营收 280.46 亿 -0.2% / 净利 7.87 亿 -43% / 调整后 EPS 0.48 / FCF +15.3 亿 / capex 47.3 亿 vs 预期 79.5 亿）；
证券时报《"黑色星期四"，两大原因曝光》（纳指期货 -1.81% / KOSPI -3.52% / 日经 -2.65% / 沪指 -1.48% / SK 海力士 -5%+ / 三星 -4% / 巴克莱与 KCM Trade 评论 / 摩根大通"担忧过度"）；
中新经纬《美股盘前速递》（WTI +2.66% $99.74 / 布伦特 +2.91% $111.99 / 伦敦金 -1.16% / 白银 -3.47% / 光通信股普跌）；
搜狐财经（美以计划中期选举后再打伊朗、伊朗革命卫队霍尔木兹表态、30Y 美债 -3bp、比特币 -2%）。<br>新浪财经《奈飞今晚放榜：广告收入冲击 30 亿美元，美国用户流失率却同比涨了 16.6%》（财报前预期）；Lumida / Bloomberg Intelligence《Netflix Trades at 19 Times Earnings》（前瞻市盈率不足 19 倍、十年均值 49 倍，内容团队一周内三度换将）；
东方财富 01/03 盘中读数（道指 +0.94%、纳指 +0.18%、标普 +0.36%、费半一度 +0.36%）；腾讯自选股 01/03 收盘快照（道指 52,495.88 / 纳指 26,424.82 / 奈飞 68.69）；<br>
CME FedWatch（10 月不动 83.9% / 12 月加息 25bp 77.3%）；Meta Q3 共识（营收 $632.9 亿、EPS $6.85、FCF -$91.4 亿、资本开支 $409.7 亿；美银计入 $100 亿和解费用后 EPS $3.79）。<br><b>财报日历与预期来源：</b>财游日历《财经日历周报 W43》、腾讯新闻《下周市场大事预告》（10/19–10/25）、财经24小时《财经首班车 10/19》；
<b>Meta Q3 预期</b>来自东方财富《一图展望：Meta Q3 财报来袭》、澎湃新闻《Meta 财报打头阵，AI 巨额投资迎"信心大考"》、凤凰网《财报前瞻：AI 信心危机下首场大考》（营收共识 $632.9 亿 / EPS $6.85 / 自由现金流 -$91.4 亿 / 资本开支 $409.7 亿 / 美银 $100 亿法律费用与 2027 年 $2,050 亿资本开支预测 / Gartner 3.5 万亿减值警告 / 高盛 2027 年超大规模云厂商资本开支 $1.2 万亿、35% 靠发债）。<br>
<b>[STALE / 待核]</b>：所有指数涨跌均用 ETF 代理自算（DIA/SPY/QQQ/IWM/SOXX），点位取 CNBC 官方收盘。<b>[仍需你确认]</b>：智能定投是否也受 HK$50,000 资产门槛限制（这一项一年值 $22.56 ≈ 净值 {22.56/NAV*100:.2f}%）。<br>
<b>⚠️ 本版为 {D_CUR} 美东{PHASE}版（行情时点 {lq("SPYM").get("as_of") or LVASOF}，生成 {time.strftime('%m/%d %H:%M')} 北京）。</b>
持仓成本以券商实际成交为准，非核心仓位的股数保留 4 位小数。第 11 节的候选池来自 <code>newpos_scan.py</code> 当日扫描（{len(_NP)} 个标的），六道关卡在构建时实时复算。<br>
本战报只负责"每日行情与当日动作"，策略配置以《我的美股投资总纲》为准，开新仓的完整推演见《开新仓方案》。<b>盘前数据只作方向参考，不作为结论依据。</b>仅供研究参考，不构成个人投资建议。版本 2026-10-23 <b>盘前 · 止损后第一个交易日</b>（02/20 官方收盘 + 10/23 盘前 ext 价）。
<b>规则变更记录：</b>①01/03 起回撤线改为高水位制（HWM × 0.9 / 0.85，只在<b>已确认的收盘</b>创新高时上移，写入 <code>hwm.json</code>）；
②盘前运行时 <code>price</code> 即为上一交易日的官方收盘，因此<b>盘前也允许上移水位</b>（盘中仍然禁止）；当前高水位 <b>{mo(HWM)}</b>（{HWM_SAVED_DATE}）→ -10% 线 <b>{mo(DD10)}</b>、-15% 线 <b>{mo(DD15)}</b>。</p>
</body>
</html>
'''

# ---------- 注入 K 线弹层（CSS + 弹层模板 + 数据）----------
if KLINE and KLINE_CSS and KLINE_TPL:
    HTML = HTML.replace('</style>', KLINE_CSS + '\n</style>', 1)
    HTML = HTML.replace('</body>',
                        KLINE_TPL.replace('/*KLINE_DATA*/',
                                          json.dumps(KLINE, ensure_ascii=False,
                                                     separators=(',', ':'))) + '\n</body>', 1)

# ---------- 应用设计层 v3 ----------
try:
    import re as _re2
    _m = _re2.search(r'<b style="font-size:15px">(.*?)</b>', HTML, _re2.S)
    _line = _re2.sub(r'<[^>]+>', '', _m.group(1)) if _m else ''
    _line = _re2.sub(r'^[\W_]+', '', _line.strip())
    _line = _re2.sub(r'\s+', ' ', _line).strip()
    HTML = _apply_design(HTML)
    # ⚠️ 换样式时会把之前注入的 K 线弹层 CSS 一并覆盖掉 → 这里补回来
    try:
        HTML = HTML.replace('</style>', KLINE_CSS + '\n</style>', 1)
    except Exception:
        pass
    HTML = _hero(HTML, _line, [
        ('净值', mo(NAV), '%s %+.2f（%+.2f%%）%s' % (NAV_CMP, NAV - NAV_PREV, (NAV / NAV_PREV - 1) * 100, '（含入金 %s）' % mo(DEPOSIT) if DEPOSIT else '')),
        ('杠杆', '%.2f%%' % (LEV / NAV * 100), '上限 15.5%'),
        ('核心三仓', '%.2f%%' % (CORE / NAV * 100), '目标 %.0f%%' % (T_SPYM + T_QQQM + T_VXUS)),
        ('现金', '%.2f%%' % (CASH / NAV * 100), mo(CASH)),
    ])
except Exception as _e:
    print('  [设计层降级]', type(_e).__name__, str(_e)[:70])

_apply_report_defined = True
HTML = _apply_report(HTML)
io.open(BRIEF_OUT, 'w', encoding='utf-8').write(HTML)
print(f'已生成 {BRIEF_OUT}  | 宏观模块 {"V2" if MACRO_V2 else "V1"}  |', len(HTML), 'chars')
print(f'{NAV_LABEL} 净值 {mo(NAV)}  较上一收盘 {NAV-NAV_PREV:+.2f} ({(NAV/NAV_PREV-1)*100:+.2f}%)')
print(f'杠杆 {LEV/NAV*100:.2f}%  核心 {CORE/NAV*100:.2f}%  现金 {CASH/NAV*100:.2f}%')
print(f"RAM 已清仓 @ ${RCL['exit']}（10 股，净亏 {RCL_NET:.2f}，占净值 {RCL_NET/NAV*100:.2f}%）")
print(f"METU {mo(LV['METU'])} ({LVP['METU']:+.2f}%)  距止盈34 {(34/LV['METU']-1)*100:+.2f}%")
print(f'篮子均值 {B_AVG:+.2f}%  RAM 放大 {RAM_AMP:.2f}x')
for k in ('SPYM', 'QQQM', 'VXUS'):
    print(f"  {k} 回调档 {OLD[k]} -> {NEW[k]:.2f} (上限 {CAP[k]})")
