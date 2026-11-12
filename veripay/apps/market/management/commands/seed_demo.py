"""Seed demo data: the Demo Carrier partner and sample marketplace listings.

Idempotent — safe to run repeatedly. Everything is clearly demo/testnet data.

    python3 manage.py seed_demo
    python3 manage.py seed_demo --partner-owner <wallet> --seller <wallet>
"""
from django.core.management.base import BaseCommand

from apps.escrow.near import parse_token
from apps.logistics.models import LogisticsPartner
from apps.market.models import Listing


class Command(BaseCommand):
    help = "Create/update the Demo Carrier logistics partner and sample listings."

    def add_arguments(self, parser):
        parser.add_argument("--partner-owner", default="steepdata3968.testnet",
                            help="Wallet that manages the demo partner in the portal")
        parser.add_argument("--partner-settlement", default="logistics.steepdata3968.testnet",
                            help="On-chain wallet that receives logistics payouts")
        parser.add_argument("--seller", default="seller.steepdata3968.testnet",
                            help="Wallet that owns the sample listings")

    def handle(self, *args, **options):
        owner = options["partner_owner"]
        settlement = options["partner_settlement"]
        seller = options["seller"]

        partner, created = LogisticsPartner.objects.get_or_create(
            company_name="Demo Carrier Logistics",
            defaults={
                "email": "ops@demo-carrier.test",
                "operating_locations": "Lagos, Abuja, Port Harcourt",
                "services": "Same-day delivery — within Lagos — 5 tUSD\nInterstate — 48h — 10 tUSD",
                "settlement_wallet": settlement,
                "owner_wallet": owner,
                "pricing_note": "Flat demo pricing in tUSD",
                "status": "ACTIVE",
                "review_notes": "Seeded demo partner (Demo Carrier, simulated tracking).",
            },
        )
        if not created:
            partner.owner_wallet = owner
            partner.settlement_wallet = settlement
            partner.save(update_fields=["owner_wallet", "settlement_wallet"])
        self.stdout.write(f"partner: {partner.company_name} [{partner.status}] owner={owner}")

        items = [
            dict(title="200 units of cotton fabric", kind="goods", category="textiles",
                 description="Premium cotton fabric, 200 units, Grade A. Shipped from Lagos with verified logistics; payment releases only after you confirm delivery.",
                 price_amount=parse_token("850.00"), location="Lagos, Nigeria",
                 dispatch_days=7, inspection_days=2, logistics_note="Approved provider arranges pickup and tracking"),
            dict(title="Solar inverter batch — 10 units", kind="goods", category="energy",
                 description="10x 5kVA hybrid inverters, new in box with warranty paperwork. Interstate delivery via verified provider.",
                 price_amount=parse_token("1200.00"), location="Abuja, Nigeria",
                 dispatch_days=10, inspection_days=3, logistics_note="Insured freight, signature on delivery"),
            dict(title="Interstate delivery service — Lagos ↔ Abuja", kind="service", category="logistics",
                 description="Door-to-door interstate delivery, 48h standard / 24h express. Tracking registered on-chain with TEE-verified delivery.",
                 price_amount=parse_token("10.00"), location="Lagos ↔ Abuja",
                 dispatch_days=2, inspection_days=1, logistics_note="Service includes pickup scan and delivery verification"),
            dict(title="Logo & brand identity package", kind="service", category="design",
                 description="Logo, colour system and brand guide delivered as digital files. Milestone delivery via escrow: files on delivery, payment on your confirmation.",
                 price_amount=parse_token("250.00"), location="Remote",
                 dispatch_days=5, inspection_days=2, logistics_note="Digital delivery — verification is buyer confirmation"),
        ]
        for item in items:
            item = dict(item)
            item["logistics_provider"] = settlement
            listing, was_created = Listing.objects.get_or_create(
                seller_account=seller, title=item["title"], defaults=item
            )
            if not was_created:
                listing.logistics_provider = settlement
                listing.save(update_fields=["logistics_provider", "updated_at"])
            state = "created" if was_created else "exists"
            self.stdout.write(f"listing {state}: /market/{listing.slug}/ (provider {settlement})")
        self.stdout.write(self.style.SUCCESS("demo data ready"))
