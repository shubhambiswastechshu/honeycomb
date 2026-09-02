"""Container healthcheck for the backend.

A script rather than a one-liner in the compose file, for two reasons: the
quoting of a python -c inside a YAML CMD-SHELL string is its own small hazard,
and the reason for the header below needs somewhere to live.

X-Forwarded-Proto matters. Outside DEBUG the project sets
SECURE_SSL_REDIRECT, so a plain HTTP request to localhost is answered with a
301 to https://127.0.0.1/... -- which has no certificate, so the check fails
and the container is marked unhealthy while the application is perfectly
fine. Django reads SECURE_PROXY_SSL_HEADER before deciding to redirect, so
claiming the hop was already TLS-terminated is what lets the check see the
real response. This is the same claim Coolify's proxy makes for real traffic.

Exits 0 when the app answers, 1 otherwise. It asks for /api/auth/csrf/
because that endpoint touches the URL router and the settings and returns a
body, so a 200 means more than "the socket is open".
"""
import sys
import urllib.error
import urllib.request

URL = 'http://127.0.0.1:8000/api/auth/csrf/'
TIMEOUT = 4


def main() -> int:
    request = urllib.request.Request(
        URL,
        headers={
            'X-Forwarded-Proto': 'https',
            'Host': '127.0.0.1',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 0 if response.status == 200 else 1
    except urllib.error.HTTPError as exc:
        # A 4xx is still the application answering. Only 5xx means the process
        # is up but broken; anything else is a working server refusing this
        # particular request, which is not what the healthcheck is asking.
        return 1 if exc.code >= 500 else 0
    except Exception:
        return 1


if __name__ == '__main__':
    sys.exit(main())
