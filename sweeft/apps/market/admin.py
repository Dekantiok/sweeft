from django.contrib import admin

from .models import Listing, PurchaseRequest


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ("title", "seller_account", "kind", "price_amount", "status", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("title", "seller_account", "category")


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ("listing", "buyer_account", "status", "escrow_id", "created_at")
    list_filter = ("status",)
