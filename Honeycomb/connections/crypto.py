"""
Symmetric encryption for the credential blob on a Connection.

**This reverses the rule the datasources app held to, and the reversal is
deliberate.** `datasources.DataSource` refused to have a password column: it
stored a *handle* and `datasources/secrets.py` resolved it against a secret
manager, so a breach of that table was worth nothing. That is still the better
design, and it is still the right one for an operator-configured warehouse.

It does not work for MCP connectors. The credential here is not something an
operator provisions ahead of time and drops into Vault -- it is an OAuth
refresh token or a personal API key that *the end user* mints at runtime, from
a browser, for their own account, seconds before it has to be usable. There is
no out-of-band step in which anyone could write it into a secret manager, and a
handle to a secret nobody has stored resolves to nothing.

So the blob lives in the row, encrypted, and the honest statement of the
tradeoff is: a database dump alone is not enough to use these credentials, but
a database dump *plus* the key material is. Keep HONEYCOMB_CRED_KEYS out of the
database and out of the repository -- that separation is the whole of the
protection.

The seam is kept narrow on purpose. Every read and write of a credential goes
through `encrypt`/`decrypt` in this module and nowhere else, so swapping in a
real secret manager (or a KMS-wrapped data key) is a change to two functions,
not a change to every caller.

Key rotation. ``settings.HONEYCOMB_CRED_KEYS`` is a *list* of Fernet keys
ordered **newest first**. MultiFernet encrypts with the first key and tries
every key on decrypt, so rotating means prepending a new key and leaving the
old one in place until the rows have been re-saved. Dropping a key before then
makes the rows it wrote permanently unreadable.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class CredentialDecryptionError(Exception):
    """The stored blob could not be read with any configured key.

    Raised instead of letting cryptography's InvalidToken escape, because the
    two causes an operator has to tell apart -- a rotated-out key and a
    corrupted row -- both surface as InvalidToken and neither is obvious from
    the library's own message.
    """


def _multi_fernet() -> MultiFernet:
    """Build the MultiFernet for the currently configured key list.

    Resolved per call rather than cached at import, for the same reason
    datasources/secrets.py resolves its backend per call: a test using
    override_settings, or a deployment that reloads configuration, must not be
    stuck with the key list that happened to be present when this module was
    first imported.
    """
    keys = getattr(settings, 'HONEYCOMB_CRED_KEYS', None) or []
    if not keys:
        raise ImproperlyConfigured(
            'HONEYCOMB_CRED_KEYS is empty. Connector credentials cannot be '
            'stored or read without at least one Fernet key. Generate one with '
            "python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())" and set it in the '
            'environment. The list is ordered newest first.'
        )
    try:
        fernets = [Fernet(key) for key in keys]
    except (TypeError, ValueError) as exc:
        # A malformed key is a deployment mistake, not a runtime condition, and
        # it must fail loudly at the first write rather than silently skip to
        # the next key in the list.
        raise ImproperlyConfigured(
            'HONEYCOMB_CRED_KEYS contains a value that is not a valid Fernet '
            'key (32 url-safe base64-encoded bytes).'
        ) from exc
    return MultiFernet(fernets)


def encrypt(value: str) -> str:
    """Encrypt a string with the newest key. Returns url-safe ciphertext."""
    return _multi_fernet().encrypt(value.encode('utf-8')).decode('ascii')


def decrypt(token: str) -> str:
    """Decrypt ciphertext written by any key still in the list."""
    try:
        return _multi_fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        # The token itself is never quoted back: it is the credential.
        raise CredentialDecryptionError(
            'Stored credentials could not be decrypted with any key in '
            'HONEYCOMB_CRED_KEYS. Either the key that wrote them has been '
            'rotated out, or the row is corrupt. Re-enter the credentials to '
            'recover the connection.'
        ) from exc


def rotate(token: str) -> str:
    """Re-encrypt an existing blob under the newest key.

    Exists so a rotation can be finished without ever handing the plaintext to
    the caller: MultiFernet.rotate decrypts with whichever key still works and
    re-encrypts with the first one, in a single step.
    """
    try:
        return _multi_fernet().rotate(token.encode('ascii')).decode('ascii')
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            'Stored credentials could not be decrypted with any key in '
            'HONEYCOMB_CRED_KEYS, so they cannot be rotated.'
        ) from exc
