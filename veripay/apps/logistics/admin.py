from django.contrib import admin

from .models import LogisticsPartner, PartnerApiCredential, Shipment, ShipmentEvent, VerificationRequest


@admin.register(LogisticsPartner)
class LogisticsPartnerAdmin(admin.ModelAdmin):
    list_display = ("company_name", "email", "status", "settlement_wallet", "owner_wallet", "created_at")
    list_filter = ("status",)
    search_fields = ("company_name", "email", "settlement_wallet", "owner_wallet")
    actions = ["approve_partners", "activate_partners", "suspend_partners"]

    @admin.action(description="Approve selected partners (APPROVED)")
    def approve_partners(self, request, queryset):
        queryset.update(status="APPROVED")

    @admin.action(description="Activate selected partners (ACTIVE — selectable)")
    def activate_partners(self, request, queryset):
        queryset.update(status="ACTIVE")

    @admin.action(description="Suspend selected partners (SUSPENDED)")
    def suspend_partners(self, request, queryset):
        queryset.update(status="SUSPENDED")


@admin.register(PartnerApiCredential)
class PartnerApiCredentialAdmin(admin.ModelAdmin):
    list_display = ("partner", "prefix", "active", "created_at", "rotated_at")
    list_filter = ("active",)


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("tracking_number", "escrow_id", "provider", "status", "simulation_scenario", "simulated", "created_at")
    list_filter = ("status", "simulated", "carrier", "simulation_scenario")
    search_fields = ("tracking_number", "escrow_id")


@admin.register(ShipmentEvent)
class ShipmentEventAdmin(admin.ModelAdmin):
    list_display = ("shipment", "status", "simulated", "created_at")
    list_filter = ("status", "simulated")


@admin.register(VerificationRequest)
class VerificationRequestAdmin(admin.ModelAdmin):
    list_display = ("escrow_id", "shipment", "status", "requested_by", "created_at", "completed_at")
    list_filter = ("status",)
