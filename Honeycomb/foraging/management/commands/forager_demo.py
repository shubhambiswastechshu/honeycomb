"""Prepare a machine for a Forager demo or a first run.

Idempotent: creates a tenant, a login and a worker token if they are missing,
and prints the pairing command. Safe to run twice -- it will not mint a second
worker for the same name, because a stray token is a live credential.
"""
from django.core.management.base import BaseCommand

from accounts.models import Tenant, User
from foraging.models import Worker


class Command(BaseCommand):
    help = 'Create a tenant, a user and a Forager worker token.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', default='Demo')
        parser.add_argument('--email', default='demo@honeycomb.local')
        parser.add_argument('--password', default='honeycomb')
        parser.add_argument('--worker', default='Workstation')
        parser.add_argument('--server', default='http://127.0.0.1:8000')

    def handle(self, *args, **options):
        tenant, made = Tenant.objects.get_or_create(name=options['tenant'])
        self.stdout.write(('created tenant ' if made else 'tenant ') + tenant.name)

        user = User.objects.filter(email=options['email'].lower()).first()
        if user is None:
            user = User.objects.create_user(
                email=options['email'], password=options['password'],
                tenant=tenant, is_staff=True, is_superuser=True,
                role=User.Role.OWNER, is_active=True,
            )
            self.stdout.write('created user  ' + user.email)
        else:
            # A demo command must never silently reset a real password.
            self.stdout.write('user exists   ' + user.email)

        existing = Worker.objects.filter(
            tenant=tenant, name=options['worker'], revoked_at__isnull=True).first()
        if existing:
            self.stdout.write(self.style.WARNING(
                'worker "{0}" already exists ({1}...). Its token was shown once, '
                'when it was minted; revoke it and re-run to get a new one.'
                .format(existing.name, existing.token_prefix)))
            return

        worker, token = Worker.mint(tenant, options['worker'], user)
        self.stdout.write(self.style.SUCCESS('\nworker token (shown once):'))
        self.stdout.write('  ' + token)
        self.stdout.write('\npair this machine with:')
        self.stdout.write('  forager pair {0} {1}'.format(options['server'], token))
        self.stdout.write('\nthen:')
        self.stdout.write('  forager agent          # foreground')
        self.stdout.write('  forager tray           # system tray')
        self.stdout.write('\nconsole: {0}/forager/'.format(options['server']))
