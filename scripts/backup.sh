#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${1:-${ROOT_DIR}/backups/${STAMP}}"

if [[ -e "${BACKUP_DIR}" ]]; then
  echo "Backup destination already exists: ${BACKUP_DIR}" >&2
  exit 2
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

for item in data secrets adb .env compose.yaml; do
  if [[ -e "${ROOT_DIR}/${item}" ]]; then
    cp -a "${ROOT_DIR}/${item}" "${BACKUP_DIR}/"
  fi
done

echo "Backup created: ${BACKUP_DIR}"
