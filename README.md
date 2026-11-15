# The Cornerstone

The working website for the Cornerstone Livelihood Restoration Act, a Galinsky 2028 policy. It runs the whole program the Act describes, and until the Act is law it runs as a public preview anyone can try.

**Public pages:** the Act and its documents (`/act` leads to the bill), the policy speech, the Displacement Event Census (231 entries), and the public Exchange (event register, associations, offers, matching rounds with their published logs, compacts, supervision decisions and quarterly reports).

**The program, by section of the Act**

| Section | Who | What works |
|---|---|---|
| 2 Events | Workers, unions, States; Labor | Petitions, a 60-day public evidence period, certification with written findings (scope, causal period, first separation) |
| 3 Claims | Workers; Labor | Claims with the Act's tests applied as you file, evidence uploads, 90-day decisions, hearings within 60 days, one successor for a worker who died |
| 4 Associations | Workers; Labor | Forming, chartering, 180-day enrollment, closed founding rolls of 2–2,500 units, ballots decided by a majority of all units |
| 5 Exchange | States; Commerce; associations | Offers with a 60-day objection period and no-new-harm findings, verification, rounds with a public random seed, sealed rankings, association-proposing deferred acceptance by oldest unremedied event |
| 6 Compacts | States; members; supervisors | Drafting within the published menu, member approval, 30+10-year term, State surplus share, independent supervision decisions |
| 8 Founding Fund | Associations; Labor | Advances paid to providers, fixed repayment percentage by formula, quarterly remittances |
| 9 Restoration | Labor | Five-condition findings, provisional then remedied, reopening |

**Preview and the switch.** In preview mode every page says the Act is not law, `/preview` gives anyone a signed-in account in any of six roles inside an example world (rebuilt once a day: by the first visit of each UTC day, so it also works on a sleeping free service), and waiting periods can be skipped. An administrator turns the site to enacted mode at `/site`: preview accounts are switched off and signed out, example records are hidden, and the warnings disappear. The switch needs an administrator account, a separate switch password (stored as a hash; changeable only with the current one), and a typed confirmation phrase, in both directions. Attempts are rate-limited and audited, and every administrator is emailed when the mode changes.

Code: `app/rules.py` (the Act's tests and formulas), `app/models.py`, `app/views/` (one module per role), `app/seed.py` (the example world), `tests/test_cornerstone.py`.

**Where it runs.** https://the-cornerstone.onrender.com, in preview mode. The web service is `the-cornerstone` on Render (Docker, Ohio, deploys automatically from `main`) and the database is the Neon project "The Cornerstone" (us-east-2); the app reaches it through the pooled connection and runs migrations over the direct one. The first administrator's email and password are the `APP_ADMIN_EMAIL` and `APP_ADMIN_PASSWORD` values on the Render service's Environment page; change the password at `/account` after the first sign-in, then set the switch password at `/site`. The service is on Render's free plan, which sleeps after 15 minutes without visitors (the next visit takes up to a minute) and cannot send email; `render.yaml` names the smallest paid plan, which removes both limits. Email needs the `APP_SMTP_*` settings in `.env.example`; until they are set, password-reset and notification emails are only written to the log.

**Tests.** `python -m pytest -q` runs 40 tests: the Act's rules, every role's desk, the whole program end to end, and the switch. CI runs them on SQLite and Postgres and checks that the Docker image builds and starts.

---

# Foundation

The shared base for every Galinsky Labs, Galinsky Arts, Galinsky 2028 and Justice For All project. A new project is a copy of this repository with its own code added under `app/`. Everything a web application needs before its first feature is already here and tested.

## What every project gets

| Area | What is included |
|---|---|
| Accounts | Email and password sign-in, three roles (`admin`, `staff`, `member`), invitations by email, optional open registration, password reset by email, an account page, deactivation that signs the person out everywhere |
| Administration | One admin page with users, invitations, API keys, background jobs, AI spend and the audit log |
| Service-to-service API | API keys with scopes for engines that other apps call; `foundation.client.ServiceClient` for calling them, with retries; interactive API docs at `/api/docs` |
| Security | Passwords hashed with scrypt; session, invitation, reset and API-key tokens stored only as keyed hashes; CSRF protection on every form; login and email rate limits; strict Content-Security-Policy and other security headers; sanitised Markdown rendering; outbound fetching restricted to public addresses (`foundation.net`); refuses to run at a public address without `APP_SECRET_KEY` |
| Data | SQLite for development, Postgres (Neon) in production, with Neon's pooled and direct connections handled; migrations applied automatically at start-up, with Foundation's tables and the project's tables on separate migration branches |
| Files | `foundation.blobstore` keeps uploaded files in the database, because Render erases the server's disk on every deploy |
| Background work | `foundation.jobs`: queued jobs with automatic retries and repeating tasks, run by one worker inside the web process |
| Email | Console backend for development, SMTP for production |
| AI | `foundation.llm`: Claude calls with per-call cost records and a monthly spend cap, and a mock provider for tests |
| Interface | A plain, responsive, light/dark base layout branded by `APP_NAME` and `APP_BRAND_COLOR`; project templates override Foundation's |
| Deployment | Dockerfile, `render.yaml`, health check at `/health`, CI on both SQLite and Postgres |

## Layout

```
foundation/          shared code: do not edit inside a project; improve it here and merge it into projects
  web.py             application factory, sign-in/registration/reset/account routes, admin routes
  auth_core.py       users, sessions, one-time tokens, API keys, route dependencies
  security.py        passwords, CSRF, rate limits, headers, sanitising
  db.py models.py    database engine, shared tables
  migrate.py         migrations (branches: foundation, app)
  jobs.py mail.py blobstore.py llm.py net.py client.py
  templates/ static/
app/                 the project's own code
  models.py          the project's tables
  routes.py          the project's pages and API (an APIRouter named `router`)
  templates/         the project's templates (override Foundation's by using the same name)
  static/            served at /app-static
  migrations/versions/
main.py              uvicorn entry point
scripts/manage.py    migrate | makemigration | createadmin | apikey
tests/               Foundation's tests; add the project's tests beside them
```

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD
uvicorn main:app --reload
python -m pytest -q         # 20 tests
```

Open http://localhost:8000 and sign in with the administrator from `.env`.

## Start a new project from Foundation

1. Create the project repository from this one, either as a fork or with "Use this template" on GitHub.
2. Set `APP_NAME`, `APP_TAGLINE`, `APP_ORGANIZATION` and `APP_BRAND_COLOR`.
3. Add tables to `app/models.py`, then run `python scripts/manage.py makemigration "describe the change"`. New columns on existing tables need a `server_default`, so rows that already exist get a value.
4. Add pages and API routes to `app/routes.py`, and templates to `app/templates/`.
5. Add tests in `tests/`, following `tests/test_auth.py`.
6. Deploy (below).

### Pattern: an engine and the apps built on it

An **engine** is a Foundation project whose main product is a JSON API under `/api/v1/`, protected with `require_api_key("scope")`. An **app** is a Foundation project with its own pages that calls the engine through `ServiceClient`:

```python
# in the engine's app/routes.py
@router.post("/api/v1/records")
def create_record(body: RecordIn, key: ApiKey = Depends(require_api_key("records:write"))): ...

# in the app
engine = ServiceClient(os.environ["ENGINE_URL"], os.environ["ENGINE_API_KEY"])
engine.post("/api/v1/records", {...})
```

The engine's administrator creates one API key per calling app on the engine's Admin page, with only the scopes that app needs.

### Taking later Foundation improvements into a project

Projects never edit `foundation/`, so merging Foundation's newer commits into a project does not conflict with the project's own code:

```bash
git remote add foundation https://github.com/Galinsky-2028/Foundation
git fetch foundation && git merge foundation/main
```

## Deploy on Render with Neon

1. Create a Neon project and copy two connection strings: the pooled one (host contains `-pooler`) and the direct one.
2. Create a Render web service from the repository with runtime Docker, region Ohio (the same AWS region as Neon's us-east-2), and the environment variables in `render.yaml`:
   - `APP_BASE_URL` set to the service's address;
   - `APP_SECRET_KEY` set to a long random value;
   - `APP_DATABASE_URL` and `APP_DATABASE_URL_DIRECT` set to the two Neon connection strings;
   - `APP_ADMIN_EMAIL` and `APP_ADMIN_PASSWORD` for the first sign-in;
   - `APP_TRUST_PROXY=true`.
3. For email, set `APP_EMAIL_BACKEND=smtp` and the `APP_SMTP_*` values. A Gmail account needs 2-step verification and an App Password. Render's free services cannot send email on ports 465 or 587.

Render's free plan works for testing, but a free service sleeps after 15 minutes without traffic, which also pauses background jobs. The smallest paid plan keeps it running.

## Configuration

Every setting is listed with a comment in `.env.example`.
