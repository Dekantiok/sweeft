import { setupWalletSelector } from '@near-wallet-selector/core';
import { setupMeteorWallet } from '@near-wallet-selector/meteor-wallet';
import { setupHereWallet } from '@near-wallet-selector/here-wallet';
import { setupNightly } from '@near-wallet-selector/nightly';
import { setupModal } from '@near-wallet-selector/modal-ui';
import { transactions } from 'near-api-js';

export const CONTRACT = process.env.NEXT_PUBLIC_CONTRACT || 'steepdata3968.testnet';
export const NETWORK = process.env.NEXT_PUBLIC_NETWORK || 'testnet';
export const RPC = NETWORK === 'mainnet' ? 'https://rpc.mainnet.near.org' : 'https://rpc.testnet.near.org';
export const EXPLORER = NETWORK === 'mainnet' ? 'https://explorer.near.org' : 'https://explorer.testnet.near.org';

let selector = null;
let modal = null;

export async function getSelector() {
  if (selector) return selector;
  selector = await setupWalletSelector({
    network: NETWORK,
    modules: [setupMeteorWallet(), setupHereWallet(), setupNightly()],
  });
  modal = setupModal(selector, { contractId: CONTRACT });
  return selector;
}

export function showWalletModal() {
  if (modal) modal.show();
}

// Subscribe to wallet state (survives page reload via wallet-selector persistence).
export async function subscribeAccount(cb) {
  const s = await getSelector();
  const emit = (state) => cb(state?.accounts?.[0]?.accountId || null);
  emit(s.store.getState());
  return s.store.observable.subscribe(emit);
}

export async function getAccount() {
  const s = await getSelector();
  return s.store.getState()?.accounts?.[0]?.accountId || null;
}

export async function signOut() {
  const s = await getSelector();
  const wallet = await s.wallet();
  await wallet.signOut();
}

// Turn every wallet/RPC error shape into one readable line.
export function errorText(e) {
  if (!e) return 'unknown error';
  if (typeof e === 'string') return e;
  const bits = [];
  const seen = new Set();
  const push = (v) => {
    if (typeof v !== 'string') return;
    const s = v.trim();
    if (!s || seen.has(s)) return;
    seen.add(s);
    bits.push(s);
  };
  push(e.message);
  if (e.type && e.type !== 'Error' && !bits.some((b) => b.includes(e.type))) push(e.type);
  if (e.kind && e.kind !== 'Error' && !bits.some((b) => b.includes(e.kind))) push(e.kind);
  const cause = e.cause || e.error;
  if (cause && cause !== e) {
    push(cause.message);
    push(cause.name && cause.name !== 'Error' ? cause.name : '');
    push(cause.info?.error_message);
    push(cause.data?.error_message);
  }
  push(e.info?.error_message);
  push(e.data?.error_message);
  if (!bits.length) {
    try {
      const json = JSON.stringify(e);
      if (json && json !== '{}') bits.push(json);
    } catch { /* ignore */ }
  }
  return bits.join(' · ') || 'unknown error';
}

async function callMethod(method, args, deposit = '0') {
  const s = await getSelector();
  const wallet = await s.wallet();
  // wallet-selector 10.x wallets expect NAJ actions ({ functionCall: ... }) and
  // convert them internally. The { type, params } shape throws
  // "Unsupported NAJ action" in najActionToInternal — do not use it.
  const action = transactions.functionCall(
    method,
    new TextEncoder().encode(JSON.stringify(args)),
    100000000000000n,
    BigInt(deposit),
  );
  return wallet.signAndSendTransaction({ receiverId: CONTRACT, actions: [action] });
}

// Wallets return FinalExecutionOutcome; contract return value is base64 in status.SuccessValue.
function successValue(res) {
  const status = res?.status;
  const value = status && typeof status === 'object' ? (status.SuccessValue ?? status.successValue) : null;
  if (typeof value !== 'string' || !value) return null;
  try {
    return JSON.parse(decodeURIComponent(escape(atob(value))));
  } catch {
    return null;
  }
}

// NEAR <-> yocto helpers, kept local so the UI has no near-api-js version coupling.
export function parseNearAmount(amount) {
  const s = String(amount ?? '').trim();
  if (!/^\d*(\.\d*)?$/.test(s) || s === '' || s === '.') return null;
  const [whole = '0', frac = ''] = s.split('.');
  if (frac.length > 24) return null;
  const yocto = `${whole || '0'}${frac.padEnd(24, '0')}`.replace(/^0+(?=\d)/, '');
  return yocto === '' ? '0' : yocto;
}

export function formatNear(yocto) {
  const s = String(yocto ?? '0');
  if (!/^\d+$/.test(s)) return s;
  const padded = s.padStart(25, '0');
  const whole = padded.slice(0, -24);
  const frac = padded.slice(-24).replace(/0+$/, '').slice(0, 5).replace(/0+$/, '');
  return frac ? `${whole}.${frac}` : whole;
}

export const escrowCall = {
  create: async (a) => {
    const amount = parseNearAmount(a.amount);
    if (!amount) throw new Error('invalid amount');
    const res = await callMethod('create', { ...a, amount, ft: 'near' });
    return successValue(res);
  },
  join: (id) => callMethod('join', { id }),
  reject: (id) => callMethod('reject', { id }),
  deposit: (id, amountYocto) => callMethod('deposit', { id }, String(amountYocto)),
  submitDelivery: (id) => callMethod('submit_delivery', { id }),
  release: (id) => callMethod('release', { id }),
  dispute: (id) => callMethod('dispute', { id }),
  aiRecommend: (id, pct, reason) => callMethod('ai_recommend', { id, pct, reason }),
  resolve: (id, releasePct) => callMethod('resolve', { id, release_pct: releasePct }),
  settle: (id) => callMethod('settle', { id }),
};

// Raw RPC view call — avoids SDK churn and needs no key.
export async function view(methodName, args) {
  const r = await fetch(RPC, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: '1', method: 'query',
      params: { request_type: 'call_function', finality: 'final', account_id: CONTRACT, method_name: methodName, args_base64: btoa(JSON.stringify(args)) },
    }),
  });
  const j = await r.json();
  if (j.error) throw new Error(errorText(j.error));
  const bytes = j.result?.result || [];
  const text = new TextDecoder().decode(Uint8Array.from(bytes));
  return text ? JSON.parse(text) : null;
}

export const getEscrow = (id) => view('get', { id });
export const myEscrows = (accountId) => view('my_escrows', { account_id: accountId });
export const incomingRequests = (accountId) => view('incoming', { account_id: accountId });
