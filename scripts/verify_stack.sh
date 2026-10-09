#!/usr/bin/env bash
# Boot the real containers and prove the deployable works.
#
# The unit suite runs workflows through Temporal's time-skipping environment and
# never calls run_worker(); scripts/run_local.py drives the pipeline in-process
# and never touches a worker at all. Both stayed green while the Dockerised
# worker crash-looped on every start. This script closes that gap: it runs the
# images that would actually ship, and fails if the worker cannot boot or a
# crawl cannot reach the index.
set -uo pipefail

# Dashboards and the Temporal web UI are for humans, not for this check.
SERVICES="opensearch postgresql temporal mock-directory mock-enrichment worker api"
FAILED=0
# Holds `compose up` output so a registry rate limit can be told apart from a
# broken stack. Removed by cleanup(), which owns the one EXIT trap -- bash
# keeps a single handler per signal, so a second `trap ... EXIT` here would
# silently replace the teardown.
UP_LOG="$(mktemp)"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }

cleanup() {
  if [ "$FAILED" -ne 0 ]; then
    log "Failure diagnostics"
    echo "--- container states ---"
    docker compose ps
    for svc in worker api temporal; do
      echo "--- $svc logs (last 40) ---"
      docker compose logs --tail=40 "$svc" 2>&1 || true
    done
  fi
  if [ -n "${KEEP_STACK:-}" ]; then
    log "Leaving the stack up (KEEP_STACK set)"
  elif [ -n "${CI:-}" ]; then
    log "Tearing down (CI: volumes removed)"
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  else
    # Locally, stop the containers but keep the volumes: a developer running
    # this should not lose whatever is already in their index.
    log "Tearing down (volumes kept -- set CI=1 to wipe them)"
    docker compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -f "$UP_LOG"
  exit "$FAILED"
}
trap cleanup EXIT

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not reachable. Start Docker and re-run." >&2
  FAILED=1
  exit 1
fi

log "Starting stack"
# Teed rather than captured: a build takes a minute and swallowing its progress
# to inspect it afterwards turns that minute into a silent hang. `pipefail` is
# set above, so the compose exit status still decides this.
if ! docker compose up -d --build $SERVICES 2>&1 | tee "$UP_LOG"; then
  # Name a registry rate limit as such. It arrives as a bare "compose up
  # failed" otherwise, which reads like a broken stack and sends the next
  # person to read container logs that do not exist -- nothing started, so
  # every diagnostic below is empty, which is its own confusing signal.
  if grep -qiE 'toomanyrequests|rate limit' "$UP_LOG"; then
    fail "registry rate limit -- no image was pulled, the stack is not at fault"
    # stdout, like fail(), so the explanation stays under the headline it
    # explains. On stderr the two streams buffer separately and CI interleaved
    # them backwards -- the advice printed above the failure it was advising on.
    echo "  Docker Hub limits unauthenticated pulls per IP per 6h, and CI runners"
    echo "  share IPs. Set DOCKERHUB_USERNAME/DOCKERHUB_TOKEN (repo secrets in CI,"
    echo "  or 'docker login' locally). Re-running will not clear it."
  else
    fail "compose up failed"
  fi
  exit 1
fi

log "Waiting for the API to report ready"
ready=0
for _ in $(seq 1 60); do
  if curl -sf --max-time 4 http://localhost:8000/readyz 2>/dev/null | grep -q '"ready": *true'; then
    ready=1; break
  fi
  sleep 5
done
[ "$ready" -eq 1 ] && pass "/readyz reports ready" || { fail "/readyz never became ready"; exit 1; }

# The check that would have caught the shutdown_event bug. A crash-looping
# worker still "exists", so container presence proves nothing -- what matters is
# that it started and stayed up.
log "Verifying the workers actually booted"
sleep 5
worker_logs="$(docker compose logs worker 2>&1)"

if grep -q "worker.starting" <<<"$worker_logs"; then
  pass "worker reported worker.starting"
else
  fail "worker never logged worker.starting"
fi

if grep -qE "Traceback|TypeError|worker.stopped" <<<"$worker_logs"; then
  fail "worker log contains a crash: $(grep -oE 'TypeError[^\"]*|Traceback' <<<"$worker_logs" | head -1)"
else
  pass "worker log is free of tracebacks"
fi

restarts="$(docker inspect --format '{{.RestartCount}}' "$(docker compose ps -q worker | head -1)" 2>/dev/null || echo 0)"
[ "${restarts:-0}" -eq 0 ] && pass "worker has not restarted" || fail "worker restarted ${restarts}x (crash loop)"

running="$(docker compose ps --status running --services 2>/dev/null | grep -cx worker || true)"
[ "${running:-0}" -ge 1 ] && pass "worker is running" || fail "worker is not running"

log "Running a crawl through Temporal"
if ! curl -sf --max-time 20 -X POST localhost:8000/ingest/crawl \
      -H 'content-type: application/json' \
      -d '{"categories":["software","logistics"],"max_pages":3}' >/dev/null; then
  fail "crawl request was rejected"; exit 1
fi
pass "crawl workflow accepted"

# Poll OpenSearch directly. Querying workflow status uses Temporal's consistent
# query, which backs up and then fails if the workflow is wedged.
log "Waiting for documents to reach the index"
docs=0
for _ in $(seq 1 36); do
  docs="$(curl -sf --max-time 5 http://localhost:9200/companies/_count 2>/dev/null \
          | sed -n 's/.*"count":\([0-9]*\).*/\1/p')"
  [ "${docs:-0}" -gt 0 ] 2>/dev/null && break
  sleep 5
done
[ "${docs:-0}" -gt 0 ] 2>/dev/null \
  && pass "$docs documents indexed" \
  || fail "no documents indexed -- the pipeline did not complete"

log "Checking the read path"
total="$(curl -sf --max-time 8 "http://localhost:8000/search?q=" 2>/dev/null \
         | sed -n 's/.*"total":\([0-9]*\).*/\1/p')"
[ "${total:-0}" -gt 0 ] 2>/dev/null \
  && pass "/search returns $total canonical records" \
  || fail "/search returned nothing"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://localhost:8000/ui)"
[ "$code" = "200" ] && pass "/ui serves the console" || fail "/ui returned $code"

log "Done"
