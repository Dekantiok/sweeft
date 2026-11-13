// SWEEFT wallet bundle: exposes a stable window.VPWallet API to Django templates.
// Built with esbuild; see package.json.
import './shims/process-shim.js';
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
  await ensureSelector();
}

// Lazily create the selector. Actions (callMethod, signLogin, signOut) run on
// freshly reloaded pages where only `init` was called by the host app — they
// must never depend on the user clicking Connect in this page load.
async function ensureSelector() {
  if (!selector) {
    selector = await setupWalletSelector({
      network: config.network,
      modules: [setupMeteorWallet(), setupHereWallet(), setupNightly()],
    });
    modal = setupModal(selector, { contractId: config.contractId });
  }
  return selector;
}

async function selectedWallet() {
  const s = await ensureSelector();
  const state = s.store.getState();
  if (!state.selectedWalletId && !(state.accounts || []).length) {
    throw new Error('Wallet session not found in this browser — click "Connect wallet" and sign in again.');
  }
  return s.wallet();
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
  try {
    if (selector) {
      const wallet = await selector.wallet();
      await wallet.signOut();
    }
  } catch (e) {
    // The wallet module can fail when it is no longer connected — the
    // persistence cleanup below still guarantees a clean state.
    console.warn('wallet signOut failed', e);
  } finally {
    clearPersistence();
  }
}

// Whether the selector currently has a connected wallet in this browser
// (used by the host app to detect session-vs-wallet mismatches).
async function isConnected() {
  try {
    const s = await ensureSelector();
    const st = s.store.getState();
    return !!(st.selectedWalletId && (st.accounts || []).length);
  } catch (e) {
    return false;
  }
}

// Remove wallet-selector persistence so the next Connect starts fresh
// (past sessions otherwise survive signOut attempts on a reloaded page).
function clearPersistence() {
  try {
    Object.keys(localStorage)
      .filter((k) => k.startsWith('near-wallet-selector'))
      .forEach((k) => localStorage.removeItem(k));
  } catch (e) {
    /* localStorage unavailable */
  }
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
  const wallet = await selectedWallet();
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

// Native NEAR transfer (used for escrow funding and direct co-pilot payments).
async function sendNear(receiverId, amountYocto) {
  const wallet = await selectedWallet();
  const action = transactions.transfer(BigInt(amountYocto));
  const res = await wallet.signAndSendTransaction({ receiverId, actions: [action] });
  return {
    txHash: res?.transaction?.hash || res?.transaction_outcome?.id || null,
    successValue: decodeSuccessValue(res),
    raw: res,
  };
}

async function signLogin(message, recipient, nonce) {
  const wallet = await selectedWallet();
  const res = await wallet.signMessage({ message, recipient, nonce });
  const out = res || {};
  // Normalize across Meteor (payload), HERE (SignedMessage) and Nightly
  // (possibly a raw signature object or string).
  let signature = out.signature ?? out.Signature ?? (typeof res === 'string' ? res : undefined);
  let publicKey = out.publicKey ?? out.public_key ?? out.publicKeyStr ?? undefined;
  if (publicKey && typeof publicKey === 'object' && typeof publicKey.toString === 'function') {
    publicKey = publicKey.toString();
  }
  return {
    accountId: out.accountId || out.account_id || account(),
    publicKey,
    signature,
  };
}

export {
  init,
  showModal,
  account,
  onChange,
  signOut,
  isConnected,
  callMethod,
  sendNear,
  signLogin,
};
