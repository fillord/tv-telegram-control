#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${TV_BOT_DEPLOY_TARGET:?Set TV_BOT_DEPLOY_TARGET, for example admin@10.0.0.10}"
: "${TV_BOT_REMOTE_DIR:?Set TV_BOT_REMOTE_DIR, for example /opt/tv-telegram-control}"

TARGET="${TV_BOT_DEPLOY_TARGET}"
REMOTE_DIR="${TV_BOT_REMOTE_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${REMOTE_DIR}/backups/${STAMP}"
ROLLBACK_TAG="tv-telegram-control:pre-deploy-${STAMP}"
BACKUP_READY=0

if [[ ! "${TARGET}" =~ ^[A-Za-z0-9._@:-]+$ ]]; then
  echo "TV_BOT_DEPLOY_TARGET contains unsupported characters" >&2
  exit 2
fi
if [[ ! "${REMOTE_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "${REMOTE_DIR}" == *".."* ]]; then
  echo "TV_BOT_REMOTE_DIR must be a safe absolute path" >&2
  exit 2
fi
case "${REMOTE_DIR}" in
  /|/home|/root|/opt|/srv|/var)
    echo "TV_BOT_REMOTE_DIR is too broad" >&2
    exit 2
    ;;
esac

rollback() {
  echo "Deployment failed; restoring ${BACKUP_DIR}" >&2
  ssh "${TARGET}" "set -eu; \
    if [ -d '${BACKUP_DIR}/project' ]; then cp -a '${BACKUP_DIR}/project/.' '${REMOTE_DIR}/'; fi; \
    if [ -d '${BACKUP_DIR}/runtime' ]; then cp -a '${BACKUP_DIR}/runtime/.' '${REMOTE_DIR}/'; fi; \
    if docker image inspect '${ROLLBACK_TAG}' >/dev/null 2>&1; then \
      cd '${REMOTE_DIR}'; \
      image_name=\$(docker compose config --images | head -n 1); \
      docker image tag '${ROLLBACK_TAG}' \"\${image_name}\"; \
      docker compose up -d --force-recreate --no-build; \
    fi"
}

rollback_on_error() {
  status=$?
  trap - ERR
  if [[ "${BACKUP_READY}" == "1" ]]; then
    rollback || true
  fi
  exit "${status}"
}

trap rollback_on_error ERR

cd "${ROOT_DIR}"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

ssh "${TARGET}" "set -eu; \
  test -d '${REMOTE_DIR}'; \
  test -f '${REMOTE_DIR}/data/config.json'; \
  test -s '${REMOTE_DIR}/secrets/telegram_token'; \
  test -f '${REMOTE_DIR}/.env'; \
  cd '${REMOTE_DIR}'; \
  docker compose version >/dev/null; \
  docker compose config --quiet; \
  mkdir -p '${BACKUP_DIR}/project' '${BACKUP_DIR}/runtime'; \
  for item in Dockerfile README.md compose.yaml config.example.json tv_bot.py tv_control scripts; do \
    if [ -e \"\${item}\" ]; then cp -a \"\${item}\" '${BACKUP_DIR}/project/'; fi; \
  done; \
  for item in data secrets adb .env; do cp -a \"\${item}\" '${BACKUP_DIR}/runtime/'; done; \
  container_id=\$(docker compose ps -q tv-telegram-control); \
  if [ -n \"\${container_id}\" ]; then \
    image_id=\$(docker inspect --format '{{.Image}}' \"\${container_id}\"); \
    docker image tag \"\${image_id}\" '${ROLLBACK_TAG}'; \
  fi"
BACKUP_READY=1

rsync -av \
  .dockerignore \
  .env.example \
  .gitignore \
  Dockerfile \
  README.md \
  compose.yaml \
  config.example.json \
  tv_bot.py \
  "${TARGET}:${REMOTE_DIR}/"
rsync -av tests tv_control scripts "${TARGET}:${REMOTE_DIR}/"

ssh "${TARGET}" "set -eu; \
  cd '${REMOTE_DIR}'; \
  python3 scripts/migrate_token.py prepare data/config.json secrets/telegram_token; \
  chmod 700 data adb logs secrets; \
  chmod 600 .env data/config.json secrets/telegram_token; \
  if [ -f adb/adbkey ]; then chmod 600 adb/adbkey; fi; \
  docker compose config --quiet; \
  docker compose build; \
  docker compose run --rm tv-telegram-control python3 /app/tv_bot.py --check-config; \
  docker compose up -d"

for _ in $(seq 1 18); do
  status="$(ssh "${TARGET}" "cd '${REMOTE_DIR}'; container_id=\$(docker compose ps -q tv-telegram-control); if [ -z \"\${container_id}\" ]; then echo missing; else docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \"\${container_id}\"; fi")"
  if [[ "${status}" == "healthy" ]]; then
    ssh "${TARGET}" "set -eu; cd '${REMOTE_DIR}'; \
      python3 scripts/migrate_token.py finalize data/config.json secrets/telegram_token; \
      chmod 600 data/config.json secrets/telegram_token; \
      docker compose ps"
    echo "Deployment completed. Backup: ${BACKUP_DIR}"
    trap - ERR
    exit 0
  fi
  if [[ "${status}" == "unhealthy" || "${status}" == "missing" ]]; then
    ssh "${TARGET}" "cd '${REMOTE_DIR}' && docker compose logs --tail=100 --no-color" || true
    rollback || true
    exit 1
  fi
  sleep 10
done

ssh "${TARGET}" "cd '${REMOTE_DIR}' && docker compose logs --tail=100 --no-color" || true
rollback || true
exit 1
