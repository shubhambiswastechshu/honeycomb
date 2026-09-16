"""Forager URLs.

The /agent/ prefix is the worker plane -- token-authenticated, no session --
and everything else is the console plane on the normal tenant session. They are
split by path so the difference is obvious at a glance in the router.
"""
from django.urls import path

from . import public, public_workspace, views

app_name = 'foraging'

urlpatterns = [
    # Worker plane: the only endpoints the always-on PC calls.
    path('agent/poll/', views.AgentPoll.as_view(), name='agent-poll'),
    path('agent/jobs/<int:job_id>/progress/', views.AgentProgress.as_view(),
         name='agent-progress'),
    path('agent/jobs/<int:job_id>/pages/', views.AgentPages.as_view(),
         name='agent-pages'),
    path('agent/jobs/<int:job_id>/links/', views.AgentLinks.as_view(),
         name='agent-links'),
    path('agent/jobs/<int:job_id>/complete/', views.AgentComplete.as_view(),
         name='agent-complete'),

    # Console plane.
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:job_id>/', views.job_detail, name='job-detail'),
    path('jobs/<int:job_id>/events/', views.job_events, name='job-events'),
    path('jobs/<int:job_id>/pages/', views.job_pages, name='job-pages'),
    path('jobs/<int:job_id>/cancel/', views.cancel_job, name='job-cancel'),
    path('workers/', views.workers, name='workers'),

    # Public plane: no login. Everything it allows, and every limit on it, is
    # documented in public.py.
    path('public/jobs/', public.PublicJobs.as_view(), name='public-jobs'),
    path('public/jobs/<int:job_id>/', public.PublicJobDetail.as_view(), name='public-job'),
    path('public/jobs/<int:job_id>/pages/', public.PublicJobPages.as_view(),
         name='public-job-pages'),
    path('public/jobs/<int:job_id>/export.csv', public.PublicJobExport.as_view(),
         name='public-job-export'),
    path('public/jobs/<int:job_id>/cancel/', public.PublicJobCancel.as_view(),
         name='public-job-cancel'),
    path('public/jobs/<int:job_id>/control/', public.PublicJobControl.as_view(),
         name='public-job-control'),
    path('public/jobs/<int:job_id>/delete/', public.PublicJobDelete.as_view(),
         name='public-job-delete'),

    # The crawler workspace: tabs, filters, issues, URL details, reports,
    # sitemap and comparison. Same scope and limits as the routes above.
    path('public/jobs/<int:job_id>/workspace/', public_workspace.PublicWorkspace.as_view(),
         name='public-job-workspace'),
    path('public/jobs/<int:job_id>/grid/', public_workspace.PublicGrid.as_view(),
         name='public-job-grid'),
    path('public/jobs/<int:job_id>/grid.csv', public_workspace.PublicGridExport.as_view(),
         name='public-job-grid-export'),
    path('public/jobs/<int:job_id>/url/', public_workspace.PublicUrlDetail.as_view(),
         name='public-job-url'),
    path('public/jobs/<int:job_id>/links/', public_workspace.PublicUrlLinks.as_view(),
         name='public-job-links'),
    path('public/jobs/<int:job_id>/tree/', public_workspace.PublicSiteTree.as_view(),
         name='public-job-tree'),
    path('public/jobs/<int:job_id>/reports/', public_workspace.PublicReports.as_view(),
         name='public-job-reports'),
    path('public/jobs/<int:job_id>/reports/<slug:name>/', public_workspace.PublicReport.as_view(),
         name='public-job-report'),
    path('public/jobs/<int:job_id>/sitemap.xml', public_workspace.PublicSitemap.as_view(),
         name='public-job-sitemap'),
    path('public/jobs/<int:job_id>/sitemap-audit/', public_workspace.PublicSitemapAudit.as_view(),
         name='public-job-sitemap-audit'),
    path('public/jobs/<int:job_id>/compare/', public_workspace.PublicCompare.as_view(),
         name='public-job-compare'),
]

# The console page is a plain server-rendered route, not under /api/. It is
# mounted separately in Honeycomb/urls.py.
console_urlpatterns = [
    path('forager/', views.console, name='console'),
    path('forager/<int:job_id>/', views.console, name='console-job'),
]

