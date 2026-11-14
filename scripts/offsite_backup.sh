#!/usr/bin/env bash
# Ежедневная резервная копия базы календаря ВНЕ сервера: restic шифрует копию паролем
# и отправляет в S3-хранилище (Yandex Object Storage, облачное хранилище reg.ru и т.п.).
# Хранит: 14 ежедневных, 8 еженедельных и 12 ежемесячных копий.
# При ошибке присылает сообщение в Telegram (если бот настроен в .env календаря).
#
# Настройки — в файле /root/.pihta-backup.env (права 600), пример: scripts/backup.env.example
# Запуск вручную:  bash /opt/guesthouse/scripts/offsite_backup.sh
# Из cron:         30 3 * * * bash /opt/guesthouse/scripts/offsite_backup.sh >> /var/log/pihta-backup.log 2>&1
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${BACKUP_ENV_FILE:-/root/.pihta-backup.env}"
STEP="подготовка"

notify() {
  (cd "$APP_DIR" && docker compose exec -T -e MSG="$1" web \
    python manage.py shell -c "import os; from booking.notify import send_telegram; send_telegram(os.environ['MSG'])") \
    >/dev/null 2>&1 || true
}
on_error() {
  echo "$(date '+%F %T') ОШИБКА на шаге «$STEP»"
  notify "⚠️ Резервная копия календаря не создана (шаг «$STEP»). Подробности: /var/log/pihta-backup.log на сервере."
}
trap on_error ERR

[ -f "$ENV_FILE" ] || { STEP="настройки"; echo "Нет файла настроек $ENV_FILE (см. scripts/backup.env.example)"; false; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
: "${RESTIC_REPOSITORY:?не задан RESTIC_REPOSITORY}" "${RESTIC_PASSWORD:?не задан RESTIC_PASSWORD}"

cd "$APP_DIR"

STEP="копия базы"
docker compose exec -T web python manage.py backup_db --keep 7

STEP="подключение к хранилищу"
if ! restic cat config >/dev/null 2>&1; then
  echo "Хранилище ещё не создано — создаю"
  restic init
fi

STEP="отправка копии"
restic backup "$APP_DIR/data/backups" --tag db --host pihta-calendar --quiet

STEP="удаление старых копий"
restic forget --tag db --host pihta-calendar --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune --quiet

STEP="проверка хранилища"
restic check --quiet

echo "$(date '+%F %T') OK: $(restic snapshots --tag db --compact --quiet | grep -c '^[0-9a-f]\{8\} ') копий в хранилище"
