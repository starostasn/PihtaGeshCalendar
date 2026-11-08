#!/usr/bin/env bash
# Собирает портативную версию для Windows x64: dist/PihtaGeshCalendar-windows.zip
# Внутри — официальный CPython из NuGet, зависимости и приложение. Установка Python не нужна.
#
#   PYTHON=.venv/bin/python scripts/build_windows.sh
#
# PYTHON — интерпретатор с установленными зависимостями проекта (нужен для collectstatic).
set -euo pipefail

PY_VERSION="${PY_VERSION:-3.13.16}"
PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build/windows"
OUT="$BUILD/PihtaGeshCalendar"
CACHE="$ROOT/build/cache"

rm -rf "${BUILD:?}"
mkdir -p "$OUT/app" "$CACHE" "$ROOT/dist"

echo "1/5 Python $PY_VERSION для Windows (NuGet)"
NUPKG="$CACHE/python.$PY_VERSION.nupkg"
if [ ! -f "$NUPKG" ]; then
  curl -fsSL -o "$NUPKG" "https://api.nuget.org/v3-flatcontainer/python/$PY_VERSION/python.$PY_VERSION.nupkg"
fi
unzip -q "$NUPKG" 'tools/*' -d "$BUILD/nupkg"
mv "$BUILD/nupkg/tools" "$OUT/python"
rm -rf "$BUILD/nupkg"
# Лишнее для работы сервиса
rm -rf "$OUT/python/Lib/test" "$OUT/python/Lib/idlelib" "$OUT/python/Lib/tkinter" \
       "$OUT/python/Lib/turtledemo" "$OUT/python/Lib/ensurepip"

echo "2/5 Зависимости (колёса win_amd64)"
"$PYTHON" -m pip install --quiet --disable-pip-version-check \
  --target "$OUT/python/Lib/site-packages" \
  --platform win_amd64 --python-version "${PY_VERSION%.*}" --implementation cp \
  --only-binary=:all: --no-compile \
  -r "$ROOT/requirements.txt"
rm -rf "$OUT/python/Lib/site-packages/bin"

echo "3/5 Приложение"
cp -r "$ROOT/manage.py" "$ROOT/run_local.py" "$ROOT/config" "$ROOT/booking" "$OUT/app/"
find "$OUT/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -f "$OUT/app/booking/tests.py"
(cd "$OUT/app" && DEBUG=0 SECRET_KEY=build-only DATA_DIR="$BUILD/tmpdata" "$PYTHON" manage.py collectstatic --noinput -v0)
rm -rf "$BUILD/tmpdata"

echo "4/5 Файлы запуска"
sed 's/$/\r/' > "$OUT/run_calendar.bat" <<'BAT'
@echo off
title PihtaGeshCalendar
cd /d "%~dp0"
set "DATA_DIR=%~dp0data"
rem -I: не зависеть от другого Python на компьютере; -X utf8: кириллица в файлах и консоли
"%~dp0python\python.exe" -I -X utf8 "%~dp0app\run_local.py"
echo.
pause
BAT
{ printf '\xef\xbb\xbf'; sed 's/$/\r/' <<'TXT'
КАЛЕНДАРЬ ГОСТЕВОГО ДОМА — версия для Windows
=============================================

Запуск
  1. Распакуйте архив в любую папку (например, C:\PihtaGeshCalendar).
     Запускать прямо из архива нельзя.
  2. Дважды щёлкните run_calendar.bat.
  3. При первом запуске придумайте логин и пароль (пароль при вводе не отображается).
  4. Откроется браузер с календарём: http://127.0.0.1:8000
     Пока календарь работает, окно с чёрным фоном должно оставаться открытым.

Если Windows покажет «Система Windows защитила ваш компьютер» — нажмите
«Подробнее» → «Выполнить в любом случае» (файл не подписан).

Данные
  Всё хранится в папке data рядом с run_calendar.bat:
    data\db.sqlite3     — база (брони, цены, площадки). Копируйте её для резервной копии.
    data\settings.txt   — настройки: часовой пояс, Telegram-уведомления, порт.

Обновление
  Распакуйте новую версию и перенесите в неё папку data из старой.

Ограничение
  Брони с Авито и других площадок загружаются автоматически (каждые 10 минут, пока
  программа открыта). Но площадки не могут забрать календарь с домашнего компьютера:
  даты, закрытые здесь, на Авито сами не закроются. Для полной двусторонней
  синхронизации сервис нужно разместить на сервере (см. README.md в репозитории).
TXT
} > "$OUT/README.txt"

echo "5/5 Архив"
rm -f "$ROOT/dist/PihtaGeshCalendar-windows.zip"
(cd "$BUILD" && zip -qr9 "$ROOT/dist/PihtaGeshCalendar-windows.zip" PihtaGeshCalendar)
ls -lh "$ROOT/dist/PihtaGeshCalendar-windows.zip"
