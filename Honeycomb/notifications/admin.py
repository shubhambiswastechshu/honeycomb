from django.contrib import admin

from .models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'template', 'to_email', 'status', 'subject')
    list_filter = ('status', 'template', 'created_at')
    search_fields = ('to_email', 'subject', 'error')
    # Nothing here is editable: it is a record of what happened, and a row that
    # can be rewritten answers no questions.
    readonly_fields = tuple(f.name for f in EmailLog._meta.fields)

    def has_add_permission(self, request):
        return False
