"""Backfill account_key for connections made before the field existed.

The value is the remote account already sitting in the encrypted credential
blob, so this decrypts each OAuth connection, reads its ``email`` and writes it
to the new column. Nothing new is learned and nothing secret is moved into the
clear -- the address was always there, just not queryable.

Where two connections in one tenant point at the SAME account -- which is the
bug this field exists to prevent, and which did happen before it -- only the
oldest is stamped. The rest are left with an empty account_key so the unique
constraint holds and NOTHING IS DELETED: a duplicate connection may already
have MCP keys minted against it, and dropping rows during a migration is not
this file's decision to make. The extras stay visible in the dashboard, where a
person can remove the one they do not want.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Connection = apps.get_model('connections', 'Connection')
    # The historical model has no methods, so decryption is done directly.
    from connections import crypto
    import json

    seen = set()
    for row in Connection.objects.order_by('created_at', 'pk').iterator():
        if not row.creds_enc:
            continue
        try:
            creds = json.loads(crypto.decrypt(row.creds_enc))
        except Exception:
            # A row we cannot decrypt is a row whose key is gone. It is already
            # broken; leaving account_key empty keeps it exactly as broken as
            # it was rather than failing the whole migration.
            continue
        email = str(creds.get('email') or '').strip()
        if not email:
            continue
        fingerprint = (row.tenant_id, row.connector, email)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        row.account_key = email[:190]
        row.save(update_fields=['account_key'])


def unstamp(apps, schema_editor):
    Connection = apps.get_model('connections', 'Connection')
    Connection.objects.update(account_key='')


class Migration(migrations.Migration):

    dependencies = [
        ('connections', '0003_connection_account_key_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unstamp),
    ]
