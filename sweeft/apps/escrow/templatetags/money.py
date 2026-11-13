"""Native-NEAR display filter."""
from django import template

from apps.escrow import near

register = template.Library()


@register.filter(name="money")
def money(value, token=None):
    """Format a native-NEAR amount with its currency suffix."""
    return f"{near.format_near_amount(value)} NEAR"
