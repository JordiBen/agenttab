// AgentTab schedule view, built on Bryntum Scheduler (45-day trial, loaded from npm via jsDelivr; not bundled in this repo).
// Rows: the agent (tab top-ups, PayPal refunds, PayPal webhooks) and each vendor (30-day budget, refund windows,
// pending approvals, SaaS renewals). The scheduling agent on the server expires stale approvals and proposes
// refunds before windows close; a human confirms each proposal from the event menu or the list below.
(function () {
  const S = window.bryntum && window.bryntum.scheduler;
  const enc = t => S ? S.StringHelper.encodeHtml(String(t ?? '')) : String(t ?? '');
  const ICON = {budget: 'fa-wallet', refund_window: 'fa-rotate-left', approval: 'fa-user-check', renewal: 'fa-repeat',
    topup: 'fa-circle-plus', paypal_refund: 'fa-paypal', webhook: 'fa-bolt', denied: 'fa-ban'};

  window.makeSchedule = function (el, opts) {
    // opts: {url, headers(), act(path) -> Promise, listEl}
    if (!S) { el.textContent = 'Bryntum Scheduler failed to load.'; return null; }
    const {Scheduler, EventModel, ResourceModel, DateHelper} = S;
    class AtEvent extends EventModel {
      static fields = ['type', 'purchaseId', 'amount', 'status', 'reason', 'funding', 'suggestion', 'confirmed',
        'flag', 'used', 'percent'];
    }
    class AtResource extends ResourceModel { static fields = ['kind', 'detail', 'agent']; }
    const now = new Date();
    const sch = new Scheduler({
      appendTo: el, height: 430, rowHeight: 48, barMargin: 9, eventStyle: 'tonal',
      startDate: DateHelper.add(now, -9, 'day'), endDate: DateHelper.add(now, 32, 'day'),
      viewPreset: 'weekAndDay',
      resourceStore: {modelClass: AtResource}, eventStore: {modelClass: AtEvent},
      columns: [{text: 'Agent / vendor', field: 'name', width: 230, htmlEncode: false,
        renderer: ({record}) => `<div class="sch-res ${record.kind}"><i class="fa ${record.kind === 'agent' ? 'fa-robot' : 'fa-store'}"></i>
           <div><b>${enc(record.name)}</b><div class="sch-sub">${enc(record.detail)}</div></div></div>`}],
      features: {
        eventDrag: false, eventResize: false, eventEdit: false, eventDragCreate: false, scheduleMenu: false,
        timeRanges: {showCurrentTimeLine: true},
        eventTooltip: {
          template: ({eventRecord: e}) => `<div class="sch-tip"><b>${enc(e.name)}</b>
            <div>${DateHelper.format(e.startDate, 'MMM D HH:mm')}${+e.endDate !== +e.startDate ? ' → ' + DateHelper.format(e.endDate, 'MMM D HH:mm') : ''}</div>
            ${e.status ? `<div>Status: <b>${enc(e.status)}</b>${e.funding ? ' · paid from ' + enc(e.funding === 'direct' ? 'PayPal approval' : 'tab') : ''}${e.confirmed ? ' · ✓ PayPal webhook confirmed' : ''}${e.used ? ' · used' : ''}</div>` : ''}
            ${e.flag ? `<div style="color:#c0392b">PayPal: capture ${enc(e.flag)}</div>` : ''}
            ${e.reason ? `<div class="sch-reason">${enc(e.reason).slice(0, 300)}</div>` : ''}
            ${e.suggestion ? `<div class="sch-sugg">💡 ${enc(e.suggestion.reason)}<br><i>Right-click → confirm or dismiss</i></div>` : ''}</div>`
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
        if (e.type === 'budget') return `${enc(e.name)} <span class="sch-pct">${e.percent}%</span>`;
        return enc(e.name) + (e.confirmed ? ' ✓' : '');
      }
    });
    async function refresh() {
      const r = await fetch(opts.url(), {headers: opts.headers()});
      if (!r.ok) return;
      const j = await r.json();
      sch.resourceStore.data = j.resources;
      sch.eventStore.data = j.events;
      if (opts.listEl) {
        opts.listEl.innerHTML = j.suggestions.length
          ? '<b>Refund suggestions from the scheduling agent</b> (nothing is refunded until you confirm):' + j.suggestions.map(s =>
            `<div class="sugg"><span>💡 ${enc(s.reason)} <small>(window closes ${new Date(s.due * 1000).toLocaleString()})</small></span>
             <button class="act" data-p="/api/suggestions/${s.id}/confirm">Confirm refund</button>
             <button class="act alt" data-p="/api/suggestions/${s.id}/dismiss">Dismiss</button></div>`).join('')
          : '<small>No refund suggestions right now. The scheduling agent proposes refunds for duplicates, unused items within 48 h of their deadline, and captures PayPal reports as reversed.</small>';
        opts.listEl.querySelectorAll('button[data-p]').forEach(b => b.onclick = () => opts.act(b.dataset.p));
      }
      return j;
    }
    return {scheduler: sch, refresh};
  };
})();
