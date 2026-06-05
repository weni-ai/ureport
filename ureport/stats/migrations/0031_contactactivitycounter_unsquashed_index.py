from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    atomic = False

    dependencies = [
        ("stats", "0030_remove_contactactivity_stats_contactactivity_org_id_contact_dec2fa2d_idx_and_more"),
    ]

    operations = [
        AddIndexConcurrently(
            "contactactivitycounter",
            models.Index(
                name="contact_activitycntr_unsquash",
                fields=["org", "date", "type", "value"],
                condition=Q(is_squashed=False),
            ),
        ),
    ]
