from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

#: Attribute stamped on ``request`` when an email matches usable accounts in
#: more than one organization. Callers (see ``accounts.serializers``) read it
#: after ``authenticate()`` returns ``None`` and turn it into an HTTP 409 that
#: asks the client to re-POST with ``organization_slug``.
AMBIGUOUS_TENANTS_ATTR = 'honeycomb_ambiguous_tenants'


class TenantAwareModelBackend(ModelBackend):
    """
    Email/password backend that tolerates per-tenant email uniqueness.

    Django's ModelBackend assumes USERNAME_FIELD is globally unique and does a
    single get(); here the same address may legitimately exist in several
    tenants. Two properties matter and are both enforced below:

    * **No silent tenant pick.** Returning the lowest-pk row whenever several
      accounts share an address makes every other organization's account
      permanently unreachable. Instead the ambiguity is reported: pass
      ``organization_slug=<Tenant.slug>`` (or ``tenant=<Tenant>``) to resolve
      it up front, otherwise the matching tenants are stamped on ``request``
      under :data:`AMBIGUOUS_TENANTS_ATTR` and ``None`` is returned.

    * **Constant hashing cost.** Exactly one password hash runs per call --
      against the real hash when a candidate row exists, against a throwaway
      instance when it does not. An attacker therefore cannot tell a
      registered address from an unregistered one, nor count the
      organizations an address belongs to, by timing the response.

    Because only one hash may run, ambiguity is decided against a single
    deterministically chosen candidate (lowest pk). A caller whose password
    differs from that candidate's must supply ``organization_slug``; the
    signup endpoint keeps addresses globally unique, so this only arises for
    accounts seeded outside the API.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is not None:
            try:
                setattr(request, AMBIGUOUS_TENANTS_ATTR, None)
            except AttributeError:
                # Some request-like objects are read-only; ambiguity simply
                # cannot be reported in that case.
                pass

        user_model = get_user_model()
        email = kwargs.get(user_model.USERNAME_FIELD, username)
        if not email or password is None:
            return None

        queryset = user_model._default_manager.filter(
            email=user_model.objects.normalize_email(email)
        )
        tenant = kwargs.get('tenant')
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)
        organization_slug = kwargs.get('organization_slug')
        if organization_slug:
            queryset = queryset.filter(tenant__slug=organization_slug)

        candidates = [
            candidate
            for candidate in queryset.select_related('tenant').order_by('pk')
            if self.user_can_authenticate(candidate)
        ]
        if not candidates:
            # Same mitigation as ModelBackend: run the hasher once so an unknown
            # address costs the same wall-clock time as a wrong password.
            user_model().set_password(password)
            return None

        # The one and only hash of this call, regardless of how many rows the
        # address matched.
        primary = candidates[0]
        matched = primary.check_password(password)

        if len(candidates) > 1:
            tenants = [c.tenant for c in candidates if c.tenant_id is not None]
            if matched and request is not None and len(tenants) > 1:
                # Only disclose the organization list to a caller who has
                # already proven knowledge of a valid password for this address.
                try:
                    setattr(request, AMBIGUOUS_TENANTS_ATTR, tenants)
                except AttributeError:
                    pass
            return None

        return primary if matched else None
