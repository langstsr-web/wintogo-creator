#!/usr/bin/env bash
# Сборка deb-пакета WinToGo Creator.
# Результат: build/wintogo-creator_<версия>_all.deb
#
# Ставит приложение в /usr/share/wintogo (+ команда wintogo и ярлык в меню).
set -euo pipefail

VERSION="0.1.0"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$DIR/build/wintogo-creator_${VERSION}_all"

rm -rf "$DIR/build"
mkdir -p \
    "$BUILD/DEBIAN" \
    "$BUILD/usr/share/wintogo/assets/bcd" \
    "$BUILD/usr/bin" \
    "$BUILD/usr/share/applications" \
    "$BUILD/usr/share/icons/hicolor/scalable/apps" \
    "$BUILD/usr/share/doc/wintogo"

# --- файлы приложения
install -m 644 "$DIR/wintogo.py" "$BUILD/usr/share/wintogo/wintogo.py"
install -m 644 "$DIR/assets/wintogo.svg" "$BUILD/usr/share/wintogo/assets/wintogo.svg"
install -m 644 "$DIR/assets/wintogo.svg" \
    "$BUILD/usr/share/icons/hicolor/scalable/apps/wintogo.svg"
install -m 644 "$DIR/README.md" "$BUILD/usr/share/doc/wintogo/README.md"
# шаблон BCD, если появится (иначе каталог просто пустой)
[ -f "$DIR/assets/bcd/BCD" ] && install -m 644 "$DIR/assets/bcd/BCD" \
    "$BUILD/usr/share/wintogo/assets/bcd/BCD" || true
[ -f "$DIR/assets/bcd/BCD.json" ] && install -m 644 "$DIR/assets/bcd/BCD.json" \
    "$BUILD/usr/share/wintogo/assets/bcd/BCD.json" || true

cat > "$BUILD/usr/bin/wintogo" <<'EOF'
#!/bin/sh
exec python3 /usr/share/wintogo/wintogo.py "$@"
EOF
chmod 755 "$BUILD/usr/bin/wintogo"

cat > "$BUILD/usr/share/applications/wintogo.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=WinToGo Creator
GenericName=Windows на внешний диск
Comment=Создание загрузочного внешнего диска с Windows из Linux
Exec=wintogo
Icon=wintogo
Terminal=false
Categories=System;Utility;Qt;
Keywords=windows;usb;wintogo;ssd;bootable;загрузочный;флешка;
EOF

# --- метаданные пакета
cat > "$BUILD/DEBIAN/control" <<EOF
Package: wintogo-creator
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6, wimtools, gdisk, dosfstools,
 ntfs-3g, policykit-1, udisks2, parted
Recommends: libhivex-bin
Maintainer: Stanislav <gronowesuzanne@mail.com>
Description: Create bootable external Windows drives from Linux
 WinToGo Creator deploys Windows 10/11 from an ISO onto an external
 USB drive so it boots on real hardware (a Linux counterpart to Rufus
 "Windows To Go" / WinToUSB). Uses wimlib to apply the image and
 copies the Windows EFI boot loader. PyQt6 GUI with strong safeguards
 against writing to the system disk.
EOF

dpkg-deb --build --root-owner-group "$BUILD" \
    "$DIR/build/wintogo-creator_${VERSION}_all.deb"
echo
echo "Готово: $DIR/build/wintogo-creator_${VERSION}_all.deb"
echo "Установка: sudo apt install ./build/wintogo-creator_${VERSION}_all.deb"
