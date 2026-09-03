"""Wipe every account and leave one platform admin.

Destructive on purpose, and gated on purpose. It runs at container start only
when HONEYCOMB_RESET_ADMIN is set, so an ordinary deploy never touches user
data; the flag is meant to be turned on for one deploy and turned off again.

The identifier is written straight to the column rather than going through
createsuperuser. USERNAME_FIELD is an EmailField, so `createsuperuser
--noinput` refuses anything that is not an address -- but authentication only
ever does an exact lookup on that column, so a non-address value works fine as
a login. Model.save() does not run field validators, which is what lets this
store one.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = 'Delete all users and create a single superuser from the environment.'

    def add_arguments(self, parser):
        parser.add_argument('--identifier', default=os.environ.get('HONEYCOMB_ADMIN_ID', ''))
        parser.add_argument('--password', default=os.environ.get('HONEYCOMB_ADMIN_PASSWORD', ''))

    def handle(self, *args, **options):
        identifier = (options['identifier'] or '').strip()
        password = options['password'] or ''
        if not identifier or not password:
            self.stderr.write('reset_admin: identifier and password are both required; nothing done.')
            return

        User = get_user_model()
        with transaction.atomic():
            removed = User.objects.all().count()
            User.objects.all().delete()

            admin = User(
                email=identifier,
                is_staff=True,
                is_superuser=True,
                is_active=True,
                # Platform-level, so it belongs to no organization. The custom
                # manager makes the same choice for a normal superuser.
                tenant=None,
            )
            admin.set_password(password)
            # No full_clean(): the identifier is deliberately allowed not to be
            # an email address.
            admin.save()

        self.stdout.write(
            'reset_admin: removed %d account(s), created %r as superuser.' % (removed, identifier)
        )
