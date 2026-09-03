# Honeycomb backend: Django + the FastAPI MCP data plane, one ASGI process.
#
# Python 3.11 because that is the floor the ported connectors need -- they use
# `X | None` in signatures, which 3.9 cannot parse at import time.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code change does not re-resolve the whole tree.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Collected at build time, not on boot: WhiteNoise serves the admin re-skin
# from here, and doing it per container start would repeat the same work on
# every restart and every replica.
#
# DJANGO_DEBUG=false is the load-bearing part. settings.py selects the manifest
# storage backend only outside DEBUG, so collecting with the default DEBUG=True
# writes the files but NO staticfiles.json. At runtime, where DEBUG is false,
# the manifest backend then raises on the first {% static %} it is asked for --
# the admin answers 500 while every other route is healthy.
# The other three exist only so settings.py can import during the build.
RUN DJANGO_DEBUG=false     DJANGO_SECRET_KEY=build-only-not-used-at-runtime     DJANGO_DB_ENGINE=sqlite     DJANGO_ALLOWED_HOSTS=localhost     HONEYCOMB_CRED_KEYS=build-only     python manage.py collectstatic --noinput

EXPOSE 8000

# gunicorn with a uvicorn worker, NOT plain gunicorn: asgi.py composes the
# FastAPI MCP app beside Django, and a WSGI worker would never reach it --
# every /mcp/ URL would 404 while the portal looked healthy.
#
# --timeout 120 sits above HONEYCOMB_MCP_TOOL_TIMEOUT (45s default) with room
# to spare, so a legitimately slow upstream is cut off by the tool timeout,
# which returns a readable error, rather than by the worker being killed.
#
# --forwarded-allow-ips is safe here only because the container is reachable
# solely through Coolify's proxy. Do not copy it onto a directly exposed
# service: it makes the client IP -- and therefore every rate limit -- caller
# controlled. See HANDOFF invariant 6 and DJANGO_NUM_PROXIES.
# createsuperuser --noinput reads DJANGO_SUPERUSER_EMAIL and
# DJANGO_SUPERUSER_PASSWORD. It exits non-zero when the account already
# exists, which is the normal case on every deploy after the first, so its
# failure is swallowed deliberately -- `|| true` here means "already there",
# not "ignore errors in general". Leave the two variables unset and no admin
# account is created at all.
#
# reset_admin runs only when HONEYCOMB_RESET_ADMIN is set, and it DELETES
# EVERY ACCOUNT before creating one. It is meant to be switched on for a
# single deploy and switched off again; left on, it wipes the user table on
# every container start.
CMD ["sh", "-c", "python manage.py migrate --noinput && { [ -n \"$DJANGO_SUPERUSER_EMAIL\" ] && python manage.py createsuperuser --noinput || true; } && { [ -n \"$HONEYCOMB_RESET_ADMIN\" ] && python manage.py reset_admin || true; } && gunicorn Honeycomb.asgi:application -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout 120 --forwarded-allow-ips='*' --access-logfile -"]
