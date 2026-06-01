# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Сводка СК** — daily report system for construction QC engineers (Строительный Контроль). Engineers fill daily reports per object; admin monitors all engineers. Companion tool to **TeamJect** (prescriptions/acts system at `app.teamject.com`). Production server: `sk.stroymanager.ru` (CentOS 7, gunicorn port 5001, nginx reverse proxy). GitHub: `stroymanager-pilot/sk-pilot`.

## Running Locally

```bash
pip3 install flask flask-cors
python3 app.py        # starts at http://localhost:5000
```

Test password for all users: `password123` (SHA-256 hashed).

## Deployment

```bash
# Deploy to production
git add <files> && git commit -m "..." && git push origin main
# Then on server:
cd /var/sk-pilot && git pull origin main && systemctl restart sk-pilot
```

**Verify after every deploy** (mandatory checklist):
```
□ Login as engineer → open report
□ Personnel: enter headcount > 0 → Save (must persist)
□ Вх.контроль: add one entry → appears in list
□ Submit report → status "Сдана"
□ Login as Admin → Все сводки → open report
□ ZIP export → downloads
```

**Rule:** if something breaks after a deploy — `git revert` first, then fix. Brand/visual changes must be in separate commits from functional changes.

## Architecture

### Backend — `app.py`
Single Flask file (~1400 lines). No authentication middleware — auth is done client-side (sessionStorage). All endpoints are public; admin-only endpoints check `user_id` param against DB role.

Key patterns:
- `get_db()` returns a new SQLite connection per request (WAL mode). Always call `db.close()` — no context manager used.
- `ok(data)` / `err(msg, code)` — standard JSON response helpers.
- `rows_to_list(rows)` — converts `sqlite3.Row` results to dicts. **Never call `.get()` on `sqlite3.Row` objects** — use direct key access `row['field']`.
- `auto_migrate()` runs at startup — safe to add new `ALTER TABLE` migrations there.

### Database — `db/schema.py`
SQLite. Path: `db/pilot.db` locally, `/var/data/pilot.db` on Render (legacy), `/var/sk-pilot/db/pilot.db` in production.

Key relationships:
- `partners` (global directory, typed: Заказчик/Генподрядчик/Субподрядчик/Проектировщик) → synced to `contractors` (per-object) via `work_type` field
- `contractors.work_type` stores **either** partner type (e.g. `'Заказчик'`) **or** a work description (e.g. `'Армирование...'`) — these overlap; filter `work_type !== 'Заказчик'` to exclude customers
- `sections` = admin-defined object sections; `user_sections` = engineer's personal sections (only they can see/delete their own)
- `daily_reports` has UNIQUE(object_id, user_id, report_date) — one report per engineer per object per day
- `meetings.agenda` carries over between reports via localStorage (not DB — device-specific)

### Frontend — `static/`
Three single-page apps, no framework, vanilla JS:

| File | Role | Key state |
|------|------|-----------|
| `login.html` | User picker → sets `sessionStorage` (userId, userRole, userName) | — |
| `report.html` | Engineer report form | Global `S` object holds all report state |
| `admin.html` | Admin dashboard | Global `state` object; page-based SPA |

**`report.html` critical patterns:**
- `S.contractors` is filtered at load time: `S.object.contractors.filter(c => c.is_active && c.work_type !== 'Заказчик')`. Changing this filter affects Personnel, IC, OC, AC dropdowns everywhere.
- All async save functions (`saveIC`, `saveOC`, `saveAC`, `saveKS2`, `savePrescription`, `saveAllPersonnel`, `uploadProtocol`) must have `try/catch/finally` with `setSaving(false)` in `finally`. Without this, fetch errors cause silent "nothing happens" failures.
- `showStep(i)` is synchronous — it calls `renderStep()` and sets innerHTML. Auto-save happens in `autoSave()` called from `goNext()`.
- `window._prows` = mutable personnel rows array. Resets to `[]` after save.
- `S.isReadonly = (status === 'submitted')` — gates all save operations. Check this if saves appear to do nothing.

### Brand identity
From TeamJect Brandbook:
- `--navy: #0F2835` (Gunmetal) — header/sidebar backgrounds
- `--navy3: #344966` (Charcoal) — secondary elements
- `--gold: #CA2E55` (Cardinal) — primary accent, CTA buttons, active nav
- Fonts: `Geologica` (headings, `--font-h`), `Roboto` (body, `--font`), `JetBrains Mono` (mono)

CSS variables are defined in `:root` in each HTML file — all three files must be updated together when changing brand.

## Common Pitfalls

1. **`sqlite3.Row` has no `.get()`** — use `row['field']`, not `row.get('field')`.
2. **`window._prows` empty → personnel saves nothing** — if `S.contractors` filtered to empty, rows are empty, `saveAllPersonnel` returns early without error.
3. **Заказчик in contractors list** — `contractors.work_type === 'Заказчик'` means it's a customer, not a subcontractor. Exclude from Personnel/IC/OC/AC dropdowns.
4. **ZIP export** — uses `_export_zip_inner()` wrapper with `try/catch`. The inner function opens new DB connections per report for detail loading (reports list is loaded then closed before the loop).
5. **`/api/reports/null/photos`** — happens when `S.reportId` is null. Guarded in `handlePhotos` and `init()`, but check if guard is bypassed.
6. **CSS specificity**: `input[type=number]` (0,1,1) beats `.p-hc` (0,1,0) — use inline `style="width:64px;flex-shrink:0"` to override.
