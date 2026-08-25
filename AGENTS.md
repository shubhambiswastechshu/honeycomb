# AGENTS.md

Read **[HANDOFF.md](HANDOFF.md)** first — it holds the invariants, conventions,
and known limitations for this repository. [README.md](README.md) has setup and
the API surface.

Short version:

- Never commit `db.sqlite3`, `.env`, `.venv/`, `node_modules/`, `.next/`.
- Auth tokens live in httpOnly cookies. Never move them into JavaScript storage.
- CSRF is enforced on every unsafe method, including public auth endpoints.
- Tenant scope always comes from `request.user`, never from client input.
- Dashboard panels are intentionally empty. Do not add mock data.
- Plain CSS only — no Tailwind, no component libraries beyond `lucide-react`.
- Python 3.9 syntax. Light mode only.

Verify before finishing:

```bash
cd Honeycomb && ../.venv/bin/python manage.py check
cd frontend && npx tsc --noEmit && npm run build
```
