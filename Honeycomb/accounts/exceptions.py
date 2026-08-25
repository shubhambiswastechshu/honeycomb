#: Returned when a platform-level (tenant-less) account reaches a tenant
#: endpoint. The contract types "tenant" as an object, never null.
TENANTLESS_DETAIL = 'This account is not attached to an organization.'

AMBIGUOUS_ORGANIZATION_DETAIL = (
    'This email address belongs to several organizations. '
    'Retry with organization_slug.'
)


class AmbiguousOrganizationError(Exception):
    """
    The submitted credentials are valid in more than one organization.

    Additive extension to the auth contract: the 200 response shape is
    untouched, but sign-in may now answer 409 with

        {"detail": "...", "organizations": [{"id", "name", "slug"}, ...]}

    and the client re-POSTs with the chosen ``organization_slug``.

    Deliberately a plain exception rather than a DRF ``APIException``:
    ``APIException`` coerces every leaf of its ``detail`` to ``ErrorDetail``
    (a ``str`` subclass), which would ship ``"id": "3"`` instead of the
    integer the contract's TENANT object declares. ``SignInView`` catches this
    and renders the 409 body itself.
    """

    def __init__(self, tenants):
        self.tenants = list(tenants)
        super(AmbiguousOrganizationError, self).__init__(
            AMBIGUOUS_ORGANIZATION_DETAIL
        )
