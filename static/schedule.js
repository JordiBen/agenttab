// AgentTab schedule view, built on Bryntum Scheduler (45-day trial, loaded from npm via jsDelivr; not bundled in this repo).
// Rows: the agent (tab top-ups, PayPal refunds, PayPal webhooks) and each vendor (30-day budget, refund windows,
// pending approvals, SaaS renewals). The scheduling agent on the server expires stale approvals and proposes
// refunds before windows close; a human confirms each proposal from the event menu or the list below.
(function () {
  const S = window.bryntum && window.bryntum.scheduler;
  // Always escaped, with or without Bryntum (the suggestions list below is HTML and reasons can come from an agent).
  const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  const enc = t => String(t ?? '').replace(/[&<>"']/g, c => ESC[c]);
  const ICON = {delivered: 'fa-check', budget: 'fa-wallet', refund_window: 'fa-rotate-left', approval: 'fa-user-check', renewal: 'fa-repeat',
    topup: 'fa-circle-plus', paypal_refund: 'fa-paypal', webhook: 'fa-bolt', denied: 'fa-ban',
    declined: 'fa-circle-xmark', voided: 'fa-circle-minus'};

  // Event names come from the server ("Stoneware mug sample $18.00 · refundable"); the bar shows a version that fits
  // (item names trimmed first, the amount kept), and the tooltip always has the full text.
  function shortLabel(e, max) {
    const name = String(e.name || '');
    if (name.length <= max) return name;
    const m = name.match(/^(.*?)(\s\$\d+\.\d\d)(.*)$/);           // keep "$18.00", trim the words before it
    if (m && (m[1] + m[2]).length <= max) return m[1] + m[2];     // drop the suffix ("· refundable") first
    if (m && max > m[2].length + 3) {
      const head = m[1].slice(0, Math.max(1, max - m[2].length - 1)).replace(/\s+\S*$/, '') || m[1].slice(0, max - m[2].length - 1);
      return head + '…' + m[2];
    }
    return name.slice(0, Math.max(1, max - 1)) + '…';
  }

  // When the timeline can't be shown (Bryntum didn't load, threw while building, or covers itself with its
  // "Trial expired" mask) the same /api/schedule data is shown as an AG Grid list with the same actions.
  // Bryntum's own trial and verify logic is left alone: we only look at what it puts on screen.
  const NOTE = 'Timeline view unavailable — showing the same schedule as a list.';
  const WATCH_MS = 90000;     // Bryntum's own check runs about 60 s after load; watch a little longer, then stop
  const when = v => v ? new Date(v).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}) : '';

  window.makeSchedule = function (el, opts) {
    // opts: {url, headers(), act(path) -> Promise, pAct(pid, act) -> Promise, grid: AG Grid options, listEl, onUnauthorized}
    let sch = null, list = null, last = null, mo = null, stopT = null;

    // One list of actions per event, used by the timeline's event menu and by the fallback list alike.
    // Approve / Reject / Refund always go through opts.pAct (app.js pAct), the same call as the Purchases table.
    function actionsFor(e) {
      const pid = e.purchaseId, vendor = e.funding === 'vendor', out = [];
      if (e.suggestion) {
        out.push({key: 'confirm', text: 'Confirm suggested refund', short: 'Confirm refund', icon: 'fa-check', run: () => opts.act(`/api/suggestions/${e.suggestion.id}/confirm`)});
        out.push({key: 'dismiss', text: 'Dismiss suggestion', short: 'Dismiss', icon: 'fa-xmark', alt: true, run: () => opts.act(`/api/suggestions/${e.suggestion.id}/dismiss`)});
      }
      if (e.type === 'approval' && e.status === 'pending_approval') {
        out.push({key: 'approve', text: vendor ? 'Approve' : 'Approve (pay with sandbox card)', short: 'Approve', icon: 'fa-credit-card', alt: true,
          run: () => opts.pAct(pid, vendor ? 'approve' : 'approve-sandbox-card')});
        out.push({key: 'reject', text: 'Reject', short: 'Reject', icon: 'fa-ban', run: () => opts.pAct(pid, 'reject')});
      }
      if (e.type === 'refund_window' && e.status === 'completed') {
        if (!e.suggestion) out.push({key: 'refund', text: vendor ? 'Request refund' : 'Refund now', short: vendor ? 'Request refund' : 'Refund',
          icon: 'fa-rotate-left', run: () => opts.pAct(pid, 'refund')});
        if (!e.used) out.push({key: 'used', text: 'Mark as used (no refund suggestion)', short: 'Mark used', icon: 'fa-box-open',
          run: () => opts.act(`/api/purchases/${pid}/used`)});
      }
      return out;
    }

    function rowsOf(j) {
      const res = {};
      for (const r of j.resources) res[r.id] = r.kind === 'vendor' ? (r.short || r.name) : r.name;
      return j.events.map(e => ({id: String(e.id), row: res[e.resourceId] || '', event: String(e.name || ''),
        start: e.startDate ? +new Date(e.startDate) : null,
        end: e.endDate && e.endDate !== e.startDate ? +new Date(e.endDate) : null,
        status: e.suggestion ? 'refund suggested' : String(e.status || '').replace(/_/g, ' '), e}));
    }

    function actionsCell(p) {
      const box = document.createElement('div'); box.className = 'schedfb-acts';
      for (const a of actionsFor(p.data.e)) {
        const b = document.createElement('button'); b.type = 'button'; b.className = 'act' + (a.alt ? ' alt' : '');
        b.textContent = a.short; b.onclick = a.run; box.append(b);
      }
      return box;
    }

    function fallback(why) {
      if (list) return;
      stopWatch();
      if (sch) { try { sch.destroy(); } catch (err) { /* already broken: the container is cleared below */ } sch = null; }
      el.replaceChildren();
      el.dataset.view = 'list'; el.dataset.fallback = why;
      const note = document.createElement('p'); note.className = 'schedfb-note'; note.textContent = NOTE;
      el.append(note);
      if (!window.agGrid) return;
      const box = document.createElement('div'); box.className = 'schedfb';
      el.append(box);
      list = agGrid.createGrid(box, {...(opts.grid || {}), rowData: [], getRowId: p => p.data.id,
        tooltipShowDelay: 300,
        columnDefs: [
          {field: 'row', headerName: 'Row', flex: 1, minWidth: 110},
          {field: 'event', headerName: 'Event', flex: 2.2, minWidth: 180, tooltipField: 'event'},
          {field: 'start', headerName: 'Start', minWidth: 112, maxWidth: 130, sort: 'desc', valueFormatter: p => when(p.value)},
          {field: 'end', headerName: 'End', minWidth: 112, maxWidth: 130, valueFormatter: p => when(p.value)},
          {field: 'status', headerName: 'Status', minWidth: 120, maxWidth: 150},
          {colId: 'actions', headerName: 'Actions', minWidth: 196, flex: 1.2, sortable: false, filter: false, cellRenderer: actionsCell}]});
      if (last) list.setGridOption('rowData', rowsOf(last));   // otherwise the caller's next refresh() fills it
    }

    function expiredMask() {
      return [...el.querySelectorAll('.b-mask')].some(m => /trial expired/i.test(m.textContent || ''));
    }
    function stopWatch() {
      if (mo) { mo.disconnect(); mo = null; }
      if (stopT) { clearTimeout(stopT); stopT = null; }
    }
    function watch() {
      if (expiredMask()) return fallback('trial-expired');
      mo = new MutationObserver(() => { if (expiredMask()) fallback('trial-expired'); });
      mo.observe(el, {childList: true, subtree: true, characterData: true});
      stopT = setTimeout(stopWatch, WATCH_MS);
    }

    if (!S) { fallback('not-loaded'); return controller(); }
    try { sch = buildTimeline(); }
    catch (err) { console.warn('Schedule timeline could not be built; showing the list instead.', err); fallback('error'); return controller(); }
    el.dataset.view = 'timeline';
    watch();
    return controller();

    function controller() {
      return {refresh, clear, get scheduler() { return sch; }, get view() { return list ? 'list' : 'timeline'; }};
    }

    function buildTimeline() {
    const {Scheduler, EventModel, ResourceModel, DateHelper} = S;
    class AtEvent extends EventModel {
      static fields = ['type', 'purchaseId', 'amount', 'status', 'reason', 'funding', 'suggestion', 'confirmed',
        'flag', 'used', 'percent'];
    }
    class AtResource extends ResourceModel { static fields = ['kind', 'detail', 'agent', 'budget', 'short']; }
    const now = new Date();
    const sch = new Scheduler({
      appendTo: el, autoHeight: true, minHeight: 300, rowHeight: 48, barMargin: 9, eventStyle: 'tonal',
      startDate: DateHelper.add(now, -8, 'day'), endDate: DateHelper.add(now, 31, 'day'),
      viewPreset: {base: 'weekAndDay', tickWidth: 46,
        headers: [{unit: 'week', dateFormat: 'MMM D'}, {unit: 'day', dateFormat: 'D'}]},
      milestoneLayoutMode: 'estimate', milestoneTextPosition: 'always-outside',
      resourceStore: {modelClass: AtResource}, eventStore: {modelClass: AtEvent},
      columns: [{text: 'Agent / vendor', field: 'name', width: innerWidth < 800 ? 124 : 250, htmlEncode: false,
        renderer: ({record}) => `<div class="sch-res ${record.kind}"><i class="fa ${record.kind === 'agent' ? 'fa-robot' : record.kind === 'paypal' ? 'fa-bolt' : 'fa-store'}"></i>
           <div style="min-width:0;flex:1"><b>${enc(record.kind === 'vendor' ? record.short || record.name : record.name)}</b>${record.budget ? `<div class="sch-bud"><span class="sch-bud-bar"><i style="width:${Math.min(100, record.budget.percent)}%"></i></span><span>${enc(record.budget.text)}</span></div>`
             : `<div class="sch-sub">${enc(record.detail)}</div>`}</div></div>`,
        // Vendor rows show the short name; the full name is in the cell tooltip.
        tooltipRenderer: ({record}) => record.kind === 'vendor' && record.short && record.short !== record.name ? enc(record.name) : null}],
      features: {
        cellTooltip: {hoverDelay: 300},
        stickyEvents: false, eventDrag: false, eventResize: false, eventEdit: false, eventDragCreate: false, scheduleMenu: false,
        timeRanges: {showCurrentTimeLine: true},
        // Short events (a 24 h approval is one day wide) carry their label to the right instead of clipping it.
        labels: {right: {renderer: ({eventRecord: e}) => e.type === 'approval' ? shortLabel(e, 44) : ''}},
        eventTooltip: {
          template: ({eventRecord: e}) => `<div class="sch-tip"><b>${enc(e.name)}</b>
            <div>${DateHelper.format(e.startDate, 'MMM D HH:mm')}${+e.endDate !== +e.startDate ? ' → ' + DateHelper.format(e.endDate, 'MMM D HH:mm') : ''}</div>
            ${e.status ? `<div>Status: <b>${enc(e.status)}</b>${e.funding ? ' · paid from ' + enc(e.funding === 'direct' ? 'PayPal approval' : 'tab') : ''}${e.confirmed ? ' · PayPal webhook confirmed' : ''}${e.used ? ' · used' : ''}</div>` : ''}
            ${e.flag ? `<div style="color:#c0392b">PayPal: capture ${enc(e.flag)}</div>` : ''}
            ${e.reason ? `<div class="sch-reason">${enc(e.reason).slice(0, 300)}</div>` : ''}
            ${e.suggestion ? `<div class="sch-sugg"><b>Suggestion:</b> ${enc(e.suggestion.reason)}<br><i>Right-click → confirm or dismiss</i></div>` : ''}</div>`
        },
        eventMenu: {
          processItems({eventRecord: e, items}) {
            Object.keys(items).forEach(k => items[k] = false);
            for (const a of actionsFor(e)) items[a.key] = {text: a.text, icon: 'fa ' + a.icon, weight: 100, onItem: a.run};
            return Object.values(items).some(Boolean);
          }
        }
      },
      eventRenderer({eventRecord: e, renderData}) {
        renderData.iconCls = 'fa ' + (e.suggestion ? 'fa-lightbulb' : (ICON[e.type] || 'fa-circle'));
        if (e.suggestion) renderData.cls['sch-has-sugg'] = 1;
        if (e.type === 'budget') return '';   // the figure is shown in the vendor's row header; the bar keeps the tooltip
        if (e.type === 'approval') return '';                 // label is drawn to the right (labels feature)
        // Fit the label to the bar: ~7 px per character after the icon; milestones get a fixed budget outside.
        const room = e.isMilestone ? 34 : Math.max(4, Math.floor(((renderData.width || 120) - 36) / 8.2));
        return enc(shortLabel(e, room)) + (e.confirmed ? ' · confirmed' : '');
      }
    });
    return sch;
    }

    async function refresh() {
      const r = await fetch(opts.url(), {headers: opts.headers()});
      if (r.status === 401 && opts.onUnauthorized) { opts.onUnauthorized(); return; }
      if (!r.ok) return;
      const j = await r.json();
      last = j;
      if (list) list.setGridOption('rowData', rowsOf(j));
      else if (sch) {
        try { drawTimeline(j); }
        catch (err) { console.warn('Schedule timeline failed to draw; showing the list instead.', err); fallback('error'); }
      }
      suggestions(j);
      return j;
    }
    function drawTimeline(j) {
      const {DateHelper} = S;
      // Each vendor's 30-day budget figure goes into its row header ("$18.00 of $200.00 · 9%").
      const bud = {};
      for (const e of j.events) if (e.type === 'budget') bud[e.resourceId] = {percent: e.percent || 0,
        text: String(e.name).replace(/^30-day budget:\s*/, '') + ' · ' + (e.percent || 0) + '%'};
      sch.resourceStore.data = j.resources.map(r => bud[r.id] ? {...r, budget: bud[r.id]} : r);
      sch.eventStore.data = j.events;
      // Open the timeline 3 days before "now", so today's events and their labels are in view (once per page load).
      if (!sch._atScrolled && j.events.length) { sch._atScrolled = true;
        Promise.resolve(sch.scrollToDate(DateHelper.add(new Date(), innerWidth < 800 ? -1 : -3, 'day'), {block: 'start', animate: false})).catch(() => {}); }
    }
    function suggestions(j) {
      if (opts.listEl) {
        opts.listEl.innerHTML = j.suggestions.length
          ? '<b>Refund suggestions from the scheduling agent</b> (nothing is refunded until you confirm):' + j.suggestions.map(s =>
            `<div class="sugg"><span><b>Suggestion:</b> ${enc(s.reason)} <small>(window closes ${new Date(s.due * 1000).toLocaleString()})</small></span>
             <button class="act" data-p="/api/suggestions/${s.id}/confirm">Confirm refund</button>
             <button class="act alt" data-p="/api/suggestions/${s.id}/dismiss">Dismiss</button></div>`).join('')
          : '<small>No refund suggestions right now. The scheduling agent proposes refunds for duplicates, unused items within 48 h of their deadline, and captures PayPal reports as reversed.</small>';
        opts.listEl.querySelectorAll('button[data-p]').forEach(b => b.onclick = () => opts.act(b.dataset.p));
      }
    }
    function clear() {
      last = null;
      if (list) list.setGridOption('rowData', []);
      else if (sch) { sch.eventStore.data = []; sch.resourceStore.data = []; }
      if (opts.listEl) opts.listEl.innerHTML = '';
    }
  };
})();
