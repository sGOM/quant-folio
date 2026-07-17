---
name: run-quantfolio
description: Build, launch, and drive the QuantFolio app (KRX quant backtesting + live auto-trading, FastAPI + Next.js on Docker Compose). Use when asked to run, start, boot, build, smoke-test, screenshot, or verify QuantFolio / the quant app / the trading dashboard end-to-end.
---

# Run QuantFolio

QuantFolio is a multi-container web app (FastAPI `web` + `engine` + Celery `worker`,
Next.js `frontend`, Postgres/TimescaleDB, Redis, Caddy `proxy`) orchestrated by
Docker Compose. Everything reaches the app through the **Caddy proxy on
`:8080`** — `/` → frontend, `/api` + `/ws` → web. The other ports bind to
`127.0.0.1` only.

Two ways to drive it, pick by what your change touches:

- **Backend / auth / API change** → run `smoke.sh` (headless: health → register
  → login → authed `/me`). No browser.
- **Frontend / UI change** → drive `http://localhost:8080` with the **Playwright
  MCP** browser tools (navigate/type/click/screenshot).

All paths below are relative to the repo root (`<unit>/`). The committed driver
lives at `.claude/skills/run-quantfolio/smoke.sh`.

## Prerequisites

- **Docker Desktop** (Compose v2). Verified with `Docker Compose v5.1.4`.
- `curl` and `bash` (Git Bash on Windows is fine — that's what this was built on).
- `secrets/*.txt` and `.env` must exist. This repo already has them
  (`secrets/` is gitignored; unused broker keys are empty files). For a clean
  machine, follow README §1–2 to generate `secret_key.txt` /
  `credential_enc_key.txt` and `cp .env.example .env`.

## Build & launch

```bash
docker compose up -d --build      # first run; drop --build to just (re)start
docker compose exec -T web alembic upgrade head   # create/upgrade tables (idempotent)
```

`up -d` is idempotent — re-running it just reconciles state; already-healthy
containers stay up. Confirm everything is healthy:

```bash
docker compose ps --format '{{.Service}}\t{{.Status}}'
```

Expect `db`, `redis`, `web` as `healthy`; `engine`, `worker`, `frontend`,
`proxy` as `Up`. Then health-check through the proxy:

```bash
curl -sk http://localhost:8080/health
# {"status":"ok","redis":true,"kis_env":"vts","paper_trading":true}
```

## Run: API smoke (agent path for backend changes)

```bash
bash .claude/skills/run-quantfolio/smoke.sh
```

Registers a throwaway user, logs in, and hits authed `/api/auth/me` through the
proxy. Prints `SMOKE OK` and exits 0 on success; exits non-zero at the first
failing step. Target the web container directly with
`BASE=http://localhost:8000 bash .claude/skills/run-quantfolio/smoke.sh`.

**Auth contract worth knowing:** `/api/auth/register` takes JSON
(`{email,password}` → 201). `/api/auth/login` takes **form** fields with the
email in `username` (OAuth2 form) → 200 + HttpOnly session cookie. Password must
be ≥ 8 chars.

## Run: UI flow (agent path for frontend changes)

Drive the running app with the **Playwright MCP** tools (this session used them;
no `chromium-cli` is installed on this host). Full verified login flow:

1. `browser_navigate` → `http://localhost:8080/` — unauthenticated, redirects to
   `/login`.
2. `browser_snapshot` to get element refs for the 이메일 / 비밀번호 textboxes and
   로그인 button.
3. `browser_type` a registered email into the 이메일 box, `smoketest123` into
   비밀번호 (create the user first via `smoke.sh` or the register curl above).
4. `browser_click` 로그인 → redirects to `/dashboard`.
5. `browser_take_screenshot` — the dashboard shows the account email, KIS link
   status, and the top nav (대시보드 / 전략 / 공유 전략 / 실시간 / 지표 / 추천 /
   스크리너 / 설정 / 용어집 / 로그아웃).

Screenshots land in `.playwright-mcp/` (or repo root if a bare filename is used;
delete strays before committing).

## Run: human path

```bash
docker compose up -d --build
```

Open <http://localhost:8080> (app), <http://localhost:8080/docs> (Swagger),
<http://localhost:8080/health>. Register at `/login`. For phone/external access
over WireGuard, see README §5 — not needed for local verification.

## Backend code change → restart, don't wait

The `web` container runs `uvicorn` **without `--reload`** (24/7 operation). After
editing `backend/`, changes do NOT hot-reload:

```bash
docker compose restart web       # (and engine/worker if you touched their code)
```

The frontend (`npm run dev`) *does* hot-reload; new frontend npm packages must be
installed **inside** the container (anonymous `node_modules` volume), not on the
host.

## Gotchas

- **Two "errors" on the login page are benign:** `401 /api/auth/me` (nobody's
  logged in yet) and `404 /favicon.ico`. Not failures.
- **Login is form-encoded, not JSON.** Posting JSON to `/api/auth/login` fails;
  the email goes in the `username` field.
- **Everything is behind `:8080`.** `web:8000` and `frontend:3000` are bound to
  `127.0.0.1` for local debugging only; test against `:8080` to exercise the
  real proxy routing.
- **`paper_trading:true` / `kis_env:vts`** in health means it's on the KIS
  *모의투자* (paper) domain — safe, no real orders.
- **DB port 5432 / Redis 6379** also bind `127.0.0.1` only; `docker compose exec`
  is the way in from a script.

## Troubleshooting

- `curl ... /health` refuses / times out → stack isn't up. `docker compose ps`;
  if empty, `docker compose up -d --build`.
- `smoke.sh` fails at step 1 → same as above (it says so).
- Login works via curl but `/me` 401s → the session cookie wasn't sent; make
  sure you reuse the same cookie jar (`-c` then `-b`), as `smoke.sh` does.
- `alembic` errors about missing tables on a fresh DB → run
  `docker compose exec -T web alembic upgrade head`.
