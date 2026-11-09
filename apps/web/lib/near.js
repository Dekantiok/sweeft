import { setupWalletSelector } from '@near-wallet-selector/core';
import { setupMyNearWallet } from '@near-wallet-selector/my-near-wallet';
import { setupModal } from '@near-wallet-selector/modal-ui';
import * as nearAPI from 'near-api-js';

export const CONTRACT = process.env.NEXT_PUBLIC_CONTRACT || 'steepdata3968.testnet';
export const NETWORK = process.env.NEXT_PUBLIC_NETWORK || 'testnet';

let selector = null;
let modal = null;

export async function getSelector() {
  if (selector) return selector;
  selector = await setupWalletSelector({
    network: NETWORK,
    modules: [setupMyNearWallet()],
  });
  modal = setupModal(selector, { contractId: CONTRACT });
  return selector;
}

export function showWalletModal() {
  if (modal) modal.show();
}

export async function getAccount() {
  const s = await getSelector();
  const state = s.store.getState();
  return state?.accounts?.[0]?.accountId || null;
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

export const escrowCall = {
  create: (a) => callMethod('create', a),
  join: (id) => callMethod('join', { id }),
  deposit: (id, amountYocto) => callMethod('deposit', { id }, amountYocto),
  submitDelivery: (id) => callMethod('submit_delivery', { id }),
  release: (id) => callMethod('release', { id }),
  dispute: (id) => callMethod('dispute', { id }),
  aiRecommend: (id, pct, reason) => callMethod('ai_recommend', { id, pct, reason }),
  resolve: (id, releasePct) => callMethod('resolve', { id, release_pct: releasePct }),
  settle: (id) => callMethod('settle', { id }),
};

export async function escrowGet(id) {
  const { keyStores, connect } = nearAPI;
  const config = { networkId: NETWORK, nodeUrl: 'https://rpc.testnet.near.org', keyStore: new keyStores.BrowserLocalStorageKeyStore() };
  const near = await connect(config);
  const account = await near.account('dontcare.testnet');
  const res = await account.viewFunction({ contractId: CONTRACT, methodName: 'get', args: { id } });
  return res;
}
