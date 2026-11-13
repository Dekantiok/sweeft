/* SWEEFT trade co-pilot — floating chat panel.
 *
 * The agent proposes; the user authorizes. Money moves only through the
 * token's on-chain allowance: a single wallet `approve` grants the co-pilot a
 * scoped budget, then payments settle server-side within it (revocable by
 * approving 0). This panel never holds keys.
 */
(function () {
  const PANEL_ID = 'copilot-panel';
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem('vp-copilot') || '[]'); } catch (e) { history = []; }

  const T = {
    bg: '#0f172a',
    bgSoft: '#111c33',
    border: '#334155',
    text: '#e2e8f0',
    dim: '#94a3b8',
    accent: '#10b981',
  };

  function persist() {
    try { sessionStorage.setItem('vp-copilot', JSON.stringify(history.slice(-20))); } catch (e) {}
  }

  function el(html) {
    const d = document.createElement('div');
    d.innerHTML = html;
    return d.firstElementChild;
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function postJson(url) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() } });
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = el(`
      <div id="${PANEL_ID}" style="position:fixed;bottom:76px;right:16px;width:min(430px,calc(100vw - 32px));max-height:70vh;display:none;flex-direction:column;background:${T.bg};border:1px solid ${T.border};border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.5);z-index:80;overflow:hidden;color:${T.text}">
        <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#020617;border-bottom:1px solid ${T.border}">
          <b style="font-size:14px;color:#f8fafc">⚡ Trade co-pilot</b>
          <span style="font-size:11px;color:${T.dim}">AI proposes · you authorize · allowance-scoped</span>
          <div style="flex:1"></div>
          <button id="copilot-close" style="background:none;border:none;color:${T.dim};font-size:18px;cursor:pointer;line-height:1">×</button>
        </div>
        <div id="copilot-messages" style="flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px"></div>
        <form id="copilot-form" style="display:flex;gap:8px;padding:10px;border-top:1px solid ${T.border};background:${T.bgSoft}">
          <input id="copilot-input" placeholder='Try “pay 0.025 NEAR to seller.steepdata3968.testnet”' style="flex:1;background:#020617;color:${T.text};border:1px solid ${T.border};border-radius:8px;padding:8px 10px;font-size:13px" autocomplete="off">
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
    const node = el(`<div style="max-width:88%;padding:8px 10px;border-radius:10px;font-size:13px;white-space:pre-wrap;${isUser ? 'align-self:flex-end;background:#059669;color:#fff' : `align-self:flex-start;background:${T.bgSoft};border:1px solid ${T.border};color:${T.text}`}"></div>`);
    node.textContent = text;
    box.appendChild(node);
    box.scrollTop = box.scrollHeight;
  }

  function addTrace(trace) {
    if (!trace || !trace.length) return;
    const box = document.getElementById('copilot-messages');
    const node = el('<div style="align-self:flex-start;display:flex;gap:4px;flex-wrap:wrap"></div>');
    trace.forEach((t) => {
      node.appendChild(el(`<span style="font-size:10px;background:#312e81;color:#c7d2fe;border-radius:999px;padding:2px 7px">${t.kind === 'prepare' ? '⚙ prepare' : '🔎 read'}: ${escapeHtml(t.tool)}</span>`));
    });
    box.appendChild(node);
    box.scrollTop = box.scrollHeight;
  }

  function statusLine(node) {
    return node.querySelector('.vp-status');
  }

  function setStatus(node, text, kind) {
    const s = statusLine(node);
    s.textContent = text;
    s.style.color = kind === 'ok' ? '#6ee7b7' : kind === 'err' ? '#fda4af' : T.dim;
  }

  function txLink(node, hash, explorer) {
    const s = statusLine(node);
    s.innerHTML = 'Paid ✓ ';
    if (hash) {
      const a = document.createElement('a');
      a.href = explorer || `https://testnet.near.org/tx/${hash}`;
      a.target = '_blank';
      a.rel = 'noreferrer';
      a.textContent = `tx ${String(hash).slice(0, 12)}…`;
      a.style.color = '#6ee7b7';
      s.appendChild(a);
    }
    if (window.SweftApp && SweftApp.refreshTokenBalance) SweftApp.refreshBalance().catch(() => {});
  }

  function toUnits(display) {
    const clean = String(display || '0').replace(/,/g, '').trim();
    const [w = '0', f = ''] = clean.split('.');
    return String(BigInt(w) * 10n ** 24n + BigInt((f + '0'.repeat(24)).slice(0, 24) || '0'));
  }

  async function execPayment(node, intentId) {
    const btns = node.querySelector('.vp-btns');
    btns.style.display = 'none';
    setStatus(node, 'Co-pilot is settling from your budget…');
    try {
      const res = await postJson(`/api/v1/ai/payments/${intentId}/execute/`);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'payment failed');
      txLink(node, data.tx_hash, data.explorer);
    } catch (e) {
      btns.style.display = 'flex';
      setStatus(node, 'Failed: ' + e.message, 'err');
    }
  }

  async function grantAndPay(node, intentId, budgetContract, grantAmount) {
    const btns = node.querySelector('.vp-btns');
    btns.style.display = 'none';
    try {
      setStatus(node, 'Confirm your NEAR budget deposit in your wallet (one signature)…');
      await SweftApp.callContractOn(budgetContract, 'deposit', {}, grantAmount, '100000000000000');
      setStatus(node, 'Budget deposited on-chain ✓ — co-pilot is paying…');
      await execPayment(node, intentId);
    } catch (e) {
      btns.style.display = 'flex';
      setStatus(node, 'Failed: ' + (e && e.message ? e.message : e), 'err');
    }
  }

  async function payWithWallet(node, to, amount, memo) {
    const btns = node.querySelector('.vp-btns');
    btns.style.display = 'none';
    try {
      setStatus(node, 'Confirm the transfer in your wallet…');
      const res = await SweftApp.sendNear(to, amount);
      txLink(node, res && res.txHash);
    } catch (e) {
      btns.style.display = 'flex';
      setStatus(node, 'Failed: ' + (e && e.message ? e.message : e), 'err');
    }
  }

  function paymentCard(c) {
    const node = el(`
      <div style="border:1px solid #065f46;background:rgba(6,78,59,.28);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:4px">
        <div style="font-size:13px;font-weight:600;color:#6ee7b7">⚡ Co-pilot payment</div>
        <div style="font-size:12px">To: <code>${escapeHtml(c.to)}</code></div>
        <div style="font-size:12px">Amount: <b>${escapeHtml(c.amount_display)} NEAR</b>${c.memo ? ` · ${escapeHtml(c.memo)}` : ''}</div>
        <div style="font-size:11px;color:${T.dim}">Your co-pilot budget: ${escapeHtml(c.allowance_display)} NEAR · per-payment limit ${escapeHtml(c.max_payment_display)} NEAR</div>
        <div class="vp-btns" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px"></div>
        <div style="font-size:11px;color:${T.dim}">Deposit exactly the NEAR the co-pilot may spend; the budget contract enforces your limit — withdraw the unspent remainder to revoke.</div>
        <div class="vp-status" style="font-size:11px;color:${T.dim}"></div>
      </div>
    `);
    const btns = node.querySelector('.vp-btns');
    if (c.can_execute) {
      const pay = el('<button style="background:#059669;color:#fff;border:none;border-radius:8px;padding:7px 10px;font-size:12.5px;cursor:pointer">Pay now — co-pilot settles it</button>');
      pay.onclick = () => execPayment(node, c.intent_id);
      btns.appendChild(pay);
    } else {
      const wrap = el('<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"></div>');
      const input = el(`<input value="${escapeHtml(c.grant_display)}" title="Budget to grant the co-pilot (NEAR)" style="width:6rem;background:#020617;border:1px solid #334155;border-radius:8px;padding:6px 8px;font-size:12.5px;color:#e2e8f0">`);
      const grant = el('<button style="background:#059669;color:#fff;border:none;border-radius:8px;padding:7px 10px;font-size:12.5px;cursor:pointer">Grant budget &amp; pay</button>');
      grant.onclick = () => grantAndPay(node, c.intent_id, c.budget_contract, toUnits(input.value));
      wrap.appendChild(input);
      wrap.appendChild(grant);
      btns.appendChild(wrap);
    }
    const wallet = el('<button style="background:none;border:1px solid #475569;color:#cbd5e1;border-radius:8px;padding:7px 10px;font-size:12.5px;cursor:pointer">Pay with my wallet</button>');
    wallet.onclick = () => payWithWallet(node, c.to, c.amount, c.memo);
    btns.appendChild(wallet);
    return node;
  }

  function addCards(cards) {
    if (!cards || !cards.length) return;
    const box = document.getElementById('copilot-messages');
    cards.forEach((c) => {
      const box_ = box;
      if (c.kind === 'payment_proposal') {
        box_.appendChild(paymentCard(c));
        box_.scrollTop = box_.scrollHeight;
        return;
      }
      const lines = [];
      if (c.listing) lines.push(`<b>${escapeHtml(c.listing)}</b>`);
      if (c.seller) lines.push(`<span style="font-size:12px">Seller: <code>${escapeHtml(c.seller)}</code></span>`);
      if (c.seller_score) lines.push(`<span style="font-size:12px">${escapeHtml(c.seller_score)}</span>`);
      if (c.totals) lines.push(`<span style="font-size:12px">Total: ${Number(c.totals.total_amount) / 1e24} NEAR (seller ${Number(c.totals.seller_amount) / 1e24} + logistics ${Number(c.totals.logistics_amount) / 1e24} + fee ${Number(c.totals.platform_fee) / 1e24})</span>`);
      if (c.buyer_balance) lines.push(`<span style="font-size:12px">Your balance: ${escapeHtml(c.buyer_balance)} NEAR — affordable: ${c.affordable ? 'yes' : 'no'}</span>`);
      if (c.total_amount) lines.push(`<span style="font-size:12px">Total to fund: ${Number(c.total_amount) / 1e24} NEAR</span>`);
      if (c.reason) lines.push(`<span style="font-size:12px">Claim: ${escapeHtml(c.reason.slice(0, 220))}</span>`);
      if (c.note) lines.push(`<span style="font-size:11px;color:${T.dim}">${escapeHtml(c.note)}</span>`);
      const card = el(`<div style="border:1px solid #065f46;background:rgba(6,78,59,.2);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:4px">
        ${lines.join('')}
        <a href="${escapeHtml(c.url || '#')}" style="margin-top:6px;display:inline-block;background:#059669;color:#fff;text-align:center;border-radius:8px;padding:7px 10px;font-size:13px;text-decoration:none">Open in wallet flow →</a>
      </div>`);
      box_.appendChild(card);
    });
    box.scrollTop = box.scrollHeight;
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
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
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
    bubble.title = 'Trade co-pilot — AI proposes, you authorize';
    bubble.style.cssText = 'position:fixed;bottom:16px;right:16px;width:48px;height:48px;border-radius:50%;background:#059669;color:#fff;border:none;font-size:22px;box-shadow:0 8px 24px rgba(0,0,0,.45);cursor:pointer;z-index:80';
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
