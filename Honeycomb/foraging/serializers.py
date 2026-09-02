"""Read shapes for the console. The worker plane parses its own payloads."""
from rest_framework import serializers

from .models import CrawlJob, CrawlPage, CrawlSegment, Worker


class WorkerSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)
    is_online = serializers.BooleanField(read_only=True)

    class Meta:
        model = Worker
        fields = [
            'id', 'name', 'status', 'is_online', 'version', 'platform',
            'last_seen_at', 'rss_mb', 'active_jobs', 'paused', 'token_prefix',
            'created_at',
        ]


class CrawlJobSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source='worker.name', default='', read_only=True)
    rate = serializers.FloatField(read_only=True)
    duration_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = CrawlJob
        fields = [
            'id', 'seed_url', 'status', 'config', 'source', 'worker_name',
            'pages_crawled', 'pages_queued', 'urls_discovered', 'links_found',
            'failures', 'bytes_downloaded', 'status_counts', 'rate',
            'duration_seconds', 'cancel_requested', 'error',
            'created_at', 'started_at', 'finished_at', 'heartbeat_at',
        ]


class CrawlPageSerializer(serializers.ModelSerializer):
    class Meta:
        model = CrawlPage
        fields = [
            'url', 'depth', 'status_code', 'content_type', 'response_time_ms',
            'size_bytes', 'redirect_count', 'final_url', 'discovered_via',
            'inlinks', 'outlinks', 'error',
            'indexability', 'indexability_status', 'title', 'title_length',
            'title_pixel_width', 'meta_description', 'meta_description_length',
            'h1_1', 'h1_count', 'h2_count', 'canonical', 'meta_robots',
            'word_count', 'text_ratio', 'flesch_reading_ease', 'readability',
            'language', 'images', 'images_missing_alt',
            'structured_data_types', 'issues',
            'link_score', 'near_duplicates', 'closest_similarity',
            'hreflang_count', 'hreflang_issues', 'structured_data_errors',
            'structured_data_warnings', 'structured_data_findings',
        ]


class CrawlSegmentSerializer(serializers.ModelSerializer):
    """Read shape only.

    Writes go through the segment tool, which validates each rule against
    CrawlPage's columns before it stores anything; a ModelSerializer would
    accept any JSON at all into `rules` and only discover the bad field at the
    moment somebody applied the segment.
    """

    rule_count = serializers.SerializerMethodField()

    class Meta:
        model = CrawlSegment
        fields = ['id', 'name', 'description', 'rules', 'rule_count', 'match',
                  'created_at']
        read_only_fields = fields

    def get_rule_count(self, obj):
        return len(obj.rules or [])
