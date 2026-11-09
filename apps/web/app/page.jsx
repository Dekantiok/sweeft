'use client';
import { useEffect, useState } from 'react';
import { getSelector, showWalletModal, getAccount, signOut, escrowCall, escrowGet, CONTRACT } from '../lib/near';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';

export default function Home() {
  const [nl, setNl] = useState('pay 50 USDC to alice.testnet for logo by Friday');
  const [form, setForm] = useState({ counterparty: 'alice.testnet', amount: '50', deadlineDays: '7', terms: 'logo delivery' });
  const [account, setAccount] = useState(null);
  const [escrowId, setEscrowId] = useState('');
  const [evidence, setEvidence] = useState('Seller delivered logo at https://example.com/logo.png, buyer claims colors wrong.');
  const [aiRec, setAiRec] = useState(null);
  const [status, setStatus] = useState(null);
  const [listening, setListening] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    getSelector().then(() => getAccount().then(setAccount).catch(() => {}));
  }, []);

  async function refreshAccount() {
    setAccount(await getAccount().catch(() => null));
  }

  async function aiFill() {
    setMsg('asking AI...');
    try {
      const r = await fetch(`${API}/parse`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: nl }) });
      const j = await r.json();
      setForm(f => ({
        ...f,
        counterparty: j.parsed.beneficiary || f.counterparty,
        amount: j.parsed.amount || f.amount,
        deadlineDays: String(j.parsed.deadlineDays || f.deadlineDays),
        terms: j.parsed.terms || f.terms,
      }));
      setMsg(`AI pre-fill (${j.source}) — verify then Create.`);
    } catch (e) { setMsg(`AI error: ${e.message}`); }
  }

  function mic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { setMsg('Mic not supported here — type or paste instead. Sample audio transcript works the same.'); return; }
    const rec = new SR();
    rec.lang = 'en-US';
    setListening(true);
    rec.onresult = (e) => { setNl(e.results[0][0].transcript); setListening(false); };
    rec.onerror = () => { setListening(false); setMsg('Mic error — type instead.'); };
    rec.onend = () => setListening(false);
    rec.start();
  }

  async function askAI() {
    setMsg('AI arbiter reviewing...');
    try {
      const r = await fetch(`${API}/recommend`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ evidence, terms: form.terms }) });
      const j = await r.json();
      setAiRec(j);
      setMsg(`AI recommends ${j.pct}% to seller (${j.source}) — human must confirm on-chain.`);
    } catch (e) { setMsg(`AI error: ${e.message}`); }
  }

  async function doTx(label, fn) {
    setMsg(`${label}...`);
    try { await fn(); }
    catch (e) { setMsg(`${label} failed: ${e.message}`); return; }
    if (label === 'Create') { setMsg('Created — copy the SWFT-XXXX code from your wallet receipt, paste it above, then Lookup.'); return; }
    if (escrowId.trim()) { setMsg(`${label} sent — refreshing status...`); setTimeout(lookup, 2500); }
    else setMsg(`${label} sent — confirm in wallet.`);
  }

  async function lookup() {
    setMsg('reading escrow...');
    try { const e = await escrowGet(escrowId.trim()); setStatus(e); setMsg(e ? `status: ${e.status}` : 'not found'); }
    catch (e) { setMsg(`lookup failed (contract TBD?): ${e.message}`); }
  }

  return (
    <main style={{ maxWidth: 760, margin: '32px auto', fontFamily: 'system-ui', padding: 16 }}>
      <h1>SWEeft — NEAR escrow (testnet v1)</h1>
      <p>Form input or AI-assistant input. Same contract path. Human confirms. Contract: <code>{CONTRACT}</code> (<a href={`https://explorer.testnet.near.org/accounts/${CONTRACT}`} target="_blank" rel="noreferrer">explorer</a>)</p>

      <div style={{ border: '1px solid #ccc', padding: 12, marginBottom: 12 }}>
        {account
          ? <span>Connected: <b>{account}</b> <button onClick={async () => { await signOut(); refreshAccount(); }}>Disconnect</button></span>
          : <button onClick={() => { showWalletModal(); setTimeout(refreshAccount, 1500); }}>Connect wallet (testnet)</button>}
      </div>

      <h3>1. AI assistant input (text or voice)</h3>
      <textarea value={nl} onChange={e => setNl(e.target.value)} rows={3} style={{ width: '100%' }} />
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button onClick={aiFill}>AI pre-fill form</button>
        <button onClick={mic}>{listening ? 'Listening...' : '🎤 Voice → text'}</button>
      </div>

      <h3>2. Form input (fixed parties)</h3>
      {Object.entries({ counterparty: 'counterparty wallet (*.testnet)', amount: 'NEAR amount (testnet)', deadlineDays: 'deadline (days)', terms: 'terms' }).map(([k, label]) => (
        <div key={k} style={{ margin: '8px 0' }}>
          <label>{label}: </label>
          <input value={form[k]} onChange={e => setForm({ ...form, [k]: e.target.value })} style={{ width: 340 }} />
        </div>
      ))}

      <h3>3. Escrow actions</h3>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button onClick={() => doTx('Create', () => escrowCall.create({
          seller: form.counterparty, buyer: account,
          amount: form.amount, ft: 'near',
          deadline_ms: String(Date.now() + Number(form.deadlineDays) * 864e5),
          terms_hash: form.terms.slice(0, 200), foreign_asset: null,
        }))}>Create (I am buyer)</button>
        <input placeholder="SWFT-XXXX code" value={escrowId} onChange={e => setEscrowId(e.target.value)} style={{ width: 140 }} />
        <button onClick={() => doTx('Join', () => escrowCall.join(escrowId.trim()))}>Join</button>
        <button onClick={() => doTx('Deposit', () => escrowCall.deposit(escrowId.trim(), form.amount))}>Deposit</button>
        <button onClick={() => doTx('Submit delivery', () => escrowCall.submitDelivery(escrowId.trim()))}>Deliver</button>
        <button onClick={() => doTx('Release', () => escrowCall.release(escrowId.trim()))}>Release (payer)</button>
        <button onClick={() => doTx('Dispute', () => escrowCall.dispute(escrowId.trim()))}>Dispute</button>
        <button onClick={() => doTx('Settle (after deadline)', () => escrowCall.settle(escrowId.trim()))}>Settle</button>
        <button onClick={lookup}>Lookup status</button>
      </div>
      <p><i>{msg}</i></p>
      {status && <>
        {status.ai_recommendation && <p style={{ background: '#fff7e6', padding: 8 }}>⛳ On-chain AI recommendation: <b>{status.ai_recommendation.pct}% to seller</b> — {status.ai_recommendation.reason}</p>}
        <pre style={{ background: '#f4f4f4', padding: 12 }}>{JSON.stringify(status, null, 2)}</pre>
      </>}
      <h3>4. AI arbiter (dispute only, human confirms)</h3>
      <textarea value={evidence} onChange={e => setEvidence(e.target.value)} rows={2} style={{ width: '100%' }} />
      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
        <button onClick={askAI}>Ask AI arbiter</button>
        {aiRec && <>
          <button onClick={() => doTx('Submit AI recommendation', () => escrowCall.aiRecommend(escrowId.trim(), aiRec.pct, aiRec.reason || ''))}>Submit {aiRec.pct}% on-chain</button>
          <button onClick={() => doTx(`Resolve ${aiRec.pct}%`, () => escrowCall.resolve(escrowId.trim(), aiRec.pct))}>Resolve {aiRec.pct}% (payer)</button>
        </>}
      </div>
      {aiRec && <pre style={{ background: '#eef6ee', padding: 12 }}>{JSON.stringify(aiRec, null, 2)}</pre>}
      <p>API: <code>{API}/health</code> · For judges without mic: paste the sample sentence above, it takes the same path as voice.</p>
    </main>
  );
}
