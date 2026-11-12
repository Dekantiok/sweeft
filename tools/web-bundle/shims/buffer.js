// Re-export the real `buffer` package via a relative path (avoids the
// esbuild alias prefix-matching its own alias target).
export { Buffer } from '../node_modules/buffer/index.js';
export { default } from '../node_modules/buffer/index.js';
