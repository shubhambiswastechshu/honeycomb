from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify


class Tenant(models.Model):
    """An organization. The root of every row-level tenancy boundary."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._build_unique_slug()
        return super().save(*args, **kwargs)

    def _build_unique_slug(self):
        base = slugify(self.name)[:120] or 'tenant'
        slug = base
        suffix = 2
        queryset = Tenant.objects.all()
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        while queryset.filter(slug=slug).exists():
            slug = '{0}-{1}'.format(base, suffix)
            suffix += 1
        return slug


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        # BaseUserManager only lowercases the domain part. Lowercase the whole
        # address so Alice@Example.com and alice@example.com are one account.
        return super().normalize_email(email).strip().lower()

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Users must have an email address.')
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('role', User.Role.OWNER)
        # A superuser is platform-level, not tenant-level, so it keeps tenant NULL.
        extra_fields.setdefault('tenant', None)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        OWNER = 'OWNER', 'Owner'
        ADMIN = 'ADMIN', 'Admin'
        MEMBER = 'MEMBER', 'Member'

    # Deliberately not unique=True: uniqueness is scoped to the tenant below.
    email = models.EmailField(max_length=254)
    full_name = models.CharField(max_length=150, blank=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='users',
        null=True,
        blank=True,
        help_text='NULL only for platform-level superusers.',
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    EMAIL_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ['email']
        constraints = [
            # Email is unique *within* an organization, not globally, so two
            # unrelated tenants can each own their own alice@example.com.
            models.UniqueConstraint(
                fields=['tenant', 'email'],
                name='uniq_user_email_per_tenant',
            ),
            # SQL considers NULLs distinct, so the constraint above would let
            # duplicate tenant-less rows through. This partial constraint keeps
            # platform-level superusers (tenant IS NULL) unique on email alone.
            models.UniqueConstraint(
                fields=['email'],
                condition=Q(tenant__isnull=True),
                name='uniq_platform_user_email',
            ),
        ]
        indexes = [
            # The composite constraint above is keyed on tenant first, so a
            # sign-in lookup by email alone needs its own index.
            models.Index(fields=['email'], name='user_email_idx'),
        ]

    def __str__(self):
        return self.email

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return self.full_name.split(' ')[0] if self.full_name else self.email


class TenantOwnedModel(models.Model):
    """
    Abstract base for tenant-scoped tables.

    Every future business model that holds customer data MUST inherit from this
    class: the tenant foreign key is what makes the shared-schema multi-tenancy
    enforceable, and TenantScopedQuerysetMixin (accounts/mixins.py) relies on
    the `tenant` field being present to filter every read and write.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='%(app_label)s_%(class)s_set',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
