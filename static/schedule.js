// AgentTab schedule view, built on Bryntum Scheduler (45-day trial, loaded from npm via jsDelivr; not bundled in this repo).
// Rows: the agent (tab top-ups, PayPal refunds, PayPal webhooks) and each vendor (30-day budget, refund windows,
// pending approvals, SaaS renewals). The scheduling agent on the server expires stale approvals and proposes
// refunds before windows close; a human confirms each proposal from the event menu or the list below.
(function () {
  const S = window.bryntum && window.bryntum.scheduler;
  const enc = t => S ? S.StringHelper.encodeHtml(String(t ?? '')) : String(t ?? '');
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

  window.makeSchedule = function (el, opts) {
    // opts: {url, headers(), act(path) -> Promise, listEl}
    if (!S) { el.textContent = 'Bryntum Scheduler failed to load.'; return null; }
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
            const add = (key, text, icon, path) => items[key] = {text, icon: 'fa ' + icon, weight: 100,
              onItem: () => opts.act(path)};
            const pid = e.purchaseId;
            if (e.suggestion) {
              add('confirm', 'Confirm suggested refund', 'fa-check', `/api/suggestions/${e.suggestion.id}/confirm`);
              add('dismiss', 'Dismiss suggestion', 'fa-xmark', `/api/suggestions/${e.suggestion.id}/dismiss`);
            }
            if (e.type === 'approval' && e.status === 'pending_approval') {
              add('approve', 'Approve (pay with sandbox card)', 'fa-credit-card', `/api/purchases/${pid}/approve-sandbox-card`);
              add('reject', 'Reject', 'fa-ban', `/api/purchases/${pid}/reject`);
            }
            if (e.type === 'refund_window' && e.status === 'completed') {
              if (!e.suggestion) add('refund', 'Refund now', 'fa-rotate-left', `/api/purchases/${pid}/refund`);
              if (!e.used) add('used', 'Mark as used (no refund suggestion)', 'fa-box-open', `/api/purchases/${pid}/used`);
            }
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
    async function refresh() {
      const r = await fetch(opts.url(), {headers: opts.headers()});
      if (r.status === 401 && opts.onUnauthorized) { opts.onUnauthorized(); return; }
      if (!r.ok) return;
      const j = await r.json();
      // Each vendor's 30-day budget figure goes into its row header ("$18.00 of $200.00 · 9%").
      const bud = {};
      for (const e of j.events) if (e.type === 'budget') bud[e.resourceId] = {percent: e.percent || 0,
        text: String(e.name).replace(/^30-day budget:\s*/, '') + ' · ' + (e.percent || 0) + '%'};
      sch.resourceStore.data = j.resources.map(r => bud[r.id] ? {...r, budget: bud[r.id]} : r);
      sch.eventStore.data = j.events;
      // Open the timeline 3 days before "now", so today's events and their labels are in view (once per page load).
      if (!sch._atScrolled && j.events.length) { sch._atScrolled = true;
        Promise.resolve(sch.scrollToDate(DateHelper.add(new Date(), innerWidth < 800 ? -1 : -3, 'day'), {block: 'start', animate: false})).catch(() => {}); }
      if (opts.listEl) {
        opts.listEl.innerHTML = j.suggestions.length
          ? '<b>Refund suggestions from the scheduling agent</b> (nothing is refunded until you confirm):' + j.suggestions.map(s =>
            `<div class="sugg"><span><b>Suggestion:</b> ${enc(s.reason)} <small>(window closes ${new Date(s.due * 1000).toLocaleString()})</small></span>
             <button class="act" data-p="/api/suggestions/${s.id}/confirm">Confirm refund</button>
             <button class="act alt" data-p="/api/suggestions/${s.id}/dismiss">Dismiss</button></div>`).join('')
          : '<small>No refund suggestions right now. The scheduling agent proposes refunds for duplicates, unused items within 48 h of their deadline, and captures PayPal reports as reversed.</small>';
        opts.listEl.querySelectorAll('button[data-p]').forEach(b => b.onclick = () => opts.act(b.dataset.p));
      }
      return j;
    }
    function clear() {
      sch.eventStore.data = [];
      sch.resourceStore.data = [];
      if (opts.listEl) opts.listEl.innerHTML = '';
    }
    return {scheduler: sch, refresh, clear};
  };
})();
