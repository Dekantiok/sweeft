/* VeriPay trade co-pilot — floating chat panel.
 *
 * The agent proposes; the wallet authorizes. This panel never signs anything.
 */
(function () {
  const PANEL_ID = 'copilot-panel';
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem('vp-copilot') || '[]'); } catch (e) { history = []; }

  function persist() {
    try { sessionStorage.setItem('vp-copilot', JSON.stringify(history.slice(-20))); } catch (e) {}
  }

  function el(html) {
    const d = document.createElement('div');
    d.innerHTML = html;
    return d.firstElementChild;
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = el(`
      <div id="${PANEL_ID}" style="position:fixed;bottom:76px;right:16px;width:min(420px,calc(100vw - 32px));max-height:70vh;display:none;flex-direction:column;background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.18);z-index:80;overflow:hidden">
        <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#0f172a;color:#fff">
          <b style="font-size:14px">Trade co-pilot</b>
          <span style="font-size:11px;opacity:.7">AI proposes · wallet signs</span>
          <div style="flex:1"></div>
          <button id="copilot-close" style="background:none;border:none;color:#fff;font-size:18px;cursor:pointer">×</button>
        </div>
        <div id="copilot-messages" style="flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px"></div>
        <form id="copilot-form" style="display:flex;gap:8px;padding:10px;border-top:1px solid #e2e8f0">
          <input id="copilot-input" placeholder='Try “buy solar inverters under 1500 tUSD”' style="flex:1;border:1px solid #cbd5e1;border-radius:8px;padding:8px 10px;font-size:13px" autocomplete="off">
          <button style="background:#059669;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer">Send</button>
        </form>
      </div>
    `);
    document.body.appendChild(panel);
    panel.querySelector('#copilot-close').onclick = () => togglePanel(false);
    panel.querySelector('#copilot-form').onsubmit = (e) => {
      e.preventDefault();
      send();
    };
    return panel;
  }

  function togglePanel(open) {
    const panel = ensurePanel();
    panel.style.display = open === undefined ? (panel.style.display === 'flex' ? 'none' : 'flex') : (open ? 'flex' : 'none');
  }

  function addMessage(role, text) {
    history.push({ role, text });
    persist();
    const box = document.getElementById('copilot-messages');
    const isUser = role === 'user';
    const node = el(`<div style="max-width:88%;padding:8px 10px;border-radius:10px;font-size:13px;white-space:pre-wrap;${isUser ? 'align-self:flex-end;background:#059669;color:#fff' : 'align-self:flex-start;background:#f1f5f9;color:#0f172a'}"></div>`);
    node.textContent = text;
    box.appendChild(node);
    box.scrollTop = box.scrollHeight;
  }

  function addTrace(trace) {
    if (!trace || !trace.length) return;
    const box = document.getElementById('copilot-messages');
    const node = el('<div style="align-self:flex-start;display:flex;gap:4px;flex-wrap:wrap"></div>');
    trace.forEach((t) => {
      node.appendChild(el(`<span style="font-size:10px;background:#eef2ff;color:#3730a3;border-radius:999px;padding:2px 7px">${t.kind === 'prepare' ? '⚙ prepare' : '🔎 read'}: ${t.tool}</span>`));
    });
    box.appendChild(node);
    box.scrollTop = box.scrollHeight;
  }

  function addCards(cards) {
    if (!cards || !cards.length) return;
    const box = document.getElementById('copilot-messages');
    cards.forEach((c) => {
      const lines = [];
      if (c.listing) lines.push(`<b>${escapeHtml(c.listing)}</b>`);
      if (c.seller) lines.push(`<span style="font-size:12px">Seller: <code>${escapeHtml(c.seller)}</code></span>`);
      if (c.seller_score) lines.push(`<span style="font-size:12px">${escapeHtml(c.seller_score)}</span>`);
      if (c.totals) lines.push(`<span style="font-size:12px">Total: ${Number(c.totals.total_amount) / 1e6} tUSD (seller ${Number(c.totals.seller_amount) / 1e6} + logistics ${Number(c.totals.logistics_amount) / 1e6} + fee ${Number(c.totals.platform_fee) / 1e6})</span>`);
      if (c.buyer_balance) lines.push(`<span style="font-size:12px">Your balance: ${escapeHtml(c.buyer_balance)} tUSD — affordable: ${c.affordable ? 'yes' : 'no'}</span>`);
      if (c.total_amount) lines.push(`<span style="font-size:12px">Total to fund: ${Number(c.total_amount) / 1e6} tUSD</span>`);
      if (c.reason) lines.push(`<span style="font-size:12px">Claim: ${escapeHtml(c.reason.slice(0, 220))}</span>`);
      if (c.note) lines.push(`<span style="font-size:11px;color:#64748b">${escapeHtml(c.note)}</span>`);
      const card = el(`<div style="border:1px solid #a7f3d0;background:#ecfdf5;border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:4px">
        ${lines.join('')}
        <a href="${c.url}" style="margin-top:6px;display:inline-block;background:#059669;color:#fff;text-align:center;border-radius:8px;padding:7px 10px;font-size:13px;text-decoration:none">Open in wallet flow →</a>
      </div>`);
      box.appendChild(card);
    });
    box.scrollTop = box.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function send() {
    const input = document.getElementById('copilot-input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    addMessage('user', message);
    addMessage('agent', 'Thinking…');
    try {
      const res = await fetch('/api/v1/ai/chat/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      // Replace the loading message.
      const box = document.getElementById('copilot-messages');
      if (box.lastElementChild) box.lastElementChild.remove();
      if (!data.ok) throw new Error(data.error || 'agent error');
      addMessage('agent', data.reply);
      addTrace(data.tool_trace);
      addCards(data.cards);
    } catch (e) {
      const box = document.getElementById('copilot-messages');
      if (box.lastElementChild) box.lastElementChild.remove();
      addMessage('agent', 'Error: ' + e.message);
    }
  }

  function init() {
    const bubble = document.createElement('button');
    bubble.id = 'copilot-bubble';
    bubble.textContent = '💬';
    bubble.title = 'Trade co-pilot — AI proposes, wallet signs';
    bubble.style.cssText = 'position:fixed;bottom:16px;right:16px;width:48px;height:48px;border-radius:50%;background:#059669;color:#fff;border:none;font-size:22px;box-shadow:0 8px 24px rgba(0,0,0,.25);cursor:pointer;z-index:80';
    bubble.onclick = () => togglePanel();
    document.body.appendChild(bubble);
  }

  window.VPCopilot = { toggle: togglePanel, init };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
