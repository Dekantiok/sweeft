import { NearBindgen, near, call, view, initialize, LookupMap, NearPromise } from 'near-sdk-js';

// States: created -> joined -> funded -> delivered -> released
//                                  \-> disputed -> resolved
//                        \-> rejected (counterparty declines, or creator cancels pre-funding)
//                        \-> settled (anyone may call after deadline iff !disputed)
//
// Discovery model (v4): no shared codes.
//   - Wallets link an email via link_email(). The creator types the counterparty's
//     email (or a *.testnet account directly); the UI resolves it to an account.
//   - At create, only the creator is added to the my_escrows index. The counterparty
//     gets a pending entry instead, listed by the incoming() view as a request they
//     can accept (join) or reject.
//   - join() accepts the request: counterparty is indexed, pending entry removed.
//   - reject() declines it: status -> rejected, pending entry removed. The creator
//     still sees it in their list so they know it was declined.
//   - join()/reject() still reject anyone who is not the fixed buyer/seller.
//   - get(id) by escrow code still works: codes remain the on-chain IDs.
//
// Money movement is unchanged: release() pays the seller instantly once funded
// (no deadline wait — the deadline only gates settle()), resolve() splits.

const VERSION = 4;
const ESCROW_PREFIX = 'e4';
const EMAIL_TO_ACCOUNT_PREFIX = 'm';
const ACCOUNT_TO_EMAIL_PREFIX = 'a';

// Email directory maps are always built fresh per call (never stored as instance
// fields): near-sdk-js reconstructs collection fields from stored state, and any
// collection field missing from older state panics the whole contract.
function emailToAccountMap() {
  return new LookupMap(EMAIL_TO_ACCOUNT_PREFIX);
}

function accountToEmailMap() {
  return new LookupMap(ACCOUNT_TO_EMAIL_PREFIX);
}

// near-sdk-js reconstructs state by copying fields onto a fresh instance and does
// not call a custom deserialize(), so enforce the shape on first use.
function normalizeState(self) {
  if (self.version !== VERSION) {
    self.version = VERSION;
    self.index = [];
    self.pending = [];
    self.count = 0;
  }
  if (!Array.isArray(self.index)) self.index = [];
  if (!Array.isArray(self.pending)) self.pending = [];
}

function cleanEmail(email) {
  const e = String(email || '').trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(e)) throw new Error('invalid email');
  if (e.length > 120) throw new Error('email too long');
  return e;
}

function removePending(self, accountId, id) {
  const before = self.pending.length;
  self.pending = self.pending.filter((entry) => entry !== `${accountId}|${id}`);
  return self.pending.length !== before;
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
    // Plain arrays migrate safely across versions (unlike collections).
    this.index = []; // "<account>|<escrowId>" — visible in my_escrows.
    this.pending = []; // "<counterparty>|<escrowId>" — visible in incoming().
    this.count = 0;
  }

  deserialize() {
    // Kept for explicitness; near-sdk-js reconstructs fields directly.
    this.version = VERSION;
    this.escrows = new LookupMap(ESCROW_PREFIX);
    this.index = [];
    this.pending = [];
    this.count = 0;
  }

  @initialize({})
  init() {
    this.version = VERSION;
    this.escrows = new LookupMap(ESCROW_PREFIX);
    this.index = [];
    this.pending = [];
    this.count = 0;
  }

  // Link the caller's wallet to an email so others can invite them by email.
  // Re-linking moves the email (old links on both sides are cleared).
  @call({})
  link_email({ email }) {
    const clean = cleanEmail(email);
    const me = near.predecessorAccountId();
    const emailToAccount = emailToAccountMap();
    const accountToEmail = accountToEmailMap();
    const prevAccount = emailToAccount.get(clean);
    if (prevAccount && prevAccount !== me) accountToEmail.remove(prevAccount);
    const prevEmail = accountToEmail.get(me);
    if (prevEmail && prevEmail !== clean) emailToAccount.remove(prevEmail);
    emailToAccount.set(clean, me);
    accountToEmail.set(me, clean);
    return clean;
  }

  @view({})
  email_account({ email }) {
    const clean = String(email || '').trim().toLowerCase();
    return emailToAccountMap().get(clean);
  }

  @view({})
  account_email({ account_id }) {
    return accountToEmailMap().get(account_id);
  }

  // Anyone creates with FIXED parties. Creator auto-joins if they are a party.
  // The counterparty gets a pending request (see incoming), not an index entry.
  @call({})
  create({ seller, buyer, amount, ft, deadline_ms, terms_hash, foreign_asset }) {
    if (!seller || !buyer) throw new Error('buyer and seller required');
    if (seller === buyer) throw new Error('parties must differ');
    normalizeState(this);
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
    // The other party gets a request they can accept or reject.
    const counterparty = creator === buyer ? seller : buyer;
    if (counterparty !== creator) this.pending.push(`${counterparty}|${id}`);
    return id;
  }

  // Accept a pending request. Only a fixed party may join. Closes escrow to strangers.
  @call({})
  join({ id }) {
    normalizeState(this);
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.status === 'rejected') throw new Error('escrow was rejected');
    const me = near.predecessorAccountId();
    if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
    if (me === esc.buyer) esc.buyer_joined = true;
    if (me === esc.seller) esc.seller_joined = true;
    if (esc.buyer_joined && esc.seller_joined && esc.status === 'created') esc.status = 'joined';
    escrows.set(id, esc);
    removePending(this, me, id);
    // The counterparty becomes visible to themselves only after joining.
    const key = `${me}|${id}`;
    if (!this.index.includes(key)) this.index.push(key);
  }

  // Decline a pending request (or creator cancels) before anything is funded.
  // The escrow stays visible to both sides, marked rejected.
  @call({})
  reject({ id }) {
    normalizeState(this);
    const escrows = new LookupMap(ESCROW_PREFIX);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    const me = near.predecessorAccountId();
    if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
    if (esc.status !== 'created') throw new Error('too late to reject');
    esc.status = 'rejected';
    escrows.set(id, esc);
    removePending(this, esc.buyer === me ? esc.buyer : esc.seller, id);
    removePending(this, esc.buyer, id);
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

  // Payer (buyer) releases instantly once funded — no deadline wait, no delivery
  // required. Only the deadline-gated settle() path waits. Seller cannot self-release.
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

  // Pending requests for an account: escrows naming them as party that they
  // have not joined yet. Accept via join(), decline via reject().
  @view({})
  incoming({ account_id }) {
    normalizeState(this);
    const prefix = `${account_id}|`;
    const ids = (this.pending || [])
      .filter((entry) => entry.startsWith(prefix))
      .map((entry) => entry.slice(prefix.length));
    return [...new Set(ids)];
  }

  // All escrow ids where account is buyer or seller (oldest first, no duplicates).
  @view({})
  my_escrows({ account_id }) {
    normalizeState(this);
    const prefix = `${account_id}|`;
    const ids = (this.index || [])
      .filter((entry) => entry.startsWith(prefix))
      .map((entry) => entry.slice(prefix.length));
    return [...new Set(ids)];
  }
}
