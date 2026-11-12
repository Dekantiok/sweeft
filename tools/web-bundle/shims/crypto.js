// Browser shim for Node's "crypto" (only what the wallet modules use).
export function randomBytes(n) {
  const bytes = new Uint8Array(n);
  crypto.getRandomValues(bytes);
  return bytes;
}

export function randomUUID() {
  return crypto.randomUUID();
}

export default { randomBytes, randomUUID };
