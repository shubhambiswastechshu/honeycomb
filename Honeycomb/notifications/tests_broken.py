"""A mail backend that always fails, so the failure path can be exercised.

Lives in the app rather than in a test file because override_settings takes an
import path, and a path into a scratch script is not one the app can resolve.
"""
from django.core.mail.backends.base import BaseEmailBackend


class BrokenBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise OSError('connection refused')
