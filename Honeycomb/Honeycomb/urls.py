"""
URL configuration for Honeycomb project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from accounts.urls import tenant_urlpatterns
from foraging.urls import console_urlpatterns as forager_console_urlpatterns

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/', include(tenant_urlpatterns)),
    # The MCP portal's control plane: the connector catalogue, a tenant's
    # configured connections, and the tool toggles, API keys and activity log
    # hanging off each one. Cookie-authenticated and CSRF-protected like every
    # other /api/ route, because it is the browser that calls it.
    #
    # Two includes. `connections` owns the catalogue, the instances and their
    # tool toggles; `mcp` owns the bearer keys minted against an instance,
    # because the key model and the data plane that validates it belong
    # together. `connectors` contributes no routes at all -- it is a registry,
    # not an app with tables.
    #
    # `mcp.urls` is only the *control* plane for keys. The MCP data plane
    # itself, /mcp/**, is deliberately not routed here. See below.
    path('api/', include('connections.urls')),
    path('api/', include('mcp.urls')),
    # Forager. The /api/forager/agent/** half is the worker plane -- a machine
    # elsewhere polling for crawl jobs on a bearer token, with no session and
    # therefore no CSRF surface. The rest is the console the dashboard reads.
    path('api/forager/', include('foraging.urls')),
    # The authorization server for /mcp/**, at the ROOT and not under /api/:
    # RFC 9728 and RFC 8414 fix the /.well-known/... paths absolutely, and a
    # client derives them from the origin alone. This is what a browser-only
    # client such as claude.ai uses instead of a pasted hc_ key -- it cannot
    # send a static header, so it has to be walked through a real OAuth flow.
    # Listed before the forager console, whose patterns are broad.
    path('', include('mcp.oauth_urls')),
    # The live console page. Server-rendered so it works with no frontend build
    # -- which is exactly the situation you are in when a crawl is misbehaving.
    path('', include((forager_console_urlpatterns, 'foraging'), namespace='foraging-console')),
]

# /mcp/** is absent from this file on purpose, and adding it would be a
# regression rather than a tidy-up. It is served by the FastAPI app composed
# beside Django in Honeycomb/asgi.py, so it never reaches ROOT_URLCONF and
# never passes through MIDDLEWARE. Routing it here would put CsrfViewMiddleware
# and APPEND_SLASH in front of a bearer-authenticated JSON-RPC endpoint, and
# the 301 that CommonMiddleware issues for a slashless POST drops the body --
# which an MCP client reports to the user as "Session terminated". The
# reasoning is written out at the top of asgi.py and in settings.py under
# "Two auth planes".
