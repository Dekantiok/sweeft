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
    await VPWallet.init({ network: state.network, contractId: state.contractId || (await cfg()).veripay_contract });
    VPWallet.showModal();
    VPWallet.onChange(async (accountId) => {
      if (accountId && accountId !== state.walletAccount) {
        await login(accountId);
      }
    });
  }

  async function login(accountId) {
    try {
      const chRes = await fetch(`/api/v1/wallet/challenge/?account_id=${encodeURIComponent(accountId)}`);
      const ch = await chRes.json();
      const nonceBytes = Uint8Array.from(atob(ch.nonce), (c) => c.charCodeAt(0));
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
    try {
      await fetch('/api/v1/wallet/logout/', { method: 'POST' });
    } catch (e) {}
    try {
      await VPWallet.signOut();
    } catch (e) {}
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

  function init(options) {
    Object.assign(state, options || {});
    window.VPApp = window.VPApp || {};
    Object.assign(window.VPApp, { state, cfg, connect, disconnect, login, callContract, callToken, explorerTx, jsonPost, requireWallet });
  }

  window.VPApp = { init };
})();
