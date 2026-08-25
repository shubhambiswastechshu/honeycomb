from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed

from .backends import AMBIGUOUS_TENANTS_ATTR
from .exceptions import TENANTLESS_DETAIL, AmbiguousOrganizationError
from .models import Tenant, User


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ['id', 'name', 'slug']
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'full_name', 'role']
        read_only_fields = fields


class SignUpSerializer(serializers.Serializer):
    organization_name = serializers.CharField(max_length=120)
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        # Signup always mints a *new* organization, so the (tenant, email)
        # constraint can never fire here -- a fresh tenant_id makes every pair
        # unique. Without this check a repeated signup silently forks a second
        # empty org that the owner may then be unable to sign in to. The
        # intended semantic is therefore that the address is globally free at
        # signup time; joining an existing org needs an invite flow, not a
        # second signup.
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return email

    def validate(self, attrs):
        # Run Django's validators against an unsaved instance so the
        # UserAttributeSimilarityValidator can compare against email/full_name.
        probe = User(email=attrs['email'], full_name=attrs['full_name'])
        try:
            validate_password(attrs['password'], user=probe)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                tenant = Tenant.objects.create(name=validated_data['organization_name'])
                user = User.objects.create_user(
                    email=validated_data['email'],
                    password=validated_data['password'],
                    full_name=validated_data['full_name'],
                    tenant=tenant,
                    role=User.Role.OWNER,
                )
        except IntegrityError:
            # Race backstop: two concurrent signups can both pass
            # validate_email() and then collide on a unique constraint.
            raise serializers.ValidationError(
                {'email': ['A user with this email already exists.']}
            )
        return user


class SignUpCheckSerializer(serializers.Serializer):
    """
    Partial, side-effect-free validation of a signup in progress.

    The signup wizard asks one thing at a time, so a problem has to surface on
    the step that caused it rather than at the end. Every field is optional and
    only the ones actually sent get validated, using the *same* rules as
    SignUpSerializer -- if a value passes here it must not fail there.
    """

    organization_name = serializers.CharField(max_length=120, required=False)
    full_name = serializers.CharField(max_length=150, required=False)
    email = serializers.EmailField(max_length=254, required=False)
    password = serializers.CharField(
        write_only=True, required=False, style={'input_type': 'password'}
    )

    # Context for the password check only; never validated on their own here.
    context_email = serializers.EmailField(
        max_length=254, required=False, allow_blank=True
    )
    context_full_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True
    )

    validate_email = SignUpSerializer.validate_email

    def validate(self, attrs):
        if 'password' in attrs:
            # Mirror SignUpSerializer.validate(): the similarity validator needs
            # the address and name the user has already entered, which arrive as
            # context_* because those steps are behind them, not being checked.
            probe = User(
                email=attrs.get('context_email') or '',
                full_name=attrs.get('context_full_name') or '',
            )
            try:
                validate_password(attrs['password'], user=probe)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs


class ProfileUpdateSerializer(serializers.ModelSerializer):
    # Declared explicitly because User.full_name is blank=True: the generated
    # field would be optional *and* accept the empty string, and a profile that
    # can be blanked leaves the UI with nothing to greet the user by.
    # CharField trims whitespace by default, so '   ' is rejected as blank.
    full_name = serializers.CharField(max_length=150)

    class Meta:
        model = User
        fields = ['full_name']


class ChangeEmailSerializer(serializers.Serializer):
    """
    Moves an account to a new address after re-proving the password.

    Re-authentication matters here specifically: the address is the sign-in
    identifier, so whoever controls it controls the account. An unlocked
    browser must not be enough to take it over.
    """

    current_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )
    new_email = serializers.EmailField(max_length=254)

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate_new_email(self, value):
        email = User.objects.normalize_email(value)
        if email == self.context['request'].user.email:
            raise serializers.ValidationError(
                'This is already your email address.'
            )
        return email

    def validate(self, attrs):
        # The existence probe lives in validate(), not in validate_new_email():
        # DRF collects every field error in one pass, so a field-level check
        # would answer "that address is taken" to a caller who supplied the
        # wrong password. validate() runs only once no field errored, which
        # makes a valid password the price of the answer.
        email = attrs['new_email']
        # Same global-uniqueness rule as SignUpSerializer.validate_email: the
        # (tenant, email) constraint would allow the address to exist in
        # another org, but sign-in resolves an address to an account, so
        # letting one address span orgs makes accounts unreachable.
        taken = User.objects.filter(email=email).exclude(
            pk=self.context['request'].user.pk
        )
        if taken.exists():
            raise serializers.ValidationError(
                {'new_email': ['A user with this email already exists.']}
            )
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )
    new_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate(self, attrs):
        if attrs['new_password'] == attrs['current_password']:
            raise serializers.ValidationError(
                {'new_password': ['The new password must differ from the current one.']}
            )
        try:
            # Against the saved instance, not a probe: the similarity validator
            # needs the real email and full_name to reject 'priya@northwind'
            # as a password for priya@northwind.test.
            validate_password(attrs['new_password'], user=self.context['request'].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})
        return attrs


class TenantUpdateSerializer(serializers.ModelSerializer):
    name = serializers.CharField(max_length=120)

    class Meta:
        model = Tenant
        fields = ['name']

    # The slug is deliberately *not* recomputed from the new name. It is the
    # sign-in disambiguator (organization_slug in SignInSerializer) and it is
    # what any bookmark or invite link would carry, so re-slugging on a rename
    # would silently break both. Only the display name moves.


class SignInSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    # Optional, additive: only needed when the address exists in several orgs.
    organization_slug = serializers.SlugField(
        max_length=140, required=False, allow_blank=True
    )

    def validate(self, attrs):
        request = self.context.get('request')
        user = authenticate(
            request=request,
            email=User.objects.normalize_email(attrs['email']),
            password=attrs['password'],
            organization_slug=attrs.get('organization_slug') or None,
        )
        if user is None:
            tenants = getattr(request, AMBIGUOUS_TENANTS_ATTR, None)
            if tenants:
                # The password matched, but in more than one organization. Ask
                # the caller to pick instead of silently signing them into the
                # lowest-pk account. SignInView renders this as HTTP 409.
                raise AmbiguousOrganizationError(tenants)
            # Deliberately generic: never reveal whether the address exists,
            # is inactive, or simply had the wrong password.
            raise AuthenticationFailed('Invalid email or password.')
        if user.tenant_id is None:
            # Platform-level superusers have no tenant. The contract types
            # "tenant" as an object, so they belong in /admin/, not here.
            raise AuthenticationFailed(TENANTLESS_DETAIL)
        attrs['user'] = user
        return attrs
