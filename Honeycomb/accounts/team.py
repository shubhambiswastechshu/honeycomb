"""The team: who is in an organization, and inviting the people who are not.

Kept apart from views.py because these are the only endpoints about *other*
people's accounts, and the rules that follow from that are worth reading in one
place:

* Anyone in the organization can see who else is in it. A workspace where you
  cannot tell who has access is worse than one where you can.
* Only an owner or an admin can invite, revoke or change a role, because those
  three are what actually grant access.
* Accepting an invitation needs no session -- the invitee has no account yet.
  The emailed token is the whole credential, so it is single-use, expiring, and
  stored only as a hash.
"""
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications import mail

from .authentication import set_auth_cookies
from .models import Invitation, User
from .serializers import (
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    UserSerializer,
)
from .views import PublicAPIView, identity_payload, tenant_guard

#: Roles that may change who has access.
MANAGERS = (User.Role.OWNER, User.Role.ADMIN)


def can_manage(user):
    return user.role in MANAGERS


def invitation_payload(invitation):
    return {
        'id': invitation.id,
        'email': invitation.email,
        'role': invitation.role,
        'state': invitation.state,
        'invited_by': invitation.invited_by.email if invitation.invited_by_id else '',
        'created_at': invitation.created_at,
        'expires_at': invitation.expires_at,
    }


class TeamView(APIView):
    """GET: everyone in the caller's organization, and the invitations out."""

    permission_classes = [IsAuthenticated]
    throttle_scope = 'profile'

    def get(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        tenant = request.user.tenant
        members = User.objects.filter(tenant=tenant, is_active=True).order_by('email')
        invitations = (
            Invitation.objects
            .filter(tenant=tenant, accepted_at__isnull=True, revoked_at__isnull=True,
                    expires_at__gt=timezone.now())
            .select_related('invited_by')
        )
        return Response({
            'members': UserSerializer(members, many=True).data,
            'invitations': [invitation_payload(row) for row in invitations],
            # The dashboard hides the invite form rather than letting someone
            # fill it in and be refused.
            'can_manage': can_manage(request.user),
        }, status=status.HTTP_200_OK)


class InvitationListView(APIView):
    """POST: invite one person. Sends the email that carries the only token."""

    permission_classes = [IsAuthenticated]
    throttle_scope = 'invite'

    def post(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        if not can_manage(request.user):
            return Response({'detail': 'Only an owner or an admin can invite people.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = InvitationCreateSerializer(
            data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        tenant = request.user.tenant
        email = serializer.validated_data['email']

        # Re-inviting the same address replaces the outstanding invitation
        # rather than stacking a second one: the newest link is the one the
        # person was told to use, and the old one stops working.
        Invitation.objects.filter(
            tenant=tenant, email=email, accepted_at__isnull=True, revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())

        invitation, token = Invitation.mint(
            tenant=tenant, email=email, role=serializer.validated_data['role'],
            invited_by=request.user)
        mail.send(
            'invitation',
            invitation.email,
            '{0} invited you to {1} on Honeycomb'.format(
                request.user.full_name or request.user.email, tenant.name),
            context={
                'organization': tenant.name,
                'inviter': request.user.full_name or request.user.email,
                'role': invitation.get_role_display(),
                'accept_url': mail.app_url('/accept-invite?token={0}'.format(token)),
                'expires_days': Invitation.LIFETIME.days,
            },
            tenant=tenant,
            user=request.user,
        )
        return Response(invitation_payload(invitation), status=status.HTTP_201_CREATED)


class InvitationRevokeView(APIView):
    """POST: withdraw an invitation that has not been accepted."""

    permission_classes = [IsAuthenticated]
    throttle_scope = 'invite'

    def post(self, request, pk):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        if not can_manage(request.user):
            return Response({'detail': 'Only an owner or an admin can withdraw an invitation.'},
                            status=status.HTTP_403_FORBIDDEN)
        # Scoped to the caller's own organization: a guessed id from another
        # workspace must read as "no such invitation", not as a refusal.
        invitation = Invitation.objects.filter(
            pk=pk, tenant=request.user.tenant).first()
        if invitation is None:
            return Response({'detail': 'No such invitation.'}, status=status.HTTP_404_NOT_FOUND)
        if invitation.accepted_at is not None:
            return Response({'detail': 'That invitation has already been accepted.'},
                            status=status.HTTP_409_CONFLICT)
        if invitation.revoked_at is None:
            invitation.revoked_at = timezone.now()
            invitation.save(update_fields=['revoked_at'])
        return Response(invitation_payload(invitation), status=status.HTTP_200_OK)


class InvitationLookupView(PublicAPIView):
    """GET ?token=: what this invitation is for, so the page can say so.

    Answers 404 for anything not currently redeemable. The page shows "this
    invitation is no longer valid" either way, so there is nothing to learn
    from the difference between expired, withdrawn and never real.
    """

    throttle_scope = 'invite_accept'

    def get(self, request):
        invitation = _redeemable(request.query_params.get('token'))
        if invitation is None:
            return Response({'detail': 'This invitation is no longer valid.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            'organization': invitation.tenant.name,
            'email': invitation.email,
            'role': invitation.role,
            'invited_by': invitation.invited_by.full_name or invitation.invited_by.email
            if invitation.invited_by_id else '',
        }, status=status.HTTP_200_OK)


class InvitationAcceptView(PublicAPIView):
    """POST: create the account the invitation was for, and sign them in."""

    throttle_scope = 'invite_accept'

    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = serializer.validated_data['invitation']

        try:
            user = User.objects.create_user(
                email=invitation.email,
                password=serializer.validated_data['password'],
                full_name=serializer.validated_data['full_name'],
                tenant=invitation.tenant,
                role=invitation.role,
            )
        except IntegrityError:
            # Someone already holds that address in this organization -- two
            # people redeeming the same link at once, or an account created in
            # between. The invitation is spent either way.
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=['accepted_at'])
            return Response(
                {'detail': 'An account already exists for this address. Sign in instead.'},
                status=status.HTTP_409_CONFLICT)

        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=['accepted_at'])
        response = Response(identity_payload(user), status=status.HTTP_201_CREATED)
        # Signed in immediately: they proved they hold the mailbox the
        # invitation was sent to, and just chose the password.
        return set_auth_cookies(response, user)


def _redeemable(token):
    """The pending, unexpired invitation a raw token names, or None."""
    token = str(token or '').strip()
    if not token:
        return None
    invitation = (
        Invitation.objects
        .select_related('tenant', 'invited_by')
        .filter(token_hash=Invitation.hash_token(token))
        .first()
    )
    if invitation is None or not invitation.is_pending:
        return None
    return invitation
