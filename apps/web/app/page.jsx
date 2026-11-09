'use client';
import { useState } from 'react';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';

export default function Home() {
  const [nl, setNl] = useState('pay 50 USDC to alice.testnet for logo by Friday');
  const [form, setForm] = useState({ counterparty: 'alice.testnet', amount: '50', deadline: '7', terms: 'logo delivery' });
  const [msg, setMsg] = useState('');

  async function aiFill() {
    setMsg('asking AI...');
    const r = await fetch(`${API}/parse`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: nl }) });
    const j = await r.json();
    setForm(f => ({ ...f, counterparty: j.parsed.beneficiary || f.counterparty, amount: j.parsed.amount || f.amount, deadline: String(j.parsed.deadlineDays || f.deadline), terms: j.parsed.terms || f.terms }));
    setMsg(`AI pre-fill (${j.source}) — verify then Create. Connect wallet to submit on-chain (testnet).`);
  }

  return (
    <main style={{ maxWidth: 720, margin: '40px auto', fontFamily: 'system-ui', padding: 16 }}>
      <h1>SWEeft — NEAR escrow (testnet v1)</h1>
      <p>Form input or AI-assistant input. Same contract path. Human confirms.</p>
      <h3>1. AI assistant input</h3>
      <textarea value={nl} onChange={e => setNl(e.target.value)} rows={3} style={{ width: '100%' }} />
      <div><button onClick={aiFill}>AI pre-fill form</button></div>
      <h3>2. Form input (fixed parties)</h3>
      {['counterparty', 'amount', 'deadline', 'terms'].map(k => (
        <div key={k} style={{ margin: '8px 0' }}>
          <label>{k}: </label>
          <input value={form[k]} onChange={e => setForm({ ...form, [k]: e.target.value })} style={{ width: 320 }} />
        </div>
      ))}
      <p><i>{msg}</i></p>
      <h3>3. Escrow actions (wallet-gated, testnet)</h3>
      <p>Connect wallet, Create with fixed counterparty, share code SWFT-N, counterparty Joins, buyer Deposits, seller Delivers, payer Releases or anyone Settles after deadline if no dispute.</p>
      <p>Contract wiring lands next commit. API: <code>{API}/health</code></p>
    </main>
  );
}
