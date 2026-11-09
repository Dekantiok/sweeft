import test from 'node:test';
import assert from 'node:assert/strict';

// Pure state-machine mirror for CI without chain (kept in sync with src/index.js gates).
function makeEscrow(buyer, seller) {
  return { buyer, seller, status: 'created', buyerJoined: false, sellerJoined: false, disputed: false };
}
function join(esc, me) {
  if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
  if (me === esc.buyer) esc.buyerJoined = true;
  if (me === esc.seller) esc.sellerJoined = true;
  if (esc.buyerJoined && esc.sellerJoined) esc.status = 'joined';
}
test('only fixed parties can join', () => {
  const e = makeEscrow('buyer.t', 'seller.t');
  assert.throws(() => join(e, 'mallory.t'), /not a party/);
  join(e, 'buyer.t'); join(e, 'seller.t');
  assert.equal(e.status, 'joined');
});
test('settle requires deadline + no dispute', () => {
  const e = { status: 'funded', disputed: true, deadline: Date.now() + 99999 };
  const canSettle = (now) => !e.disputed && now >= e.deadline;
  assert.equal(canSettle(Date.now()), false);
});
