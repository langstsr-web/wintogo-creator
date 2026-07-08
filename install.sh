#!/usr/bin/env bash
# Регистрирует WinToGo Creator в меню приложений текущего пользователя.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

sed -e "s|^Exec=.*|Exec=python3 \"$DIR/wintogo.py\"|" \
    -e "s|^Icon=.*|Icon=$DIR/assets/wintogo.svg|" \
    "$DIR/wintogo.desktop" > "$APPS/wintogo.desktop"

update-desktop-database "$APPS" 2>/dev/null || true
echo "Готово: WinToGo Creator добавлен в меню приложений."
echo "Запуск из терминала: python3 \"$DIR/wintogo.py\""
