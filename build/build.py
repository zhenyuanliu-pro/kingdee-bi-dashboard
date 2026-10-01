"""
经营驾驶舱：从金蝶云星辰原始快照（pull.js 产出）生成页面数据
用法：python3 build.py <snapshot.json.gz> [输出 design_data.json] [config.json]
- 口径随数据截止日自动变化：本年累计（1 月至截止月）、去年同期、近 3 个月、近 12 个月
- 内置剔除规则、FIFO 账龄校验、净销售与销售汇总表核对；任一校验不通过即报错退出
"""
import gzip, json, re, sys, collections, calendar, datetime as dt

SNAP_PATH = sys.argv[1] if len(sys.argv) > 1 else 'snapshot.json.gz'
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else 'design_data.json'
CFG_PATH = sys.argv[3] if len(sys.argv) > 3 else 'config.json'
SNAP = json.load(gzip.open(SNAP_PATH))
CFG = json.load(open(CFG_PATH, encoding='utf-8'))
RULES = {k: CFG.get('exclusion_rules', {}).get(k, []) for k in ('name_exact', 'name_contains', 'customer_group')}
LEDGERS = [a['name'] for a in CFG['accounts']]
REG = CFG.get('regions', {})
REG_KW, REG_MAP, RG_ORDER = REG.get('keywords', []), REG.get('map', []), REG.get('order', [])

meta = SNAP.get('meta', {})
AS_OF = dt.date.fromisoformat(meta['as_of'])
CY, CM = AS_OF.year, AS_OF.month
EPOCH = dt.date.fromisoformat(CFG.get('start_date', '2024-01-01'))
ym = lambda y, m: f'{y}-{m:02d}'
MONTHS = []
y, m = EPOCH.year, EPOCH.month
while (y, m) <= (CY, CM):
    MONTHS.append(ym(y, m)); m += 1
    if m == 13: y, m = y + 1, 1
MI = {k: i for i, k in enumerate(MONTHS)}
d = dt.date.fromisoformat
day = lambda s: (d(s) - EPOCH).days
dstr = lambda n: (EPOCH + dt.timedelta(days=n)).isoformat()
ASOFD = day(AS_OF.isoformat())
R2 = lambda v: round(v, 2)
YTD, PY = (ym(CY, 1), ym(CY, CM)), (ym(CY - 1, 1), ym(CY - 1, CM))
LAST3 = MONTHS[-3:]; LAST3_DAYS = sum(calendar.monthrange(int(x[:4]), int(x[5:]))[1] for x in LAST3); LAST3_DAYS -= calendar.monthrange(CY, CM)[1] - AS_OF.day
T12 = (MONTHS[max(0, len(MONTHS) - 12)], MONTHS[-1])
inR = lambda mi, r: r[0] in MI and r[1] in MI and MI[r[0]] <= mi <= MI[r[1]] if r[0] in MI else (r[1] in MI and mi <= MI[r[1]] and MONTHS[mi] >= r[0])
mtxt = lambda a, b: f'{a} 月' if a == b else f'{a}–{b} 月'
LB = {'ytd': f'{CY} 年 {mtxt(1, CM)}', 'py': f'{CY - 1} 年同期', 'y': [str(CY - 2), str(CY - 1), str(CY)], 'cy': str(CY),
      'yCol': [str(CY - 2), str(CY - 1), f'{CY} {mtxt(1, CM)}'.replace(' 月', '月')], 'monthEnd': f'{CM} 月末', 'cm': CM,
      'last3': mtxt(int(LAST3[0][5:]), CM) if LAST3[0][:4] == str(CY) else '近 3 个月', 'dsoDays': LAST3_DAYS, 'newFrom': f'{CY} 年'}


def excl(name, group):
    name, group = (name or '').strip(), (group or '').strip()
    if name in RULES['name_exact']: return '集团内部公司'
    for k in RULES['name_contains']:
        if k in name: return k
    if group in RULES['customer_group']: return '客户分类:' + group
    return None


def region_of(path, leaf):
    """按部门全路径归到大区：先匹配 keywords（如 北区/东区），再匹配 map 中的 [关键词, 显示名]，都不中则用末级部门名"""
    for k in REG_KW:
        if k in path: return k
    for k, v in REG_MAP:
        if k in path: return v
    return leaf or '未分配部门'


def due_date(bill_date, term):
    bd = d(bill_date); m_ = re.match(r'^(\d+)天$', term or '')
    if m_: return bd + dt.timedelta(days=int(m_.group(1)))
    if term and '20号' in term:
        return bd.replace(day=20) if bd.day <= 5 else (bd.replace(day=1) + dt.timedelta(days=32)).replace(day=20)
    return bd


def mk_index():
    idx, arr = {}, []
    def f(v):
        if v not in idx: idx[v] = len(arr); arr.append(v)
        return idx[v]
    return f, arr


cust_idx, C = {}, []
def cid(name, channel):
    k = name.strip()
    if k not in cust_idx:
        cust_idx[k] = len(C); C.append({'n': k, 'ch': channel or '未分类', 'rep': '', 'tm': '', 'term': '', 'lim': 0})
    return cust_idx[k]
rep_i, REPS = mk_index(); ch_i, CH = mk_index(); team_i, TEAMS = mk_index(); prod_i, PRODS = mk_index(); per_i, PERS = mk_index()
checks, excluded = {}, collections.Counter()
SALES = collections.defaultdict(float); buy = collections.defaultdict(set)
COLL = collections.defaultdict(float); BAL = collections.defaultdict(float)
LOTS, PS, INV = [], [], []

for li, co in enumerate(LEDGERS):
    L = SNAP['ledgers'][co]
    CU = {c[0]: c for c in L['customers']}
    DEP = {x[0]: x for x in L['dept']}; DEPN = {}
    for x in L['dept']: DEPN.setdefault(x[1], x)
    team = lambda dp: team_i(('未分配部门', '未分配部门')) if not dp else team_i((dp[1], region_of(dp[2], dp[1])))
    def ok(cust_id):
        c = CU.get(cust_id)
        if c is None: raise SystemExit(f'{co} 出现未知客户 {cust_id}，请检查快照')
        e = excl(c[2], c[3]); return (None, e) if e else (cid(c[2], c[3]), None)
    for b in L['so']:
        if b[6] != 'C' or b[0][:7] not in MI: continue
        c, e = ok(b[2])
        if e: excluded[(co, e, b[0][:4])] += b[4]; continue
        SALES[(MI[b[0][:7]], li, c, rep_i(b[3] or '未指定'), team(DEP.get(b[7].split('|')[0]) if b[7] else None), ch_i(CU[b[2]][3] or '未分类'))] += b[4]
        buy[c].add(day(b[0]))
    for r in L['si']:
        if r[5] != 'C' or r[0][:7] not in MI: continue
        c, e = ok(r[2])
        if e: excluded[(co, e + '(退货)', r[0][:4])] -= r[4]; continue
        SALES[(MI[r[0][:7]], li, c, rep_i(r[3] or '未指定'), team(DEPN.get(r[6])), ch_i(CU[r[2]][3] or '未分类'))] -= r[4]
    for ymk, cust_id, rp, bal, pre in L['arm']:
        c, e = ok(cust_id)
        if e or ymk not in MI: continue
        COLL[(MI[ymk], li, c)] += rp; BAL[(MI[ymk], li)] += bal
    ar_tot = sum(x[1] for x in L['ar0930'] if not ok(x[0])[1]); lot_tot = 0.0
    for cust_id, lots in L['lots'].items():
        c, e = ok(cust_id)
        if e: continue
        det = L['cdet'].get(cust_id, ['', 0, '', '', '']); cc = C[c]
        cc['term'] = cc['term'] or det[0]; cc['lim'] = max(cc['lim'], det[1] or 0); cc['rep'] = cc['rep'] or det[2]
        if det[3] and not cc['tm']:
            dp = DEPN.get(det[3]); cc['tm'] = det[3]; cc['rg'] = region_of(dp[2], det[3]) if dp else det[3]
        for bd, bno, amt in lots:
            bd = '2024-01-01' if bd == '期初' else bd
            LOTS.append([li, c, day(bd), round(amt, 2), (due_date(bd, det[0]) - EPOCH).days]); lot_tot += amt
    checks[f'{co} 账龄明细合计 vs 应收余额表'] = [round(lot_tot, 2), round(ar_tot, 2)]
    for r in L['ps']:
        s, e_, cust_id, _, mno, mname, cat, qty, unit, amt = r
        c, e = ok(cust_id)
        if e: continue
        PS.append([per_i((s, e_)), li, c, prod_i((mno, mname, cat or '未分类', unit)), qty, amt])
    for mid, mno, mname, model, stock, batch, kf, valid, qty in L['inv']:
        INV.append([li, mno, mname, model, stock, batch, kf, valid, qty])

# ---------- 校验 ----------
ytd = collections.defaultdict(float)
for k, v in SALES.items():
    if MONTHS[k[0]] >= YTD[0]: ytd[LEDGERS[k[1]]] += v
psy = collections.defaultdict(float)
for p, li, c, pr, q, a in PS:
    if PERS[p][0][:4] == str(CY): psy[LEDGERS[li]] += a
checks['本年净销售（剔除后）'] = {k: round(v, 2) for k, v in ytd.items()}
checks['本年销售汇总表合计（剔除后）'] = {k: round(psy.get(k, 0), 2) for k in ytd}
checks['剔除金额'] = {f'{k[0]}|{k[1]}|{k[2]}': round(v, 2) for k, v in sorted(excluded.items())}
bad = [k for k, v in checks.items() if '账龄' in k and abs(v[0] - v[1]) > 0.01] + [k for k in ytd if abs(ytd[k] - psy.get(k, 0)) > 1]
if bad: raise SystemExit('校验不通过：' + '；'.join(bad) + '\n' + json.dumps(checks, ensure_ascii=False, indent=1))

cs = collections.defaultdict(lambda: collections.defaultdict(float))
for k, v in SALES.items(): cs[k[2]][k[5]] += v * (3 if MONTHS[k[0]] >= YTD[0] else 1)
for c, m_ in cs.items(): C[c]['ch'] = CH[max(m_, key=m_.get)]
for c, days in buy.items():
    ds = sorted(days); C[c].update(fd=ds[0], ld=ds[-1], nb=len(ds), gap=round((ds[-1] - ds[0]) / (len(ds) - 1), 1) if len(ds) > 1 else None)

# ---------- 视图 ----------
lbl = lambda t: TEAMS[t][0] if TEAMS[t][0] == TEAMS[t][1] else TEAMS[t][1] + '·' + TEAMS[t][0]
AGE = [30, 60, 90, 180, 365, 10**9]; ODB = [0, 30, 90, 180, 10**9]
ageI = lambda a: next(i for i, x in enumerate(AGE) if a <= x)
odI = lambda o: 0 if o <= 0 else next(i for i in range(1, 5) if o <= ODB[i])
rng = lambda mi, r: MI.get(r[0], -1) <= mi <= MI.get(r[1], -1) if r[0] in MI else (mi <= MI.get(r[1], -1))
PER_SORTED = sorted(range(len(PERS)), key=lambda i: PERS[i][0])
CUR_P = [i for i in PER_SORTED if PERS[i][0][:4] == str(CY)]
PREV_P = [i for i in PER_SORTED if PERS[i][0] < (PERS[CUR_P[0]][0] if CUR_P else '9999')][-1:] if CUR_P else []
SHOW_P = PER_SORTED[-4:]
mcount = lambda p: (int(PERS[p][1][:4]) - int(PERS[p][0][:4])) * 12 + int(PERS[p][1][5:7]) - int(PERS[p][0][5:7]) + 1
def plabel(p):
    s, e = PERS[p]
    if s[5:7] == '01' and e[5:7] == '06': return f'{s[:4]} 上半年'
    if s[5:7] == '07' and e[5:7] == '12': return f'{s[:4]} 下半年'
    return f'{s[:4]} 年 {mtxt(int(s[5:7]), int(e[5:7]))}'
LB['prevHalf'] = plabel(PREV_P[0]) if PREV_P else '上一时段'


def variant(Lx):
    inL = lambda li: Lx is None or li == Lx
    S = [(*k, v) for k, v in SALES.items() if inL(k[1])]
    out = {}
    s26 = sum(r[6] for r in S if rng(r[0], YTD)); s25 = sum(r[6] for r in S if rng(r[0], PY))
    c26 = sum(v for k, v in COLL.items() if inL(k[1]) and rng(k[0], YTD)); c25 = sum(v for k, v in COLL.items() if inL(k[1]) and rng(k[0], PY))
    bal = collections.defaultdict(float)
    for k, v in BAL.items():
        if inL(k[1]): bal[k[0]] += v
    ar0 = bal.get(MI.get(ym(CY - 1, 12), -1), 0); ar = bal[len(MONTHS) - 1]
    q3 = sum(r[6] for r in S if MONTHS[r[0]] in LAST3)
    bc26, bc25 = collections.defaultdict(float), collections.defaultdict(float)
    byM, cm = collections.defaultdict(float), collections.defaultdict(float)
    for r in S:
        byM[MONTHS[r[0]]] += r[6]
        if rng(r[0], YTD): bc26[r[2]] += r[6]
        elif rng(r[0], PY): bc25[r[2]] += r[6]
    for k, v in COLL.items():
        if inL(k[1]): cm[MONTHS[k[0]]] += v
    trend = [[R2(byM.get(ym(yy, mm), 0)) if ym(yy, mm) in MI else None for mm in range(1, 13)] for yy in (CY - 2, CY - 1, CY)]
    ch26, ch25, t26, t25, rp = collections.defaultdict(float), collections.defaultdict(float), collections.defaultdict(float), collections.defaultdict(float), {}
    for r in S:
        cur = rng(r[0], YTD); prv = rng(r[0], PY)
        if not (cur or prv): continue
        o = rp.setdefault(r[3], {'s26': 0, 's25': 0, 'cs': set(), 'tm': collections.defaultdict(float)})
        if cur:
            ch26[CH[r[5]]] += r[6]; t26[r[4]] += r[6]; o['s26'] += r[6]; o['tm'][r[4]] += r[6]
            if r[6] > 0: o['cs'].add(r[2])
        else:
            ch25[CH[r[5]]] += r[6]; t25[r[4]] += r[6]; o['s25'] += r[6]
    tids = sorted([t for t in set(t26) | set(t25) if abs(t26[t]) + abs(t25[t]) > 1], key=lambda t: ((RG_ORDER.index(TEAMS[t][1]) if TEAMS[t][1] in RG_ORDER else 99), -t26[t]))
    reps = sorted([{'rep': REPS[k], 'team': lbl(max(o['tm'], key=o['tm'].get)) if o['tm'] else '—', 's26': R2(o['s26']), 's25': R2(o['s25']),
                    'yoy': (o['s26'] / o['s25'] - 1) if o['s25'] > 0 else None, 'n': len(o['cs']), 'sh': o['s26'] / s26 if s26 else 0}
                   for k, o in rp.items() if abs(o['s26']) + abs(o['s25']) > 1], key=lambda x: -x['s26'])
    out['ov'] = {'s26': R2(s26), 's25': R2(s25), 'c26': R2(c26), 'c25': R2(c25), 'ar0': R2(ar0), 'ar': R2(ar), 'q3': R2(q3),
                 'n26': sum(1 for v in bc26.values() if v > 0), 'n25': sum(1 for v in bc25.values() if v > 0), 'trend': trend,
                 'm26': [{'m': f'{int(mm[5:])}月', 's': R2(byM.get(mm, 0)), 'c': R2(cm.get(mm, 0))} for mm in MONTHS if mm >= YTD[0]],
                 'channels': sorted([{'n': k, 'v26': R2(ch26[k]), 'v25': R2(ch25[k])} for k in set(ch26) | set(ch25)], key=lambda x: -x['v26']),
                 'teams': [{'n': lbl(t), 'rg': TEAMS[t][1], 'v26': R2(t26[t]), 'v25': R2(t25[t])} for t in tids],
                 'reps': reps[:25], 'balTrend': [R2(bal[i]) for i in range(len(MONTHS))]}
    cu = {}
    for li, c, bd, amt, due in LOTS:
        if not inL(li): continue
        o = cu.setdefault(c, {'pos': 0, 'neg': 0, 'od': 0, 'maxod': 0, 'o180': 0, 'due30': 0, 'first': None, 'mx': [0] * 30, 'wk': [0] * 12, 'led': set()})
        o['led'].add(LEDGERS[li])
        if amt > 0:
            o['pos'] += amt
            if due < ASOFD: o['od'] += amt; o['maxod'] = max(o['maxod'], ASOFD - due)
            if ASOFD - bd > 180: o['o180'] += amt
            if ASOFD <= due <= ASOFD + 30: o['due30'] += amt
            o['first'] = bd if o['first'] is None else min(o['first'], bd)
            o['mx'][ageI(ASOFD - bd) * 5 + odI(ASOFD - due)] += amt
            w = (due - ASOFD - 1) // 7
            if due > ASOFD and 0 <= w < 12: o['wk'][w] += amt
        else: o['neg'] += amt
    arr = []
    for c, o in cu.items():
        cc = C[c]; lim = cc['lim'] if cc['lim'] and cc['lim'] > 1 else None; b = o['pos'] + o['neg']; over = lim is not None and b > lim
        arr.append({'n': cc['n'], 'ch': cc['ch'], 'rep': cc['rep'] or '—', 'tm': cc['tm'] or '—', 'rg': cc.get('rg') or '未分配部门', 'term': cc['term'] or '未设置',
                    'lim': lim, 'bal': R2(b), 'pos': R2(o['pos']), 'neg': R2(o['neg']), 'od': R2(o['od']), 'maxod': o['maxod'] if o['od'] > 0 else None,
                    'o180': R2(o['o180']), 'due30': R2(o['due30']), 'first': dstr(o['first']) if o['first'] is not None else '—', 'over': over,
                    'st': 'cr' if b <= 0 else ('crit' if o['od'] > 0 and o['maxod'] > 90 else 'ser' if o['od'] > 0 else 'warn' if over else 'ok'),
                    'led': '、'.join(sorted(o['led'])), 'mx': [R2(x) for x in o['mx']], 'wk': [R2(x) for x in o['wk']]})
    out['ar'] = sorted(arr, key=lambda x: -x['od'])
    agg = {}
    for r in S:
        o = agg.setdefault(r[2], {'s26': 0, 's25': 0, 't12': 0, 'reps': collections.defaultdict(float), 'm': [0] * CM})
        if rng(r[0], YTD): o['s26'] += r[6]; o['m'][r[0] - MI[YTD[0]]] += r[6]
        elif rng(r[0], PY): o['s25'] += r[6]
        if rng(r[0], T12): o['t12'] += r[6]
        o['reps'][r[3]] += max(r[6], 0)
    arC = collections.defaultdict(lambda: [0, 0])
    for li, c, bd, amt, due in LOTS:
        if inL(li):
            arC[c][0] += amt
            if amt > 0 and due < ASOFD: arC[c][1] += amt
    NEW0 = day(f'{CY}-01-01'); isNew = lambda c: C[c].get('fd') is not None and C[c]['fd'] >= NEW0
    b26 = [c for c, o in agg.items() if o['s26'] > 0]; tot = sum(agg[c]['s26'] for c in b26) or 1
    newC = [c for c in b26 if isNew(c)]
    churn = [c for c in agg if (C[c].get('nb') or 0) >= 3 and C[c].get('gap') and max(1.5 * C[c]['gap'], 45) < ASOFD - C[c]['ld'] <= 365]
    gaps = sorted(C[c]['gap'] for c in agg if (C[c].get('nb') or 0) >= 3 and C[c].get('gap'))
    srt = sorted((agg[c]['s26'] for c in b26), reverse=True)
    nm, om = [0] * CM, [0] * CM
    for c, o in agg.items():
        for i, v in enumerate(o['m']): (nm if isNew(c) else om)[i] += v
    acc, cum = 0, []
    for v in srt[:100]: acc += v; cum.append(round(acc / tot * 100, 1))
    topRep = lambda o: REPS[max(o['reps'], key=o['reps'].get)] if o['reps'] else '—'
    rows = sorted([{'n': C[c]['n'], 'ch': C[c]['ch'], 'rep': topRep(o), 's26': R2(o['s26']), 's25': R2(o['s25']), 'yoy': (o['s26'] / o['s25'] - 1) if o['s25'] > 0 else None,
                    'nb': C[c].get('nb'), 'ld': dstr(C[c]['ld']) if C[c].get('ld') is not None else '—', 'gap': C[c].get('gap'), 'bal': R2(arC[c][0]), 'od': R2(arC[c][1]),
                    'new': isNew(c), 'm': [round(x) for x in o['m']]} for c, o in agg.items() if o['s26'] or o['s25']], key=lambda x: -x['s26'])
    out['cu'] = {'n26': len(b26), 'n25': sum(1 for o in agg.values() if o['s25'] > 0), 'newN': len(newC), 'newS': R2(sum(agg[c]['s26'] for c in newC)), 'tot': R2(tot),
                 'churnN': len(churn), 'churnT12': R2(sum(max(agg[c]['t12'], 0) for c in churn)), 'medGap': gaps[len(gaps) // 2] if gaps else None,
                 'top10': R2(sum(srt[:10])), 'newM': [R2(x) for x in nm], 'oldM': [R2(x) for x in om], 'cum': cum, 'rows': rows[:200],
                 'churn': sorted([{'n': C[c]['n'], 'ch': C[c]['ch'], 'rep': topRep(agg[c]), 'ld': dstr(C[c]['ld']), 'since': ASOFD - C[c]['ld'], 'gap': C[c]['gap'],
                                   'ratio': (ASOFD - C[c]['ld']) / C[c]['gap'], 't12': R2(agg[c]['t12']), 'bal': R2(arC[c][0])} for c in churn], key=lambda x: -x['t12'])}
    pa, cat = {}, collections.defaultdict(lambda: collections.defaultdict(float))
    for p, li, c, pr, q, a in PS:
        if not inL(li): continue
        no, nme, ct, unit = PRODS[pr]
        o = pa.setdefault(no, {'no': no, 'n': nme, 'cat': ct, 'unit': unit, 'per': collections.defaultdict(float), 'q': collections.defaultdict(float)})
        o['per'][p] += a; o['q'][p] += q; cat[ct][p] += a
    curA = lambda o: sum(o['per'][p] for p in CUR_P); curQ = lambda o: sum(o['q'][p] for p in CUR_P)
    ps26 = sum(curA(o) for o in pa.values()) or 1
    prows, price, mq = [], {}, {}
    for o in pa.values():
        a, q = curA(o), curQ(o)
        if q > 0: price[o['no']] = a / q
        mq[o['no']] = q / CM
        if a:
            m25 = o['per'][PREV_P[0]] / mcount(PREV_P[0]) if PREV_P else 0
            prows.append({'no': o['no'], 'n': o['n'], 'cat': o['cat'], 'unit': o['unit'], 'q': R2(q), 'a': R2(a), 'sh': a / ps26, 'avg': a / q if q > 0 else None,
                          'chg': (a / CM / m25 - 1) if m25 > 0 else None})
    prows.sort(key=lambda x: -x['a'])
    inv = [r for r in INV if inL(r[0])]; pq = collections.defaultdict(float)
    for r in inv: pq[r[1]] += r[8]
    irows = []
    for r in inv:
        left = day(r[7]) - ASOFD if r[7] else None
        irows.append({'n': r[2], 'model': r[3], 'stock': r[4], 'batch': r[5] or '—', 'valid': r[7] or '—', 'left': left, 'q': r[8],
                      'val': R2(price[r[1]] * r[8]) if r[1] in price else None, 'cover': round(pq[r[1]] / mq[r[1]], 1) if mq.get(r[1]) else None,
                      'st': 'na' if left is None else 'crit' if left < 0 else 'ser' if left <= 90 else 'warn' if left <= 180 else 'ok'})
    irows.sort(key=lambda x: (x['left'] is None, x['left'] if x['left'] is not None else 0))
    em = collections.defaultdict(float)
    for r in irows:
        if r['valid'] != '—': em['已过期' if r['left'] < 0 else r['valid'][:7]] += r['q']
    ek = sorted(k for k in em if k != '已过期'); ek = (['已过期'] if '已过期' in em else []) + ek
    ip = {}
    for r in irows:
        o = ip.setdefault(r['n'], {'n': r['n'], 'q': 0, 'val': 0, 'near': None, 'cover': r['cover'], 'batches': 0})
        o['q'] += r['q']; o['val'] += r['val'] or 0; o['batches'] += 1
        if r['left'] is not None and (o['near'] is None or r['left'] < o['near']): o['near'] = r['left']
    cats = sorted(cat, key=lambda k: -sum(cat[k][p] for p in CUR_P))
    out['pr'] = {'invProd': [{**x, 'val': R2(x['val'])} for x in sorted(ip.values(), key=lambda x: -x['val'])], 'total': R2(ps26 if pa else 0),
                 'nProd': sum(1 for o in pa.values() if curA(o) > 0),
                 'cats': [{'n': k, 'm': [R2(cat[k][p] / mcount(p)) for p in SHOW_P], 'sh': sum(cat[k][p] for p in CUR_P) / ps26} for k in cats],
                 'rows': prows, 'inv': irows,
                 'exp': [{'k': k, 'q': em[k], 'st': 'crit' if k == '已过期' else ('near' if day(k + '-01') - ASOFD <= 180 else 'ok')} for k in ek]}
    return out


ly = [{'n': l, 'y': [0, 0, 0]} for l in LEDGERS]
for k, v in SALES.items():
    yy = int(MONTHS[k[0]][:4])
    if yy >= CY - 2: ly[k[1]]['y'][yy - (CY - 2)] += v
for x in ly: x['y'] = [R2(v) for v in x['y']]
res = {'asOf': AS_OF.isoformat(), 'pulledAt': meta.get('pulled_at', ''), 'expires': meta.get('expires', {}), 'ledgers': LEDGERS, 'months': MONTHS, 'ledgerYears': ly,
       'lb': LB, 'psPeriods': [plabel(p) for p in SHOW_P], 'rules': RULES, 'checks': checks, 'v': {'all': variant(None)}}
for i, l in enumerate(LEDGERS): res['v'][l] = variant(i)
json.dump(res, open(OUT_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
a = res['v']['all']
print(f"截止 {AS_OF}｜{LB['ytd']}净销售 {a['ov']['s26']:,.2f}｜回款 {a['ov']['c26']:,.2f}｜月末应收 {a['ov']['ar']:,.2f}｜逾期 {sum(x['od'] for x in a['ar']):,.2f}｜成交客户 {a['cu']['n26']}｜流失预警 {a['cu']['churnN']}")
print('校验全部通过；输出', OUT_PATH)
