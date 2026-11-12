// Browser shim for Node's "util" (only what bundled deps use).
export function format(...args) {
  return args
    .map((a) => (typeof a === 'string' ? a : JSON.stringify(a)))
    .join(' ');
}

export function inspect(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export default { format, inspect };
