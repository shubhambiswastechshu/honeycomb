from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import BaseUserCreationForm, UserChangeForm

from .models import Tenant, User


class UserCreationForm(BaseUserCreationForm):
    # Django's stock form is keyed on `username`; this model logs in by email.
    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ('email', 'full_name', 'tenant', 'role')


class UserAdminChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug', 'created_at')
    search_fields = ('name', 'slug')
    readonly_fields = ('created_at',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserAdminChangeForm
    add_form = UserCreationForm
    list_display = ('id', 'email', 'full_name', 'tenant', 'role', 'is_active', 'is_staff')
    list_filter = ('tenant', 'role', 'is_active', 'is_staff', 'is_superuser')
    list_select_related = ('tenant',)
    search_fields = ('email', 'full_name')
    ordering = ('email',)
    filter_horizontal = ('groups', 'user_permissions')
    readonly_fields = ('date_joined', 'last_login')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Profile', {'fields': ('full_name',)}),
        ('Organization', {'fields': ('tenant', 'role')}),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'full_name', 'tenant', 'role', 'password1', 'password2'),
        }),
    )
