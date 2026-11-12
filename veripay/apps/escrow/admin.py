from django.contrib import admin

from .models import ContractEvent, EscrowAgreement, EscrowInvitation, SettlementRecord


@admin.register(EscrowAgreement)
class EscrowAgreementAdmin(admin.ModelAdmin):
    list_display = ("escrow_id", "product_name", "seller_account", "buyer_account", "state", "total_amount", "created_at")
    list_filter = ("state",)
    search_fields = ("escrow_id", "seller_account", "buyer_account", "product_name")


@admin.register(EscrowInvitation)
class EscrowInvitationAdmin(admin.ModelAdmin):
    list_display = ("agreement", "status", "created_at", "accepted_by")
    list_filter = ("status",)


@admin.register(ContractEvent)
class ContractEventAdmin(admin.ModelAdmin):
    list_display = ("escrow_id", "event_type", "tx_hash", "created_at")
    list_filter = ("event_type",)


@admin.register(SettlementRecord)
class SettlementRecordAdmin(admin.ModelAdmin):
    list_display = ("escrow_id", "party", "account_id", "amount", "status", "created_at")
    list_filter = ("party", "status")
