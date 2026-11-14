"""Create the shared database cache table.

Rate limiters must be shared across gunicorn workers; the default settings use
`django.core.cache.backends.db.DatabaseCache`. The table is created here (a
migration) instead of relying on `manage.py createcachetable`, so test
databases and fresh deployments always have it.
"""
from django.core.management import call_command
from django.db import migrations

TABLE = "django_cache"


def create_cache_table(apps, schema_editor):
    # Idempotent: the command inspects existing tables and mirrors Django's
    # own DDL, so this stays correct across database backends.
    call_command("createcachetable", verbosity=0)


def drop_cache_table(apps, schema_editor):
    connection = schema_editor.connection
    if TABLE in connection.introspection.table_names():
        connection.cursor().execute(f"DROP TABLE {connection.ops.quote_name(TABLE)}")


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
