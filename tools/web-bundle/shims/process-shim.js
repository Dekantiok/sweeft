// Browser globals shim for Node-isms inside wallet dependencies.
// Runs before everything else in the bundle (first import of entry.js).
import { Buffer as NodeBuffer } from '../node_modules/buffer/index.js';

// `Buffer.from(...)` is used by the wallet SDKs for base64/NEP-413 encoding.
if (typeof globalThis.Buffer === 'undefined') {
  globalThis.Buffer = NodeBuffer;
}

// Some dependencies reference `process` as a bare global.
if (typeof globalThis.process === 'undefined') {
  globalThis.process = {
    env: { NODE_ENV: 'production' },
    browser: true,
    version: '',
    versions: {},
    platform: 'browser',
    nextTick: (fn, ...args) => setTimeout(() => fn(...args), 0),
  };
}

// Node scheduling shim (some crypto/async helpers expect it).
if (typeof globalThis.setImmediate === 'undefined') {
  globalThis.setImmediate = (fn, ...args) => setTimeout(() => fn(...args), 0);
}
if (typeof globalThis.clearImmediate === 'undefined') {
  globalThis.clearImmediate = (id) => clearTimeout(id);
}
