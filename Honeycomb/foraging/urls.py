"""Forager URLs.

The /agent/ prefix is the worker plane -- token-authenticated, no session --
and everything else is the console plane on the normal tenant session. They are
split by path so the difference is obvious at a glance in the router.
"""
from django.urls import path

from . import views

app_name = 'foraging'

urlpatterns = [
    # Worker plane: the only endpoints the always-on PC calls.
    path('agent/poll/', views.AgentPoll.as_view(), name='agent-poll'),
    path('agent/jobs/<int:job_id>/progress/', views.AgentProgress.as_view(),
         name='agent-progress'),
    path('agent/jobs/<int:job_id>/pages/', views.AgentPages.as_view(),
         name='agent-pages'),
    path('agent/jobs/<int:job_id>/complete/', views.AgentComplete.as_view(),
         name='agent-complete'),

    # Console plane.
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:job_id>/', views.job_detail, name='job-detail'),
    path('jobs/<int:job_id>/events/', views.job_events, name='job-events'),
    path('jobs/<int:job_id>/pages/', views.job_pages, name='job-pages'),
    path('jobs/<int:job_id>/cancel/', views.cancel_job, name='job-cancel'),
    path('workers/', views.workers, name='workers'),
]

# The console page is a plain server-rendered route, not under /api/. It is
# mounted separately in Honeycomb/urls.py.
console_urlpatterns = [
    path('forager/', views.console, name='console'),
    path('forager/<int:job_id>/', views.console, name='console-job'),
]

