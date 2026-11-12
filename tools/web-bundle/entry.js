// VeriPay wallet bundle: exposes a stable window.VPWallet API to Django templates.
// Built with esbuild; see package.json.
import { setupWalletSelector } from '@near-wallet-selector/core';
import { setupMeteorWallet } from '@near-wallet-selector/meteor-wallet';
import { setupHereWallet } from '@near-wallet-selector/here-wallet';
import { setupNightly } from '@near-wallet-selector/nightly';
import { setupModal } from '@near-wallet-selector/modal-ui';
import { transactions } from 'near-api-js';

let selector = null;
let modal = null;
let config = { network: 'testnet', contractId: '' };

async function init(options) {
  config = { ...config, ...(options || {}) };
  if (selector) return;
  selector = await setupWalletSelector({
    network: config.network,
    modules: [setupMeteorWallet(), setupHereWallet(), setupNightly()],
  });
  modal = setupModal(selector, { contractId: config.contractId });
}

function showModal() {
  if (modal) modal.show();
}

function account() {
  if (!selector) return null;
  const state = selector.store.getState();
  return state?.accounts?.[0]?.accountId || null;
}

function onChange(cb) {
  if (!selector) return;
  selector.store.observable.subscribe((state) => cb(state?.accounts?.[0]?.accountId || null));
}

async function signOut() {
  if (!selector) return;
  const wallet = await selector.wallet();
  await wallet.signOut();
}

function decodeSuccessValue(res) {
  const status = res?.status;
  const value = status && typeof status === 'object' ? status.SuccessValue ?? status.successValue : null;
  if (typeof value !== 'string' || !value) return null;
  try {
    return JSON.parse(atob(value));
  } catch {
    return null;
  }
}

async function callMethod(contractId, method, args, gas = '100000000000000', deposit = '0') {
  const wallet = await selector.wallet();
  const action = transactions.functionCall(
    method,
    new TextEncoder().encode(JSON.stringify(args)),
    BigInt(gas),
    BigInt(deposit),
  );
  const res = await wallet.signAndSendTransaction({ receiverId: contractId, actions: [action] });
  return {
    txHash: res?.transaction?.hash || res?.transaction_outcome?.id || null,
    successValue: decodeSuccessValue(res),
    raw: res,
  };
}

async function signLogin(message, recipient, nonce) {
  const wallet = await selector.wallet();
  const res = await wallet.signMessage({ message, recipient, nonce });
  return {
    accountId: res?.accountId || account(),
    publicKey: res?.publicKey,
    signature: res?.signature,
  };
}

export {
  init,
  showModal,
  account,
  onChange,
  signOut,
  callMethod,
  signLogin,
};
