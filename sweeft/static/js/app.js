/* SWEEFT app glue: wallet login (NEP-413) and shared helpers. */
(function () {
  const state = {
    network: 'testnet',
    contractId: '',
    explorer: '',
    walletAccount: null,
    cfg: null,
  };

  async function cfg() {
    if (!state.cfg) {
      const r = await fetch('/api/v1/config/');
      state.cfg = await r.json();
    }
    return state.cfg;
  }

  function requireWallet() {
    if (!state.walletAccount) {
      alert('Connect your wallet first.');
      SweftWallet.showModal();
      return false;
    }
    return true;
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  async function connect() {
    try {
      const c = await cfg();
      const network = (state.network || c.network || 'testnet').trim() || 'testnet';
      await SweftWallet.init({ network, contractId: state.contractId || c.sweeft_contract });
      SweftWallet.showModal();
      SweftWallet.onChange(async (accountId) => {
        if (accountId && accountId !== state.walletAccount) {
          await login(accountId);
        }
      });
    } catch (e) {
      console.error('Wallet connect failed', e);
      alert('Wallet connect failed: ' + (e && e.message ? e.message : e));
    }
  }

  async function login(accountId) {
    try {
      const chRes = await fetch(`/api/v1/wallet/challenge/?account_id=${encodeURIComponent(accountId)}`);
      const ch = await chRes.json();
      // wallet-selector core requires a real Buffer of exactly 32 bytes.
      const nonceBytes = Buffer.from(ch.nonce, 'base64');
      const signed = await SweftWallet.signLogin(ch.message, ch.recipient, nonceBytes);
      const res = await fetch('/api/v1/wallet/verify/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({
          account_id: signed.accountId || accountId,
          public_key: signed.publicKey,
          signature: signed.signature,
          nonce: ch.nonce,
        }),
      });
      const data = await res.json();
      if (!data.ok) {
        alert('Login failed: ' + (data.error || 'unknown error'));
        return;
      }
      window.location.reload();
    } catch (e) {
      alert('Login failed: ' + (e && e.message ? e.message : e));
    }
  }

  async function disconnect() {
    // 1. Clear the server session.
    try {
      await fetch('/api/v1/wallet/logout/', { method: 'POST', headers: { 'X-CSRFToken': csrfToken() } });
    } catch (e) {}
    // 2. Disconnect the wallet module. The selector is only initialised by
    //    connect() on a fresh page load, so initialise it here first —
    //    otherwise signOut silently no-ops and the wallet stays persisted.
    try {
      const c = await cfg();
      const network = (state.network || c.network || 'testnet').trim() || 'testnet';
      await SweftWallet.init({ network, contractId: state.contractId || c.sweeft_contract });
      await SweftWallet.signOut();
    } catch (e) {
      console.warn('wallet disconnect failed', e);
    }
    // 3. Guarantee clean state even if the module threw.
    try {
      Object.keys(localStorage)
        .filter((k) => k.startsWith('near-wallet-selector'))
        .forEach((k) => localStorage.removeItem(k));
    } catch (e) {}
    state.walletAccount = null;
    window.location.reload();
  }

  async function callContract(method, args, deposit = '0', gas = '150000000000000') {
    if (!requireWallet()) throw new Error('wallet not connected');
    const c = await cfg();
    return SweftWallet.callMethod(c.sweeft_contract, method, args, gas, deposit);
  }

  // Call any contract (e.g. the governance DAO: add_proposal / act_proposal).
  async function callContractOn(contractId, method, args, deposit = '0', gas = '150000000000000') {
    if (!requireWallet()) throw new Error('wallet not connected');
    return SweftWallet.callMethod(contractId, method, args, gas, deposit);
  }

  function explorerTx(hash) {
    return hash ? `${state.explorer}/txns/${hash}` : null;
  }

  async function jsonPost(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify(body || {}),
    });
    const text = await r.text();
    try {
      return text ? JSON.parse(text) : {};
    } catch (e) {
      // Never let an HTML error page surface as a JSON parse mystery.
      throw new Error(`${url} → HTTP ${r.status}: ${text.slice(0, 120).replace(/<[^>]*>/g, ' ').trim()}`);
    }
  }

  async function jsonGet(url) {
    const r = await fetch(url, { headers: { Accept: 'application/json' } });
    const text = await r.text();
    try {
      return text ? JSON.parse(text) : {};
    } catch (e) {
      throw new Error(`${url} → HTTP ${r.status}: ${text.slice(0, 120).replace(/<[^>]*>/g, ' ').trim()}`);
    }
  }

  // ------------------------------------------------------------------
  // Native NEAR helpers: balance display + faucet link.
  // ------------------------------------------------------------------

  async function nativeBalance(accountId) {
    const c = await cfg();
    const r = await fetch(c.rpc_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0', id: 'bal', method: 'query',
        params: { request_type: 'view_account', finality: 'final', account_id: accountId },
      }),
    });
    const j = await r.json();
    return BigInt(j.result?.amount || 0);
  }

  async function sendNear(receiverId, amountYocto) {
    if (!requireWallet()) throw new Error('wallet not connected');
    return SweftWallet.sendNear(receiverId, amountYocto);
  }

  function fmtUnits(units) {
    const s = units.toString().padStart(25, '0');
    const whole = s.slice(0, -24).replace(/^0+(?=\d)/, '') || '0';
    const frac = s.slice(-24).replace(/0+$/, '').slice(0, 6);
    return frac ? `${whole}.${frac}` : whole;
  }

  async function refreshBalance() {
    const el = document.getElementById('near-balance');
    if (!el || !state.walletAccount) return;
    try {
      el.textContent = fmtUnits(await nativeBalance(state.walletAccount)) + ' NEAR';
    } catch (e) {
      el.textContent = '— NEAR';
    }
  }

  async function faucet() {
    // Judges already hold testnet NEAR. For a fresh wallet, open the public
    // faucet rather than requiring a project-specific token mint.
    window.open('https://near-faucet.io/', '_blank', 'noopener,noreferrer');
  }

  // ------------------------------------------------------------------
  // Session vs wallet-connection mismatch
  // ------------------------------------------------------------------

  function showWalletMismatchBanner() {
    if (document.getElementById('wallet-mismatch')) return;
    const bar = document.createElement('div');
    bar.id = 'wallet-mismatch';
    bar.style.cssText =
      'position:sticky;top:0;z-index:60;background:#fef3c7;border-bottom:1px solid #fde68a;' +
      'color:#92400e;font-size:13px;padding:8px 16px;display:flex;gap:12px;align-items:center;justify-content:center;flex-wrap:wrap';
    bar.innerHTML =
      '<span>Your wallet app is disconnected in this browser — sign in again to act on escrows.</span>' +
      '<button id="wm-reconnect" style="border:1px solid #b45309;border-radius:6px;padding:2px 10px;background:#fff">Reconnect</button>' +
      '<button id="wm-signout" style="border:1px solid #d1d5db;border-radius:6px;padding:2px 10px;background:#fff;color:#374151">Sign out</button>';
    document.body.prepend(bar);
    document.getElementById('wm-reconnect').onclick = () => connect();
    document.getElementById('wm-signout').onclick = () => disconnect();
  }

  function hideWalletMismatchBanner() {
    const bar = document.getElementById('wallet-mismatch');
    if (bar) bar.remove();
  }

  async function checkWalletConnection() {
    if (!state.walletAccount) return;
    try {
      const connected = await SweftWallet.isConnected();
      if (!connected) showWalletMismatchBanner();
      else hideWalletMismatchBanner();
    } catch (e) {
      /* leave the UI as-is */
    }
  }

  function init(options) {
    Object.assign(state, options || {});
    window.SweftApp = window.SweftApp || {};
    Object.assign(window.SweftApp, {
      state, cfg, connect, disconnect, login, callContract,
      callContractOn, sendNear,
      explorerTx, jsonPost, jsonGet, requireWallet, nativeBalance, refreshBalance, faucet,
      checkWalletConnection,
    });
    if (state.walletAccount) {
      // Logged-in session: warm up the wallet selector so on-chain actions work
      // without re-clicking Connect, then flag a disconnected wallet clearly.
      cfg()
        .then((c) => SweftWallet.init({
          network: (state.network || c.network || 'testnet').trim() || 'testnet',
          contractId: state.contractId || c.sweeft_contract,
        }))
        .then(() => checkWalletConnection())
        .catch((e) => console.warn('wallet warm-up failed', e));
      refreshBalance();
    }
  }

  window.SweftApp = { init };
})();
