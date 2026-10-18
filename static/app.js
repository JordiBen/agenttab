// AgentTab dashboard. Plain JS, no build step, no framework.
const $ = id => document.getElementById(id);
const enc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const usd = v => '$' + Number(v || 0).toFixed(2);
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const PILL = {completed: 'ok', allow: 'ok', fund: 'ok', charge: 'blue', denied: 'bad', deny: 'bad', block: 'bad',
  pending_approval: 'warn', approval: 'warn', refunded: 'violet', refund: 'violet', credit: 'violet', rejected: 'gray',
  expired: 'gray', paying: 'warn', completing: 'warn', refunding: 'violet', failed_refunded: 'gray',
  failed_voided: 'gray', capture_declined: 'bad', frozen: 'bad', active: 'ok', reconciled: 'ok', mismatch: 'bad'};
// Purchase statuses from authorize-then-capture approvals, in plain words, with a hover explanation.
// Stamp vocabulary (docs/DESIGN.md): paid / held / blocked / refunded. Text keeps its case; only stamps are capitals.
const LABEL = {failed_voided: 'hold cancelled', capture_declined: 'charge declined', completed: 'paid', pending_approval: 'held',
  denied: 'blocked', deny: 'blocked', charge: 'paid', refunded: 'refunded', refund: 'refunded'};
const TIP = {failed_voided: 'The PayPal hold was voided, so nothing was charged (vendor failure, expiry, frozen agent or amount mismatch).',
  capture_declined: 'PayPal declined the charge after delivery. The agent is frozen until the owner reconciles and unfreezes it.',
  expired: 'The approval link expired before anyone paid.', pending_approval: 'Held: waiting for a human to approve or reject it.'};
const pill = (v, label, tip) => `<span class="pill ${PILL[v] || 'gray'}"${(tip || TIP[v]) ? ` title="${enc(tip || TIP[v])}"` : ''}>${enc(label || LABEL[v] || String(v).replace(/_/g, ' '))}</span>`;
const frozenBadge = a => a && a.frozen ? pill('frozen', 'frozen', 'Frozen: ' + (a.frozen_reason || 'no new spending until the owner unfreezes it')) : pill('active', 'active', 'Spending allowed');
const pillCell = p => p.value == null ? '' : pill(p.value);

// ---- toasts ----
// toast marker: the sawtooth tooth in the verdict colour (no emoji or symbol glyphs as UI)
const TOAST = {ok: ['ok', 'var(--ok)'], bad: ['bad', 'var(--bad)'], warn: ['warn', 'var(--warn)'], info: ['info', 'var(--muted)'], violet: ['refund', 'var(--muted)']};
function toast(kind, title, text = '') {
  const [ic, c] = TOAST[kind] || TOAST.info, el = document.createElement('div');
  el.className = 'toast'; el.dataset.kind = ic; el.innerHTML = `<span class="ti" style="--c:${c}" aria-hidden="true"></span><div><b>${enc(title)}</b>${text ? enc(text).slice(0, 180) : ''}</div>`;
  $('toasts').appendChild(el);
  while ($('toasts').children.length > 4) $('toasts').firstChild.remove();
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, 4600);
}

// AG Grid v33 Theming API in the design tokens (olive on bone, Roundo); no CSS overrides
const gridTheme = agGrid.themeQuartz.withParams({fontFamily: 'Roundo, system-ui, sans-serif', fontSize: 13.5, headerFontWeight: 600,
  backgroundColor: '#f4f4f1', foregroundColor: '#38450f', headerBackgroundColor: '#e1dfd9', headerTextColor: '#38450f',
  chromeBackgroundColor: '#f4f4f1', borderColor: '#e1dfd9', rowBorder: {color: '#ecebe7'}, wrapperBorderRadius: 6, borderRadius: 6,
  accentColor: '#38450f', oddRowBackgroundColor: '#f4f4f1', rowHoverColor: '#ecebe7', selectedRowBackgroundColor: '#e1dfd9',
  spacing: 7, browserColorScheme: 'light'});
const DEF = {flex: 1, minWidth: 90, sortable: true, filter: true, resizable: true};
const DENY_ROW = {background: '#f3e6e3'};

// ---- owner console ----
localStorage.removeItem('tok');   // the admin token lives only in this tab's sessionStorage
if (sessionStorage.tok) { $('tok').value = sessionStorage.tok; $('ownerhint').open = true; }
const T = () => $('tok').value;
const H = () => ({'Content-Type': 'application/json', 'X-Admin-Token': T()});
const money = p => p.value == null ? '' : '$' + Number(p.value).toFixed(4);
const agentsGrid = agGrid.createGrid($('agents'), {theme: gridTheme, rowData: [],
  rowSelection: {mode: 'singleRow'},
  columnDefs: [{field: 'name'}, {headerName: 'Status', field: 'frozen', cellRenderer: p => frozenBadge(p.data), minWidth: 105, maxWidth: 120, tooltipValueGetter: p => p.data.frozen_reason || ''}, {field: 'agent_id'}, {field: 'balance_usd', headerName: 'Balance', valueFormatter: money},
    {field: 'spent_24h_usd', headerName: 'Spent 24h', valueFormatter: money}, {field: 'daily_cap_usd', headerName: 'Daily cap', valueFormatter: money, editable: true},
    {field: 'per_call_cap_usd', headerName: 'Per-call cap', valueFormatter: money, editable: true}, {field: 'policy', flex: 2, editable: true, tooltipField: 'policy'}],
  defaultColDef: DEF,
  onCellValueChanged: async e => { await fetch('/api/agents/' + e.data.agent_id, {method: 'PATCH', headers: H(), body: JSON.stringify({policy: e.data.policy, daily_cap_usd: +e.data.daily_cap_usd, per_call_cap_usd: +e.data.per_call_cap_usd})}); load(); }
});
const hhmm = p => new Date(p.value * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
const ledgerCols = [{field: 'ts', headerName: 'Time', sort: 'desc', valueFormatter: hhmm, minWidth: 122, maxWidth: 130},
  {field: 'kind', cellRenderer: pillCell, minWidth: 110, maxWidth: 120},
  {field: 'amount_usd', headerName: 'Amount', valueFormatter: money, minWidth: 95, maxWidth: 110}, {field: 'tool', minWidth: 170, tooltipField: 'tool'},
  {field: 'target', flex: 1.2, minWidth: 150, tooltipField: 'target'},
  {field: 'purpose', flex: 2, minWidth: 180, tooltipField: 'purpose'}, {field: 'reason', flex: 2, minWidth: 200, tooltipField: 'reason'}, {field: 'paypal_ref', headerName: 'PayPal ref', minWidth: 130}];
const ledgerGrid = agGrid.createGrid($('ledger'), {theme: gridTheme, columnDefs: [{field: 'agent_id'}, ...ledgerCols], defaultColDef: DEF, rowData: []});
async function load() {
  if (!T()) return;
  const a = await fetch('/api/agents', {headers: H()}); if (a.ok) agentsGrid.setGridOption('rowData', await a.json());
  const l = await fetch('/api/ledger', {headers: H()}); if (l.ok) ledgerGrid.setGridOption('rowData', await l.json());
  const a0 = agentsGrid.getSelectedRows()[0] || agentsGrid.getDisplayedRowAtIndex(0)?.data;
  if (a0) { const p = await fetch('/api/purchases?agent_id=' + a0.agent_id, {headers: H()}); if (p.ok) oPurch.setGridOption('rowData', await p.json()); }
}
const sel = () => { const r = agentsGrid.getSelectedRows()[0] || agentsGrid.getDisplayedRowAtIndex(0)?.data; if (!r) toast('warn', 'Create an agent first'); return r; };
async function createAgent() {
  const r = await fetch('/api/agents', {method: 'POST', headers: H(), body: JSON.stringify({name: $('name').value, policy: $('policy').value, daily_cap_usd: +$('cap').value, per_call_cap_usd: +$('pcap').value})});
  const j = await r.json(); $('newkey').textContent = r.ok ? ('Agent key (give to your agent): ' + j.api_key) : JSON.stringify(j);
  r.ok ? toast('ok', 'Agent created', 'Copy its key from the console.') : toast('bad', 'Could not create agent', JSON.stringify(j.detail || j)); load();
}
async function reconcile() { const a = sel(); if (!a) return; $('report').textContent = 'Checking the newest captures with PayPal…';
  const r = await fetch('/api/agents/' + a.agent_id + '/reconcile', {method: 'POST', headers: H()}); const j = await r.json();
  if (!r.ok) { toast('bad', 'Reconcile failed', typeof j.detail === 'string' ? j.detail : ''); $('report').textContent = JSON.stringify(j.detail || j); return; }
  $('report').innerHTML = (j.ok ? pill('reconciled', 'matches PayPal') : pill('mismatch', j.mismatches.length + ' mismatch(es)')) + ' ' + frozenBadge(j) +
    ` <span class="dim">${j.checked} capture(s) checked${j.unverified ? ', ' + j.unverified + ' unverified' : ''} · balance ${usd(j.balance_usd)}</span>` +
    (j.mismatches.length ? '<br>' + j.mismatches.map(enc).join('<br>') : '');
  j.ok ? toast('ok', 'Ledger matches PayPal', j.checked + ' capture(s) checked') : toast('bad', 'Mismatch: agent frozen', j.mismatches[0] || ''); load(); }
async function unfreeze() { const a = sel(); if (!a) return;
  const r = await fetch('/api/agents/' + a.agent_id + '/unfreeze', {method: 'POST', headers: H()});
  r.ok ? toast('ok', 'Agent unfrozen', 'Spending is allowed again.') : toast('bad', 'Could not unfreeze'); load(); }
async function topup() { const a = sel(); if (!a) return;
  const r = await fetch('/api/agents/' + a.agent_id + '/topup', {method: 'POST', headers: H(), body: JSON.stringify({amount_usd: +$('amt').value})});
  const j = await r.json(); if (j.approve_url) location.href = j.approve_url; else toast('bad', 'Top-up failed', JSON.stringify(j.detail || j)); }
async function cardTopup() { const a = sel(); if (!a) return;
  const r = await fetch('/api/agents/' + a.agent_id + '/topup-sandbox-card', {method: 'POST', headers: H(), body: JSON.stringify({amount_usd: +$('amt').value})});
  const j = await r.json(); $('report').textContent = JSON.stringify(j); r.ok ? toast('ok', 'Funded ' + usd(j.amount_usd), 'PayPal sandbox capture ' + j.capture_id) : toast('bad', 'Top-up failed'); load(); }
async function refund() { const a = sel(); if (!a) return;
  const r = await fetch('/api/agents/' + a.agent_id + '/refund', {method: 'POST', headers: H()}); const j = await r.json();
  $('report').textContent = JSON.stringify(j); r.ok ? toast('violet', 'Refunded ' + usd(j.refunded_usd) + ' to PayPal') : toast('bad', 'Refund failed'); load(); }
async function report() { const a = sel(); if (!a) return;
  const r = await fetch('/api/agents/' + a.agent_id + '/report', {headers: H()}); const j = await r.json(); $('report').textContent = j.report || JSON.stringify(j); }

// ---- health: live AI guard badge (real state from /healthz; the model name is shown only in Proof) ----
const LIVE_TXT = {live: 'Live AI guard', busy: 'AI busy, human approval', off: 'Keyword mode (no AI configured)'};
const LIVE_TIP = {live: 'Every purchase that reaches the policy check is judged by the AI guard in real time. If it is unavailable, the purchase waits for a human.',
  busy: 'The AI guard cannot judge right now (not configured, rate-limited or out of budget), so purchases that reach the policy check wait for human approval. By design.',
  off: 'Local/offline mode: no AI provider configured, a transparent keyword check is used.'};
function setLive(h) {
  const st = h.ai_status || (h.ai_guard ? 'live' : 'off');
  for (const id of ['navlive', 'herolive']) {
    const el = $(id); el.classList.toggle('off', st === 'off'); el.classList.toggle('busy', st === 'busy'); el.dataset.status = st;
    el.querySelector('span').textContent = LIVE_TXT[st] || LIVE_TXT.off; el.title = LIVE_TIP[st] || '';
  }
  const pm = $('proofmodel');
  if (pm) pm.textContent = h.models && h.models.length ? `AI guard model: ${h.models.join(', ')} (${h.guard_provider === 'anthropic' ? 'Anthropic' : h.guard_provider})` : 'AI guard model: none configured';
}

// ---- hero feed: real decisions, typed out like a log ----
const FEED = {queue: [], live: [], busy: false, replay: [], i: 0, visible: true, all: []};
// Stamps appear only on real decisions. A replayed eval line is a dry run, so an allowed one reads ALLOWED, never PAID.
const STAMP = {block: ['block', 'BLOCKED'], approval: ['hold', 'HELD'], refund: ['refund', 'REFUNDED']};
const stampFor = (outcome, live) => outcome === 'allow' ? (live ? ['paid', 'PAID'] : ['pass', 'ALLOWED']) : (STAMP[outcome] || ['pass', String(outcome).toUpperCase()]);
const stamp = (outcome, live) => { const [c, w] = stampFor(outcome, live); return `<span class="stamp ${c}">${w}</span>`; };
const itemName = (vendor, item) => (CAT || []).find(v => v.vendor === vendor)?.items.find(i => i.item === item)?.name || item;
function feedEntry(d, live) {
  const el = document.createElement('div'); el.className = 'fe';
  const req = `<span class="what">POST purchase · ${enc(d.vendor)} · ${enc(itemName(d.vendor, d.item))}</span>` + (d.amount_usd != null ? `<span class="amt">${usd(d.amount_usd)}</span>` : '');
  el.innerHTML = `<div class="req"></div><div class="pur">${d.target ? enc(d.target) + ' · ' : ''}"${enc(d.purpose)}"</div>
    <div class="res" hidden>${live ? '<span class="tag livetag">LIVE</span>' : ''}${stamp(d.outcome, live)}
    <span class="meta">layer: ${enc(d.layer)}${d.latency_ms ? ' · ' + Math.round(d.latency_ms) + ' ms' : ''}</span>
    <span class="why">${enc(d.reason)}</span></div>`;
  return {el, req};
}
async function typeInto(node, html) {
  if (REDUCED) { node.innerHTML = html; return; }       // reduced motion: the receipt prints instantly
  const tmp = document.createElement('div'); tmp.innerHTML = html; const text = tmp.textContent; node.textContent = '';
  const caret = document.createElement('span'); caret.className = 'caret';
  for (let k = 1; k <= text.length; k += 2) { node.textContent = text.slice(0, k); node.appendChild(caret); feedFill($('feed')); await new Promise(r => setTimeout(r, 14)); }
  node.innerHTML = html;
}
async function feedNext() {
  if (FEED.busy || document.hidden || !FEED.visible) return; FEED.busy = true;   // pauses while off-screen
  try {
    const live = FEED.live.shift(); const d = live || FEED.replay[FEED.i++ % (FEED.replay.length || 1)];
    if (!d) return;
    const box = $('feed'), {el, req} = feedEntry(d, !!live); box.appendChild(el);
    await typeInto(el.querySelector('.req'), req);
    await new Promise(r => setTimeout(r, REDUCED ? 0 : 380));
    const res = el.querySelector('.res'); res.hidden = false;
    if (!REDUCED) res.querySelector('.stamp.block')?.classList.add('land');
    feedFill(box);
  } finally { FEED.busy = false; }
}
// The receipt prints at the bottom and older lines scroll up under the tear line, so the paper is always full:
// a line is dropped only once it has scrolled completely out of view.
function feedTrim(box) { const top = box.getBoundingClientRect().top;
  // drop a line only once the next one has reached the tear (bottom + row gap), or the gap itself would show as empty paper
  const gap = parseFloat(getComputedStyle(box).rowGap) || 0;
  while (box.children.length > 1 && box.firstElementChild.getBoundingClientRect().bottom + gap <= top) box.firstElementChild.remove();
  for (const e of box.children) e.classList.toggle('past', e.getBoundingClientRect().top < top - 1); }   // partly fed past the tear: dimmed
function feedFill(box, min = 0) {    // top the paper up with replayed lines until no empty space is left
  for (let k = 0; k < 16 && FEED.replay.length && (box.children.length < min || !feedFull(box)); k++) {
    const {el, req} = feedEntry(FEED.replay[FEED.i++ % FEED.replay.length], false); el.querySelector('.req').innerHTML = req; el.querySelector('.res').hidden = false;
    box.insertBefore(el, box.firstElementChild);   // older lines go above: the line being printed stays at the bottom
  }
  feedTrim(box); }
const feedFull = box => !!box.firstElementChild && box.firstElementChild.getBoundingClientRect().top <= box.getBoundingClientRect().top + 1;
function feedLive(d) { FEED.live.push(d); const m = $('feedmode'); m.textContent = 'live'; m.classList.add('on');
  $('feedfoot').textContent = 'Live: your demo purchases, judged right now (PayPal sandbox). Between them, eval decisions are replayed as dry runs.'; FEED.visible = true; feedNext(); }
async function initFeed() {
  try { const r = await fetch('/api/replay'); if (!r.ok) return; const j = await r.json();
    FEED.all = j.decisions; howBar(j.decisions); showcase(j.decisions);
    const ds = j.decisions.slice(), rng = () => Math.random() - .5; ds.sort(rng);
    // interleave: attacks and legitimate buys alternate so the feed shows both outcomes
    const atk = ds.filter(d => d.attack), ok = ds.filter(d => !d.attack); FEED.replay = [];
    while (atk.length || ok.length) { if (atk.length) FEED.replay.push(atk.shift()); if (atk.length) FEED.replay.push(atk.shift()); if (ok.length) FEED.replay.push(ok.shift()); }
    const box = $('feed'); feedFill(box, 3);
    document.fonts?.ready.then(() => feedFill(box));            // the web font changes line heights: top up once it lands
    document.fonts?.addEventListener?.('loadingdone', () => feedFill(box));
    new ResizeObserver(() => feedFill(box)).observe(box);
    setInterval(() => feedFill(box), 1000);                     // cheap safety net: a gap never lasts more than a second
    new IntersectionObserver(es => { FEED.visible = es[0].isIntersecting; }).observe($('feed'));
    setInterval(feedNext, REDUCED ? 6000 : 3400);
  } catch (e) {}
}

// ---- how it works: one bar, segments sized by where the eval decisions were made (real /api/replay data) ----
const SEG_OF = d => d.outcome === 'allow' ? 'paid' : d.outcome === 'approval' ? 'hold' : d.layer === 'caps' ? 'caps'
  : String(d.layer).startsWith('policy') ? 'ai' : 'rules';
function howBar(ds) {
  const n = {rules: 0, caps: 0, ai: 0, hold: 0, paid: 0}; ds.forEach(d => n[SEG_OF(d)]++);
  const bar = $('howbar'); bar.style.setProperty('--total', ds.length);
  bar.querySelectorAll('.count').forEach(c => { const k = c.dataset.k; c.textContent = `${n[k]} of ${ds.length}`;
    c.closest('.seg').style.setProperty('--n', Math.max(n[k], 1)); });
}

// ---- showcase: real eval verdicts, one per scenario ----
let STALE_AI = false;
function showcase(ds) {
  const by = Object.fromEntries(ds.map(d => [d.id, d])), stage = $('stage');
  const draw = d => { if (!d) { stage.innerHTML = '<p class="purp">This case is not in the committed run.</p>'; return; }
    stage.innerHTML = `<div class="row2"><span>POST purchase · ${enc(d.vendor)} · ${enc(itemName(d.vendor, d.item))}</span><span>${d.amount_usd != null ? usd(d.amount_usd) : ''}</span></div>
      <p class="purp">${d.target ? enc(d.target) + ' · ' : ''}"${enc(d.purpose)}"</p>${stamp(d.outcome, false)}
      <p class="layer">Decided by: ${enc(d.layer)} · eval case ${enc(d.id)} (dry run)${STALE_AI ? ' · previous catalog' : ''}</p><p>${enc(d.reason)}</p>`; };
  const pick = btn => { $('scen').querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', b === btn ? 'true' : 'false'));
    if (REDUCED) return draw(by[btn.dataset.case]);
    stage.classList.add('swap'); setTimeout(() => { draw(by[btn.dataset.case]); stage.classList.remove('swap'); }, 260); };
  $('scen').querySelectorAll('button').forEach(b => { b.onclick = () => pick(b);
    b.onkeydown = e => { const all = [...$('scen').querySelectorAll('button')], i = all.indexOf(b);
      const k = {ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1}[e.key]; if (!k) return; e.preventDefault();
      const nb = all[(i + k + all.length) % all.length]; nb.focus(); pick(nb); }; });
  draw(by[$('scen').querySelector('[aria-selected="true"]').dataset.case]);
}

// ---- short entrances for section parts ----
function initMotion() {
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), {threshold: .12});
  document.querySelectorAll('.reveal').forEach((el, i) => { el.style.transitionDelay = (i % 4) * 60 + 'ms'; io.observe(el); });
}

// ---- sparklines ----
function spark(values, w, h, color) {
  if (!values.length) values = [0, 0];
  if (values.length === 1) values = [values[0], values[0]];
  const max = Math.max(...values, 1e-9), min = Math.min(...values, 0), span = (max - min) || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * w, h - 2 - ((v - min) / span) * (h - 4)]);
  const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}
const SIGN = {fund: 1, credit: 1, charge: -1, refund: -1};
function balanceSeries(entries) { let b = 0; return entries.slice().sort((x, y) => x.ts - y.ts).filter(e => SIGN[e.kind]).map(e => (b += SIGN[e.kind] * e.amount_usd)); }
function vendorSeries(purchases, vid) { let s = 0; return [0, ...purchases.filter(p => p.vendor === vid && p.status === 'completed').sort((x, y) => x.ts - y.ts).map(p => (s += p.amount_usd))]; }

// ---- public demo ----
const dGrid = agGrid.createGrid($('dledger'), {theme: gridTheme, columnDefs: ledgerCols, defaultColDef: DEF, rowData: [],
  getRowStyle: p => p.data.kind === 'deny' ? DENY_ROW : null});
const DK = () => sessionStorage.dkey;
const show = o => { $('dout').textContent = typeof o === 'string' ? o : JSON.stringify(o, null, 1); };
const verdict = html => { $('dverdict').innerHTML = html || ''; };
// Demo key no longer valid (e.g. the free server restarted and its sandbox data was reset): forget it.
function demoExpired() { if (!sessionStorage.dkey) return; delete sessionStorage.dkey;
  const n = $('dexpired'); n.textContent = 'Your demo session expired (the free demo server restarts now and then and resets its sandbox data). Press "1. Start demo session" to begin a new one.'; n.style.display = '';
  toast('warn', 'Demo session expired', 'Start a new one; the free server reset its sandbox data.');
  dGrid.setGridOption('rowData', []); dPurch.setGridOption('rowData', []); $('dbudgets').innerHTML = '<p class="sub">Start a session to see the tab.</p>'; DS && DS.clear && DS.clear(); verdict(''); mcpPanel(); }
async function dfetch(url, opts = {}) { const r = await fetch(url, opts); if (r.status === 401 && DK()) { demoExpired(); throw new Error('expired'); } return r; }
let DSIG = null;
async function dload() { try { await dload_(); } catch (e) {} }
async function dload_() { if (!DK()) return; const h = {'X-Agent-Key': DK()};
  const [r, p, m] = await Promise.all([dfetch('/api/demo/ledger', {headers: h}), dfetch('/api/demo/purchases', {headers: h}), dfetch('/api/me', {headers: h})]);
  const led = r.ok ? await r.json() : null, pur = p.ok ? await p.json() : null;
  if (led) dGrid.setGridOption('rowData', led);
  if (pur) { dPurch.setGridOption('rowData', pur);
    // A status changed outside this tab (webhook, declined capture, expiry): redraw the timeline now, not on its 8 s poll.
    const sig = pur.map(x => x.id + ':' + x.status).join(',');
    if (DSIG !== null && sig !== DSIG && DS) DS.refresh(); DSIG = sig; }
  if (m.ok) { const j = await m.json();
    $('dbudgets').innerHTML = `<div class="bal"><div><b>${usd(j.balance_usd)}</b><div class="sub" style="margin:6px 0 0">${j.frozen ? frozenBadge(j) + ' ' : ''}tab balance · ${usd(j.spent_24h_usd)} spent in 24 h of ${usd(j.daily_cap_usd)} daily cap</div></div>${spark(balanceSeries(led || []), 150, 38, '#38450f')}</div>` + bars(j.vendor_budgets, pur || []);
    $('dpolicy').textContent = '"' + j.policy + '"'; } }
// ---- MCP connect panel (demo key lives only in this tab's sessionStorage) ----
let MCP_SHOW = false;
function mcpPanel() { const k = DK(), url = location.origin + '/mcp';
  $('mcpurl').textContent = url; $('mcpbox').style.display = k ? '' : 'none'; $('mcpnone').style.display = k ? 'none' : '';
  if (!k) { $('mcpcfg').textContent = ''; return; } const shown = MCP_SHOW ? k : k.slice(0, 6) + '•'.repeat(10);
  $('mcpkey').textContent = shown; $('mcpreveal').textContent = MCP_SHOW ? 'Hide' : 'Reveal';
  $('mcpcfg').textContent = JSON.stringify({mcpServers: {agenttab: {url, headers: {'X-Agent-Key': shown}}}}, null, 1); }
function mcpToggle() { MCP_SHOW = !MCP_SHOW; mcpPanel(); }
async function mcpCopy(what) { const k = DK(); if (!k) return; const url = location.origin + '/mcp';
  const txt = what === 'key' ? k : JSON.stringify({mcpServers: {agenttab: {url, headers: {'X-Agent-Key': k}}}}, null, 1);
  try { await navigator.clipboard.writeText(txt); toast('ok', what === 'key' ? 'Agent key copied' : 'MCP config copied', 'Paste it into your MCP client.'); }
  catch (e) { toast('warn', 'Copy blocked by the browser', 'Use Reveal and copy it by hand.'); } }
async function demoStart() { show('Funding your agent through PayPal sandbox…'); verdict(pill('approval', 'working'));
  const r = await fetch('/api/demo/session', {method: 'POST'}); const j = await r.json();
  if (!r.ok) { verdict(pill('deny', 'error')); toast('bad', 'Could not start a session', j.detail || ''); return show(j.detail || j); }
  sessionStorage.dkey = j.agent_key; $('dexpired').style.display = 'none'; MCP_SHOW = false; mcpPanel();
  DS && DS.refresh(); verdict(pill('fund', 'funded'));
  toast('ok', 'Funded ' + usd(j.balance_usd) + ' via PayPal sandbox', 'Capture ' + j.paypal_capture_id);
  show(`Agent ${j.name} funded: ${usd(j.balance_usd)}\nPayPal sandbox order ${j.paypal_order_id}\nCapture ${j.paypal_capture_id}`); dload(); }
async function demoBuy(vendor, item, target, purpose, quantity = 1) { if (!DK()) { toast('info', 'Start a demo session first', 'Step 1 funds your agent with $100 of sandbox money.'); return show('Start a demo session first.'); }
  show('Guard is checking the purchase…'); verdict(pill('approval', 'checking')); $('dbusy').style.display = 'none';
  let r; try { r = await dfetch('/api/purchases', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Key': DK()}, body: JSON.stringify({vendor, item, quantity, target, purpose})}); } catch (e) { return show(''); }
  const j = await r.json(); const g = j.guard || (j.detail && j.detail.layer ? j.detail : null);
  const who = g ? `<span class="pill gray">layer: ${enc(g.layer)}</span>` : '';
  const unit = (CAT || []).find(v => v.vendor === vendor)?.items.find(i => i.item === item)?.price_usd; const amt = unit == null ? null : unit * quantity;
  const live = o => g && feedLive({vendor, item, target, purpose, amount_usd: amt, outcome: o, layer: g.layer, model: g.model, latency_ms: null, reason: g.reason});
  if (r.status === 200) { verdict('<span class="stamp paid">Paid</span> ' + pill('allow', 'Bought') + who); live('allow'); toast('ok', 'Bought · ' + usd(j.charged_usd), 'Tab balance ' + usd(j.balance_usd));
    show(`${g.reason}\n\nCharged ${usd(j.charged_usd)} · tab balance ${usd(j.balance_usd)} · ` + (j.refund_deadline ? `refundable until ${new Date(j.refund_deadline * 1000).toLocaleString()}` : 'delivered data (final sale)') + '\n\n' + JSON.stringify(j.result, null, 1).slice(0, 700)); }
  else if (r.status === 202 && j.ai_unavailable) {   // fail closed: no AI verdict means a human decides, never auto-approve
    refreshLive();
    verdict('<span class="stamp hold">Held</span> ' + pill('approval', 'AI guard busy · waiting for you') + who); live('approval');
    toast('warn', 'AI guard is busy', 'Purchase is waiting for human approval, by design.');
    $('dbusy').textContent = j.message; $('dbusy').style.display = '';
    show(`${j.message}.\n\n${g.reason}\n\nApprove it in the Purchases table ("Approve (sandbox card)") or reject it.`); }
  else if (r.status === 202) { verdict('<span class="stamp hold">Held</span> ' + pill('approval', 'Needs your approval') + who); live('approval'); toast('warn', 'Held for your approval', 'Approve it in Purchases, or reject it.');
    show(`${g.reason}\n\nApprove it in the Purchases table ("Approve (sandbox card)") or right-click it on the schedule. The approval link expires in 24 h.`); }
  else if (r.status === 403) { verdict(`<span class="stamp block${REDUCED ? '' : ' land'}">Blocked</span> ` + who); live('block'); toast('bad', 'Blocked by the guard', g.reason); show(g.reason); }
  else if (r.status === 423) { verdict(pill('frozen', 'Agent frozen')); toast('bad', 'Agent frozen', 'No new spending until the owner reconciles and unfreezes it.'); show(j.detail || j); }
  else if (r.status === 402) { verdict(pill('deny', 'Tab empty')); toast('warn', 'Tab empty', 'The agent would now ask its human for a PayPal top-up.'); show(j); }
  else { verdict(pill('deny', 'Error ' + r.status)); toast('bad', 'Error ' + r.status, typeof j.detail === 'string' ? j.detail : ''); show(j.detail || j); }
  dload(); DS && DS.refresh(); }
const ACT_DONE = {'approve-sandbox-card': ['ok', 'Approved and paid with the sandbox card'], reject: ['info', 'Purchase rejected'], refund: ['violet', 'Purchase refunded']};
async function pAct(pid, act, owner) { const h = owner ? H() : {'X-Agent-Key': DK()}; let r;
  try { r = await (owner ? fetch : dfetch)(`/api/purchases/${pid}/${act}`, {method: 'POST', headers: h}); } catch (e) { return; }
  const j = await r.json(); const out = r.ok ? JSON.stringify(j, null, 1) : 'Error: ' + JSON.stringify(j.detail || j);
  r.ok ? toast(...(ACT_DONE[act] || ['ok', 'Done'])) : toast('bad', 'Could not ' + act.replace(/-/g, ' '), typeof j.detail === 'string' ? j.detail : '');
  if (owner) $('report').textContent = out; else { verdict(r.ok ? pill('allow', act.replace(/-/g, ' ') + ' done') : pill('deny', 'error')); show(out); }
  owner ? (load(), OS && OS.refresh()) : (dload(), DS && DS.refresh()); }
function purchCols(owner) { const o = owner ? 'true' : 'false'; return [
  {field: 'ts', headerName: 'Time', sort: 'desc', valueFormatter: hhmm, minWidth: 122, maxWidth: 130},
  {headerName: 'Item', minWidth: 210, flex: 1.3, valueGetter: p => p.data.vendor + ' · ' + p.data.item, tooltipValueGetter: p => p.data.vendor + ' · ' + p.data.item},
  {field: 'amount_usd', headerName: 'Amount', valueFormatter: p => '$' + Number(p.value).toFixed(2), minWidth: 90, maxWidth: 100},
  {field: 'status', cellRenderer: pillCell, minWidth: 165},
  {field: 'funding', headerName: 'Paid from', minWidth: 100, maxWidth: 110},
  {field: 'refund_deadline', headerName: 'Refundable until', minWidth: 140, valueFormatter: p => p.value ? new Date(p.value * 1000).toLocaleDateString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : (p.data.status === 'completed' ? 'final sale' : '')},
  {field: 'decision_source', headerName: 'Decided by', minWidth: 115}, {field: 'reason', flex: 2, minWidth: 200, tooltipField: 'reason'},
  {headerName: 'Actions', minWidth: 215, sortable: false, filter: false, cellRenderer: p => { const d = p.data;
    if (d.status === 'pending_approval') return `<button class="act alt" onclick="pAct('${d.id}','approve-sandbox-card',${o})">Approve (sandbox card)</button><button class="act" onclick="pAct('${d.id}','reject',${o})">Reject</button>`;
    if (d.status === 'completed' && d.refund_deadline * 1000 > Date.now()) return `<button class="act" onclick="pAct('${d.id}','refund',${o})">Refund</button>`; return ''; }}]; }
const gopt = {theme: gridTheme, defaultColDef: DEF, rowData: [], getRowStyle: p => p.data.status === 'denied' ? DENY_ROW : null};
const dPurch = agGrid.createGrid($('dpurch'), {...gopt, columnDefs: purchCols(false)});
const oPurch = agGrid.createGrid($('opurch'), {...gopt, columnDefs: purchCols(true)});
function bars(list, purchases = []) { return (list || []).map(v => { const p = Math.min(100, 100 * v.spent_30d_usd / (v.budget_usd || 1));
  const c = p >= 90 ? 'var(--stamp-block)' : p >= 60 ? 'var(--stamp-hold)' : 'var(--ink)';
  return `<div class="vb"><span>${enc(v.name)}</span><span class="track" role="progressbar" aria-label="${enc(v.name)} budget used" aria-valuenow="${Math.round(p)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${p}%;background:${c}"></i></span>${spark(vendorSeries(purchases, v.vendor), 84, 24, p >= 90 ? '#a3261b' : '#38450f')}<span class="mono">${usd(v.spent_30d_usd)} / ${usd(v.budget_usd)}</span></div>`; }).join(''); }
let CAT = null;
async function catalog() { CAT = (await (await fetch('/api/catalog')).json()).vendors || [];
  $('dvendor').innerHTML = CAT.map(v => `<option value="${enc(v.vendor)}">${enc(v.name)}</option>`).join(''); fillItems(); }
function fillItems() { const v = (CAT || []).find(x => x.vendor === $('dvendor').value);
  if (v) $('ditem').innerHTML = v.items.map(i => `<option value="${enc(i.item)}">${enc(i.name)} ($${i.price_usd})</option>`).join(''); }
async function demoReport() { if (!DK()) return show('Start a demo session first.'); let r;
  try { r = await dfetch('/api/demo/report', {headers: {'X-Agent-Key': DK()}}); } catch (e) { return; }
  const j = await r.json(); verdict(r.ok ? pill('charge', 'report · ' + (j.model ? 'AI commentary' : 'template')) : pill('deny', 'error')); show(j.report || j.detail || j);
  r.ok ? toast('info', 'Spend report ready', 'Numbers from the ledger; ' + (j.model ? 'AI commentary.' : 'template commentary.')) : toast('bad', 'Report unavailable', typeof j.detail === 'string' ? j.detail : ''); }
async function demoRefund() { if (!DK()) return show('Start a demo session first.'); let r;
  try { r = await dfetch('/api/demo/refund', {method: 'POST', headers: {'X-Agent-Key': DK()}}); } catch (e) { return; }
  const j = await r.json(); verdict(r.ok ? pill('refund', 'refunded ' + usd(j.refunded_usd) + ' to PayPal') : pill('deny', 'error')); show(j);
  r.ok ? toast('violet', 'Refunded ' + usd(j.refunded_usd) + ' to PayPal', j.refunded_usd ? 'Payments v2 refund of your sandbox capture.' : 'Nothing left to refund.') : toast('bad', 'Refund failed', typeof j.detail === 'string' ? j.detail : ''); dload(); }
function openDemo() { $('workspace').scrollIntoView({behavior: REDUCED ? 'auto' : 'smooth'}); if (!DK() && $('demo').style.display !== 'none') return demoStart(); }
// Primary action: open the demo with a funded sandbox agent, then run the injected-purchase button.
async function overspend() { if ($('demo').style.display === 'none') return openDemo();
  await openDemo(); if (!DK()) return;
  const b = [...document.querySelectorAll('#dguided .btn-step')].find(x => x.textContent.includes('injected instruction')); b && b.click(); }

// ---- scorecard + proof tiles ----
async function scorecard() { const r = await fetch('/api/scorecard'); if (!r.ok) { $('scoretxt').textContent = 'no eval results yet'; return; } const j = await r.json();
  $('scoretxt').textContent = `${j.cases} labeled purchases (${j.attacks} attacks or rule violations, ${j.legit} legitimate) for the ${j.persona}. Run ${new Date(j.generated_at).toLocaleDateString()}. "Stopped" = blocked or held for approval.`;
  $('scoretbl').querySelector('tbody').innerHTML = Object.entries(j.modes).map(([k, m]) => `<tr class="${k === 'ai' ? 'hl' : ''}"><td>${enc(m.label)}${(j.stale_modes || []).includes(k) ? ' <span class="pill gray" title="Run on the previous catalog and prices; re-run pending">previous catalog</span>' : ''}</td>
    <td class="num">${m.attacks_stopped}/${m.attacks_total} <span class="pill ${m.attacks_stopped === m.attacks_total ? 'ok' : m.attacks_stopped ? 'warn' : 'bad'}">${Math.round(m.attacks_stopped_pct)}%</span></td>
    <td class="num">${m.legit_correct}/${m.legit_total}</td><td class="num">${m.false_blocks}</td>
    <td class="num">${k === 'ai' ? Math.round(m.latency_p50_ms) + ' / ' + Math.round(m.latency_p95_ms) + ' ms' : '–'}</td><td class="num">${k === 'ai' ? m.llm_calls : '0'}</td></tr>`).join('');
  const col = {none: 'var(--ink-20)', keyword: 'var(--ink-60)', ai: 'var(--ink)'};
  $('cats').innerHTML = Object.entries(j.by_category).map(([c, v]) => `<div class="cat"><span>${enc(c.replace(/_/g, ' '))}</span><span style="display:grid;gap:3px">` +
    ['none', 'keyword', 'ai'].filter(m => m in v).map(m => `<span class="track"><i style="width:${100 * v[m] / v.total}%;background:${col[m]}"></i></span>`).join('') +
    `</span><span class="mono">${['none', 'keyword', 'ai'].filter(m => m in v).map(m => v[m]).join(' → ')} / ${v.total}</span></div>`).join('');
  tiles(j); }
// Proof tiles: every number from /api/scorecard (eval/results.json) with its denominator; a stale mode says "previous catalog".
function tiles(j) { const m = j.modes, stale = j.stale_modes || [], frac = x => `${x.attacks_stopped}<small> / ${x.attacks_total}</small>`;
  if (m.keyword) $('tkw').innerHTML = frac(m.keyword);
  if (m.none) $('tnone').innerHTML = frac(m.none);
  const ai = m.ai; if (!ai) return;
  if (stale.includes('ai')) { STALE_AI = true; if (!$('feedmode').classList.contains('on')) $('feedfoot').textContent = 'Replayed from our committed eval run, previous catalog (its reasons quote the old prices; dry run: no money moved). Your own demo purchases print here live.';
    const l = $('stage').querySelector('.layer'); if (l && !l.textContent.includes('previous catalog')) l.textContent += ' · previous catalog'; }
  $('tai').innerHTML = frac(ai); $('tstale').hidden = !stale.includes('ai');
  if (m.keyword) $('taitxt').textContent = `attacks stopped with the AI guard added: ${ai.attacks_stopped - m.keyword.attacks_stopped} more than rules alone`;
  $('hlegit').innerHTML = `${ai.legit_correct}<small> / ${ai.legit_total}</small>`; $('hfb').textContent = ai.false_blocks;
  $('hlat').innerHTML = `${Math.round(ai.latency_p50_ms)}<small> ms</small>`;
  const L = Object.entries(ai.stopped_by_layer || {}).sort((a, b) => b[1] - a[1]);
  $('tlayers').textContent = L.map(([k, v]) => `${k} ${v}`).join(' · ') + ` (of ${ai.attacks_total} attacks, rules + AI${stale.includes('ai') ? ', previous catalog' : ''})`; }

// ---- live Gauntlet ----
async function runGauntlet() { const b = $('gbtn'); b.disabled = true; b.textContent = 'Running through the live guard…';
  $('glist').innerHTML = ''; $('gsum').innerHTML = pill('approval', 'running');
  try { const r = await fetch('/api/gauntlet', {method: 'POST'}); const j = await r.json();
    if (!r.ok) { $('gsum').innerHTML = pill('deny', 'unavailable') + ` <span class="sub" style="margin:0">${enc(j.detail || 'error')}</span>`; toast('warn', 'Gauntlet unavailable', j.detail || ''); return; }
    $('gsum').innerHTML = pill('allow', `${j.attacks_stopped}/${j.attacks_total} attacks stopped`) + pill(j.legit_correct === j.legit_total ? 'allow' : 'approval', `${j.legit_correct}/${j.legit_total} legit handled right`) +
      pill('gray', `no guard would buy ${j.unguarded_would_buy}/${j.attacks_total}`) + pill('blue', j.live_ai ? `${j.llm_calls} live AI calls` : 'keyword mode') + pill('gray', 'PayPal calls: 0');
    toast(j.attacks_stopped === j.attacks_total ? 'ok' : 'warn', `${j.attacks_stopped}/${j.attacks_total} attacks stopped live`, `${j.legit_correct}/${j.legit_total} legitimate buys handled right · no money moved`);
    const out = {allow: 'Bought', block: 'Blocked', approval: 'Held for approval'};
    j.cases.forEach((c, i) => setTimeout(() => { $('glist').insertAdjacentHTML('beforeend', `<div class="g-row"><div class="top">
      ${pill(c.attack ? 'deny' : 'allow', c.attack ? c.category.replace(/_/g, ' ') : 'legitimate')}
      <span class="mono">${enc(c.vendor)}:${enc(c.item)} ${usd(c.amount_usd)}</span>
      <span style="flex:1"></span>${pill(c.outcome, out[c.outcome] || c.outcome)}<span class="pill gray">${enc(c.layer)}</span>${c.model ? `<span class="pill blue">${c.latency_ms} ms</span>` : ''}</div>
      <div class="p">"${enc(c.purpose)}"${c.target ? ' → ' + enc(c.target) : ''}</div><div class="r">${c.attack ? '' : 'Expected: ' + enc(out[c.expected] || c.expected) + '. '}${enc(c.reason)}</div></div>`);
      if (i < 4) feedLive(c); }, i * 120));
  } finally { b.disabled = false; b.textContent = 'Run it again'; } }
function runGauntletFromHero() { $('proof').scrollIntoView({behavior: REDUCED ? 'auto' : 'smooth'}); setTimeout(runGauntlet, 350); }

// ---- schedules (Bryntum) ----
let DS = null, OS = null;
async function schedAct(path, owner) { let r; try { r = await (owner ? fetch : dfetch)(path, {method: 'POST', headers: owner ? H() : {'X-Agent-Key': DK()}}); } catch (e) { return; }
  const j = await r.json(); const out = r.ok ? JSON.stringify(j, null, 1) : 'Error: ' + JSON.stringify(j.detail || j);
  r.ok ? toast('ok', 'Done', path.split('/').pop().replace(/-/g, ' ')) : toast('bad', 'Action failed', typeof j.detail === 'string' ? j.detail : '');
  if (owner) $('report').textContent = out; else { verdict(r.ok ? pill('refund', 'done') : pill('deny', 'error')); show(out); }
  owner ? (load(), OS && OS.refresh()) : (dload(), DS && DS.refresh()); }
function initSchedules() {
  if (!window.makeSchedule) return;
  DS = makeSchedule($('dsched'), {url: () => '/api/me/schedule', headers: () => ({'X-Agent-Key': DK() || ''}), act: p => schedAct(p, false), listEl: $('dsugg'), onUnauthorized: demoExpired});
  OS = makeSchedule($('osched'), {url: () => { const a = agentsGrid.getSelectedRows()[0] || agentsGrid.getDisplayedRowAtIndex(0)?.data; return '/api/schedule?agent_id=' + (a ? a.agent_id : ''); }, headers: H, act: p => schedAct(p, true), listEl: $('osugg')});
  setInterval(() => { if (DK() && DS) DS.refresh(); if (T() && OS) OS.refresh(); }, 8000);
  if (DK() && DS) DS.refresh(); if (T() && OS) OS.refresh();
}
async function demoFF() { if (!DK()) return show('Start a demo session first.'); let r;
  try { r = await dfetch('/api/demo/fast-forward?days=6', {method: 'POST', headers: {'X-Agent-Key': DK()}}); } catch (e) { return; }
  const j = await r.json(); verdict(r.ok ? pill('approval', 'demo clock +6 days') : pill('deny', 'error'));
  r.ok && toast('info', 'Demo clock +6 days', `${j.new_suggestions} new refund suggestion(s) from the scheduling agent.`);
  show(r.ok ? `Demo clock moved 6 days ahead for your agent. The scheduling agent made ${j.new_suggestions} new refund suggestion(s). Confirm or dismiss them below the timeline.` : JSON.stringify(j.detail || j)); dload(); DS && DS.refresh(); }

// ---- compare panel: the same purchase through rules + keywords and through rules + AI (dry run) ----
// Stamps never say PAID: nothing is paid here. Any verdict we don't recognise is shown as HELD.
const CMP_STAMP = {allowed: ['ALLOWED', 'pass'], blocked: ['BLOCKED', 'block'], held: ['HELD', 'hold']};
const cmpStamp = v => CMP_STAMP[v] || CMP_STAMP.held;
const cmpWords = s => String(s || '').replace(/[_-]+/g, ' ');
const CMP_LAYER = {catalog: 'Catalog', unsafe_target: 'Unsafe target', purpose: 'Purpose rule', injection: 'Injection rule', allowlist: 'Vendor allowlist',
  caps: 'Spending caps', approval: 'Approval threshold', 'policy-ai': 'Claude', 'policy-keyword': 'Keyword check', 'policy-unavailable': 'Claude unavailable', none: 'No guard'};
let CMP_CASES = [], CMP_CAT = [];
async function initCompare() { if (!$('compare')) return;
  try {
    const [c, k] = await Promise.all([fetch('/api/compare/cases').then(r => r.json()), fetch('/api/catalog').then(r => r.json())]);
    CMP_CASES = c.cases || []; CMP_CAT = k.vendors || [];
  } catch (e) { $('cmpstatus').textContent = 'Could not load the purchases. Reload the page to try again.'; return; }
  const sel = $('cmpcase'); sel.textContent = '';
  for (const x of CMP_CASES) { const o = document.createElement('option'); o.value = x.id;
    const p = x.purpose.length > 70 ? x.purpose.slice(0, 69) + '…' : x.purpose;
    o.textContent = `${cmpWords(x.category)}: ${p}`; sel.appendChild(o); }
  const own = document.createElement('option'); own.value = ''; own.textContent = 'Write your own purchase…'; sel.appendChild(own);
  const vs = $('cmpvendor'); vs.textContent = '';
  for (const v of CMP_CAT) { const o = document.createElement('option'); o.value = v.vendor; o.textContent = v.name; vs.appendChild(o); }
  cmpItems(); cmpMode(); }
function cmpItems() { const v = CMP_CAT.find(x => x.vendor === $('cmpvendor').value), is = $('cmpitem'); is.textContent = '';
  for (const i of (v ? v.items : [])) { const o = document.createElement('option'); o.value = i.item; o.textContent = `${i.name} ($${i.price_usd})`; is.appendChild(o); } }
function cmpMode() { $('cmpcustom').hidden = $('cmpcase').value !== ''; }
function cmpReceipt(side, purchase, latency) {
  const [word, cls] = cmpStamp(side.verdict);
  const meta = side.llm_called ? `Claude called · ${enc(side.model)} · ${enc(latency)} ms`
    : side.cached ? `<span class="cmp-flag">cached</span> same verdict as a recent run · ${enc(side.model)} · Claude not called again`
    : side.ai_unavailable ? 'Claude gave no verdict, so a human decides'
    : side.label === 'Rules + AI' ? 'Claude not called: the rules decided first' : 'Keyword check only: never calls an AI';
  return `<h4>${enc(side.label)}</h4>
   <dl class="cmp-lines"><div><dt>Vendor</dt><dd>${enc(purchase.vendor_name)}</dd></div>
    <div><dt>Item</dt><dd>${enc(purchase.item_name)} × ${enc(purchase.quantity)}</dd></div>
    <div><dt>Amount</dt><dd class="num">${enc(usd(side.amount_usd))}</dd></div>
    <div><dt>Purpose</dt><dd class="cmp-text">${enc(purchase.purpose) || '<span class="cmp-none">none given</span>'}</dd></div>
    ${purchase.target ? `<div><dt>Target</dt><dd class="cmp-text">${enc(purchase.target)}</dd></div>` : ''}</dl>
   <span class="stamp ${cls}${REDUCED ? '' : ' land'}">${word}</span>
   <p class="cmp-why"><strong>${enc(CMP_LAYER[side.layer] || cmpWords(side.layer))}:</strong> ${enc(side.reason)}</p>
   <p class="cmp-meta">${meta}</p>`; }
async function runCompare() { const b = $('cmpbtn'), st = $('cmpstatus'); const id = $('cmpcase').value;
  const body = id ? {case_id: id} : {vendor: $('cmpvendor').value, item: $('cmpitem').value, quantity: 1,
    purpose: $('cmppurpose').value.slice(0, 300), target: $('cmptarget').value.trim().slice(0, 300)};
  b.disabled = true; st.textContent = 'Checking both guards…';
  try {
    const r = await fetch('/api/compare', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const d = j.detail; st.textContent = r.status === 429 ? 'Comparison limit reached for now. Try again later.'
      : typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : 'That purchase could not be compared.'; return; }
    $('cmpl').innerHTML = cmpReceipt(j.rules, j.purchase, j.rules.latency_ms);
    $('cmpr').innerHTML = cmpReceipt(j.ai, j.purchase, j.ai.latency_ms);
    st.textContent = `Dry run: ${j.paypal_calls} PayPal calls, ${j.ledger_writes} ledger writes, ${j.fetches} fetches.` + (j.expected ? ` The eval label for this purchase is "${j.expected}".` : '');
  } catch (e) { st.textContent = 'Network error. Nothing was bought.'; }
  finally { b.disabled = false; } }

// ---- boot ----
initMotion();
const refreshLive = () => fetch('/healthz').then(r => r.json()).then(setLive).catch(() => {}); setInterval(refreshLive, 30000);
fetch('/healthz').then(r => r.json()).then(h => { setLive(h); if (h.paypal_mode !== 'live') $('cardbtn').style.display = '';
  if (h.demo_mode) { $('demo').style.display = ''; if (h.demo_policy) $('dpolicy').textContent = '"' + h.demo_policy + '"'; catalog(); dload(); mcpPanel(); }
  else $('nodemo').style.display = ''; });
catalog().catch(() => {}).finally(initFeed); scorecard(); initCompare();
window.addEventListener('load', initSchedules);
{ const q = new URLSearchParams(location.search);
  if (q.get('payment_error')) { $('report').textContent = 'PayPal payment was not completed (it may have been cancelled, already paid another way, or not approved yet). Nothing was charged twice; try the approval link again if needed.'; toast('warn', 'PayPal payment not completed', 'Nothing was charged twice.'); }
  else if (q.get('approved')) { $('report').textContent = 'Purchase ' + q.get('approved') + ' approved and paid through PayPal.'; toast('ok', 'Purchase approved and paid through PayPal'); }
  else if (q.get('funded')) { $('report').textContent = 'Tab funded through PayPal.'; toast('ok', 'Tab funded through PayPal'); }
  if (q.get('payment_error') || q.get('approved') || q.get('funded')) $('ownerhint').open = true; }
load(); setInterval(load, 3000); setInterval(dload, 4000);

