'use client';
import { useEffect, useState } from 'react';
import '@near-wallet-selector/modal-ui/styles.css';
import {
  showWalletModal, subscribeAccount, signOut, escrowCall, getEscrow, myEscrows,
  formatNear, CONTRACT, EXPLORER, getAccount,
} from '../lib/near';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';
const BUILD = 'v3-wallets-2026-11-10';

const S = {
  page: { maxWidth: 860, margin: '0 auto', padding: '28px 16px 64px', fontFamily: 'ui-sans-serif, system-ui, sans-serif', color: '#111' },
  card: { border: '1px solid #e3e3e3', borderRadius: 12, padding: 16, marginBottom: 14, background: '#fff' },
  label: { display: 'block', fontSize: 13, color: '#555', marginBottom: 4 },
  input: { width: '100%', padding: '9px 10px', border: '1px solid #ccc', borderRadius: 8, fontSize: 14, boxSizing: 'border-box' },
  btn: { padding: '9px 14px', borderRadius: 8, border: '1px solid #111', background: '#111', color: '#fff', cursor: 'pointer', fontSize: 14 },
  btnGhost: { padding: '9px 14px', borderRadius: 8, border: '1px solid #ccc', background: '#fff', color: '#111', cursor: 'pointer', fontSize: 14 },
  btnSmall: { padding: '6px 10px', borderRadius: 8, border: '1px solid #ccc', background: '#fff', cursor: 'pointer', fontSize: 13 },
  note: (kind) => ({ padding: '10px 12px', borderRadius: 8, fontSize: 14, margin: '10px 0',
    background: kind === 'err' ? '#fdecec' : kind === 'ok' ? '#eaf7ec' : '#f3f4f6',
    color: kind === 'err' ? '#8a1f1f' : kind === 'ok' ? '#1d5c2a' : '#333',
    border: `1px solid ${kind === 'err' ? '#f5c2c2' : kind === 'ok' ? '#bfe3c6' : '#e3e3e3'}` }),
  tab: (active) => ({ padding: '9px 16px', borderRadius: 999, cursor: 'pointer', fontSize: 14,
    border: '1px solid ' + (active ? '#111' : '#ddd'), background: active ? '#111' : '#fff', color: active ? '#fff' : '#333' }),
  badge: (color) => ({ display: 'inline-block', padding: '3px 9px', borderRadius: 999, fontSize: 12, fontWeight: 600,
    background: color.bg, color: color.fg }),
};

const STATUS_COLORS = {
  created: { bg: '#eef2ff', fg: '#3730a3' },
  joined: { bg: '#e0f2fe', fg: '#075985' },
  funded: { bg: '#fef3c7', fg: '#92400e' },
  delivered: { bg: '#ede9fe', fg: '#5b21b6' },
  released: { bg: '#dcfce7', fg: '#166534' },
  disputed: { bg: '#fee2e2', fg: '#991b1b' },
  resolved: { bg: '#d1fae5', fg: '#065f46' },
  settled: { bg: '#e5e7eb', fg: '#374151' },
};

export default function Home() {
  const [account, setAccount] = useState(null);
  const [ready, setReady] = useState(false);
  const [tab, setTab] = useState('create');
  const [note, setNote] = useState({ text: '', kind: '' });
  const [busy, setBusy] = useState('');

  // create
  const [role, setRole] = useState('buyer');
  const [nl, setNl] = useState('pay 50 USDC to alice.testnet for logo by Friday');
  const [listening, setListening] = useState(false);
  const [form, setForm] = useState({ counterparty: 'seller.steepdata3968.testnet', amount: '0.05', deadlineDays: '7', terms: 'logo delivery' });
  const [created, setCreated] = useState('');

  // join / detail
  const [codeInput, setCodeInput] = useState('');
  const [escrow, setEscrow] = useState(null);
  const [evidence, setEvidence] = useState('Seller delivered the files at the agreed link; buyer says the colours differ from the brief.');
  const [aiRec, setAiRec] = useState(null);

  // mine
  const [mine, setMine] = useState([]);
  const [mineBusy, setMineBusy] = useState(false);

  useEffect(() => {
    let unsub;
    (async () => {
      try {
        unsub = await subscribeAccount((addr) => setAccount(addr));
      } catch (e) {
        flash(`Wallet init failed: ${e.message}. Click Connect to retry.`, 'err');
      } finally {
        setReady(true);
      }
    })();
    return () => { if (typeof unsub === 'function') unsub(); };
  }, []); // eslint-disable-line

  async function connect() {
    showWalletModal();
    // Some wallets update the store without firing an event; poll once as fallback.
    setTimeout(async () => {
      const addr = await getAccount().catch(() => null);
      if (addr) setAccount(addr);
    }, 1800);
  }

  async function disconnect() {
    await signOut();
    setAccount(null);
    setEscrow(null);
    setTab('create');
  }

  function flash(text, kind = '') { setNote({ text, kind }); }

  // ---------- AI input ----------
  async function aiFill() {
    if (!nl.trim()) return flash('Type what you need first.', 'err');
    setBusy('ai');
    try {
      const r = await fetch(`${API}/parse`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: nl }) });
      const j = await r.json();
      const p = j.parsed || {};
      setForm((f) => ({
        ...f,
        counterparty: p.beneficiary || f.counterparty,
        amount: p.amount != null ? String(p.amount) : f.amount,
        deadlineDays: p.deadlineDays != null ? String(p.deadlineDays) : f.deadlineDays,
        terms: p.terms || f.terms,
      }));
      flash(`AI pre-filled the form (${j.source}). Verify the details, then Create.`, 'ok');
    } catch (e) { flash(`AI error: ${e.message}`, 'err'); }
    finally { setBusy(''); }
  }

  function mic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return flash('Mic not supported in this browser — type instead. Same path either way.', 'err');
    const rec = new SR();
    rec.lang = 'en-US';
    setListening(true);
    rec.onresult = (e) => { setNl(e.results[0][0].transcript); setListening(false); };
    rec.onerror = () => { setListening(false); flash('Mic error — type instead.', 'err'); };
    rec.onend = () => setListening(false);
    rec.start();
  }

  // ---------- create ----------
  async function createEscrow() {
    if (!account) return flash('Connect a wallet first.', 'err');
    const cp = form.counterparty.trim().toLowerCase();
    if (!cp) return flash('Counterparty wallet is required.', 'err');
    if (!form.amount || Number(form.amount) <= 0) return flash('Amount must be greater than zero.', 'err');
    setBusy('create');
    setCreated('');
    try {
      const before = await myEscrows(account).catch(() => []);
      const parties = role === 'buyer'
        ? { buyer: account, seller: cp }
        : { buyer: cp, seller: account };
      const returned = await escrowCall.create({
        ...parties,
        amount: form.amount,
        deadline_ms: String(Date.now() + Number(form.deadlineDays || 7) * 864e5),
        terms_hash: (form.terms || '').slice(0, 200),
        foreign_asset: null,
      });
      let id = typeof returned === 'string' ? returned : null;
      if (!id) {
        for (let i = 0; i < 6 && !id; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          const now = await myEscrows(account).catch(() => []);
          id = now.find((x) => !before.includes(x)) || null;
        }
      }
      if (!id) throw new Error('transaction sent, but the code could not be read — open My escrows');
      setCreated(id);
      setCodeInput(id);
      setEscrow(await getEscrow(id));
      flash(`Escrow ${id} is live on testnet. Share this code with ${cp}.`, 'ok');
      loadMine();
    } catch (e) { flash(`Create failed: ${e.message}`, 'err'); }
    finally { setBusy(''); }
  }

  // ---------- join / detail ----------
  async function loadEscrow(id) {
    const clean = (id || '').trim().toUpperCase();
    if (!clean) return flash('Enter an escrow code like SWFT-1234.', 'err');
    setBusy('lookup');
    setAiRec(null);
    try {
      const e = await getEscrow(clean);
      if (!e) { setEscrow(null); return flash(`No escrow ${clean} on ${CONTRACT}.`, 'err'); }
      setEscrow(e);
      setCodeInput(clean);
      flash(`Loaded ${clean} — status: ${e.status}.`, 'ok');
    } catch (err) { flash(`Lookup failed: ${err.message}`, 'err'); }
    finally { setBusy(''); }
  }

  async function refresh() {
    if (!escrow) return;
    try { setEscrow(await getEscrow(escrow.id)); } catch { /* keep old */ }
  }

  async function act(label, fn) {
    if (!escrow) return;
    setBusy(label);
    try {
      await fn();
      flash(`${label} sent — refreshing status...`, 'ok');
      setTimeout(refresh, 2200);
    } catch (e) { flash(`${label} failed: ${e.message}`, 'err'); }
    finally { setBusy(''); }
  }

  async function askAI() {
    if (!escrow) return;
    if (escrow.status !== 'disputed') return flash('AI arbiter runs only while the escrow is disputed.', 'err');
    setBusy('arbiter');
    try {
      const r = await fetch(`${API}/recommend`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ evidence, terms: escrow.terms_hash || '' }),
      });
      const j = await r.json();
      setAiRec(j);
      flash(`AI recommends ${j.pct}% to the seller (${j.source}). You still confirm on-chain.`, 'ok');
    } catch (e) { flash(`AI error: ${e.message}`, 'err'); }
    finally { setBusy(''); }
  }

  // ---------- mine ----------
  async function loadMine() {
    if (!account) return;
    setMineBusy(true);
    try {
      const ids = await myEscrows(account);
      const items = await Promise.all((ids || []).slice().reverse().map(async (id) => {
        try { return await getEscrow(id); } catch { return null; }
      }));
      setMine(items.filter(Boolean).slice(0, 20));
      if (!ids || !ids.length) flash('No escrows for this wallet yet — create one first.');
    } catch (e) { flash(`Could not load your escrows: ${e.message}`, 'err'); }
    finally { setMineBusy(false); }
  }

  useEffect(() => { if (tab === 'mine' && account) loadMine(); }, [tab, account]); // eslint-disable-line

  // ---------- landing ----------
  if (!ready || !account) {
    return (
      <main style={{ ...S.page, maxWidth: 620, textAlign: 'center', paddingTop: 96 }}>
        <h1 style={{ fontSize: 44, margin: '0 0 10px' }}>SWEeft</h1>
        <p style={{ fontSize: 18, color: '#444', margin: '0 0 6px' }}>
          Escrow for freelancers and digital-goods sellers.
        </p>
        <p style={{ color: '#666', margin: '0 0 28px' }}>
          Buyer and seller are fixed at creation. Funds sit in a NEAR contract and release when the payer
          approves, or after the deadline if nobody disputes. AI pre-fills the deal and advises disputes —
          a human always confirms.
        </p>
        {ready
          ? <button style={{ ...S.btn, padding: '13px 22px', fontSize: 16 }} onClick={connect}>Connect wallet (testnet)</button>
          : <p style={{ color: '#888' }}>Loading wallet...</p>}
        <p style={{ marginTop: 30, fontSize: 12, color: '#999' }}>
          Contract <code>{CONTRACT}</code> · <a href={`${EXPLORER}/accounts/${CONTRACT}`} target="_blank" rel="noreferrer">explorer</a>
          {' · '}build <code>{BUILD}</code>
        </p>
      </main>
    );
  }

  return (
    <main style={S.page}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 18 }}>
        <h1 style={{ fontSize: 24, margin: 0 }}>SWEeft</h1>
        <span style={{ fontSize: 12, color: '#777' }}>testnet v1</span>
        <span style={{ fontSize: 11, color: '#999' }}>build {BUILD}</span>
        <div style={{ flex: 1 }} />
        <code style={{ fontSize: 12, background: '#f3f4f6', padding: '5px 8px', borderRadius: 6 }}>{account}</code>
        <button style={S.btnSmall} onClick={disconnect}>Disconnect</button>
      </header>

      <nav style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
        <button style={S.tab(tab === 'create')} onClick={() => setTab('create')}>Create</button>
        <button style={S.tab(tab === 'join')} onClick={() => setTab('join')}>Join</button>
        <button style={S.tab(tab === 'mine')} onClick={() => setTab('mine')}>My escrows</button>
      </nav>

      {note.text && <div style={S.note(note.kind)}>{note.text}</div>}

      {/* ---------------- CREATE ---------------- */}
      {tab === 'create' && (
        <section>
          <div style={S.card}>
            <h3 style={{ margin: '0 0 10px' }}>Your role</h3>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                style={role === 'buyer' ? S.btn : S.btnGhost}
                onClick={() => setRole('buyer')}
              >I&apos;m the buyer (I pay)</button>
              <button
                style={role === 'seller' ? S.btn : S.btnGhost}
                onClick={() => setRole('seller')}
              >I&apos;m the seller (I receive)</button>
            </div>
            <p style={{ fontSize: 13, color: '#666', margin: '8px 0 0' }}>
              {role === 'buyer'
                ? 'You will deposit the funds and release them when satisfied.'
                : 'The counterparty deposits; you deliver and can dispute if needed.'}
            </p>
          </div>

          <div style={S.card}>
            <h3 style={{ margin: '0 0 8px' }}>Describe the deal (AI fills the form)</h3>
            <textarea value={nl} onChange={(e) => setNl(e.target.value)} rows={3} style={{ ...S.input, resize: 'vertical' }} />
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <button style={S.btnGhost} onClick={aiFill} disabled={busy === 'ai'}>
                {busy === 'ai' ? 'Asking AI...' : 'AI pre-fill form'}
              </button>
              <button style={S.btnGhost} onClick={mic}>{listening ? 'Listening...' : '🎤 Voice → text'}</button>
            </div>
            <p style={{ fontSize: 12, color: '#888', margin: '8px 0 0' }}>
              No mic? Pasting the sentence takes the same path.
            </p>
          </div>

          <div style={S.card}>
            <h3 style={{ margin: '0 0 12px' }}>Escrow details</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={S.label}>Counterparty wallet (*.testnet)</label>
                <input style={S.input} value={form.counterparty}
                  onChange={(e) => setForm({ ...form, counterparty: e.target.value })} />
              </div>
              <div>
                <label style={S.label}>Amount (NEAR, testnet)</label>
                <input style={S.input} value={form.amount}
                  onChange={(e) => setForm({ ...form, amount: e.target.value })} />
              </div>
              <div>
                <label style={S.label}>Deadline (days)</label>
                <input style={S.input} type="number" min="1" value={form.deadlineDays}
                  onChange={(e) => setForm({ ...form, deadlineDays: e.target.value })} />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={S.label}>Terms (short description)</label>
                <input style={S.input} value={form.terms}
                  onChange={(e) => setForm({ ...form, terms: e.target.value })} />
              </div>
            </div>
            <div style={{ marginTop: 14 }}>
              <button style={S.btn} onClick={createEscrow} disabled={busy === 'create'}>
                {busy === 'create' ? 'Creating...' : 'Create escrow'}
              </button>
            </div>
          </div>

          {created && (
            <div style={{ ...S.card, borderColor: '#86efac', background: '#f0fdf4' }}>
              <h3 style={{ margin: '0 0 6px' }}>Escrow created</h3>
              <p style={{ margin: '0 0 10px', fontSize: 14 }}>Share this code with the counterparty:</p>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                <code style={{ fontSize: 26, fontWeight: 700, letterSpacing: 1 }}>{created}</code>
                <button style={S.btnSmall} onClick={() => { navigator.clipboard?.writeText(created); flash('Code copied.', 'ok'); }}>Copy</button>
                <button style={S.btnSmall} onClick={() => { setTab('join'); loadEscrow(created); }}>Open in Join</button>
              </div>
              <p style={{ fontSize: 12, color: '#555', margin: '10px 0 0' }}>
                <a href={`${EXPLORER}/accounts/${CONTRACT}`} target="_blank" rel="noreferrer">View contract on explorer</a>
              </p>
            </div>
          )}
        </section>
      )}

      {/* ---------------- JOIN ---------------- */}
      {tab === 'join' && (
        <section>
          <div style={S.card}>
            <h3 style={{ margin: '0 0 10px' }}>Join an escrow</h3>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <input style={{ ...S.input, maxWidth: 220 }} placeholder="SWFT-1234" value={codeInput}
                onChange={(e) => setCodeInput(e.target.value.toUpperCase())} />
              <button style={S.btn} onClick={() => loadEscrow(codeInput)} disabled={busy === 'lookup'}>
                {busy === 'lookup' ? 'Loading...' : 'Load escrow'}
              </button>
            </div>
            <p style={{ fontSize: 12, color: '#888', margin: '8px 0 0' }}>
              Only the buyer and seller named at creation can join. Anyone else is rejected by the contract.
            </p>
          </div>

          {escrow && (
            <div style={S.card}>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                <h3 style={{ margin: 0 }}>{escrow.id}</h3>
                <span style={S.badge(STATUS_COLORS[escrow.status] || STATUS_COLORS.created)}>{escrow.status}</span>
                {escrow.disputed && <span style={S.badge(STATUS_COLORS.disputed)}>disputed</span>}
                <div style={{ flex: 1 }} />
                <span style={S.badge({ bg: '#f3f4f6', fg: '#111' })}>
                  you are {escrow.buyer === account ? 'buyer' : escrow.seller === account ? 'seller' : 'not a party'}
                </span>
              </div>

              <table style={{ width: '100%', fontSize: 14, marginTop: 12, borderCollapse: 'collapse' }}>
                <tbody>
                  <tr><td style={{ color: '#666', padding: '4px 0' }}>Buyer (pays)</td><td>{escrow.buyer}{escrow.buyer_joined ? ' ✓ joined' : ''}</td></tr>
                  <tr><td style={{ color: '#666', padding: '4px 0' }}>Seller (delivers)</td><td>{escrow.seller}{escrow.seller_joined ? ' ✓ joined' : ''}</td></tr>
                  <tr><td style={{ color: '#666', padding: '4px 0' }}>Amount</td><td>{formatNear(escrow.amount)} NEAR</td></tr>
                  <tr><td style={{ color: '#666', padding: '4px 0' }}>Deadline</td><td>{new Date(Number(escrow.deadline)).toLocaleString()}</td></tr>
                  <tr><td style={{ color: '#666', padding: '4px 0' }}>Terms</td><td>{escrow.terms_hash || '—'}</td></tr>
                </tbody>
              </table>

              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
                {(escrow.buyer === account || escrow.seller === account) && escrow.status === 'created'
                  && !(escrow.buyer === account ? escrow.buyer_joined : escrow.seller_joined) && (
                  <button style={S.btn} onClick={() => act('Join', () => escrowCall.join(escrow.id))}>Join escrow</button>
                )}

                {escrow.buyer === account && escrow.status === 'joined'
                  && escrow.buyer_joined && escrow.seller_joined && (
                  <button style={S.btn} onClick={() => act('Deposit', () => escrowCall.deposit(escrow.id, escrow.amount))}>
                    Deposit {formatNear(escrow.amount)} NEAR
                  </button>
                )}

                {escrow.seller === account && escrow.status === 'funded' && (
                  <button style={S.btn} onClick={() => act('Submit delivery', () => escrowCall.submitDelivery(escrow.id))}>Mark delivered</button>
                )}

                {escrow.buyer === account && (escrow.status === 'funded' || escrow.status === 'delivered') && (
                  <button style={S.btn} onClick={() => act('Release', () => escrowCall.release(escrow.id))}>Release funds to seller</button>
                )}

                {(escrow.buyer === account || escrow.seller === account)
                  && (escrow.status === 'funded' || escrow.status === 'delivered') && (
                  <button style={S.btnGhost} onClick={() => act('Dispute', () => escrowCall.dispute(escrow.id))}>Raise dispute</button>
                )}

                {!escrow.disputed && ['funded', 'delivered'].includes(escrow.status) && (
                  <button style={S.btnGhost} onClick={() => act('Settle after deadline', () => escrowCall.settle(escrow.id))}>
                    Settle after deadline
                  </button>
                )}

                <button style={S.btnSmall} onClick={refresh}>Refresh</button>
              </div>

              {escrow.ai_recommendation && (
                <div style={{ marginTop: 12, padding: 12, background: '#fff7e6', border: '1px solid #fde68a', borderRadius: 8, fontSize: 14 }}>
                  <b>On-chain AI recommendation: {escrow.ai_recommendation.pct}% to seller.</b> {escrow.ai_recommendation.reason}
                </div>
              )}

              <p style={{ fontSize: 12, color: '#888', margin: '10px 0 0' }}>
                Settle is permissionless but only works after the deadline and only while there is no dispute.
              </p>
            </div>
          )}

          {escrow && escrow.status === 'disputed' && (
            <div style={S.card}>
              <h3 style={{ margin: '0 0 8px' }}>AI arbiter (advice only — the payer confirms)</h3>
              <label style={S.label}>Evidence from both sides</label>
              <textarea rows={3} style={{ ...S.input, resize: 'vertical' }} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                <button style={S.btnGhost} onClick={askAI} disabled={busy === 'arbiter'}>
                  {busy === 'arbiter' ? 'Reviewing...' : 'Ask AI arbiter'}
                </button>
                {aiRec && <>
                  <button style={S.btnGhost} onClick={() => act('Submit AI recommendation on-chain', () => escrowCall.aiRecommend(escrow.id, aiRec.pct, aiRec.reason || ''))}>
                    Record {aiRec.pct}% on-chain
                  </button>
                  {escrow.buyer === account && (
                    <button style={S.btn} onClick={() => act(`Resolve at ${aiRec.pct}%`, () => escrowCall.resolve(escrow.id, aiRec.pct))}>
                      Confirm payout {aiRec.pct}%
                    </button>
                  )}
                </>}
              </div>
              {aiRec && (
                <div style={{ marginTop: 12, padding: 12, background: '#eef6ee', borderRadius: 8, fontSize: 14 }}>
                  <b>{aiRec.pct}% to seller.</b> {aiRec.reason}
                  <div style={{ fontSize: 12, color: '#777', marginTop: 4 }}>source: {aiRec.source}</div>
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {/* ---------------- MINE ---------------- */}
      {tab === 'mine' && (
        <section>
          <div style={S.card}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <h3 style={{ margin: 0 }}>Your escrows</h3>
              <div style={{ flex: 1 }} />
              <button style={S.btnSmall} onClick={loadMine} disabled={mineBusy}>{mineBusy ? 'Loading...' : 'Refresh'}</button>
            </div>
            <p style={{ fontSize: 13, color: '#666', margin: '8px 0 0' }}>
              Escrows where this wallet is buyer or seller, newest first.
            </p>
          </div>

          {mine.length === 0 && !mineBusy && (
            <div style={S.card}><p style={{ margin: 0, color: '#666', fontSize: 14 }}>Nothing here yet. Create an escrow or join one with a code.</p></div>
          )}

          {mine.map((e) => (
            <div key={e.id} style={{ ...S.card, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <code style={{ fontSize: 16, fontWeight: 600 }}>{e.id}</code>
              <span style={S.badge(STATUS_COLORS[e.status] || STATUS_COLORS.created)}>{e.status}</span>
              <span style={{ fontSize: 13, color: '#555' }}>
                {formatNear(e.amount)} NEAR · {e.buyer === account ? 'you pay' : 'you receive'} · {e.seller}
              </span>
              <div style={{ flex: 1 }} />
              <button style={S.btnSmall} onClick={() => { setTab('join'); loadEscrow(e.id); }}>Open</button>
            </div>
          ))}
        </section>
      )}

      <footer style={{ marginTop: 26, fontSize: 12, color: '#999' }}>
        Contract <code>{CONTRACT}</code> · <a href={`${EXPLORER}/accounts/${CONTRACT}`} target="_blank" rel="noreferrer">explorer</a>
        {' · '}API <a href={`${API}/health`} target="_blank" rel="noreferrer">health</a>
      </footer>
    </main>
  );
}
