#!/usr/bin/env bash
# QuantFolio API smoke test — headless, no browser needed.
#
# Drives the *running* stack through the Caddy proxy (:8080):
#   health  ->  register  ->  login (sets session cookie)  ->  authed /me
#
# This is the agent path for verifying a backend/auth/API change without a
# browser. For UI changes, drive the frontend with the Playwright MCP instead
# (see SKILL.md "Run: UI flow").
#
# Usage:
#   bash .claude/skills/run-quantfolio/smoke.sh          # against proxy :8080
#   BASE=http://localhost:8000 bash .../smoke.sh         # hit web directly
#
# Exit 0 = all steps passed. Non-zero = first failing step.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
EMAIL="smoke_$(date +%s)_$$@example.com"
PW="smoketest123"
COOKIES="$(mktemp)"
trap 'rm -f "$COOKIES"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "== 1/4 health ($BASE/health)"
HEALTH="$(curl -sk --max-time 10 "$BASE/health")" || fail "health unreachable — is the stack up? (docker compose ps)"
echo "   $HEALTH"
echo "$HEALTH" | grep -q '"status":"ok"' || fail "health not ok: $HEALTH"

echo "== 2/4 register ($EMAIL)"
CODE="$(curl -sk --max-time 10 -o /dev/null -w '%{http_code}' \
  -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}")"
[ "$CODE" = "201" ] || fail "register expected 201, got $CODE"
echo "   201 Created"

echo "== 3/4 login (form: username=email)"
CODE="$(curl -sk --max-time 10 -c "$COOKIES" -o /dev/null -w '%{http_code}' \
  -X POST "$BASE/api/auth/login" \
  -d "username=$EMAIL&password=$PW")"
[ "$CODE" = "200" ] || fail "login expected 200, got $CODE"
grep -qi 'session' "$COOKIES" || fail "login did not set a session cookie"
echo "   200 OK (session cookie set)"

echo "== 4/4 authed /api/auth/me"
ME="$(curl -sk --max-time 10 -b "$COOKIES" "$BASE/api/auth/me")" || fail "/me request failed"
echo "   $ME"
echo "$ME" | grep -q "\"email\":\"$EMAIL\"" || fail "/me returned wrong/no user: $ME"

echo
echo "SMOKE OK — health, register, login, authed /me all passed."
