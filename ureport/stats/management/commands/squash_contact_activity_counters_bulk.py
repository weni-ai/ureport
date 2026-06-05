from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

TABLE = "stats_contactactivitycounter"

# Atomic, non-destructive collapse of a slice of delta rows:
# the DELETE ... RETURNING and the SUM see exactly the same set, so rows inserted
# concurrently by the activity triggers are never lost. The summed counts are
# identical to what the regular squash produces (GREATEST(0, SUM(count))).
COLLAPSE_SQL = f"""
WITH deleted AS (
    DELETE FROM {TABLE}
    WHERE id >= %s AND id < %s
    RETURNING org_id, date, type, value, count
)
INSERT INTO {TABLE} (org_id, date, type, value, count, is_squashed)
SELECT org_id, date, type, value, GREATEST(0, SUM(count)), TRUE
FROM deleted
GROUP BY org_id, date, type, value
"""


class Command(BaseCommand):
    help = (
        "Collapse (squash) stats_contactactivitycounter delta rows in bulk and non-destructively.\n"
        "Sweeps the table by primary-key id ranges, summing counts per (org_id, date, type, value).\n"
        "Counts are preserved exactly; this only reduces the number of rows.\n"
        "NOTE: removed rows become dead tuples. The heap file is only physically shrunk by a later "
        "VACUUM FULL / pg_repack (ask the cloud team). Build the new index AFTER that reclaim for it to be fast.\n"
        "Usage: python manage.py squash_contact_activity_counters_bulk [--batch-size N] [--max-passes N] [--dry-run]"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1_000_000,
            help="Width of the id range processed per transaction (default: 1,000,000).",
        )
        parser.add_argument(
            "--max-passes",
            type=int,
            default=6,
            help="Maximum number of full sweeps (each pass further collapses cross-batch duplicates).",
        )
        parser.add_argument(
            "--min-reduction",
            type=float,
            default=0.05,
            help="Stop when a pass reduces the row count by less than this fraction (default: 0.05).",
        )
        parser.add_argument(
            "--statement-timeout",
            default="30min",
            help="Per-statement timeout safeguard for each batch (default: 30min).",
        )
        parser.add_argument(
            "--work-mem",
            default="512MB",
            help="work_mem used per batch for the aggregation (default: 512MB).",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Seconds to sleep between batches to pace WAL/IO (default: 0).",
        )
        parser.add_argument(
            "--start-id",
            type=int,
            default=None,
            help="Resume the first pass from this id (lower ids assumed already collapsed).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only print id bounds and the approximate row count, then exit.",
        )

    def _bounds(self):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT MIN(id), MAX(id) FROM {TABLE}")
            return cursor.fetchone()

    def _approx_rows(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s", [TABLE])
            row = cursor.fetchone()
            return row[0] if row else None

    def handle(self, *args, **options):
        batch = options["batch_size"]
        max_passes = options["max_passes"]
        min_reduction = options["min_reduction"]
        statement_timeout = options["statement_timeout"]
        work_mem = options["work_mem"]
        sleep_s = options["sleep"]
        start_id = options["start_id"]
        dry_run = options["dry_run"]

        lo_all, hi_all = self._bounds()
        if lo_all is None:
            self.stdout.write("Table is empty, nothing to do.")
            return

        approx = self._approx_rows()
        approx_str = f"{approx:,}" if approx is not None else "?"
        self.stdout.write(f"id range [{lo_all:,}, {hi_all:,}], approx rows ~{approx_str}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run, exiting without changes."))
            return

        overall_start = time.time()
        prev_inserted = None

        for current_pass in range(1, max_passes + 1):
            lo, hi_limit = self._bounds()
            if lo is None:
                break
            if current_pass == 1 and start_id is not None:
                lo = max(lo, start_id)

            pass_start = time.time()
            inserted_total = 0
            cur_lo = lo

            while cur_lo <= hi_limit:
                cur_hi = min(cur_lo + batch, hi_limit + 1)
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = %s", [statement_timeout])
                        try:
                            cursor.execute("SET LOCAL work_mem = %s", [work_mem])
                        except Exception:
                            # If the DB disallows changing work_mem, proceed without failing
                            pass
                        cursor.execute(COLLAPSE_SQL, [cur_lo, cur_hi])
                        if cursor.rowcount and cursor.rowcount > 0:
                            inserted_total += cursor.rowcount

                cur_lo = cur_hi
                span = (hi_limit + 1) - lo
                pct = 100.0 * (cur_hi - lo) / span if span > 0 else 100.0
                self.stdout.write(
                    f"  pass {current_pass}: {pct:5.1f}% (id {cur_hi:,}/{hi_limit:,}), rows kept {inserted_total:,}",
                    ending="\r",
                )
                if sleep_s:
                    time.sleep(sleep_s)

            took = time.time() - pass_start
            self.stdout.write(
                f"\npass {current_pass} done in {took:.1f}s, rows after pass ~{inserted_total:,}"
            )

            if prev_inserted is not None and prev_inserted > 0:
                reduction = 1 - (inserted_total / prev_inserted)
                if reduction < min_reduction:
                    self.stdout.write(f"reduction {reduction:.1%} < {min_reduction:.0%}; converged.")
                    break
            prev_inserted = inserted_total

        self.stdout.write(
            self.style.SUCCESS(
                "Done in %.1fs. Final approx rows ~%s.\n"
                "Next: ANALYZE %s; then ask cloud for VACUUM FULL / pg_repack to reclaim disk; "
                "then build the partial index (migration 0031)."
                % (
                    time.time() - overall_start,
                    f"{self._approx_rows():,}" if self._approx_rows() is not None else "?",
                    TABLE,
                )
            )
        )
