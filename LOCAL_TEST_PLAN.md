# Local Testing Plan — feature/bug-fixes

## Goal
Run the app locally against a local PostgreSQL DB (not production) to verify bug fixes.

## Changes needed (and how to revert)

### 1. `backend/.env` — NOT git-tracked (gitignored)
**Original state:**
```
# DATABASE_URL=postgresql://postgres:Elital00@localhost:5432/money_tracker
```
**Change:** Uncomment that line so the app can connect to local PostgreSQL.
**Revert:** Comment it back out after testing.

---

### 2. `backend/App.py` — git-tracked
**Change:** Make `sslmode` conditional — `require` in production, `disable` locally.
**Revert:** `git stash` (or `git checkout -- backend/App.py`) after testing.

---

## Steps

- [ ] 1. Confirm PostgreSQL is running locally (`psql -U postgres -c "\l"`)
- [ ] 2. Create local `money_tracker` database if it doesn't exist
- [ ] 3. (Optional) Restore from `backup-2026-04-12.sql` for realistic test data
- [ ] 4. Uncomment `DATABASE_URL` in `backend/.env`
- [ ] 5. Patch `App.py` SSL mode to be environment-aware
- [ ] 6. Start backend: `cd backend && python App.py`
- [ ] 7. Start frontend: `cd MoneyTracker_frontend && npm run dev`
- [ ] 8. Test in browser:
  - [ ] Register a new user
  - [ ] Log in
  - [ ] Log out and log back in
  - [ ] Check `/api/me` returns correct user

## Revert (after testing is done)
```bash
# Revert App.py changes
git checkout -- backend/App.py

# Manually re-comment .env:
# DATABASE_URL=postgresql://...  →  # DATABASE_URL=postgresql://...
```
