// 金蝶云星辰（open.jdy.com）多账套数据拉取脚本
// 用法：在已登录或任意打开的 https://open.jdy.com 页面控制台里，先执行
//   window.KD_CONFIG = { ...config.json 的内容... }
// 再执行本脚本。脚本立即返回 'started'，拉数在后台进行，用 window.JOB 查看进度；
// 完成后快照在 window.PACKGZ 中，执行 KD_DOWNLOAD() 下载为 kd_snapshot_<日期>.json.gz。
(() => {
const CFG = window.KD_CONFIG;
if (!CFG || !CFG.client_id || !CFG.client_secret || !Array.isArray(CFG.accounts)) throw new Error('请先设置 window.KD_CONFIG（格式见 config.example.json）');
const CID = String(CFG.client_id), CSEC = CFG.client_secret;
const ACCTS = CFG.accounts.map(a => ({ co: a.name, oid: String(a.outer_instance_id) }));
const RU = CFG.exclusion_rules || {};
const INTERNAL = RU.name_exact || [], EXCL_CONTAINS = RU.name_contains || [], EXCL_GROUP = RU.customer_group || [];
const START = CFG.start_date || '2024-01-01', Y0 = Number(START.slice(0, 4));
const today = new Date(); const pad = n => String(n).padStart(2, '0');
const END = today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate());
const n = x => Number(x || 0);
const enc = s => encodeURIComponent(s).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
async function hmacHexB64(key, msg) {
  const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(msg)));
  return btoa(Array.from(sig).map(b => b.toString(16).padStart(2, '0')).join(''));
}
// 业务接口的 CORS 头重复，普通 fetch 会被浏览器拦截；用 null 源的沙箱 iframe 转发即可
const ifr = document.createElement('iframe'); ifr.sandbox = 'allow-scripts'; ifr.style.display = 'none';
ifr.srcdoc = `<script>window.addEventListener('message',async e=>{const m=e.data;try{const r=await fetch(m.url,{method:m.method,headers:m.headers,body:m.body});parent.postMessage({id:m.id,status:r.status,text:await r.text()},'*')}catch(err){parent.postMessage({id:m.id,error:err.message},'*')}});parent.postMessage({ready:true},'*');<\/script>`;
const pending = {}; let seq = 0;
window.addEventListener('message', e => { const d = e.data; if (d && d.id && pending[d.id]) { pending[d.id](d); delete pending[d.id]; } });
const ready = new Promise(res => { const h = e => { if (e.data && e.data.ready) { window.removeEventListener('message', h); res(); } }; window.addEventListener('message', h); });
document.body.appendChild(ifr);
const relay = (url, opt) => new Promise(res => { const id = 'r' + (++seq); pending[id] = res; ifr.contentWindow.postMessage({ id, url, ...opt }, '*'); });
async function call(method, url, params = {}, extra = {}) {
  const u = new URL(url); const keys = Object.keys(params).sort();
  const qs1 = keys.map(k => enc(k) + '=' + enc(String(params[k]))).join('&');
  const qs2 = keys.map(k => enc(enc(k)) + '=' + enc(enc(String(params[k])))).join('&');
  const ts = String(Date.now()), nonce = String(Math.floor(Math.random() * 9e9) + 1e9);
  const raw = method + '\n' + enc(u.pathname) + '\n' + qs2 + '\nx-api-nonce:' + nonce + '\nx-api-timestamp:' + ts + '\n';
  const headers = { 'Content-Type': 'application/json', 'X-Api-ClientID': CID, 'X-Api-Auth-Version': '2.0', 'X-Api-TimeStamp': ts,
    'X-Api-SignHeaders': 'X-Api-TimeStamp,X-Api-Nonce', 'X-Api-Nonce': nonce, 'X-Api-Signature': await hmacHexB64(CSEC, raw), ...extra };
  const r = await relay(u.origin + u.pathname + (qs1 ? '?' + qs1 : ''), { method, headers });
  if (r.error) throw new Error('网络错误 ' + r.error);
  try { return JSON.parse(r.text); } catch (e) { return { http: r.status, text: r.text.slice(0, 300) }; }
}
const biz = (a, path, params) => call('GET', 'https://api.kingdee.com' + path, params, { 'app-token': a.token, 'X-GW-Router-Addr': a.domain });
// 分页：page_size 超过 100 会被静默截断；销售汇总表只有第 1 页的 total_page 正确 → 固定 100、只用第 1 页的页数、校验行数
async function all(a, path, params = {}) {
  let rows = [], p = 1, tp = 1, count = 0;
  do { const r = await biz(a, path, { ...params, page: p, page_size: 100 });
    if (r.errcode !== 0) throw new Error(a.co + ' ' + path + ' ' + JSON.stringify(r).slice(0, 200));
    if (p === 1) { tp = Number(r.data.total_page || 1); count = Number(r.data.count); }
    const got = r.data.rows || []; rows = rows.concat(got); if (!got.length) break; p++;
  } while (p <= tp);
  if (count !== rows.length) throw new Error(a.co + ' ' + path + ' 行数不完整 ' + rows.length + '/' + count);
  return rows;
}
const excl = c => { if (!c) return true; const nm = (c.name || '').trim(); return INTERNAL.includes(nm) || EXCL_CONTAINS.some(k => nm.includes(k)) || EXCL_GROUP.includes((c.group_name || '').trim()); };
const months = []; for (let y = Y0; ; y++) { let stop = false; for (let m = 1; m <= 12; m++) { const s = y + '-' + pad(m) + '-01'; if (s > END) { stop = true; break; }
  const e = new Date(y, m, 0); const ee = y + '-' + pad(m) + '-' + pad(e.getDate()); months.push([s, ee > END ? END : ee]); } if (stop) break; }
const halfs = []; for (let y = Y0; ; y++) { let stop = false; for (const [a, b] of [['01-01', '06-30'], ['07-01', '12-31']]) { const s = y + '-' + a; if (s > END) { stop = true; break; } const e = y + '-' + b; halfs.push([s, e > END ? END : e]); } if (stop) break; }

window.JOB = { log: [], done: false, err: null, asOf: END };
const L = m => JOB.log.push(new Date().toTimeString().slice(0, 8) + ' ' + m);
(async () => { try {
  await ready;
  const DS = window.DS = {};
  await Promise.all(ACCTS.map(async a => {
    const D = DS[a.co] = {};
    // 授权与 token
    const au = await call('POST', 'https://api.kingdee.com/jdyconnector/app_management/push_app_authorize', { outerInstanceId: a.oid });
    const x = Array.isArray(au.data) ? au.data[0] : au.data;
    if (!x || Number(x.status) !== 1) throw new Error(a.co + ' 授权失效或获取失败 ' + JSON.stringify(au).slice(0, 200));
    a.domain = x.domain; a.expires = new Date(x.instanceExpiresTime).toISOString().slice(0, 10);
    const tk = await call('GET', 'https://api.kingdee.com/jdyconnector/app_management/kingdee_auth_token', { app_key: x.appKey, app_signature: await hmacHexB64(x.appSecret, x.appKey) });
    if (tk.errcode !== 0) throw new Error(a.co + ' 换 token 失败 ' + JSON.stringify(tk).slice(0, 200));
    a.token = tk.data['app-token']; L(a.co + ' 授权成功，实例到期 ' + a.expires);
    // 客户（列表默认不含已禁用客户，后面补齐）
    const cust = await all(a, '/jdy/v2/bd/customer', {}); const cmap = {}; cust.forEach(c => cmap[c.id] = c);
    const so = await all(a, '/jdy/v2/scm/sal_out_bound', { start_bill_date: START, end_bill_date: END });
    D.so = so.map(r => [r.bill_date, r.bill_no, r.customer_id, r.emp_name || '', n(r.total_amount), r.settle_status, r.bill_status, '']); L(a.co + ' 出库 ' + so.length);
    const si = await all(a, '/jdy/v2/scm/sal_in_bound', { start_bill_date: START, end_bill_date: END });
    D.si = []; for (const r of si) { const d = await biz(a, '/jdy/v2/scm/sal_in_bound_detail', { id: r.id }); if (d.errcode !== 0) throw new Error('退货详情 ' + JSON.stringify(d).slice(0, 120));
      D.si.push([r.bill_date, r.bill_no, r.customer_id, r.emp_name || '', n(d.data.total_amount), r.bill_status, r.dept_name || '']); } L(a.co + ' 退货 ' + si.length);
    D.cr = (await all(a, '/jdy/v2/arap/ar_credit', { start_bill_date: START, end_bill_date: END })).map(r => [r.bill_date, r.bill_no, r.customer_id, r.emp_name || '', n(r.total_amount_for || r.total_amount), r.bill_status, r.dept_name || '']);
    // 部门与出库单部门归属（列表不返回部门，按部门筛选反查；上级部门不含下级）
    const dept = await all(a, '/jdy/v2/bd/department', {}); D.dept = dept.map(x => [x.id, x.name, x.full_name || '', x.parent_name || '', x.is_leaf ? 1 : 0]);
    const bd = {}; for (const dp of dept) { (await all(a, '/jdy/v2/scm/sal_out_bound', { start_bill_date: START, end_bill_date: END, dept_id: dp.id })).forEach(r => bd[r.bill_no] = dp.id); }
    D.so.forEach(r => r[7] = bd[r[1]] || ''); L(a.co + ' 部门归属完成');
    // 月度回款与月末余额（应收账款汇总表；客户应收统计表不加筛选时最多 100 行，不能用）
    D.arm = []; let arEnd = [];
    for (const [s, e] of months) { const rows = await all(a, '/jdy/v2/arap/ar_summary_report', { start_date: s, end_date: e, summary_type: 'item' }); arEnd = rows;
      rows.forEach(x => { if (Math.abs(n(x.balance_for)) > .005 || Math.abs(n(x.rp_scm_amount_for)) > .005 || Math.abs(n(x.pre_balance_for)) > .005) D.arm.push([s.slice(0, 7), x.customer_id, n(x.rp_scm_amount_for), n(x.balance_for), n(x.pre_balance_for)]); }); }
    D.ar0930 = arEnd.map(x => [x.customer_id, n(x.balance_for), n(x.pre_balance_for)]); L(a.co + ' 月度应收 ' + D.arm.length);
    // 补齐禁用客户
    const ids = new Set(); [D.so, D.si, D.cr].forEach(t => t.forEach(r => ids.add(r[2]))); D.arm.forEach(r => ids.add(r[1]));
    for (const id of ids) if (!cmap[id]) { const d = await biz(a, '/jdy/v2/bd/customer_detail', { id }); const v = d.data || {}; cmap[id] = { id, number: v.number, name: v.name || id, group_name: v.group_name || '' }; }
    D.customers = Object.values(cmap).map(c => [c.id, c.number, c.name, c.group_name || '']);
    // 账龄：对剔除后有余额的客户逐个拉应收明细表（流水账，余额取负号），先进先出
    const arCust = arEnd.filter(x => Math.abs(n(x.balance_for)) > .005 && !excl(cmap[x.customer_id]));
    D.cdet = {}; D.lots = {}; let bad = 0;
    for (const x of arCust) {
      const v = (await biz(a, '/jdy/v2/bd/customer_detail', { id: x.customer_id })).data || {};
      D.cdet[x.customer_id] = [v.setting_term_name || '', n(v.credit_limit), v.saler_name || '', v.sale_dept_name || '', v.province_name || ''];
      let rows = [], p = 1, tp = 1, pre = 0;
      do { const r = await biz(a, '/jdy/v2/arap/ar_order_statement_report', { customer_id: x.customer_id, start_date: '2018-01-01', end_date: END, page: p, page_size: 100 });
        if (r.errcode !== 0) throw new Error('应收明细 ' + JSON.stringify(r).slice(0, 150)); if (p === 1) { pre = n(r.data.pre_total_amount); tp = Number(r.data.total_page || 1); }
        rows = rows.concat(r.data.rows || []); p++; } while (p <= tp);
      const lots = []; let prev = -pre; if (Math.abs(prev) > .005) lots.push(['期初', '期初', prev]);
      for (const r of rows) { const owed = -n(r.balance_for); let d = owed - prev; prev = owed; if (Math.abs(d) < .005) continue;
        if (d > 0) { while (d > .005 && lots.length && lots[0][2] < 0) { const t = Math.min(d, -lots[0][2]); lots[0][2] += t; d -= t; if (Math.abs(lots[0][2]) < .005) lots.shift(); } if (d > .005) lots.push([r.bill_date, r.bill_no, d]); }
        else { let c = -d; while (c > .005 && lots.length && lots[0][2] > 0) { const t = Math.min(c, lots[0][2]); lots[0][2] -= t; c -= t; if (lots[0][2] < .005) lots.shift(); } if (c > .005) lots.push([r.bill_date, r.bill_no, -c]); } }
      D.lots[x.customer_id] = lots.map(l => [l[0], l[1], Math.round(l[2] * 100) / 100]);
      if (Math.abs(lots.reduce((t, l) => t + l[2], 0) - n(x.balance_for)) > .01) bad++;
    }
    if (bad) throw new Error(a.co + ' 有 ' + bad + ' 户账龄明细与余额表不平');
    L(a.co + ' 账龄 ' + arCust.length + ' 户，全部对平');
    // 产品销售（销售汇总表按客户，单次最多 6 个月）
    D.ps = []; for (const [s, e] of halfs) (await all(a, '/jdy/v2/sal/sal_summary_rpt_ims', { filter_summary_field: 'customerid', start_date: s, end_date: e }))
      .forEach(x => D.ps.push([s, e, x.customer_id, x.material_id, x.material_number, x.material_name, x.material_category || '', n(x.base_qty), x.base_unit_name || '', n(x.all_amount)]));
    D.inv = (await all(a, '/jdy/v2/scm/inventory', { include_batch_kf_period: 'true' })).filter(x => n(x.qty) !== 0)
      .map(x => [x.material_id, x.material_number, x.material_name, x.material_model || '', x.stock_name, x.batch_no || '', x.kf_date || '', x.valid_date || '', n(x.qty)]);
    L(a.co + ' 产品 ' + D.ps.length + ' 行，库存批次 ' + D.inv.length);
  }));
  const pack = { meta: { pulled_at: new Date().toISOString(), as_of: END, expires: Object.fromEntries(ACCTS.map(a => [a.co, a.expires])) }, ledgers: DS };
  const gz = await new Response(new Blob([JSON.stringify(pack)]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer();
  window.PACKGZ = gz; window.PACKNAME = 'kd_snapshot_' + END + '.json.gz';
  window.KD_DOWNLOAD = () => { const u = URL.createObjectURL(new Blob([gz], { type: 'application/gzip' })); const el = document.createElement('a'); el.href = u; el.download = window.PACKNAME; document.body.appendChild(el); el.click(); return window.PACKNAME; };
  L('拉取完成，快照 ' + window.PACKNAME + '（' + Math.round(gz.byteLength / 1024) + ' KB）已在内存中，执行 KD_DOWNLOAD() 下载'); JOB.done = true;
} catch (e) { JOB.err = String(e); L('出错：' + e); } })();
return 'started';
})()
