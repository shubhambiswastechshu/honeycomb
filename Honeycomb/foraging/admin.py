import secrets

from django.contrib import admin

from django.contrib import messages
from django.utils.html import format_html

from .models import CrawlEvent, CrawlJob, CrawlPage, Worker


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'status', 'version', 'last_seen_at',
                    'rss_mb', 'active_jobs', 'paused')
    list_filter = ('paused', 'tenant')
    search_fields = ('name', 'token_prefix')
    readonly_fields = ('token_prefix', 'token_hash', 'last_seen_at')

    def save_model(self, request, obj, form, change):
        """Adding a worker here mints its token, exactly as the API does.

        Without this, a Worker added from the admin saved with an empty hash and
        could never authenticate -- the two credential columns are read-only, so
        the form had no way to fill them. Support has one other route to a
        token (the console's pair button), and that route needs a session on an
        account with a tenant; this one needs staff. Both are useful, for
        different people.

        The plaintext is shown once, in the message, and never stored.
        """
        if change:
            super().save_model(request, obj, form, change)
            return
        plain = Worker.PREFIX + secrets.token_urlsafe(32)
        obj.token_prefix = plain[:len(Worker.PREFIX) + 4]
        obj.token_hash = Worker.hash_token(plain)
        super().save_model(request, obj, form, change)
        self.message_user(
            request,
            format_html(
                'Worker token, shown once and stored only as a hash — copy it now:'
                '<br><code style="user-select:all">{}</code>', plain),
            level=messages.WARNING,
        )


@admin.register(CrawlJob)
class CrawlJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'seed_url', 'status', 'worker', 'pages_crawled',
                    'urls_discovered', 'created_at')
    list_filter = ('status', 'source', 'tenant')
    search_fields = ('seed_url',)
    readonly_fields = ('claimed_at', 'started_at', 'finished_at', 'heartbeat_at')


@admin.register(CrawlPage)
class CrawlPageAdmin(admin.ModelAdmin):
    list_display = ('url', 'status_code', 'depth', 'size_bytes', 'response_time_ms')
    list_filter = ('status_code',)
    search_fields = ('url',)


@admin.register(CrawlEvent)
class CrawlEventAdmin(admin.ModelAdmin):
    list_display = ('job', 'seq', 'level', 'text', 'at')
    list_filter = ('level',)
