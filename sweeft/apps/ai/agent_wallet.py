"""Server-side signing for the SWEEFT AI co-pilot agent account (testnet demo).

The co-pilot holds no user keys. A user grants it an on-chain NEAR allowance
with ONE wallet signature (`approve(spender=agent, amount)` on the token);
the agent can then call `ft_transfer_from` and the token contract itself
enforces the budget — scoped, capped and revocable on-chain. That allowance
is the security boundary.

Trust disclosure (testnet MVP): `AI_AGENT_PRIVATE_KEY` lives on the demo
server because this is a testnet build. On mainnet the same key would sit
inside a TEE / KMS — the on-chain allowance design does not change. Even a
fully compromised agent key can never move more than the granted allowance,
and the user can revoke at any time by approving 0.

The borsh/ed25519/broadcast pipeline itself now lives in
`apps.escrow.server_tx` and is shared with the verification relayer. The
modules import it via explicit signer keys; no user keys pass through either.
"""
from __future__ import annotations

from django.conf import settings

from apps.escrow.server_tx import GAS_FUNCTION_CALL, call_function_as, transfer_near_as

__all__ = [
    "GAS_FUNCTION_CALL",
    "agent_configured",
    "sign_and_send",
    "send_near_from",
    "transfer_from_allowance",
]


def agent_configured() -> bool:
    return bool(
        getattr(settings, "AI_AGENT_ACCOUNT", "")
        and getattr(settings, "AI_AGENT_PRIVATE_KEY", "")
        and getattr(settings, "AI_AGENT_ENABLED", True)
    )


def sign_and_send(
    *,
    method_name: str,
    args: dict,
    receiver_id: str | None = None,
    gas: int = GAS_FUNCTION_CALL,
    deposit: int = 1,
) -> dict:
    """Sign one FunctionCall with the co-pilot agent key and broadcast it."""
    if not agent_configured():
        raise RuntimeError("co-pilot agent wallet is not configured on this deployment")
    return call_function_as(
        signer=settings.AI_AGENT_ACCOUNT,
        private_key=settings.AI_AGENT_PRIVATE_KEY,
        method_name=method_name,
        args=args,
        receiver_id=receiver_id or settings.AI_AGENT_ACCOUNT,
        gas=gas,
        deposit=deposit,
    )


def send_near_from(*, signer: str, private_key: str, receiver: str, amount_yocto: int) -> dict:
    """Plain NEAR transfer signed by an explicit key (ops / treasury demos)."""
    return transfer_near_as(signer=signer, private_key=private_key, receiver=receiver, amount_yocto=amount_yocto)


def transfer_from_allowance(*, owner: str, receiver: str, amount: int, memo: str | None = None) -> dict:
    """Pay `receiver` `amount` yoctoNEAR from `owner`'s co-pilot budget.

    Kept under the historical function name so API call sites stay stable;
    this now calls the native NEAR budget contract's agent-only `pay` method.
    The memo remains in the off-chain PaymentIntent audit record.
    """
    return sign_and_send(
        method_name="pay",
        args={"owner": owner, "receiver": receiver, "amount": str(amount)},
        receiver_id=settings.AI_BUDGET_CONTRACT,
        gas=GAS_FUNCTION_CALL,
        deposit=0,
    )
