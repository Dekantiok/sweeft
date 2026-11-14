"""Read-only verification of the live deployment — no keys, no wallet, public RPC.

    python3 manage.py verify_live
    python3 manage.py verify_live --contract sweeft-eco.testnet
    python3 manage.py verify_live --expect-wasm-hash <sha256>

Re-checks, straight from testnet: the deployed code hash equals the reproducible
build artifact, the contract is keyless (immutable), governance/carrier wiring,
and the signed lifecycle evidence quoted in the README (completed lifecycle,
refund/reject/cancel probes, terminal index pruning, reputation).

Exits non-zero when any check fails, so CI or a judge can gate on it.
"""
from __future__ import annotations

import base64
import hashlib
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.escrow import near

# The reproducible artifact for the sweeft-eco deployment
# (`cargo near build reproducible-wasm`, SHA-256 of target/near/sweeft.wasm).
DEFAULT_WASM_SHA256 = "c4d0b2c4733fc846d69faee2d7b7058a8b612ea14e7212ca2b6d1a39cab3e284"

# Escrows quoted in the README's "Verified end-to-end on testnet" table.
LIFECYCLE_ESCROW = "SWFT-014021"   # create → TEE → settle → confirm
REFUND_ESCROW = "SWFT-984999"      # funded, never dispatched → buyer refund
REJECT_ESCROW = "SWFT-747532"      # buyer rejected
CANCEL_ESCROW = "SWFT-054544"      # seller cancelled


class Command(BaseCommand):
    help = "Verify the live deployment read-only: code hash, keyless state, governance and lifecycle evidence."

    def _call(self, fn, *args, attempts: int = 4, **kwargs):
        """Public RPC can be slow; retry transient transport failures."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — retried until the deadline
                last = exc
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _view(self, contract: str, method: str, args: dict):
        return self._call(near.view_call, contract, method, args) or {}

    def add_arguments(self, parser):
        parser.add_argument(
            "--contract",
            default=settings.SWEEFT_CONTRACT,
            help="escrow contract account (default: SWEEFT_CONTRACT)",
        )
        parser.add_argument(
            "--expect-wasm-hash",
            default=DEFAULT_WASM_SHA256,
            help="expected on-chain code SHA-256 (reproducible build artifact)",
        )

    def handle(self, *args, **options):
        contract = options["contract"]
        if not contract:
            raise SystemExit("no contract configured — pass --contract or set SWEEFT_CONTRACT")

        checks: list[tuple[str, bool, str]] = []

        def check(label: str, ok: bool, detail: str = "") -> None:
            checks.append((label, ok, detail))

        self.stdout.write(f"SWEEFT live verification — {contract} (public RPC, read-only)\n")

        # 1. Deployed code hash == reproducible artifact.
        code = self._call(
            near._rpc,
            "query",
            {"request_type": "view_code", "finality": "final", "account_id": contract},
        )
        remote = base64.b64decode((code.get("result") or {}).get("code_base64", ""))
        remote_hash = hashlib.sha256(remote).hexdigest() if remote else ""
        check(
            "deployed code matches the reproducible build",
            remote_hash == options["expect_wasm_hash"],
            remote_hash or "no code returned",
        )

        # 2. Keyless / immutable.
        keys = self._call(
            near._rpc,
            "query",
            {"request_type": "view_access_key_list", "finality": "final", "account_id": contract},
        )
        key_list = (keys.get("result") or {}).get("keys", [])
        check("contract has no deploy keys (immutable)", not key_list, f"{len(key_list)} key(s)")

        # 3. Governance + carrier wiring.
        gov = self._view(contract, "get_gov_info", {})
        board = getattr(settings, "GOVERNANCE_DAO", "") or "sweeft-board.testnet"
        check(
            "review board + treasury wired",
            gov.get("arbiter") == board and gov.get("treasury") == board,
            f"arbiter={gov.get('arbiter')} treasury={gov.get('treasury')}",
        )
        carrier = self._view(contract, "get_carrier_pubkey", {}) or ""
        check(
            "carrier signing key registered on-chain",
            len(carrier) == 64 and all(c in "0123456789abcdef" for c in carrier),
            carrier or "(none)",
        )
        config = self._view(contract, "get_config", {})
        check(
            "platform fee set (5%)",
            config.get("platform_fee_bps") == 500,
            f"{config.get('platform_fee_bps')} bps",
        )

        # 4. Lifecycle evidence.
        escrow = self._view(contract, "get_escrow", {"escrow_id": LIFECYCLE_ESCROW})
        paid = all(escrow.get(f) for f in ("seller_paid", "logistics_paid", "platform_paid"))
        verified = bool((escrow.get("last_verification") or {}).get("verified"))
        check(
            f"lifecycle {LIFECYCLE_ESCROW} completed with TEE verification",
            escrow.get("state") == "Completed" and paid and verified,
            f"state={escrow.get('state')} paid={paid} tee={verified}",
        )

        # 5. Terminal escrows leave the active indexes.
        pruned = True
        for account in (escrow.get("seller"), escrow.get("buyer")):
            if not account:
                continue
            active = self._view(
                contract, "escrows_for", {"account_id": account, "from_index": 0, "limit": 50}
            ) or []
            if any(row.get("id") == LIFECYCLE_ESCROW for row in active):
                pruned = False
        check(f"{LIFECYCLE_ESCROW} left the active indexes (terminal pruning)", pruned)

        # 6. Other terminal paths.
        state = self._view(contract, "get_escrow", {"escrow_id": REFUND_ESCROW}).get("state")
        check(f"buyer refund path {REFUND_ESCROW}", state == "Refunded", f"state={state}")
        state = self._view(contract, "get_escrow", {"escrow_id": REJECT_ESCROW}).get("state")
        check(f"rejection path {REJECT_ESCROW}", state == "Rejected", f"state={state}")
        state = self._view(contract, "get_escrow", {"escrow_id": CANCEL_ESCROW}).get("state")
        check(f"cancellation path {CANCEL_ESCROW}", state == "Cancelled", f"state={state}")

        # 7. Reputation moved for the completed seller.
        seller = escrow.get("seller")
        reputation = self._view(contract, "reputation_of", {"account_id": seller})
        completed = int(reputation.get("completed_as_seller", 0))
        check(
            f"reputation recorded for {seller}",
            completed >= 1,
            f"completed_as_seller={completed} volume={reputation.get('volume')}",
        )

        count = self._view(contract, "escrow_count", {}) or 0
        self.stdout.write(f"  book: {count} escrows created on this deployment\n")

        for label, ok, detail in checks:
            mark = "✓" if ok else "✗"
            line = f"  {mark} {label}"
            if detail and not ok:
                line += f" — {detail}"
            elif detail and options["verbosity"] >= 2:
                line += f" — {detail}"
            self.stdout.write(line)

        failed = [label for label, ok, _ in checks if not ok]
        if failed:
            self.stderr.write(self.style.ERROR(f"\n{len(failed)}/{len(checks)} checks FAILED"))
            raise SystemExit(1)
        self.stdout.write(
            self.style.SUCCESS(f"\n{len(checks)}/{len(checks)} checks passed — the live deployment matches the submitted evidence.")
        )
