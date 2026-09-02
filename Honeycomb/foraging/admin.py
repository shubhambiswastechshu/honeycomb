from django.contrib import admin

from .models import CrawlEvent, CrawlJob, CrawlPage, Worker


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'status', 'version', 'last_seen_at',
                    'rss_mb', 'active_jobs', 'paused')
    list_filter = ('paused', 'tenant')
    search_fields = ('name', 'token_prefix')
    readonly_fields = ('token_prefix', 'token_hash', 'last_seen_at')


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
