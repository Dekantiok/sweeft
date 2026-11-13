"""Run the verification relayer loop (platform-side automation).

    python3 manage.py run_relayer            # loop every RELAYER_POLL_SECONDS
    python3 manage.py run_relayer --once     # single pass (cron / demos)

The relayer submits the permissionless on-chain calls that complete the happy
path: TEE-verify the carrier's delivery report, pay logistics on verified
pickup, and release escrow funds after the inspection window. Every action is
gated by the contract state machine and re-reads chain state first, so running
it continuously is idempotent. Requires RELAYER_ENABLED=1 plus
RELAYER_ACCOUNT / RELAYER_PRIVATE_KEY (see veripay/.env.example).
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.escrow import relayer


class Command(BaseCommand):
    help = "Run the platform relayer: auto TEE-verify deliveries, settle logistics, release after inspection."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="run a single pass and exit")
        parser.add_argument(
            "--poll", type=float, default=None, help="poll interval in seconds (default RELAYER_POLL_SECONDS)"
        )

    def handle(self, *args, **options):
        if not relayer.relayer_configured():
            self.stderr.write(
                "relayer is not configured — set RELAYER_ENABLED=1, RELAYER_ACCOUNT and RELAYER_PRIVATE_KEY "
                "(see veripay/.env.example)"
            )
            return
        poll = options["poll"] or settings.RELAYER_POLL_SECONDS
        self.stdout.write(
            f"relayer {settings.RELAYER_ACCOUNT} — contract {settings.VERIPAY_CONTRACT}, poll every {poll:g}s"
        )
        while True:
            report = relayer.relayer_tick()
            for line in report.lines():
                self.stdout.write(line)
            if options["once"]:
                return
            time.sleep(poll)
