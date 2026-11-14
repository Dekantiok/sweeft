"""Trade co-pilot engine.

Provider-neutral by construction: the model returns a JSON object with a human
reply and an optional tool call. The server validates and executes the tool,
then returns the reply plus ACTION CARDS for the wallet steps. Works with any
provider; the deterministic echo router handles the no-AI case.
"""
from __future__ import annotations

import re

from apps.market.models import Offer

from .providers.base import AIProviderError
from .registry import get_provider
from .tools import TOOLS, ToolError, execute_tool
from .services import extract_json

AGENT_SYSTEM = """You are the SWEEFT trade co-pilot. You propose actions; you never sign or move funds.

Available tools (call by returning an action object):
- search_listings {query?, max_price?, kind?} — find ACTIVE listings ranked by seller on-chain trust (top 5 with reasons)
- compare_listings {slugs: [2–4 slugs]} — factual side-by-side comparison (price, seller record, location, pros)
- get_seller_score {account_id} — a wallet's on-chain trust record
- check_my_balance {} — the buyer's NEAR balance
- get_my_trade_status {} — open escrows (state, tracking, verification) and open offers for the connected wallet
- send_purchase_request {listing_slug, message?} — file an off-chain purchase request for the listing owner to act on
- prepare_purchase {listing_slug} — full purchase proposal card (seller score, totals, affordability)
- create_offer {listing_slug, amount, max_amount, message?} — propose a price to the seller (buyer only; off-chain, no funds move)
- counter_offer {offer_id, amount} — buyer counters a seller counteroffer; amount must stay within the buyer's ceiling
- accept_offer {offer_id} — seller accepts a proposed/countered offer, or the buyer accepts a seller counteroffer
- reject_offer {offer_id} — seller rejects an open offer
- get_offer_status {offer_id} — offer card plus the full negotiation history
- prepare_funding {escrow_id} — accept+fund action card for an existing escrow owned by the buyer
- prepare_dispute {escrow_id, reason} — dispute case-file card with the user's reason
- prepare_payment {to, amount, memo?} — propose paying a NEAR account in NEAR from the budget the user granted the co-pilot (one wallet approval). The user confirms on the card; execution is capped by the platform limit and the user's on-chain allowance.

Rules:
- Reply with exactly one JSON object: {"reply": string, "action": {"name": string, "args": object}|null}
- Prefer search_listings before proposing a purchase; mention seller scores when relevant.
- check_my_balance before claiming affordability.
- Never invent listings, balances, or reputations.
- Offers are off-chain negotiation records: creating, countering, accepting or rejecting an offer never moves funds. The seller then creates the escrow at the agreed price.
- For payments: only prepare_payment; never claim a payment was made in this turn — execution happens on the user's confirmation and the result is reported back.
- If the request needs no tool, use action null and just reply.
- Never claim to have signed, paid, shipped, or settled anything. You prepare; the wallet authorizes.
"""

ACCOUNT_RE = re.compile(r"([a-z0-9._-]+\.(?:testnet|near))\b", re.I)
ESCROW_RE = re.compile(r"\b(SWFT-\d{6})\b", re.I)
PRICE_RE = re.compile(r"(?:under|below|max(?:imum)?|less than|cheaper than)\s*(?:t?USD\s*)?([\d,.]+)", re.I)
SLUG_RE = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+)+)\b", re.I)
PAY_VERB_RE = re.compile(r"\b(pay|paying|send|sending|transfer|tip)\b", re.I)
PAY_AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(?:near|usd|\$)", re.I)
PAY_MEMO_RE = re.compile(r"\bfor\s+(.+)$", re.I)
OFFER_CREATE_RE = re.compile(r"\boffer\s+(?:of\s+)?([\d][\d,]*(?:\.\d+)?)\s*(?:near)?\s+(?:for|on)\s+(.+)$", re.I)
OFFER_MAX_RE = re.compile(r"\bmax(?:imum)?\s+(?:budget\s+)?([\d][\d,]*(?:\.\d+)?)", re.I)
COUNTER_WITH_RE = re.compile(r"\bcounter(?:\s+offer)?\s+#?(\d+)\s+(?:with|at|to|for)\s+([\d][\d,]*(?:\.\d+)?)", re.I)
COUNTER_OFFER_ID_RE = re.compile(r"\bcounter\s+offer\s+#?(\d+)\b", re.I)
ACCEPT_OFFER_RE = re.compile(r"\baccept\s+(?:the\s+)?(?:offer\s+)?#?(\d+)\b", re.I)
REJECT_OFFER_RE = re.compile(r"\breject\s+(?:the\s+)?(?:offer\s+)?#?(\d+)\b", re.I)
OFFER_STATUS_FULL_RE = re.compile(r"^\s*(?:get\s+)?offer\s+#?(\d+)\s*$", re.I)
OFFER_STATUS_WORD_RE = re.compile(r"\boffer\s+#?(\d+)\s+status\b|\bstatus\s+(?:of\s+)?(?:the\s+)?offer\s+#?(\d+)\b", re.I)

SEARCH_STOPWORDS = {
    "buy", "purchase", "find", "search", "looking", "want", "need", "for",
    "me", "an", "item", "a", "the", "under", "below", "less", "than",
    "maximum", "max", "cheaper", "usd", "near", "please", "show",
}


def _search_query(lowered: str) -> str:
    """Strip intent stopwords as WHOLE WORDS (substring replacement would
    mangle real words like "solar" -> "sol r"), then drop price tokens."""
    return " ".join(
        tok for tok in lowered.split()
        if tok not in SEARCH_STOPWORDS and not re.match(r"^\d+(?:[.,]\d+)?$", tok)
    )[:120]


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _card(kind: str, result: dict) -> dict:
    return {"kind": kind, **result}


def _run(name: str, args: dict, wallet: str, trace: list, cards: list) -> str | None:
    """Execute one tool, recording trace and any resulting card."""
    spec = TOOLS.get(name)
    if spec is None:
        return f"(unknown tool '{name}' — ignored)"
    try:
        result = execute_tool(name, args, wallet)
    except ToolError as e:
        return f"(couldn't run {name}: {e})"
    trace.append({"tool": name, "kind": spec.kind, "args": args})
    if spec.kind in ("prepare", "write"):
        cards.append(_card(result.get("type", "proposal"), result))
    return None


def keyword_route(message: str, wallet: str) -> dict:
    """Deterministic agent path used when no LLM provider is configured."""
    text = (message or "").strip()
    lowered = text.lower()
    trace: list = []
    cards: list = []

    # Disputes take precedence (a dispute is a specific escrow action).
    if any(w in lowered for w in ("dispute", "problem", "damaged", "wrong", "issue with")):
        eid = ESCROW_RE.search(text)
        if eid:
            reason = text
            try:
                result = execute_tool("prepare_dispute", {"escrow_id": eid.group(1).upper(), "reason": reason}, wallet)
                cards.append(_card(result.get("type", "dispute_proposal"), result))
                trace.append({"tool": "prepare_dispute", "kind": "prepare", "args": {"escrow_id": eid.group(1).upper()}})
                return {"reply": f"I've prepared the dispute case file for {eid.group(1).upper()}. Review it and submit on-chain in your wallet — I can't file it for you.", "cards": cards, "tool_trace": trace}
            except ToolError as e:
                return {"reply": f"I can't open that dispute: {e}", "cards": [], "tool_trace": trace}
        return {"reply": "Tell me which escrow (e.g. SWFT-482913) and what went wrong, and I'll prepare the dispute form for you.", "cards": [], "tool_trace": trace}

    # Balance questions.
    if any(w in lowered for w in ("balance", "afford", "how much", "have i got")):
        result = execute_tool("check_my_balance", {}, wallet)
        trace.append({"tool": "check_my_balance", "kind": "read", "args": {}})
        return {"reply": f"Your NEAR balance is {result['balance_display']} NEAR. Tell me an item and I'll check whether you can cover the full escrow total (price + logistics + platform fee).", "cards": [], "tool_trace": trace}

    # Reputation questions about a wallet.
    acct = ACCOUNT_RE.search(text)
    if any(w in lowered for w in ("score", "reputation", "trust")) and acct:
        result = execute_tool("get_seller_score", {"account_id": acct.group(1).lower()}, wallet)
        trace.append({"tool": "get_seller_score", "kind": "read", "args": {"account_id": acct.group(1).lower()}})
        return {"reply": f"{acct.group(1).lower()}: {result['summary']}.", "cards": [], "tool_trace": trace}

    # Co-pilot payments: "pay 25 NEAR to seller.testnet for invoice 12".
    if PAY_VERB_RE.search(text) and acct:
        amount_match = (
            PAY_AMOUNT_RE.search(text)
            or re.search(r"\b(?:pay|send|transfer|tip)\s+(?:t?usd\s*)?([\d][\d,]*(?:\.\d+)?)", lowered)
            or re.search(r"\b([\d][\d,]*(?:\.\d+)?)\b", text)
        )
        if amount_match:
            to_account = acct.group(1).lower()
            amount = amount_match.group(1).replace(",", "")
            memo = ""
            memo_match = PAY_MEMO_RE.search(text)
            if memo_match:
                memo = ACCOUNT_RE.sub("", memo_match.group(1)).strip(" ,.-—")
            try:
                result = execute_tool(
                    "prepare_payment",
                    {"to": to_account, "amount": amount, "memo": memo},
                    wallet,
                )
                cards.append(_card(result.get("type", "payment_proposal"), result))
                trace.append({"tool": "prepare_payment", "kind": "prepare", "args": {"to": to_account, "amount": amount}})
                if result["can_execute"]:
                    how = (
                        f"Your co-pilot budget has {result['allowance_display']} NEAR left, so confirm "
                        "“Pay now” on the card — the co-pilot settles it for you with no extra popups."
                    )
                else:
                    how = (
                        f"Your co-pilot budget is {result['allowance_display']} NEAR — grant the suggested budget "
                        "on the card (one wallet signature) and the co-pilot pays instantly."
                    )
                memo_text = f" for {memo}" if memo else ""
                return {
                    "reply": (
                        f"Payment ready: {result['amount_display']} NEAR to {to_account}{memo_text}. "
                        f"{how} The budget contract enforces your limit on-chain — withdraw the unspent remainder to revoke."
                    ),
                    "cards": cards,
                    "tool_trace": trace,
                }
            except ToolError as e:
                return {"reply": f"I can't prepare that payment: {e}", "cards": [], "tool_trace": trace}
        return {
            "reply": "Tell me the amount too, e.g. “pay 25 NEAR to seller.steepdata3968.testnet for invoice 12”.",
            "cards": [],
            "tool_trace": trace,
        }

    # Offers: counter (buyer), accept (seller or buyer counter), reject (seller),
    # status by id, and create ("offer X for <slug> max Y").
    counter_word = re.search(r"\bcounter\b", lowered)
    if counter_word:
        offer_id = None
        amount = None
        m = COUNTER_WITH_RE.search(lowered)
        if m:
            offer_id = int(m.group(1))
            amount = m.group(2)
        else:
            id_m = COUNTER_OFFER_ID_RE.search(lowered)
            if id_m:
                offer_id = int(id_m.group(1))
            after = lowered[counter_word.start():]
            amt_m = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*(?:near)?\b", after)
            if amt_m:
                candidate = amt_m.group(1)
                if id_m is None or candidate != id_m.group(1):
                    amount = candidate
        if amount is None:
            return {"reply": "Tell me the counter amount, e.g. “counter 3 with 1200” — your max ceiling still applies.", "cards": [], "tool_trace": trace}
        if offer_id is None:
            latest = Offer.objects.filter(buyer_account=wallet, status="countered").order_by("-updated_at").first()
            if latest is None:
                return {"reply": "You have no seller counteroffer waiting. Ask me for your “offer status” or send a new offer from the listing page.", "cards": [], "tool_trace": trace}
            offer_id = latest.id
        try:
            result = execute_tool("counter_offer", {"offer_id": offer_id, "amount": amount}, wallet)
        except ToolError as e:
            return {"reply": f"I can't counter that: {e}", "cards": [], "tool_trace": trace}
        cards.append(_card("offer", result))
        trace.append({"tool": "counter_offer", "kind": "write", "args": {"offer_id": offer_id, "amount": amount}})
        return {"reply": f"Counteroffer sent: {result['amount_display']} NEAR on “{result['listing']}”. The seller can accept or counter back.", "cards": cards, "tool_trace": trace}

    accept_m = ACCEPT_OFFER_RE.search(lowered)
    if accept_m:
        try:
            result = execute_tool("accept_offer", {"offer_id": int(accept_m.group(1))}, wallet)
        except ToolError as e:
            return {"reply": f"I can't accept that offer: {e}", "cards": [], "tool_trace": trace}
        cards.append(_card("offer", result))
        trace.append({"tool": "accept_offer", "kind": "write", "args": {"offer_id": int(accept_m.group(1))}})
        return {"reply": f"Offer #{result['offer_id']} accepted at {result['amount_display']} NEAR. The seller creates the escrow at that price and you accept + fund it in your wallet.", "cards": cards, "tool_trace": trace}

    reject_m = REJECT_OFFER_RE.search(lowered)
    if reject_m:
        try:
            result = execute_tool("reject_offer", {"offer_id": int(reject_m.group(1))}, wallet)
        except ToolError as e:
            return {"reply": f"I can't reject that offer: {e}", "cards": [], "tool_trace": trace}
        cards.append(_card("offer", result))
        trace.append({"tool": "reject_offer", "kind": "write", "args": {"offer_id": int(reject_m.group(1))}})
        return {"reply": f"Offer #{result['offer_id']} rejected.", "cards": cards, "tool_trace": trace}

    status_m = OFFER_STATUS_FULL_RE.match(lowered) or OFFER_STATUS_WORD_RE.search(lowered)
    if status_m:
        offer_id = int(next(group for group in status_m.groups() if group))
        try:
            result = execute_tool("get_offer_status", {"offer_id": offer_id}, wallet)
        except ToolError as e:
            return {"reply": f"I can't show that offer: {e}", "cards": [], "tool_trace": trace}
        cards.append(_card("offer", result))
        trace.append({"tool": "get_offer_status", "kind": "read", "args": {"offer_id": offer_id}})
        return {"reply": f"Offer #{result['offer_id']} is {result['status']} at {result['amount_display']} NEAR — full history on the card.", "cards": cards, "tool_trace": trace}

    if re.search(r"\boffer\b", lowered) and not re.search(r"\b(?:counter|accept|reject)\b", lowered):
        create_m = OFFER_CREATE_RE.search(lowered)
        if create_m:
            amount = create_m.group(1).replace(",", "")
            target = create_m.group(2)
            slug_m = SLUG_RE.search(target)
            if not slug_m:
                return {"reply": "Which listing? Send the listing slug, e.g. “offer 1200 for solar-inverter-batch max 1300”.", "cards": [], "tool_trace": trace}
            max_m = OFFER_MAX_RE.search(text)
            max_amount = max_m.group(1).replace(",", "") if max_m else amount
            try:
                result = execute_tool(
                    "create_offer",
                    {"listing_slug": slug_m.group(1).lower(), "amount": amount, "max_amount": max_amount},
                    wallet,
                )
            except ToolError as e:
                return {"reply": f"I can't send that offer: {e}", "cards": [], "tool_trace": trace}
            cards.append(_card("offer", result))
            trace.append({"tool": "create_offer", "kind": "write", "args": {"listing_slug": slug_m.group(1).lower(), "amount": amount, "max_amount": max_amount}})
            return {"reply": f"Offered {result['amount_display']} NEAR for “{result['listing']}” (your ceiling {result['max_display']} NEAR). The seller can accept, counter or reject.", "cards": cards, "tool_trace": trace}

    # Status / tracking questions ("status", "where is my order", "tracking").
    if any(w in lowered for w in ("status", "where is my order", "tracking", "my order")):
        result = execute_tool("get_my_trade_status", {}, wallet)
        trace.append({"tool": "get_my_trade_status", "kind": "read", "args": {}})
        cards.append({"kind": "trade_status", **result})
        return {
            "reply": f"You have {len(result['escrows'])} open escrow(s) and {len(result['offers'])} open offer(s) — details on the card.",
            "cards": cards,
            "tool_trace": trace,
        }

    # Comparison: "compare A and B" with 2–4 listing slugs.
    if "compare" in lowered:
        slugs = _unique(slug.lower() for slug in SLUG_RE.findall(text))
        if 2 <= len(slugs) <= 4:
            try:
                result = execute_tool("compare_listings", {"slugs": slugs}, wallet)
            except ToolError as e:
                return {"reply": f"I can't compare those: {e}", "cards": [], "tool_trace": trace}
            cards.append({"kind": "comparison", **result})
            trace.append({"tool": "compare_listings", "kind": "read", "args": {"slugs": slugs}})
            return {"reply": f"Compared {len(result['rows'])} listings — prices, seller records and locations are on the card.", "cards": cards, "tool_trace": trace}
        return {"reply": "Send 2–4 listing slugs to compare, e.g. “compare solar-inverter-batch and cotton-fabric”.", "cards": [], "tool_trace": trace}

    # Discovery: "find/search/looking for X", or a bare "under X NEAR" budget.
    search_intent = any(w in lowered for w in ("find", "search", "looking"))
    buy_intent = any(w in lowered for w in ("buy", "purchase", "want", "need"))
    if search_intent or (PRICE_RE.search(text) and not buy_intent):
        max_price = None
        m = PRICE_RE.search(text)
        if m:
            max_price = float(m.group(1).replace(",", ""))
        query = _search_query(lowered)
        result = execute_tool("search_listings", {"query": query, "max_price": max_price}, wallet)
        trace.append({"tool": "search_listings", "kind": "read", "args": {"query": query, "max_price": max_price}})
        cards.append({"kind": "search_results", **{k: v for k, v in result.items() if k != "kind"}, "note": "Ranked by each seller's on-chain trust record."})
        if not result["results"]:
            return {"reply": f"I couldn't find an active listing matching “{query or 'that'}” (max {max_price or 'any'} NEAR). Try different words or a higher budget.", "cards": cards, "tool_trace": trace}
        top = result["results"][0]
        return {
            "reply": (
                f"Found {result['count']} active listing(s). Top match: **{top['title']}** at {top['price_display']} NEAR "
                f"from {top['seller']}. Ask me to “compare {top['slug']} and …” or “offer X for {top['slug']} max Y”."
            ),
            "cards": cards,
            "tool_trace": trace,
        }

    # Purchase intent: existing full proposal flow (search + action card).
    if buy_intent:
        max_price = None
        m = PRICE_RE.search(text)
        if m:
            max_price = float(m.group(1).replace(",", ""))
        query = _search_query(lowered)
        result = execute_tool("search_listings", {"query": query, "max_price": max_price}, wallet)
        trace.append({"tool": "search_listings", "kind": "read", "args": {"query": query, "max_price": max_price}})
        if not result["results"]:
            return {"reply": f"I couldn't find an active listing matching {query or 'that'} (max {max_price or 'any'} NEAR). Try a different item or budget.", "cards": [], "tool_trace": trace}
        top = result["results"][0]
        proposal = execute_tool("prepare_purchase", {"listing_slug": top["slug"]}, wallet)
        cards.append(_card(proposal.get("type", "purchase_proposal"), proposal))
        trace.append({"tool": "prepare_purchase", "kind": "prepare", "args": {"listing_slug": top["slug"]}})
        summary = f"I found **{top['title']}** at {top['price']} NEAR from {top['seller']}. Seller record: {top['seller_score']}. "
        summary += "Your balance: " + proposal["buyer_balance"] + " NEAR. "
        summary += "Affordable: yes." if proposal["affordable"] else "Affordable: no — mint more test NEAR first."
        return {"reply": summary + " Ready to proceed? Send the purchase request from the listing page — the seller then issues the escrow and you accept + fund in your wallet.", "cards": cards, "tool_trace": trace}

    return {
        "reply": "I'm your trade co-pilot. Try: “find solar inverters under 1200 NEAR”, “compare solar-inverter-batch and cotton-fabric”, “offer 1150 for solar-inverter-batch max 1200”, “counter 3 with 1180”, “status”, or “check my balance”. I find, evaluate and prepare — your wallet signs the money steps.",
        "cards": [],
        "tool_trace": trace,
    }


def run_agent(message: str, wallet: str) -> dict:
    """One agent turn: model (or rules) -> validated tool execution -> reply + cards."""
    if not message or not message.strip():
        return {"reply": "Tell me what you want to trade or review, and I'll help.", "cards": [], "tool_trace": []}

    provider = get_provider()
    if provider.name == "echo":
        return keyword_route(message, wallet)

    try:
        raw = provider.complete(system=AGENT_SYSTEM, user=message, json_mode=True, max_tokens=700)
        data = extract_json(raw.text)
    except AIProviderError:
        data = None

    if not isinstance(data, dict):
        return keyword_route(message, wallet)

    trace: list = []
    cards: list = []
    reply = str(data.get("reply") or "").strip()[:2500]
    action = data.get("action")
    if isinstance(action, dict) and action.get("name"):
        error = _run(
            str(action["name"]),
            action.get("args") if isinstance(action.get("args"), dict) else {},
            wallet,
            trace,
            cards,
        )
        if error:
            reply = (reply + "\n\n" + error).strip()
    return {"reply": reply or "Here's what I found.", "cards": cards, "tool_trace": trace}
