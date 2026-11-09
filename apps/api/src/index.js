import express from 'express';
import cors from 'cors';

const app = express();
app.use(cors());
app.use(express.json({ limit: '1mb' }));

const AI_BASE = process.env.NEAR_AI_BASE || 'https://cloud.near.ai/api/v1';
const AI_KEY = process.env.NEAR_AI_KEY || '';

app.get('/health', (_req, res) => res.json({ ok: true, ai: Boolean(AI_KEY) }));

// Parse NL -> escrow fields. Falls back to regex if no key (judges can run without key).
app.post('/parse', async (req, res) => {
  const { text = '' } = req.body || {};
  if (AI_KEY) {
    try {
      const r = await fetch(`${AI_BASE}/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${AI_KEY}` },
        body: JSON.stringify({
          model: process.env.NEAR_AI_MODEL || 'deepseek/deepseek-v4.1-flash',
          messages: [
            { role: 'system', content: 'Extract escrow JSON: {amount, token, beneficiary, deadlineDays, terms}. Reply JSON only.' },
            { role: 'user', content: text }
          ],
          max_tokens: 800
        })
      });
      const j = await r.json();
      const content = (j.choices?.[0]?.message?.content || '{}').replace(/```json|```/g, '');
      let parsed = {};
      try { parsed = JSON.parse(content.match(/\{[\s\S]*\}/)?.[0] || '{}'); } catch { parsed = {}; }
      return res.json({ source: 'near-ai', parsed, raw: text });
    } catch (e) { /* fall through to regex */ }
  }
  const amount = text.match(/(\d+(?:\.\d+)?)\s*(usdc|usdt|near)/i)?.[1] || '';
  const beneficiary = text.match(/to\s+([a-z0-9._-]+\.testnet)/i)?.[1] || '';
  const days = /friday/i.test(text) ? 4 : (text.match(/(\d+)\s*day/i)?.[1] || '7');
  res.json({ source: 'fallback', parsed: { amount, token: 'USDC', beneficiary, deadlineDays: Number(days), terms: text.slice(0, 500) }, raw: text });
});

// AI dispute recommendation (recommend-only, human confirms on-chain).
app.post('/recommend', async (req, res) => {
  const { evidence = '', terms = '' } = req.body || {};
  if (AI_KEY) {
    try {
      const r = await fetch(`${AI_BASE}/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${AI_KEY}` },
        body: JSON.stringify({
          model: process.env.NEAR_AI_MODEL || 'deepseek/deepseek-v4.1-flash',
          messages: [
            { role: 'system', content: 'You are an escrow arbiter. Reply JSON {pct: 0-100 release to seller, reason: string}.' },
            { role: 'user', content: `Terms: ${terms}\nEvidence: ${evidence}` }
          ],
          max_tokens: 800
        })
      });
      const j = await r.json();
      const content = (j.choices?.[0]?.message?.content || '{}').replace(/```json|```/g, '');
      let rec = {};
      try { rec = JSON.parse(content.match(/\{[\s\S]*\}/)?.[0] || '{}'); } catch { rec = {}; }
      return res.json({ source: 'near-ai', pct: Number(rec.pct ?? 50), reason: String(rec.reason || 'AI gave no reason; human must decide.') });
    } catch (e) { /* fallback */ }
  }
  res.json({ source: 'fallback', pct: 50, reason: 'No AI key: default 50/50, human must decide.' });
});

const port = process.env.PORT || 4000;
app.listen(port, () => console.log(`api:${port} ai:${Boolean(AI_KEY)}`));
