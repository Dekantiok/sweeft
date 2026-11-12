/* VeriPay app glue: wallet login (NEP-413) and shared helpers. */
(function () {
  const state = {
    network: 'testnet',
    contractId: '',
    tokenId: '',
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
      VPWallet.showModal();
      return false;
    }
    return true;
  }

  async function connect() {
    try {
      const c = await cfg();
      const network = (state.network || c.network || 'testnet').trim() || 'testnet';
      await VPWallet.init({ network, contractId: state.contractId || c.veripay_contract });
      VPWallet.showModal();
      VPWallet.onChange(async (accountId) => {
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
      const signed = await VPWallet.signLogin(ch.message, ch.recipient, nonceBytes);
      const res = await fetch('/api/v1/wallet/verify/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
      await fetch('/api/v1/wallet/logout/', { method: 'POST' });
    } catch (e) {}
    // 2. Disconnect the wallet module. The selector is only initialised by
    //    connect() on a fresh page load, so initialise it here first —
    //    otherwise signOut silently no-ops and the wallet stays persisted.
    try {
      const c = await cfg();
      const network = (state.network || c.network || 'testnet').trim() || 'testnet';
      await VPWallet.init({ network, contractId: state.contractId || c.veripay_contract });
      await VPWallet.signOut();
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
    return VPWallet.callMethod(c.veripay_contract, method, args, gas, deposit);
  }

  async function callToken(method, args, deposit = '1', gas = '100000000000000') {
    if (!requireWallet()) throw new Error('wallet not connected');
    const c = await cfg();
    return VPWallet.callMethod(c.tusd_contract, method, args, gas, deposit);
  }

  function explorerTx(hash) {
    return hash ? `${state.explorer}/tx/${hash}` : null;
  }

  function jsonPost(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json());
  }

  // ------------------------------------------------------------------
  // tUSD helpers: balance display + self-serve faucet (open testnet mint)
  // ------------------------------------------------------------------

  async function tokenBalance(accountId) {
    const c = await cfg();
    const r = await fetch(c.rpc_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 'bal',
        method: 'query',
        params: {
          request_type: 'call_function',
          finality: 'final',
          account_id: c.tusd_contract,
          method_name: 'ft_balance_of',
          args_base64: btoa(JSON.stringify({ account_id: accountId })),
        },
      }),
    });
    const j = await r.json();
    const text = new TextDecoder().decode(Uint8Array.from(j.result?.result || []));
    return text ? BigInt(JSON.parse(text)) : 0n;
  }

  function fmtUnits(units) {
    const s = units.toString().padStart(7, '0');
    const whole = s.slice(0, -6).replace(/^0+(?=\d)/, '') || '0';
    const frac = s.slice(-6).replace(/0+$/, '');
    return frac ? `${whole}.${frac}` : whole;
  }

  async function refreshTokenBalance() {
    const el = document.getElementById('tusd-balance');
    if (!el || !state.walletAccount) return;
    try {
      el.textContent = fmtUnits(await tokenBalance(state.walletAccount)) + ' tUSD';
    } catch (e) {
      el.textContent = '— tUSD';
    }
  }

  async function faucet() {
    if (!requireWallet()) return;
    const btn = document.getElementById('faucet-btn');
    try {
      if (btn) { btn.disabled = true; btn.textContent = 'Minting…'; }
      const c = await cfg();
      await VPWallet.callMethod(
        c.tusd_contract,
        'mint',
        { account_id: state.walletAccount, amount: '100000000' }, // 100 tUSD (6 decimals)
        '100000000000000',
        '0',
      );
      alert('100 test tUSD minted to ' + state.walletAccount + ' (testnet demo token).');
      await refreshTokenBalance();
    } catch (e) {
      alert('Faucet failed: ' + (e && e.message ? e.message : e));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Get test tUSD'; }
    }
  }

  function init(options) {
    Object.assign(state, options || {});
    window.VPApp = window.VPApp || {};
    Object.assign(window.VPApp, {
      state, cfg, connect, disconnect, login, callContract, callToken,
      explorerTx, jsonPost, requireWallet, tokenBalance, refreshTokenBalance, faucet,
    });
    if (state.walletAccount) refreshTokenBalance();
  }

  window.VPApp = { init };
})();
