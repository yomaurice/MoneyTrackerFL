# How this backend is deployed

Written down because two wrong assumptions about it cost a broken production
login during the Phase 0 rollout.

## Render configuration

- **Root directory:** `backend`. The service's working directory is
  `/opt/render/project/src/backend`, not the repository root.
- **Start command:** set in the Render dashboard, and it **overrides the
  Procfile entirely**. `Procfile` in this directory is kept in sync as
  documentation and as a fallback, but editing it changes nothing on its own —
  the dashboard value is what runs.

The start command is:

```
PYTHONPATH=. FLASK_APP=App.py flask db upgrade && gunicorn --bind 0.0.0.0:$PORT App:app
```

Migrations run at boot. `&&` means a failed migration stops gunicorn from
starting, so the deploy fails and Render keeps serving the previous version —
a bad migration is a failed deploy, not an outage.

## Why `PYTHONPATH=.` is required

`backend/__init__.py` exists, so Flask's CLI treats `backend` as a package: it
walks up to the repository root, puts *that* on `sys.path`, and imports the app
as `backend.App`. `App.py` then fails on `from models import ...`, because
`backend/` itself is no longer on the path.

Gunicorn does not hit this — it adds the working directory to `sys.path`
itself. The `flask` console script does not. Neither does anything else, which
is why `python -m flask db upgrade` works locally and hid the problem during
development: `python -m` adds the working directory too.

`PYTHONPATH=.` puts `backend/` back on the path. It applies only to the `flask`
command; gunicorn starts exactly as it did before.

Deleting `backend/__init__.py` would remove the need for it — nothing imports
`backend.*` — but that is a code change and the environment variable is
sufficient.

## Database

Neon Postgres, connection string in Render's `DATABASE_URL`. Note that Neon
branches each have their own endpoint **and their own SQL editor session**: a
query run against a snapshot branch will not show changes made to `main`. Check
which branch is selected before concluding a migration did not run.

## Verifying a deploy that includes a migration

```sql
select version_num from alembic_version;   -- on the main branch
```

Then log in. Login writes to `refresh_token`, so a successful login proves the
schema the application is actually connected to is up to date — which no amount
of reading logs can.
