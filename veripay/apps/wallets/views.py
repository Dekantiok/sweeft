import base64
import secrets
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import ratelimit
from .models import WalletChallenge, WalletIdentity
from .nep413 import verify_nep413

CHALLENGE_TTL = timedelta(minutes=10)


def current_wallet(request) -> str | None:
    return request.session.get("wallet")


def account_has_public_key(account_id: str, public_key: str) -> bool:
    """Bind a login signature to the claimed NEAR account, not just a keypair.

    NEP-413 proves possession of *a* private key. Without this on-chain check,
    anyone could sign with their own key while claiming another account. We
    fail closed on RPC errors, and require the key to be a FULL-ACCESS key:
    function-call keys are scoped to a single contract and must not be able
    to mint a platform session.
    """
    from apps.escrow.near import _rpc

    try:
        result = _rpc(
            "query",
            {
                "request_type": "view_access_key_list",
                "finality": "final",
                "account_id": account_id,
            },
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return False
    if "error" in result:
        return False
    keys = (result.get("result") or {}).get("keys") or []
    return any(
        k.get("public_key") == public_key
        and (k.get("access_key") or {}).get("permission") == "FullAccess"
        for k in keys
    )


@ensure_csrf_cookie
@require_GET
def challenge(request):
    """Create a single-use challenge bound to an optional account and domain."""
    if not ratelimit.allow(
        f"challenge:{ratelimit.client_ip(request)}", settings.RATE_LIMIT_CHALLENGE_PER_MIN
    ):
        return JsonResponse({"ok": False, "error": "too many challenges — slow down"}, status=429)
    account_id = (request.GET.get("account_id") or "").strip()
    nonce = base64.b64encode(secrets.token_bytes(32)).decode()
    recipient = request.get_host()
    issued = timezone.now().isoformat()
    message = (
        f"Sign in to VeriPay\n"
        f"Domain: {recipient}\n"
        f"Account: {account_id or 'any'}\n"
        f"Issued: {issued}"
    )
    WalletChallenge.objects.create(
        nonce=nonce,
        account_id=account_id,
        message=message,
        recipient=recipient,
        expires_at=timezone.now() + CHALLENGE_TTL,
    )
    return JsonResponse({"nonce": nonce, "message": message, "recipient": recipient})


@require_POST
def verify(request):
    import json

    if not ratelimit.allow(f"verify:{ratelimit.client_ip(request)}", settings.RATE_LIMIT_LOGIN_PER_MIN):
        return JsonResponse({"ok": False, "error": "too many login attempts — slow down"}, status=429)

    body = json.loads(request.body or "{}")
    account_id = (body.get("account_id") or "").strip()
    public_key = body.get("public_key") or ""
    signature = body.get("signature") or ""

    try:
        challenge_row = WalletChallenge.objects.get(nonce=body.get("nonce", ""))
    except WalletChallenge.DoesNotExist:
        return JsonResponse({"ok": False, "error": "unknown challenge"}, status=400)

    if challenge_row.consumed:
        return JsonResponse({"ok": False, "error": "challenge already used"}, status=400)
    if challenge_row.expires_at < timezone.now():
        return JsonResponse({"ok": False, "error": "challenge expired"}, status=400)
    if challenge_row.account_id and challenge_row.account_id != account_id:
        return JsonResponse({"ok": False, "error": "challenge account mismatch"}, status=400)
    if challenge_row.recipient != request.get_host():
        return JsonResponse({"ok": False, "error": "wrong domain"}, status=400)
    if not account_has_public_key(account_id, public_key):
        return JsonResponse(
            {"ok": False, "error": "public key is not a live access key on this account"},
            status=400,
        )

    ok = verify_nep413(
        public_key=public_key,
        signature_b64=signature,
        message=challenge_row.message,
        nonce_b64=challenge_row.nonce,
        recipient=challenge_row.recipient,
        callback_url=body.get("callback_url"),
    )
    if not ok:
        return JsonResponse({"ok": False, "error": "signature verification failed"}, status=400)

    challenge_row.consumed = True
    challenge_row.used_by = account_id
    challenge_row.save(update_fields=["consumed", "used_by"])

    identity, _ = WalletIdentity.objects.get_or_create(account_id=account_id)
    identity.last_login = timezone.now()
    identity.save(update_fields=["last_login"])

    # Rotate the session identifier on privilege change (fixation defence).
    if hasattr(request.session, "cycle_key"):
        request.session.cycle_key()
    request.session["wallet"] = account_id
    return JsonResponse({"ok": True, "account_id": account_id})


@require_GET
def me(request):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"authenticated": False})
    identity = WalletIdentity.objects.filter(account_id=wallet).first()
    return JsonResponse(
        {
            "authenticated": True,
            "account_id": wallet,
            "first_seen": identity.first_seen.isoformat() if identity else None,
        }
    )


@require_POST
def logout(request):
    request.session.pop("wallet", None)
    return JsonResponse({"ok": True})
