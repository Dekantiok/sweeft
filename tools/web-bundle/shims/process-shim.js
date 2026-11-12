// Minimal `process` shim — some wallet dependencies reference it as a bare
// global (Node-ism). Runs before everything else in the bundle.
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
