"""Trade co-pilot engine.

Provider-neutral by construction: the model returns a JSON object with a human
reply and an optional tool call. The server validates and executes the tool,
then returns the reply plus ACTION CARDS for the wallet steps. Works with any
provider; the deterministic echo router handles the no-AI case.
"""
from __future__ import annotations

import re

from .providers.base import AIProviderError
from .registry import get_provider
from .tools import TOOLS, ToolError, execute_tool
from .services import extract_json

AGENT_SYSTEM = """You are the SWEEFT trade co-pilot. You propose actions; you never sign or move funds.

Available tools (call by returning an action object):
- search_listings {query?, max_price?, kind?} — find ACTIVE listings ranked by seller on-chain trust
- get_seller_score {account_id} — a wallet's on-chain trust record
- check_my_balance {} — the buyer's NEAR balance
- get_my_trade_status {} — the buyer's open escrows, completed trades, disputes
- send_purchase_request {listing_slug, message?} — file an off-chain purchase request for the listing owner to act on
- prepare_purchase {listing_slug} — full purchase proposal card (seller score, totals, affordability)
- prepare_funding {escrow_id} — accept+fund action card for an existing escrow owned by the buyer
- prepare_dispute {escrow_id, reason} — dispute case-file card with the user's reason
- prepare_payment {to, amount, memo?} — propose paying a NEAR account in NEAR from the budget the user granted the co-pilot (one wallet approval). The user confirms on the card; execution is capped by the platform limit and the user's on-chain allowance.

Rules:
- Reply with exactly one JSON object: {"reply": string, "action": {"name": string, "args": object}|null}
- Prefer search_listings before proposing a purchase; mention seller scores when relevant.
- check_my_balance before claiming affordability.
- Never invent listings, balances, or reputations.
- For payments: only prepare_payment; never claim a payment was made in this turn — execution happens on the user's confirmation and the result is reported back.
- If the request needs no tool, use action null and just reply.
- Never claim to have signed, paid, shipped, or settled anything. You prepare; the wallet authorizes.
"""

ACCOUNT_RE = re.compile(r"([a-z0-9._-]+\.(?:testnet|near))\b", re.I)
ESCROW_RE = re.compile(r"\b(VP-\d{6})\b", re.I)
PRICE_RE = re.compile(r"(?:under|below|max(?:imum)?|less than|cheaper than)\s*(?:t?USD\s*)?([\d,.]+)", re.I)
SLUG_RE = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+)+)\b", re.I)
PAY_VERB_RE = re.compile(r"\b(pay|paying|send|sending|transfer|tip)\b", re.I)
PAY_AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(?:near|tusd|usd|\$)", re.I)
PAY_MEMO_RE = re.compile(r"\bfor\s+(.+)$", re.I)


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
    if spec.kind == "prepare":
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
        return {"reply": "Tell me which escrow (e.g. VP-000006) and what went wrong, and I'll prepare the dispute form for you.", "cards": [], "tool_trace": trace}

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

    # Purchase / discovery intent.
    if any(w in lowered for w in ("buy", "purchase", "find", "search", "looking", "want", "need")):
        max_price = None
        m = PRICE_RE.search(text)
        if m:
            max_price = float(m.group(1).replace(",", ""))
        # Strip stopwords as WHOLE WORDS (substring replacement would mangle
        # real words like "solar" -> "sol r"), then drop price tokens.
        stopwords = {
            "buy", "purchase", "find", "search", "looking", "want", "need", "for",
            "me", "an", "item", "a", "the", "under", "below", "less", "than",
            "maximum", "max", "cheaper", "tusd", "usd", "near", "please",
        }
        query = " ".join(
            tok for tok in lowered.split()
            if tok not in stopwords and not re.match(r"^\d+(?:[.,]\d+)?$", tok)
        )[:120]
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
        "reply": "I'm your trade co-pilot. Try: “buy solar inverters under 1200 NEAR”, “check my balance”, “score for seller.steepdata3968.testnet”, or “dispute VP-000006 goods arrived damaged”. I find, evaluate and prepare — your wallet signs the money steps.",
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
