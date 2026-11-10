import { NearBindgen, near, call, view, initialize, LookupMap, NearPromise } from 'near-sdk-js';

// States: created -> joined -> funded -> delivered -> released
//                                  \-> disputed -> resolved
//                        \-> settled (anyone may call after deadline iff !disputed)
//
// Visibility model (v3):
//   - At create, only the creator is added to the my_escrows index.
//   - The counterparty is added only when they call join() with the escrow code.
//   - So nobody except the creator sees an escrow until the code is shared and used.
//   - join() still rejects anyone who is not the fixed buyer/seller.

// v3 storage namespace. Old v1/v2 escrows live under prefix "e" and are ignored,
// which gives us the clean test state approved for the v3 redeploy without
// needing to delete account storage keys.
const VERSION = 3;
const ESCROW_PREFIX = 'e3';

// near-sdk-js reconstructs state by copying fields onto a fresh instance and does
// not call a custom deserialize(), so enforce the v3 shape on first use.
function normalizeIndex(self) {
  if (self.version !== VERSION) {
    self.version = VERSION;
    self.index = [];
    self.count = 0;
  }
  if (!Array.isArray(self.index)) self.index = [];
}

function newEscrowId(escrows) {
  const seed = near.randomSeed();
  for (let i = 0; i < 10; i++) {
    const n = 1000 + ((seed[i % seed.length] * 256 + seed[(i + 1) % seed.length]) % 9000);
    const id = `SWFT-${n}`;
    if (!escrows.containsKey(id)) return id;
  }
  throw new Error('id collision, retry');
}

@NearBindgen({})
class EscrowContract {
  constructor() {
    this.version = VERSION;
    this.escrows = new LookupMap(ESCROW_PREFIX);
    // Plain array of "<account>|<escrowId>" strings; array fields migrate safely.
    this.index = [];
    this.count = 0;
  }

  deserialize() {
    // Kept for explicitness; near-sdk-js reconstructs fields directly.
    this.version = VERSION;
    this.escrows = new LookupMap(ESCROW_PREFIX);
    this.index = [];
    this.count = 0;
  }

  @initialize({})
  init() {
    this.version = VERSION;
    this.escrows = new LookupMap(ESCROW_PREFIX);
    this.index = [];
    this.count = 0;
  }

  // Anyone creates with FIXED parties. Creator auto-joins if they are a party.
  @call({})
  create({ seller, buyer, amount, ft, deadline_ms, terms_hash, foreign_asset }) {
    if (!seller || !buyer) throw new Error('buyer and seller required');
    if (seller === buyer) throw new Error('parties must differ');
    normalizeIndex(this);
    const escrows = new LookupMap(ESCROW_PREFIX);
    const id = newEscrowId(escrows);
    this.count += 1;
    const creator = near.predecessorAccountId();
    const buyerJoined = creator === buyer;
    const sellerJoined = creator === seller;
    const esc = {
      id, creator, buyer, seller, amount: String(amount), ft: ft || 'near',
      deadline: Number(deadline_ms), terms_hash: terms_hash || '',
      foreign_asset: foreign_asset || null,
      status: buyerJoined && sellerJoined ? 'joined' : 'created',
      buyer_joined: buyerJoined, seller_joined: sellerJoined,
      disputed: false, ai_recommendation: null,
    };
    escrows.set(id, esc);
    // Only the creator gets the escrow in their list up front.
    this.index.push(`${creator}|${id}`);
    return id;
  }

  // Only a fixed party may join via escrow code (id). Closes the escrow to strangers.
  @call({})
  join({ id }) {
    normalizeIndex(this);
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    const me = near.predecessorAccountId();
    if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
    if (me === esc.buyer) esc.buyer_joined = true;
    if (me === esc.seller) esc.seller_joined = true;
    if (esc.buyer_joined && esc.seller_joined && esc.status === 'created') esc.status = 'joined';
    escrows.set(id, esc);
    // The counterparty becomes visible to themselves only after joining.
    const key = `${me}|${id}`;
    if (!this.index.includes(key)) this.index.push(key);
  }

  // Buyer funds after both joined. Payable: exact amount in yoctoNEAR for v1 (FT path next).
  @call({ payableFunction: true })
  deposit({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (!(esc.buyer_joined && esc.seller_joined)) throw new Error('both must join first');
    if (near.predecessorAccountId() !== esc.buyer) throw new Error('only buyer funds');
    if (esc.status !== 'joined') throw new Error('wrong state');
    if (near.attachedDeposit().toString() !== esc.amount) throw new Error('amount mismatch');
    esc.status = 'funded';
    escrows.set(id, esc);
  }

  @call({})
  submit_delivery({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (near.predecessorAccountId() !== esc.seller) throw new Error('only seller delivers');
    if (esc.status !== 'funded') throw new Error('wrong state');
    esc.status = 'delivered';
    escrows.set(id, esc);
  }

  // Payer (buyer) releases. Seller cannot self-release. Returns amount for transfer by caller flow.
  @call({})
  release({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.disputed) throw new Error('disputed: resolve first');
    if (near.predecessorAccountId() !== esc.buyer) throw new Error('only payer releases');
    if (esc.status !== 'funded' && esc.status !== 'delivered') throw new Error('wrong state');
    esc.status = 'released';
    escrows.set(id, esc);
    // Payout: the deposit held by this contract goes to the seller.
    return NearPromise.new(esc.seller).transfer(BigInt(esc.amount));
  }

  @call({})
  dispute({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    const me = near.predecessorAccountId();
    if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
    if (esc.status !== 'funded' && esc.status !== 'delivered') throw new Error('wrong state');
    esc.disputed = true;
    esc.status = 'disputed';
    escrows.set(id, esc);
  }

  // AI recommend-only: stored, never moves funds.
  @call({})
  ai_recommend({ id, pct, reason }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.status !== 'disputed') throw new Error('no dispute');
    esc.ai_recommendation = { pct: Number(pct), reason: String(reason).slice(0, 500) };
    escrows.set(id, esc);
  }

  // Human resolves after dispute; payer confirms split. Pays both sides.
  @call({})
  resolve({ id, release_pct }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (!esc.disputed) throw new Error('not disputed');
    if (near.predecessorAccountId() !== esc.buyer) throw new Error('only payer resolves');
    const pct = Math.max(0, Math.min(100, Number(release_pct)));
    const total = BigInt(esc.amount);
    const toSeller = (total * BigInt(pct)) / 100n;
    const toBuyer = total - toSeller;
    esc.status = 'resolved';
    esc.disputed = false;
    escrows.set(id, esc);
    const paySeller = NearPromise.new(esc.seller).transfer(toSeller);
    // Chain the refund after the seller payout (parallel joint promises are
    // prohibited as a contract return value, chained ones are allowed).
    return toBuyer > 0n ? paySeller.then(NearPromise.new(esc.buyer).transfer(toBuyer)) : paySeller;
  }

  // Timeout settle: anyone may call after deadline iff !disputed.
  @call({})
  settle({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.disputed) throw new Error('disputed: resolve first');
    if (near.blockTimestamp() < BigInt(esc.deadline) * 1000000n) throw new Error('deadline not passed');
    if (esc.status !== 'funded' && esc.status !== 'delivered') throw new Error('wrong state');
    esc.status = 'settled';
    escrows.set(id, esc);
    // Timeout default: seller is paid, since funds were already deposited.
    return NearPromise.new(esc.seller).transfer(BigInt(esc.amount));
  }

  @view({})
  get({ id }) {
    const escrows = new LookupMap(ESCROW_PREFIX);
    return escrows.get(id);
  }

  // All escrow ids where account is buyer or seller (oldest first, no duplicates).
  @view({})
  my_escrows({ account_id }) {
    normalizeIndex(this);
    const prefix = `${account_id}|`;
    const ids = (this.index || [])
      .filter((entry) => entry.startsWith(prefix))
      .map((entry) => entry.slice(prefix.length));
    return [...new Set(ids)];
  }
}
