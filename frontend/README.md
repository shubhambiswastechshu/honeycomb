# Honeycomb frontend

A deliberately small Next.js 14 (App Router, TypeScript) app with exactly two screens:
a sign up page and a sign in page. There is no home page and no dashboard — `/` redirects
to `/signin`, and after a successful sign in or sign up the form is replaced in place by a
small signed-in panel.

## Requirements

- Node.js 18.17 or newer (Next.js 14 requires it). Install from https://nodejs.org or via
  a version manager such as `nvm`. Verify with `node --version`.
- The Django backend running at http://localhost:8000 — the pages call it directly from the
  browser, so nothing works until it is up.

## Setup

```
cd "/Users/shubhambiswas/Desktop/HONEYCOMB BACKEND/frontend"
npm install
cp .env.local.example .env.local
npm run dev
```

Then open http://localhost:3000 — it redirects to http://localhost:3000/signin.

`.env.local` holds a single variable:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api
```

The client falls back to that same value if the file is missing, so `.env.local` is only
needed when the backend lives somewhere else. No trailing slash.

## API contract

The client in `lib/api.ts` speaks exactly these endpoints, relative to `NEXT_PUBLIC_API_BASE_URL`:

| Method | Path             | Request body                                             | Response                                   |
| ------ | ---------------- | -------------------------------------------------------- | ------------------------------------------ |
| POST   | `/auth/signup/`  | `organization_name`, `full_name`, `email`, `password`     | `{user, tenant, access, refresh}`          |
| POST   | `/auth/signin/`  | `email`, `password`                                       | `{user, tenant, access, refresh}`          |
| POST   | `/auth/refresh/` | `refresh`                                                 | `{access}`                                 |
| GET    | `/auth/me/`      | none, `Authorization: Bearer <access>`                    | `{user, tenant}`                           |

Errors are rendered from either shape the backend may return: `{"detail": "..."}` or a field
error map such as `{"email": ["..."]}`. A non-JSON response (for example an HTML 500 page)
falls back to a plain message pointing at the backend on port 8000.

## Notes

- The backend must allow CORS from http://localhost:3000.
- Tokens are written to `localStorage` under `honeycomb.access` and `honeycomb.refresh`.
  "Sign out" clears both and returns to the form.
- No Tailwind and no UI library: `app/globals.css` is the entire design system.

## Files

```
app/AuthCard.tsx      shared card shell, field, error banner, signed-in panel
app/globals.css       the whole stylesheet, including a dark mode block
app/layout.tsx        root layout
app/page.tsx          redirects to /signin
app/signin/page.tsx   sign in form
app/signup/page.tsx   sign up form
lib/api.ts            typed fetch client and localStorage token helpers
```
