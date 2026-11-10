import { setupWalletSelector } from '@near-wallet-selector/core';
import { setupMyNearWallet } from '@near-wallet-selector/my-near-wallet';
import { setupModal } from '@near-wallet-selector/modal-ui';
import * as nearAPI from 'near-api-js';

export const CONTRACT = process.env.NEXT_PUBLIC_CONTRACT || 'steepdata3968.testnet';
export const NETWORK = process.env.NEXT_PUBLIC_NETWORK || 'testnet';
export const RPC = NETWORK === 'mainnet' ? 'https://rpc.mainnet.near.org' : 'https://rpc.testnet.near.org';
export const EXPLORER = NETWORK === 'mainnet' ? 'https://explorer.near.org' : 'https://explorer.testnet.near.org';

let selector = null;
let modal = null;

export async function getSelector() {
  if (selector) return selector;
  selector = await setupWalletSelector({ network: NETWORK, modules: [setupMyNearWallet()] });
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

async function callMethod(method, args, deposit = '0') {
  const s = await getSelector();
  const wallet = await s.wallet();
  return wallet.signAndSendTransaction({
    receiverId: CONTRACT,
    actions: [{ type: 'FunctionCall', params: { methodName: method, args, gas: '100000000000000', deposit } }],
  });
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

export const escrowCall = {
  create: async (a) => {
    const amount = nearAPI.utils.format.parseNearAmount(String(a.amount));
    if (!amount) throw new Error('invalid amount');
    const res = await callMethod('create', { ...a, amount, ft: 'near' });
    return successValue(res);
  },
  join: (id) => callMethod('join', { id }),
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
      params: { request_type: 'call', finality: 'final', account_id: CONTRACT, method_name: methodName, args_base64: btoa(JSON.stringify(args)) },
    }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error.cause?.name || j.error.message || 'view call failed');
  const bytes = j.result?.result || [];
  const text = new TextDecoder().decode(Uint8Array.from(bytes));
  return text ? JSON.parse(text) : null;
}

export const getEscrow = (id) => view('get', { id });
export const myEscrows = (accountId) => view('my_escrows', { account_id: accountId });

export function formatNear(yocto) {
  try { return nearAPI.utils.format.formatNearAmount(String(yocto)); } catch { return String(yocto); }
}
