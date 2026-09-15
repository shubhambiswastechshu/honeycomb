"""Switch the new Meta Business Suite ad-management tools off on existing connections.

disabled_tools is a deny-list, so a tool added to a connector is on for every
connection that already exists. That is the right default for a read tool and the
wrong one for a tool that pauses campaigns and changes budgets: a connection made
when the connector was read-only must not quietly gain the power to spend.
New connections get the same treatment from ConnectionSerializer.create.
"""
from django.db import migrations

NEW_WRITE_TOOLS = ('update_campaign', 'update_ad_set', 'update_ad', 'create_campaign')


def switch_off(apps, schema_editor):
    Connection = apps.get_model('connections', 'Connection')
    for row in Connection.objects.filter(connector='meta_business_suite'):
        disabled = list(row.disabled_tools or [])
        missing = [name for name in NEW_WRITE_TOOLS if name not in disabled]
        if missing:
            row.disabled_tools = disabled + missing
            row.save(update_fields=['disabled_tools'])


class Migration(migrations.Migration):

    dependencies = [
        ('connections', '0005_alter_connectoroauthstate_options'),
    ]

    operations = [
        migrations.RunPython(switch_off, migrations.RunPython.noop),
    ]
