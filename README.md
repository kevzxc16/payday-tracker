# Payday Budget Tracker

A simple, stdlib-only Python web app for people paid weekly, biweekly, or
monthly who want to plan their money between paychecks.

**Built with the Python standard library only** — no Flask, no Django, no
SQLAlchemy. Just `http.server`, `sqlite3`, `smtplib`, `threading`, and
`string.Template`. The whole thing is ~3,500 lines and runs anywhere
Python 3.10+ does.

## Features

- **Per-period budgeting**: define your pay schedule + anchor payday, and
  the app figures out the current pay period and computes your remaining
  discretionary funds.
- **Bills**: one-time or recurring (weekly/biweekly/monthly/yearly).
  Mark paid → next occurrence auto-spawns for recurring bills.
- **Expenses**: log spending by category, see per-category totals.
- **Savings goals**: set targets, track contributions, watch progress bars
  fill. Auto-flips to "achieved" when the target is reached.
- **Debts**: starting balance, current balance, minimum payment, optional
  interest rate and payoff date. Payments auto-decrement balance and
  flip to "paid off" at zero.
- **Email reminders** via SMTP (or console mode for dev). Bills due,
  payday savings nudges, debt deadlines, end-of-period check-ins.
- **JSON API** mirrors every HTML page (`/api/v1/...`) so the same backend
  can power a future mobile app or CLI.
- **Background scheduler** runs notification generation, email dispatch,
  and session housekeeping every minute.
- **Activity log** records everything you do so you can see your own
  progress over time.

## Quick start

```bash
# 1. Unpack the repo, then:
cd payday_tracker
cp .env.example .env

# 2. Edit .env at a minimum to set a long random SECRET_KEY.
#    On macOS/Linux: openssl rand -hex 32
#    On Windows (PowerShell):
#      [Convert]::ToBase64String((1..32 | %{[byte](Get-Random -Max 256)}))

# 3. Run it.
python run.py
```

Then open <http://127.0.0.1:8000/signup> in your browser.

The first run creates `data/payday.db`. Stop with Ctrl+C — the SQLite
database persists between runs.

### Requirements

- Python 3.10 or newer.
- No third-party packages — *everything* is stdlib.
- Works on Windows, macOS, and Linux. Tested with Python 3.12.

## Configuration

All configuration is via environment variables, optionally loaded from
a `.env` file at the project root.

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Set to `0.0.0.0` to expose on LAN. |
| `PORT` | `8000` | HTTP port. |
| `DB_PATH` | `data/payday.db` | Relative paths resolve from project root. |
| `SECRET_KEY` | `dev-only-insecure-key` | **Set to a long random string in production.** |
| `SESSION_LIFETIME_DAYS` | `30` | How long the auth cookie lasts. |
| `BASE_URL` | `http://127.0.0.1:8000` | Used in outgoing email links. |
| `DEBUG` | `false` | Shows tracebacks on 500 errors. Also disables `Secure` cookie flag. |
| `SCHEDULER_INTERVAL_SECONDS` | `60` | How often the background worker ticks. |
| `SMTP_HOST` | *(empty)* | Empty = console mode (prints emails to stdout). |
| `SMTP_PORT` | `587` | |
| `SMTP_USERNAME` | *(empty)* | |
| `SMTP_PASSWORD` | *(empty)* | |
| `SMTP_FROM_NAME` | `Payday Tracker` | |
| `SMTP_FROM_EMAIL` | *(empty)* | Defaults to `SMTP_USERNAME` if not set. |
| `SMTP_USE_TLS` | `true` | STARTTLS on port 587. Set false for plain SMTP. |

### Setting up Gmail SMTP

If you want to send real emails through Gmail:

1. Turn on **2-Step Verification** for your Google account at
   <https://myaccount.google.com/security>.
2. Generate an **App password** at
   <https://myaccount.google.com/apppasswords>. Pick "Mail" as the app
   and any device label.
3. Set in `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=your@gmail.com
   SMTP_PASSWORD=the-16-char-app-password
   SMTP_FROM_EMAIL=your@gmail.com
   SMTP_USE_TLS=true
   ```

Don't use your regular Gmail password — App passwords are required when
2FA is on. Any other SMTP provider (Mailgun, SendGrid, Postmark, etc.)
works the same way: set host, port, username, password.

### Console mode (default)

If `SMTP_HOST` is empty, emails get printed to stdout instead of being
sent. This lets you develop without configuring SMTP and lets you see
exactly what the system would send. The notification rows in the database
still get marked `sent` so you can see the flow.

## Project layout

```
payday_tracker/
├── run.py                       # Entry point. `python run.py`.
├── README.md                    # This file.
├── .env.example                 # Copy to .env and edit.
├── data/                        # SQLite database lives here.
└── app/
    ├── config.py                # .env loader + frozen Settings dataclass.
    ├── db.py                    # sqlite3 connection + full schema.
    ├── security.py              # scrypt password hashing + HMAC signing.
    ├── sessions.py              # Server-side session store.
    ├── http_utils.py            # Request/Response classes; HTTPError.
    ├── router.py                # Decorator-based router with path params.
    ├── middleware.py            # Auth + CSRF decorators; session loader.
    ├── templating.py            # string.Template wrapper + helpers.
    ├── server.py                # ThreadingHTTPServer bootstrap; static files.
    ├── auth.py                  # Signup, login, logout, password reset.
    ├── notifications.py         # Generation rules + dispatch queue.
    ├── email_sender.py          # SMTP via stdlib smtplib.
    ├── scheduler.py             # Background daemon thread.
    ├── services/
    │   ├── money.py             # cents↔dollars conversion.
    │   ├── payday.py            # Pay-period math.
    │   ├── budget.py            # Aggregation for the dashboard.
    │   └── progress.py          # Activity log helpers.
    ├── routes/                  # one module per resource
    ├── templates/               # string.Template HTML files.
    └── static/style.css
tests/                           # python -m unittest discover tests
```

## HTML routes

| Method | Path | Description |
|---|---|---|
| GET | `/` | Redirect to `/dashboard` (logged in) or `/login`. |
| GET, POST | `/signup` | Account creation. |
| GET, POST | `/login` | |
| POST | `/logout` | |
| GET, POST | `/forgot-password` | |
| GET, POST | `/reset-password?token=…` | |
| GET | `/dashboard` | KPI tiles + bills/goals/debts/expenses panels. |
| GET, POST | `/profile` | Pay schedule, display name, timezone. |
| GET, POST | `/income` | List + log paychecks. |
| POST | `/income/<id>/delete` | |
| GET, POST | `/bills` | List + create. |
| POST | `/bills/<id>/pay` | Mark paid; recurring → spawns next occurrence. |
| POST | `/bills/<id>/delete` | |
| GET, POST | `/expenses` | List + log spending. |
| POST | `/expenses/<id>/delete` | |
| GET, POST | `/savings` | List goals + create. |
| POST | `/savings/<id>/contribute` | |
| POST | `/savings/<id>/delete` | |
| GET, POST | `/debts` | List + create. |
| POST | `/debts/<id>/pay` | Record a payment. |
| POST | `/debts/<id>/delete` | |
| GET | `/notifications` | In-app notifications panel. |
| POST | `/notifications/<id>/dismiss` | |
| GET | `/activity` | Personal activity log. |

## JSON API

All endpoints accept and return `application/json`. State-changing
endpoints (POST/PUT/PATCH/DELETE) require an `X-CSRF-Token` header
containing the session's CSRF token. Authentication uses the same
session cookie as the HTML pages.

### Authentication

| Method | Path | Notes |
|---|---|---|
| POST | `/signup` | Body: `{email, password, password_confirm, pay_schedule, first_payday}`. |
| POST | `/login` | Body: `{email, password}`. |
| POST | `/logout` | Requires CSRF. |

### User

| Method | Path |
|---|---|
| GET | `/api/v1/me` |
| PUT | `/api/v1/me` |

### Dashboard

| Method | Path |
|---|---|
| GET | `/api/v1/dashboard` |

Returns the full aggregated `BudgetSummary` for the current pay period.

### Income

| Method | Path |
|---|---|
| GET | `/api/v1/paychecks` |
| POST | `/api/v1/paychecks` |
| DELETE | `/api/v1/paychecks/<id>` |

### Bills

| Method | Path |
|---|---|
| GET | `/api/v1/bills` (optional `?status=unpaid`) |
| POST | `/api/v1/bills` |
| GET | `/api/v1/bills/<id>` |
| PUT | `/api/v1/bills/<id>` |
| DELETE | `/api/v1/bills/<id>` |
| POST | `/api/v1/bills/<id>/pay` |

### Expenses

| Method | Path |
|---|---|
| GET | `/api/v1/expenses` (optional `?category=X&since=YYYY-MM-DD`) |
| POST | `/api/v1/expenses` |
| DELETE | `/api/v1/expenses/<id>` |

### Savings

| Method | Path |
|---|---|
| GET | `/api/v1/savings/goals` |
| POST | `/api/v1/savings/goals` |
| GET | `/api/v1/savings/goals/<id>` |
| PUT | `/api/v1/savings/goals/<id>` |
| DELETE | `/api/v1/savings/goals/<id>` |
| POST | `/api/v1/savings/goals/<id>/contributions` |

### Debts

| Method | Path |
|---|---|
| GET | `/api/v1/debts` |
| POST | `/api/v1/debts` |
| GET | `/api/v1/debts/<id>` |
| PUT | `/api/v1/debts/<id>` |
| DELETE | `/api/v1/debts/<id>` |
| POST | `/api/v1/debts/<id>/payments` |

### Notifications & activity

| Method | Path |
|---|---|
| GET | `/api/v1/notifications` |
| POST | `/api/v1/notifications/<id>/dismiss` |
| GET | `/api/v1/activity` |

### Example: create a bill via curl

```bash
# 1. Sign up — saves the session cookie to cookies.txt
curl -c cookies.txt -X POST http://127.0.0.1:8000/signup \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json' \
     -d '{"email":"u@example.com","password":"hunter2222",
          "password_confirm":"hunter2222",
          "pay_schedule":"biweekly","first_payday":"2026-06-12"}'

# 2. Get a CSRF token from the dashboard HTML.
CSRF=$(curl -s -b cookies.txt http://127.0.0.1:8000/dashboard \
      | grep -oP 'name="csrf_token" value="\K[^"]+' | head -1)

# 3. Create a bill.
curl -b cookies.txt -X POST http://127.0.0.1:8000/api/v1/bills \
     -H "Content-Type: application/json" \
     -H "Accept: application/json" \
     -H "X-CSRF-Token: $CSRF" \
     -d '{"name":"Rent","amount":"1200.00","due_date":"2026-07-01",
          "is_recurring":true,"recurrence":"monthly"}'
```

## Security model

- **Passwords**: hashed with `hashlib.scrypt` (`N=2^14`, `r=8`, `p=1`).
  Per-user random salt. Verification is constant-time.
- **Sessions**: random 32-byte URL-safe tokens stored server-side.
  HttpOnly + SameSite=Lax cookies. `Secure` flag set unless `DEBUG=true`.
- **CSRF**: every session carries a CSRF token. Unsafe methods
  (`POST`/`PUT`/`PATCH`/`DELETE`) require it either as a form field
  named `csrf_token` or as the `X-CSRF-Token` header.
- **Password reset**: single-use tokens, 1-hour expiry, all sessions
  invalidated on password change.
- **Static files**: served from `app/static/` with explicit traversal
  guard.
- **SQL**: every query uses parameterized statements.
- **Open-redirect protection**: `?next=` parameter on login restricted
  to relative paths.

## Testing

```bash
python -m unittest discover tests
```

58 tests across:

- `test_money.py` — currency conversion round-trip and edge cases.
- `test_payday.py` — weekly / biweekly / monthly + Jan 31 → Feb 28 clamp.
- `test_security.py` — scrypt + HMAC signing.
- `test_sessions.py` — full session lifecycle including expiry.
- `test_budget.py` — aggregation with seeded data.
- `test_notifications.py` — generation rules + idempotency.
- `test_routes.py` — full HTTP integration: auth, CSRF, JSON API,
  bill lifecycle, recurring rollover.

Each integration test spins up the real server on an ephemeral port
with an isolated SQLite database.

## Deployment notes

This is **not** a production-ready setup as-is. To deploy seriously
you'd want:

1. **Reverse proxy**: nginx, Caddy, or similar, handling TLS termination.
2. **Process manager**: systemd, supervisor, or a similar tool that
   restarts the process on crash. Run `python run.py` as a service.
3. **Database backups**: SQLite is in WAL mode, so back up the `.db`,
   `.db-shm`, and `.db-wal` files together. Tools like LiteStream
   handle continuous replication.
4. **Environment variables**: never commit your real `.env`. Use the
   process manager's secret store.
5. **HTTPS**: with TLS + `DEBUG=false`, cookies get the `Secure` flag
   automatically.

The scheduler runs in the same process as the web server, so a single
process is enough. There's no Celery, no Redis, no extra worker.

## Roadmap & monetization hooks

The schema already includes:

- `users.tier` (`free` | `premium`) — gate features behind a tier check.
- `feature_flags` table — flip per-user betas without schema changes.

Plausible premium-tier additions:

- Detailed analytics dashboards (spending trends, savings velocity,
  debt payoff projections).
- Savings challenges (52-week challenge, no-spend weekend).
- Debt strategy tools (avalanche / snowball calculators with payoff
  date forecasts).
- Custom email reminder rules.
- CSV / PDF exports.
- A short embedded course on personal budgeting.

The clean services layer (`app/services/`) keeps the math separated
from the UI, so swapping in richer visualizations doesn't touch the
business logic.

## License

Built as a learning exercise. Use, modify, and redistribute freely.
