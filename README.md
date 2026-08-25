# Honeycomb

A multi-tenant SaaS starter: Django REST backend, Next.js frontend, cookie-based
JWT auth with CSRF protection, and a dashboard shell ready for product features.

Every organization gets its own tenant — separate data, separate members,
separate access — from a single deployment.

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Django 4.2 + DRF 3.16 | Custom `User`, email login, row-level tenancy |
| Auth | simplejwt in httpOnly cookies | CSRF-enforced, no tokens in JS |
| Database | PostgreSQL 14+ | Set `DJANGO_DB_ENGINE=sqlite` for a throwaway run |
| Frontend | Next.js 14 App Router, TypeScript | Plain CSS, no Tailwind |
| Icons | lucide-react | |

---

## Quick start

Requires Python 3.9+, Node 18+ and PostgreSQL 14+.

**Database**, once:

```bash
psql -U postgres -c "CREATE ROLE honeycomb LOGIN PASSWORD 'honeycomb_dev_pw'"
psql -U postgres -c "CREATE DATABASE honeycomb OWNER honeycomb"
cp .env.example .env    # then fill in DJANGO_DB_PASSWORD
```

`settings.py` reads `.env` from the repo root itself, so no `python-dotenv`
and no `export` needed. A real environment variable always beats the file.

No Postgres to hand? `DJANGO_DB_ENGINE=sqlite` falls back to a local file —
nothing in this project uses a Postgres-only feature.

**Backend** (port 8000):

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r Honeycomb/requirements.txt
cd Honeycomb && ../.venv/bin/python manage.py migrate && ../.venv/bin/python manage.py runserver
```

**Frontend** (port 3000), in a second terminal:

```bash
cd frontend && cp .env.local.example .env.local && npm install && npm run dev
```

Open http://localhost:3000 — you will be redirected to `/signin`. Create a
workspace at `/signup`.

Create an admin for `/admin/`:

```bash
cd Honeycomb && ../.venv/bin/python manage.py createsuperuser
```

---

## How multi-tenancy works

Shared-schema, **row-level** tenancy: every tenant-owned row carries a `tenant`
foreign key. There is no schema-per-tenant and no `django-tenants`: one schema
per tenant multiplies migrations by the tenant count, and every schema-per-
tenant migration becomes an outage risk that grows with the customer list.

- `Tenant` — an organization. `slug` is auto-derived and **stable**; renaming the
  organization never changes it, because the slug disambiguates sign-in.
- `User` — email is the login field. Email is unique **per tenant**, not globally,
  so two organizations can each have `alice@example.com`. A partial unique index
  keeps platform superusers (no tenant) globally unique.
- `TenantOwnedModel` — abstract base every future tenant-scoped model should
  inherit.
- `TenantScopedQuerysetMixin` — filters a viewset's queryset to
  `request.user.tenant`. Use it on every new tenant-scoped endpoint.

Signup enforces **global** email uniqueness, so one address owns one workspace.
Sign-in still handles the ambiguous case (409 + organization picker) for users
who end up in several organizations through a future invite flow.

The tenant id also travels inside the JWT as a `tenant_id` claim, so a request
can be scoped without a database round-trip.

---

## API

Base URL `http://localhost:8000/api`. All responses are JSON.

```
USER   = {id, email, full_name, role}
TENANT = {id, name, slug}
IDENTITY = {user: USER, tenant: TENANT}
```

| Method | Path | Auth | Body | Returns |
|---|---|---|---|---|
| GET | `/auth/csrf/` | — | — | `{ok}` + sets `csrftoken` |
| POST | `/auth/signup/` | — | `organization_name, full_name, email, password` | IDENTITY + auth cookies |
| POST | `/auth/signup/check/` | — | any subset of the above | `{ok}` or 400 field map |
| POST | `/auth/signin/` | — | `email, password, organization_slug?` | IDENTITY + cookies, or 409 picker |
| POST | `/auth/refresh/` | cookie | — | `{ok}` + new access cookie |
| POST | `/auth/logout/` | — | — | `{ok}`, cookies cleared |
| GET | `/auth/me/` | cookie | — | IDENTITY |
| PATCH | `/auth/me/` | cookie + CSRF | `full_name` | IDENTITY |
| POST | `/auth/change-email/` | cookie + CSRF | `new_email, current_password` | IDENTITY |
| POST | `/auth/change-password/` | cookie + CSRF | `current_password, new_password` | `{ok}` + reissued cookies |
| PATCH | `/tenant/` | cookie + CSRF | `name` | TENANT (owner/admin only) |

Errors are `{"field": ["message"]}` or `{"detail": "message"}`.

`/auth/signup/check/` exists so the signup wizard can report a taken email on the
email step instead of at the end. It applies exactly the rules the real signup
applies, so nothing that passes a step can fail at submit.

---

## Security model

**Tokens live in httpOnly cookies, never in JavaScript.** `hc_access` (1h) and
`hc_refresh` (7d) are `HttpOnly; SameSite=Lax`, `Secure` when `DEBUG=False`.
JS cannot read them, so XSS cannot exfiltrate a session.

**CSRF is mandatory and enforced.** Cookies are sent ambiently, so cookie auth
without a CSRF check is exploitable. Unsafe methods require an `X-CSRFToken`
header matching the `csrftoken` cookie — including on `/auth/signin/`, which
prevents login CSRF.

**Routes are guarded server-side.** `frontend/middleware.ts` redirects anonymous
requests for `/dashboard/**` before any markup is generated. It checks cookie
*presence* only to prevent a flash of protected chrome — **the Django API is the
real authority** and re-validates every request.

**Rate limits** are per-IP via DRF scoped throttles: signin 10/min, signup 5/hour,
signup-check 40/min, password/email change 5/hour. Admin login is throttled by
custom middleware. These depend on `DJANGO_NUM_PROXIES` being correct.

Also: open-redirect guard on the `?next=` parameter, CSP and security headers on
both servers, generic auth failures that never reveal whether an address exists.

### Before deploying

- [ ] Set `DJANGO_SECRET_KEY` and `DJANGO_DEBUG=False` (Django refuses to boot otherwise)
- [ ] Set `DJANGO_NUM_PROXIES` to your real proxy count — a wrong value makes every rate limit forgeable
- [ ] Rotate any superuser password created during development
- [ ] Replace `LocMemCache` with Redis or Memcached — throttle counters are **per process**, so N workers multiply every limit by N
- [ ] Move to PostgreSQL
- [ ] Run `python manage.py check --deploy` and clear every warning
- [ ] Serve over HTTPS so the `Secure` cookie flag takes effect

---

## Project layout

```
Honeycomb/                 Django project
  accounts/                the only app: tenancy, auth, profile
    models.py              Tenant, User, TenantOwnedModel
    authentication.py      CookieJWTAuthentication + CSRF enforcement
    backends.py            per-tenant email resolution
    mixins.py              TenantScopedQuerysetMixin
    middleware.py          admin login throttle
  Honeycomb/settings.py

frontend/                  Next.js App Router
  middleware.ts            URL-level route guard
  lib/api.ts               typed client, CSRF, refresh-and-retry
  app/signin, app/signup   auth pages (signup is a 4-step wizard)
  app/dashboard/           shell + one panel per rail icon
  components/dashboard/    SessionProvider, IconRail, TopBar, EmptyState
  components/ui/           Logo, honeycomb loader
```

---

## Working on this

See **[HANDOFF.md](HANDOFF.md)** for conventions, invariants that must not be
broken, and the state of every feature. Read it before changing auth, tenancy,
or styling.
