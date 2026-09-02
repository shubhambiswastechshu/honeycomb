# Handoff

Context for anyone — human or AI — picking this project up. Read this before
changing auth, tenancy, or styling. [README.md](README.md) covers setup and the
API surface; this file covers **why things are the way they are** and what will
break if you change them.

---

## Ground rules

**Never commit:** `db.sqlite3` (real password hashes), `.env`, `.venv/`,
`node_modules/`, `.next/`. All are in `.gitignore` — keep them there.

**No new dependencies without a reason you can defend.** The frontend is
deliberately plain CSS with no Tailwind, no shadcn, no styled-components, no CSS-in-JS.
`lucide-react` is the only UI dependency. If a task description tells you to install
a toolchain to get one component, port the component instead — that has been done
once already (the honeycomb loader).

**No fabricated data.** Dashboard panels are empty states by explicit request.
Do not add sample rows, mock charts, placeholder metrics, or lorem ipsum. The only
real values shown anywhere come from the authenticated session.

**Python 3.11.** Raised from 3.9 when the connectors were ported: eleven of
them use `X | None` in signatures, which 3.9 cannot parse at import time.
`render.yaml` and the Dockerfile both pin 3.11.

**Light mode only.** `color-scheme: light` is set deliberately. There is no dark
palette; do not add `prefers-color-scheme: dark` blocks.

---

## Invariants — breaking these is a security regression

1. **Tokens never reach JavaScript.** They live in `hc_access` / `hc_refresh`
   httpOnly cookies. Do not add them to a response body, `localStorage`,
   `sessionStorage`, or a non-httpOnly cookie. `grep -r localStorage frontend/app
   frontend/lib` must stay empty.

2. **CSRF is enforced on every unsafe method**, including the public auth
   endpoints. `PublicAPIView.initial()` calls `enforce_csrf` — this was added
   after an audit found login CSRF was possible without it. Do not set
   `authentication_classes = []` on a view that accepts POST without also
   enforcing CSRF.

3. **Middleware is not the security boundary.** `frontend/middleware.ts` checks
   only that the cookie *exists*; it does not verify the signature. Its job is to
   prevent a flash of protected chrome. Every request is independently
   authenticated by Django. Never move an authorization decision into middleware.

4. **Tenant scope comes from `request.user`, never from the client.** No endpoint
   accepts a tenant id in the body or URL. New tenant-scoped viewsets must use
   `TenantScopedQuerysetMixin`.

5. **`Tenant.slug` is immutable.** Renaming an organization changes `name` only.
   The slug disambiguates sign-in when an address exists in several tenants;
   changing it would break those users' sign-in.

6. **Rate limits depend on `DJANGO_NUM_PROXIES`.** If it does not match the real
   proxy count, `X-Forwarded-For` becomes attacker-controlled and every throttle
   is bypassable. This was a real finding, not a hypothetical.

7. **The `?next=` guard rejects control characters before comparing origins.**
   `/\t//evil.example` bypassed an earlier shape-only check. Do not simplify
   `safeNextPath()` back to string inspection.

---

## Architecture decisions worth knowing

**Why Django and not Next.js API routes.** The intended product direction is
data-platform shaped (Databricks/Snowflake/Fabric territory), which means heavy
Python: pandas, analytics, ML tooling, background jobs. Next.js owns the UI only.

**Why row-level tenancy and not `django-tenants`.** Schema-per-tenant requires
PostgreSQL schemas; the project runs on SQLite. Row-level with a `tenant` FK is
portable and moves to Postgres unchanged. Revisit only if isolation requirements
harden.

**Why email is unique per tenant, not globally.** The data model supports one
person belonging to several organizations. Signup enforces global uniqueness
anyway (one address → one workspace) so the common path is unambiguous, but
sign-in retains the 409 organization-picker path for users who will arrive
through a future invite flow. Both halves must keep working.

**Why cookies replaced localStorage.** Middleware cannot read localStorage, so
URL-level route guarding was impossible before the migration. Cookies also remove
the XSS token-theft vector. Cost: CSRF protection became mandatory.

**Why `/auth/signup/check/` exists.** The signup wizard asks one question per
step, so a taken email must surface on the email step. It reuses the real
serializer's rules, so a passing step can never fail at submit. It is a
deliberate, throttled email-existence oracle — that tradeoff was made knowingly.

---

## Known limitations

- **Logout does not revoke tokens.** It clears cookies. A token already issued
  stays cryptographically valid until it expires (access 1h, refresh 7d). Real
  revocation needs `rest_framework_simplejwt.token_blacklist` plus
  `ROTATE_REFRESH_TOKENS` and `BLACKLIST_AFTER_ROTATION`. Same applies after a
  password change.
- **Throttle counters are per-process** (`LocMemCache`). With N workers each gets
  its own counter, multiplying every limit by N. Needs Redis in production.
- **The search box is decoration.** Real input, real `⌘K` shortcut, no backend.
  There is nothing to search yet.
- **No email delivery.** No verification, no password reset, no invites.
- **No test suite.** Verification so far has been live HTTP probes and a
  throwaway APIClient script. `accounts/tests.py` is empty. Adding real tests is
  the highest-value next task.

---

## Feature status

| Area | State |
|---|---|
| Signup (4-step wizard, per-step validation) | Done |
| Sign-in, incl. multi-org picker | Done |
| Cookie auth + CSRF + refresh-and-retry | Done |
| URL route guarding | Done |
| Dashboard shell (rail, top bar, panels) | Done — panels are empty by design |
| Profile: change name / email / password | Done |
| Settings: rename organization, role-gated | Done |
| Team panel | Empty — no invite flow exists |
| Data / Activity panels | Empty — no domain models exist |
| Email delivery, password reset | Not started |
| Tests | Not started |

---

## Conventions

**Backend.** Single quotes. Module docstrings where neighbours have them.
Comments explain *why*, not *what* — the existing comments are load-bearing
explanations of non-obvious decisions; do not strip them. Serializers own
validation; views stay thin. Every new endpoint declares a `throttle_scope`.

**Frontend.** Named function expressions in callbacks (`function onChange(e)`),
explicit types, no `any`. Auth pages use the global classes in `globals.css`
(`.card .title .field .input .button .error`); the dashboard uses
`app/dashboard/dashboard.css`, scoped under `.dash` so it cannot restyle the auth
pages. Brand colors are CSS variables on `:root` — never hardcode a hex.

**Palette.** `--amber-500 #ea9d3e`, `--amber-400 #e5ac3f`, `--amber-300 #e5bd3f`,
`--amber-200 #eec33d`, `--ink #312f17`. The logo, loader, step pips, and empty-state
icons are all the same seven-hexagon honeycomb cluster — keep new brand elements
consistent with it.

**Motion.** Entrances use `rise` (0.34s) and `fade`; the signup wizard slides
direction-aware. Every animation is disabled under `prefers-reduced-motion` —
add new ones to that block.

---

## Verifying a change

```bash
# Backend
cd Honeycomb && ../.venv/bin/python manage.py check
../.venv/bin/python manage.py makemigrations --check --dry-run

# Frontend
cd frontend && npx tsc --noEmit && npm run build
```

Security smoke tests that must keep passing:

```bash
# Anonymous /dashboard must redirect, not render
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://localhost:3000/dashboard

# Write with a valid session cookie but no CSRF token must be 403
curl -s -b jar.txt -o /dev/null -w "%{http_code}\n" -X PATCH \
  http://127.0.0.1:8000/api/auth/me/ -H 'Content-Type: application/json' \
  -d '{"full_name":"x"}'
```

If the Next dev server throws `Cannot find module './###.js'`, the build cache is
corrupt from concurrent writes: `rm -rf frontend/.next` and restart.
