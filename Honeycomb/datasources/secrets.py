"""
Turning a DataSource's `secret_name` into an actual password.

`DataSource` stores a *handle*, never a credential -- see the model docstring
for why. Something still has to do the lookup, and this is the one place that
does it. Keeping it here means there is exactly one function to audit, one
place a real secret manager gets wired in, and no temptation to sprinkle
`os.environ[...]` through the connector.

Rules this module holds to:

* A secret is returned to the caller that is about to open a socket with it,
  and to nothing else. It is never serialized, logged, or put in an exception
  message. `SecretNotFound` names the *handle*, which is not sensitive.
* A missing secret is an error, not an empty password. Falling through to ''
  would turn "the operator forgot to set this" into "try to connect as this
  user with no password", which against a misconfigured server can succeed.

The default backend reads environment variables, which is what a development
machine and a twelve-factor deployment both already have. Point
``HONEYCOMB_SECRET_BACKEND`` at a dotted path to swap in AWS Secrets Manager,
Vault, or GCP Secret Manager; the callable takes the handle and returns the
secret as a string.

Environment backend naming::

    secret_name "warehouse_ro"  ->  HONEYCOMB_SECRET_WAREHOUSE_RO

Anything that is not a letter, digit or underscore becomes an underscore, so a
handle like "prod/warehouse-ro" resolves to HONEYCOMB_SECRET_PROD_WAREHOUSE_RO.
"""

import os
import re

from django.conf import settings
from django.utils.module_loading import import_string


class SecretError(Exception):
    """Base for every failure to produce a credential."""


class SecretNotFound(SecretError):
    """The handle resolved to nothing.

    The message quotes the handle and the environment variable it looked for,
    because an operator staring at a failed connection needs to know exactly
    what to set. Neither is a secret.
    """


ENV_PREFIX = 'HONEYCOMB_SECRET_'


def env_var_for(secret_name):
    """The environment variable a handle maps to, e.g. 'warehouse_ro'."""
    cleaned = re.sub(r'[^A-Za-z0-9]+', '_', secret_name or '').strip('_')
    return ENV_PREFIX + cleaned.upper()


def environment_backend(secret_name):
    """Read the secret from the process environment."""
    variable = env_var_for(secret_name)
    value = os.environ.get(variable)
    if value is None or value == '':
        raise SecretNotFound(
            'No secret is configured for "{0}". Set {1} in the environment '
            '(or point HONEYCOMB_SECRET_BACKEND at your secret manager).'.format(
                secret_name, variable
            )
        )
    return value


def get_backend():
    """The configured lookup callable.

    Resolved per call rather than cached at import: a test that overrides the
    setting, or a deployment that reloads configuration, should not have to
    restart the process to be believed.
    """
    dotted = getattr(settings, 'HONEYCOMB_SECRET_BACKEND', None)
    if not dotted:
        return environment_backend
    return import_string(dotted)


def resolve(secret_name):
    """Return the secret behind `secret_name`.

    Raises SecretNotFound when the handle is blank or unset. Callers should let
    that propagate to a message the operator sees -- it tells them what to
    configure, which no generic "connection failed" does.
    """
    if not (secret_name or '').strip():
        raise SecretNotFound(
            'This source has no secret name, so there is no password to look '
            'up. Set one on the source and store the password under it.'
        )
    return get_backend()(secret_name.strip())


def has_secret(secret_name):
    """True when `resolve` would succeed. Used to show status without connecting.

    Deliberately discards the value it fetched: this answers "is it
    configured", and returning the secret from a predicate is how secrets end
    up somewhere they were never meant to go.
    """
    try:
        resolve(secret_name)
    except SecretError:
        return False
    return True
