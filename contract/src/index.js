import { NearBindgen, near, call, view, initialize, LookupMap } from 'near-sdk-js';

// States: created -> joined -> funded -> delivered -> released
//                                  \-> disputed -> resolved
//                        \-> settled (anyone may call after deadline iff !disputed)

function loadEscrows(self) {
  return Object.assign(new LookupMap('e'), self.escrows);
}

@NearBindgen({})
class EscrowContract {
  constructor() {
    this.escrows = new LookupMap('e');
    this.count = 0;
  }

  deserialize() {
    this.escrows = Object.assign(new LookupMap('e'), this.escrows);
  }

  @initialize({})
  init() {
    this.escrows = new LookupMap('e');
    this.count = 0;
  }

  // Anyone creates with FIXED parties. Creator auto-joins if they are a party.
  @call({})
  create({ seller, buyer, amount, ft, deadline_ms, terms_hash, foreign_asset }) {
    if (!seller || !buyer) throw new Error('buyer and seller required');
    if (seller === buyer) throw new Error('parties must differ');
    const escrows = loadEscrows(this);
    this.count += 1;
    const id = `SWFT-${this.count}`;
    const me = near.predecessorAccountId();
    const buyerJoined = me === buyer;
    const sellerJoined = me === seller;
    const esc = {
      id, buyer, seller, amount: String(amount), ft: ft || 'near',
      deadline: Number(deadline_ms), terms_hash: terms_hash || '',
      foreign_asset: foreign_asset || null,
      status: buyerJoined && sellerJoined ? 'joined' : 'created',
      buyer_joined: buyerJoined, seller_joined: sellerJoined,
      disputed: false, ai_recommendation: null,
    };
    escrows.set(id, esc);
    return id;
  }

  // Only a fixed party may join via escrow code (id). Closes the escrow to strangers.
  @call({})
  join({ id }) {
    const escrows = loadEscrows(this);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    const me = near.predecessorAccountId();
    if (me !== esc.buyer && me !== esc.seller) throw new Error('not a party');
    if (me === esc.buyer) esc.buyer_joined = true;
    if (me === esc.seller) esc.seller_joined = true;
    if (esc.buyer_joined && esc.seller_joined && esc.status === 'created') esc.status = 'joined';
    escrows.set(id, esc);
  }

  // Buyer funds after both joined. Payable: exact amount in yoctoNEAR for v1 (FT path next).
  @call({ payableFunction: true })
  deposit({ id }) {
    const escrows = loadEscrows(this);
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
    const escrows = loadEscrows(this);
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
    const escrows = loadEscrows(this);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.disputed) throw new Error('disputed: resolve first');
    if (near.predecessorAccountId() !== esc.buyer) throw new Error('only payer releases');
    if (esc.status !== 'funded' && esc.status !== 'delivered') throw new Error('wrong state');
    esc.status = 'released';
    escrows.set(id, esc);
    return esc.amount;
  }

  @call({})
  dispute({ id }) {
    const escrows = loadEscrows(this);
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
    const escrows = loadEscrows(this);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.status !== 'disputed') throw new Error('no dispute');
    esc.ai_recommendation = { pct: Number(pct), reason: String(reason).slice(0, 500) };
    escrows.set(id, esc);
  }

  // Human resolves after dispute; payer confirms split.
  @call({})
  resolve({ id, release_pct }) {
    const escrows = loadEscrows(this);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (!esc.disputed) throw new Error('not disputed');
    if (near.predecessorAccountId() !== esc.buyer) throw new Error('only payer resolves');
    esc.status = 'resolved';
    esc.disputed = false;
    escrows.set(id, esc);
    return { release_pct: Number(release_pct) };
  }

  // Timeout settle: anyone may call after deadline iff !disputed.
  @call({})
  settle({ id }) {
    const escrows = loadEscrows(this);
    const esc = escrows.get(id);
    if (!esc) throw new Error('unknown escrow');
    if (esc.disputed) throw new Error('disputed: resolve first');
    if (near.blockTimestamp() < BigInt(esc.deadline) * 1000000n) throw new Error('deadline not passed');
    if (esc.status !== 'funded' && esc.status !== 'delivered' && esc.status !== 'joined') throw new Error('wrong state');
    esc.status = 'settled';
    escrows.set(id, esc);
    return esc.amount;
  }

  @view({})
  get({ id }) {
    const escrows = loadEscrows(this);
    return escrows.get(id);
  }
}
