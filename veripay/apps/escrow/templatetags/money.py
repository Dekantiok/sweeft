"""Currency display filters (native NEAR by default, legacy FT supported)."""
from django import template

from apps.escrow import near

register = template.Library()


@register.filter(name="money")
def money(value, token="near"):
    currency = token or "near"
    suffix = "NEAR" if currency == near.NATIVE_NEAR_TOKEN else "tUSD"
    return f"{near.format_currency(value, currency)} {suffix}"
