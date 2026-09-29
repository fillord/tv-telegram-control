#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${TV_BOT_DEPLOY_TARGET:-yola@100.68.109.75}"
REMOTE_DIR="${TV_BOT_REMOTE_DIR:-/home/yola/docker/tv-telegram-control}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/home/yola/backups/tv-telegram-control/${STAMP}"
ROLLBACK_TAG="tv-telegram-control:pre-deploy-${STAMP}"

rollback() {
  echo "Rolling back to ${ROLLBACK_TAG}" >&2
  ssh "${TARGET}" "docker image tag '${ROLLBACK_TAG}' tv-telegram-control-tv-telegram-control:latest && \
    cd '${REMOTE_DIR}' && docker compose up -d --force-recreate --no-build"
}

cd "${ROOT_DIR}"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

ssh "${TARGET}" "set -eu; umask 077; \
  mkdir -p '${BACKUP_DIR}'; \
  cp -a '${REMOTE_DIR}/data/config.json' '${REMOTE_DIR}/compose.yaml' \
    '${REMOTE_DIR}/Dockerfile' '${REMOTE_DIR}/tv_bot.py' '${BACKUP_DIR}/'; \
  cp -a '${REMOTE_DIR}/adb' '${BACKUP_DIR}/'; \
  image_id=\$(docker inspect --format '{{.Image}}' tv-telegram-control); \
  docker image tag \"\${image_id}\" '${ROLLBACK_TAG}'"

ssh "${TARGET}" "mkdir -p '${REMOTE_DIR}/tests'"
rsync -av \
  .dockerignore \
  .gitignore \
  Dockerfile \
  README.md \
  compose.yaml \
  config.example.json \
  tv_bot.py \
  "${TARGET}:${REMOTE_DIR}/"
rsync -av tests/test_tv_bot.py "${TARGET}:${REMOTE_DIR}/tests/"

ssh "${TARGET}" "cd '${REMOTE_DIR}' && \
  chmod 700 data adb logs && \
  chmod 600 data/config.json adb/adbkey && \
  docker compose build && \
  docker compose run --rm tv-telegram-control python3 /app/tv_bot.py --check-config && \
  docker compose up -d"

for _ in $(seq 1 18); do
  status="$(ssh "${TARGET}" "docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' tv-telegram-control")"
  if [[ "${status}" == "healthy" ]]; then
    ssh "${TARGET}" "cd '${REMOTE_DIR}' && docker compose ps"
    exit 0
  fi
  if [[ "${status}" == "unhealthy" ]]; then
    ssh "${TARGET}" "cd '${REMOTE_DIR}' && docker compose logs --tail=100 --no-color"
    rollback
    echo "Deployment failed: container is unhealthy" >&2
    exit 1
  fi
  sleep 10
done

ssh "${TARGET}" "cd '${REMOTE_DIR}' && docker compose logs --tail=100 --no-color"
rollback
echo "Deployment failed: health check did not become healthy in time" >&2
exit 1
