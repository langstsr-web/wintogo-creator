#!/usr/bin/env python3
"""WinToGo Creator — создание загрузочного внешнего диска с Windows из Linux.

Разворачивает Windows 10/11 из ISO на внешний USB-SSD так, чтобы система
загружалась с него на реальном железе (аналог Rufus «Windows To Go» / WinToUSB,
которых под Linux нет). Работает через нативные инструменты Linux: wimlib для
применения образа, parted/mkfs для разметки, копирование EFI-загрузчика.

Один файл, два режима:
  python3 wintogo.py            — графическое окно (обычный пользователь)
  python3 wintogo.py --core F   — привилегированный конвейер записи (запускается
                                  самим GUI через pkexec; F — JSON с параметрами)

Зависимости GUI:   python3-pyqt6
Зависимости ядра:  wimtools (wimlib-imagex), gdisk, dosfstools, ntfs-3g,
                   и по возможности libhivex-bin/python3-hivex (для BCD).

ВНИМАНИЕ: приложение перезаписывает выбранный диск целиком. Встроены
предохранители против записи в системный/примонтированный диск, но
окончательная ответственность за выбор устройства — на пользователе.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "WinToGo Creator"
APP_ID = "wintogo"
ORG = "WinToGo"
VERSION = "0.2.3"

# Каталоги с системными утилитами — pkexec обрезает PATH, поэтому дополняем явно.
SBIN_PATHS = ["/usr/sbin", "/sbin", "/usr/local/sbin"]
APP_DIR = Path(__file__).resolve().parent
BCD_TEMPLATE = APP_DIR / "assets" / "bcd" / "BCD"          # опциональный шаблон
BCD_TEMPLATE_META = APP_DIR / "assets" / "bcd" / "BCD.json"  # его GUID-плейсхолдеры

# BCD-SYS (github.com/jpz4085/BCD-SYS, GPL-3.0) — вендорится как отдельная
# GPL-программа и вызывается через subprocess (наш код остаётся MIT). Создаёт
# BCD и копирует EFI-загрузчик, как bcdboot, но из Linux.
BCD_SYS_DIR = APP_DIR / "third_party" / "bcd-sys"
# peres (пакет pev) намеренно НЕ в списке: на свежих Ubuntu его нет в репозитории,
# а в нашем сценарии (всегда свежий диск) BCD-SYS его не вызывает — требование
# снимается в run_bcd_sys.
BCD_SYS_TOOLS = ["hivexsh", "hivexregedit", "setfattr", "fatattr", "xxd"]

# Размеры разметки
ESP_SIZE_MIB = 300          # системный EFI-раздел (FAT32)
ALIGN_START_MIB = 1         # выравнивание первого раздела


# ───────────────────────────── общие помощники ──────────────────────────────

def _augmented_env():
    env = dict(os.environ)
    path = env.get("PATH", "")
    parts = path.split(":") if path else []
    for p in SBIN_PATHS:
        if p not in parts:
            parts.append(p)
    env["PATH"] = ":".join(parts)
    env["LC_ALL"] = "C"
    return env


def which(name):
    """Найти утилиту с учётом sbin-каталогов."""
    env = _augmented_env()
    return shutil.which(name, path=env["PATH"])


def run(cmd, check=True, capture=True, text=True, input=None, timeout=None):
    """Запуск команды. Возвращает CompletedProcess."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=text,
        input=input,
        timeout=timeout,
        env=_augmented_env(),
    )


def human_size(nbytes):
    if not nbytes:
        return "—"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    v = float(nbytes)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.0f} {u}" if u in ("Б", "КБ") else f"{v:.1f} {u}"
        v /= 1024


# ───────────────────────── перечисление устройств ───────────────────────────

def _lsblk_tree():
    out = run(["lsblk", "-J", "-b", "-O"]).stdout
    return json.loads(out).get("blockdevices", [])


def _collect_mountpoints(node):
    mps = [m for m in (node.get("mountpoints") or []) if m]
    for ch in node.get("children", []) or []:
        mps.extend(_collect_mountpoints(ch))
    return mps


def system_disks():
    """Диски, на которых лежат /, /boot, /boot/efi — их трогать нельзя."""
    disks = set()
    for mp in ("/", "/boot", "/boot/efi", "/home"):
        try:
            src = run(["findmnt", "-n", "-o", "SOURCE", mp], check=False).stdout.strip()
        except Exception:
            src = ""
        if not src or not src.startswith("/dev/"):
            continue
        # Разворачиваем до диска верхнего уровня (учёт luks/lvm — по цепочке PKNAME).
        cur = src
        for _ in range(6):
            pk = run(["lsblk", "-no", "PKNAME", cur], check=False).stdout.strip().splitlines()
            pk = [p for p in pk if p]
            if not pk:
                break
            cur = "/dev/" + pk[0]
        disks.add(cur)
    return disks


def list_disks(usb_only=True):
    """Список дисков-кандидатов с метаданными и флагом системного диска."""
    sysd = system_disks()
    result = []
    for d in _lsblk_tree():
        if d.get("type") != "disk":
            continue
        path = d.get("path") or ("/dev/" + d.get("name", ""))
        is_usb = (d.get("tran") == "usb") or bool(d.get("hotplug"))
        if usb_only and not is_usb:
            continue
        mps = _collect_mountpoints(d)
        model = (d.get("model") or d.get("vendor") or "").strip()
        result.append({
            "path": path,
            "name": d.get("name"),
            "size": int(d.get("size") or 0),
            "model": model or "неизвестно",
            "tran": (d.get("tran") or "?"),
            "hotplug": bool(d.get("hotplug")),
            "removable": bool(d.get("rm")),
            "readonly": bool(d.get("ro")),
            "serial": (d.get("serial") or "").strip(),
            "mountpoints": mps,
            "is_system": path in sysd,
        })
    return result


# ───────────────────────── инспекция ISO / WIM ──────────────────────────────

class IsoMount:
    """Контекст: смонтировать ISO только для чтения (udisks, без root)."""

    def __init__(self, iso_path):
        self.iso = str(iso_path)
        self.loop = None
        self.mountpoint = None

    def __enter__(self):
        out = run(["udisksctl", "loop-setup", "-r", "-f", self.iso]).stdout
        # "Mapped file <iso> as /dev/loopN."
        for tok in out.replace(".", " ").split():
            if tok.startswith("/dev/loop"):
                self.loop = tok
        if not self.loop:
            raise RuntimeError(f"udisks не вернул loop-устройство: {out!r}")
        time.sleep(0.3)
        mout = run(["udisksctl", "mount", "-b", self.loop]).stdout
        # "Mounted /dev/loopN at /media/.../..."
        if " at " in mout:
            self.mountpoint = mout.split(" at ", 1)[1].strip().rstrip(".")
        if not self.mountpoint or not os.path.isdir(self.mountpoint):
            raise RuntimeError(f"не удалось смонтировать ISO: {mout!r}")
        return self

    def __exit__(self, *exc):
        try:
            if self.loop:
                run(["udisksctl", "unmount", "-b", self.loop], check=False)
                run(["udisksctl", "loop-delete", "-b", self.loop], check=False)
        except Exception:
            pass


def find_wim(mount_root):
    """Путь к install.wim / install.esd внутри смонтированного ISO."""
    sources = os.path.join(mount_root, "sources")
    for name in ("install.wim", "install.esd"):
        p = os.path.join(sources, name)
        if os.path.exists(p):
            return p
    # регистр может отличаться на некоторых сборках
    if os.path.isdir(sources):
        for f in os.listdir(sources):
            if f.lower() in ("install.wim", "install.esd"):
                return os.path.join(sources, f)
    return None


def parse_wim_editions(wim_path):
    """Список редакций из `wimlib-imagex info`: [{index, name}]."""
    if not which("wimlib-imagex"):
        raise RuntimeError("не найден wimlib-imagex (установите пакет wimtools)")
    out = run(["wimlib-imagex", "info", wim_path]).stdout
    editions, cur = [], {}
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("index:"):
            if cur.get("index"):
                editions.append(cur)
            cur = {"index": s.split(":", 1)[1].strip(), "name": ""}
        elif s.lower().startswith("name:") and "index" in cur and not cur.get("name"):
            cur["name"] = s.split(":", 1)[1].strip()
        elif s.lower().startswith("description:") and cur.get("index") and not cur.get("name"):
            cur["name"] = s.split(":", 1)[1].strip()
    if cur.get("index"):
        editions.append(cur)
    return editions


def inspect_iso(iso_path):
    """Открыть ISO, вернуть {'wim': path_in_mount, 'editions': [...]}. Для GUI."""
    with IsoMount(iso_path) as m:
        wim = find_wim(m.mountpoint)
        if not wim:
            raise RuntimeError("в ISO не найден sources/install.wim(.esd) — "
                               "это точно установочный образ Windows?")
        editions = parse_wim_editions(wim)
        return {"editions": editions}


# ─────────────────────── привилегированное ядро ─────────────────────────────
# Всё ниже выполняется от root (через pkexec), общается с GUI JSON-строками
# в stdout: {"t":"log"|"stage"|"progress"|"done"|"error", ...}

def emit(t, **kw):
    kw["t"] = t
    sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg):
    emit("log", msg=msg)


def stage(name):
    emit("stage", name=name)


def require_tools(names):
    missing = [n for n in names if not which(n)]
    if missing:
        raise RuntimeError("не хватает утилит: " + ", ".join(missing))


def partitions_of(device):
    out = run(["lsblk", "-nlo", "PATH,TYPE", device], check=False).stdout
    parts = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[1] == "part":
            parts.append(cols[0])
    return parts


def unmount_all(device):
    for part in partitions_of(device):
        run(["umount", part], check=False)
        run(["udisksctl", "unmount", "-b", part], check=False)


def wait_for(path, tries=40, delay=0.25):
    for _ in range(tries):
        if os.path.exists(path):
            return True
        time.sleep(delay)
    return False


def partition_device(device, firmware):
    """Разметить диск. Возвращает (esp_part, win_part)."""
    stage("Разметка диска")
    unmount_all(device)
    run(["wipefs", "-a", device], check=False)

    if firmware == "uefi":
        run(["parted", "-s", device, "mklabel", "gpt"])
        run(["parted", "-s", device, "mkpart", "ESP", "fat32",
             f"{ALIGN_START_MIB}MiB", f"{ALIGN_START_MIB + ESP_SIZE_MIB}MiB"])
        run(["parted", "-s", device, "set", "1", "esp", "on"])
        run(["parted", "-s", device, "mkpart", "Windows", "ntfs",
             f"{ALIGN_START_MIB + ESP_SIZE_MIB}MiB", "100%"])
        esp, win = f"{device}1", f"{device}2"
        # nvme/mmc используют pN
        if not os.path.exists(esp):
            esp, win = f"{device}p1", f"{device}p2"
    else:  # bios / mbr
        run(["parted", "-s", device, "mklabel", "msdos"])
        run(["parted", "-s", device, "mkpart", "primary", "ntfs",
             f"{ALIGN_START_MIB}MiB", "100%"])
        run(["parted", "-s", device, "set", "1", "boot", "on"])
        esp, win = None, f"{device}1"
        if not os.path.exists(win):
            win = f"{device}p1"

    run(["partprobe", device], check=False)
    time.sleep(1)
    if esp:
        wait_for(esp)
    wait_for(win)
    return esp, win


def format_partitions(esp, win, label):
    stage("Форматирование")
    if esp:
        run(["mkfs.fat", "-F", "32", "-n", "SYSTEM", esp])
    run(["mkfs.ntfs", "--quick", "-L", (label or "Windows")[:32], win])


def apply_image(wim_path, index, win_mount):
    stage("Развёртывание Windows (это долго)")
    require_tools(["wimlib-imagex"])
    proc = subprocess.Popen(
        ["wimlib-imagex", "apply", wim_path, str(index), win_mount],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=_augmented_env(), bufsize=1,
    )
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        pct = None
        if "%" in line:
            frag = line.split("%")[0].split()[-1].replace(",", ".")
            try:
                pct = float(frag)
            except ValueError:
                pct = None
        if pct is not None:
            emit("progress", value=pct, stage="apply")
        else:
            log(line)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"wimlib-imagex apply завершился с кодом {rc}")


def part_uuid(part):
    return run(["lsblk", "-ndo", "PARTUUID", part], check=False).stdout.strip()


def disk_uuid(device):
    return run(["lsblk", "-ndo", "PTUUID", device], check=False).stdout.strip()


def bcd_sys_script():
    p = BCD_SYS_DIR / "Linux" / "bcd-sys.sh"
    return str(p) if p.exists() else None


def bcd_sys_missing_tools():
    return [t for t in BCD_SYS_TOOLS if not which(t)]


def run_bcd_sys(win_mount, esp_mount, firmware, prodname=None):
    """Настроить загрузчик и BCD через вендоренный BCD-SYS. Возвращает True при
    успехе. BCD-SYS сам копирует EFI-файлы и создаёт BCD (как bcdboot).

    Скрипт запускается из копии в /tmp: у копии снимается защита «не запускать
    от root» (мы уже в root-конвейере, всё смонтировали сами и передаём -s, так
    что его пути авто-монтирования не задействуются — это безопасно). Вендорный
    исходник остаётся нетронутым.
    """
    script = bcd_sys_script()
    if not script:
        return False
    work = tempfile.mkdtemp(prefix="wintogo-bcdsys-")
    try:
        dst = os.path.join(work, "bcd-sys")
        shutil.copytree(BCD_SYS_DIR, dst)
        main = os.path.join(dst, "Linux", "bcd-sys.sh")
        text = Path(main).read_text()
        text = text.replace("if [[ $EUID -eq 0 ]]; then",
                            "if false; then  # neutralized by WinToGo (root pipeline)", 1)
        # peres (пакет pev) на новых Ubuntu отсутствует, а на свежем диске BCD-SYS
        # его не вызывает (нужен только при обновлении существующей установки) —
        # снимаем стартовую проверку, чтобы не блокироваться зря.
        if not which("peres"):
            text = text.replace(
                'if [[ -z $(command -v peres) ]]; then missing+=" pev/peres"; fi',
                ': # peres requirement dropped by WinToGo (unused on fresh disk)', 1)
        Path(main).write_text(text)

        cmd = ["bash", "./bcd-sys.sh", win_mount, "-f", firmware, "-v"]
        if esp_mount:
            cmd += ["-s", esp_mount]
        if prodname:
            cmd += ["-n", prodname]
        log("Запуск BCD-SYS: " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=os.path.join(dst, "Linux"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=_augmented_env(), bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log("[bcd-sys] " + line)
        return proc.wait() == 0
    except Exception as e:
        log(f"BCD-SYS: исключение {e}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def try_bcd_sys(win_mount, esp_mount, firmware):
    """Обёртка с проверками. Возвращает True, если загрузчик настроен BCD-SYS."""
    if not bcd_sys_script():
        log("BCD-SYS не найден (third_party/bcd-sys) — запасной способ")
        return False
    missing = bcd_sys_missing_tools()
    if missing:
        log("BCD-SYS есть, но не хватает утилит: " + ", ".join(missing) +
            " (sudo apt install libhivex-bin libwin-hivex-perl attr fatattr xxd)"
            " — запасной способ")
        return False
    if run_bcd_sys(win_mount, esp_mount, firmware):
        log("Загрузчик и BCD настроены через BCD-SYS ✓")
        return True
    log("BCD-SYS завершился с ошибкой — запасной способ")
    return False


def install_bootloader_uefi(esp_mount, win_mount, device, win_part):
    """Настроить UEFI-загрузчик. Предпочтительно через BCD-SYS (копирует
    EFI-файлы и создаёт BCD). Если он недоступен/не справился — запасной способ:
    ручное копирование EFI-файлов + BCD (шаблон/предупреждение).
    Возвращает needs_bcd (True, если BCD так и не создан)."""
    stage("Установка загрузчика (UEFI)")
    if try_bcd_sys(win_mount, esp_mount, "uefi"):
        return False

    log("Запасной способ: ручное копирование EFI-файлов")
    src_efi = os.path.join(win_mount, "Windows", "Boot", "EFI")
    if not os.path.isdir(src_efi):
        raise RuntimeError("в образе нет Windows\\Boot\\EFI — образ повреждён?")

    dst_ms = os.path.join(esp_mount, "EFI", "Microsoft", "Boot")
    dst_boot = os.path.join(esp_mount, "EFI", "Boot")
    os.makedirs(dst_ms, exist_ok=True)
    os.makedirs(dst_boot, exist_ok=True)

    # Все файлы загрузчика (bootmgfw.efi, шрифты, локали)
    for root, _dirs, files in os.walk(src_efi):
        rel = os.path.relpath(root, src_efi)
        target = os.path.join(dst_ms, rel) if rel != "." else dst_ms
        os.makedirs(target, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(target, f))

    # Фоллбэк-путь \EFI\Boot\bootx64.efi
    bootmgfw = os.path.join(dst_ms, "bootmgfw.efi")
    if os.path.exists(bootmgfw):
        shutil.copy2(bootmgfw, os.path.join(dst_boot, "bootx64.efi"))
    else:
        log("ВНИМАНИЕ: bootmgfw.efi не найден в образе")

    # Шрифты для boot-меню (если лежат отдельно)
    src_fonts = os.path.join(win_mount, "Windows", "Boot", "Fonts")
    if os.path.isdir(src_fonts):
        shutil.copytree(src_fonts, os.path.join(dst_ms, "Fonts"), dirs_exist_ok=True)

    needs_bcd = _write_bcd(dst_ms, device, win_part, uefi=True)
    return needs_bcd


def _write_bcd(dst_ms_dir, device, win_part, uefi):
    """Создать BCD. Возвращает True, если BCD НЕ удалось создать автоматически.

    Порядок попыток:
      1) готовый шаблон assets/bcd/BCD  → патчим GUID раздела (чистый Python).
      2) python3-hivex                  → сборка с нуля (если модуль доступен).
      3) не вышло                       → предупреждение + инструкция.
    """
    bcd_path = os.path.join(dst_ms_dir, "BCD")
    part_guid = part_uuid(win_part)         # PARTUUID (GPT partition GUID)
    dsk_guid = disk_uuid(device)

    # 1) Шаблон
    if BCD_TEMPLATE.exists() and BCD_TEMPLATE_META.exists():
        try:
            meta = json.loads(BCD_TEMPLATE_META.read_text())
            data = BCD_TEMPLATE.read_bytes()
            data = _patch_guid(data, meta["part_guid"], part_guid)
            if meta.get("disk_guid") and dsk_guid:
                data = _patch_guid(data, meta["disk_guid"], dsk_guid)
            Path(bcd_path).write_bytes(data)
            log(f"BCD создан из шаблона (раздел {part_guid})")
            return False
        except Exception as e:
            log(f"шаблон BCD не подошёл: {e}")

    # 2) hivex
    try:
        import hivex  # noqa: F401
        if _build_bcd_hivex(bcd_path, part_guid, dsk_guid, uefi):
            log(f"BCD собран через hivex (раздел {part_guid})")
            return False
    except ImportError:
        log("python3-hivex недоступен — BCD через hivex пропущен")
    except Exception as e:
        log(f"сборка BCD через hivex не удалась: {e}")

    # 3) не получилось
    emit("warn", msg=(
        "BCD не создан автоматически. Образ развёрнут и EFI-файлы скопированы, "
        "но диск пока не загрузится. Способы завершить:\n"
        "  • загрузиться с установочной флешки Windows, Shift+F10, и выполнить:\n"
        f"      bcdboot W:\\Windows /s S: /f UEFI\n"
        "    (W: — раздел Windows, S: — EFI-раздел этого диска), либо\n"
        "  • положить рабочий шаблон в assets/bcd/BCD (см. README)."))
    return True


def _patch_guid(data, old_guid_str, new_guid_str):
    """Заменить 16-байтовый GUID раздела в двоичном BCD (mixed-endian GUID)."""
    old = _guid_to_bytes(old_guid_str)
    new = _guid_to_bytes(new_guid_str)
    if old not in data:
        raise RuntimeError(f"плейсхолдер GUID {old_guid_str} не найден в шаблоне")
    return data.replace(old, new)


def _guid_to_bytes(guid):
    """'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' → 16 байт (Windows mixed-endian)."""
    import uuid
    return uuid.UUID(guid).bytes_le


def _build_bcd_hivex(bcd_path, part_guid, disk_guid, uefi):
    """Сборка BCD с нуля через libhivex. Экспериментально — требует проверки
    на железе. Возвращает True при успехе. Вынесено, чтобы легко дорабатывать
    именно эту часть без затрагивания остального конвейера."""
    # Реализация BCD-хайва: создаётся пустой куст и наполняется объектами
    # {bootmgr} и {default}. Двоичный формат device-элементов — самая
    # чувствительная часть; оставлено как точка доработки после первого
    # теста загрузки на реальном оборудовании.
    raise NotImplementedError("BCD через hivex ещё не реализован в v0.1")


def install_bootloader_bios(win_mount, device, win_part):
    stage("Установка загрузчика (BIOS/MBR)")
    # Загрузочные сектора (NTFS boot sector + MBR bootstrap) — их BCD-SYS не
    # делает, они нужны для BIOS-цепочки к bootmgr.
    if which("ms-sys"):
        run(["ms-sys", "-n", win_part], check=False)   # NTFS boot sector
        run(["ms-sys", "-m", device], check=False)      # MBR bootstrap
    else:
        log("ms-sys не найден: загрузочный сектор NTFS/MBR не записан — "
            "BIOS-загрузка может не заработать (эксперим. режим).")
    # Файлы bootmgr + BCD (\boot\BCD) через BCD-SYS; система = сам раздел Windows.
    if try_bcd_sys(win_mount, win_mount, "bios"):
        return False
    log("Запасной способ: BCD в \\boot\\BCD автоматически не создан.")
    dst = os.path.join(win_mount, "boot")
    os.makedirs(dst, exist_ok=True)
    return _write_bcd(dst, device, win_part, uefi=False)


def core_create(params):
    """Основной конвейер (root)."""
    device = params["device"]
    firmware = params.get("firmware", "uefi")
    dry_run = params.get("dry_run", False)

    # Защита №2 (в дополнение к GUI): не системный диск.
    if device in system_disks():
        raise RuntimeError(f"{device} — системный диск, запись запрещена")
    if not os.path.exists(device):
        raise RuntimeError(f"устройство {device} не найдено")

    if dry_run:
        stage("Пробный прогон (диск не изменяется)")
        plan = [
            f"wipefs -a {device}",
            f"parted -s {device} mklabel {'gpt' if firmware=='uefi' else 'msdos'}",
            (f"parted … ESP {ESP_SIZE_MIB}MiB + Windows(остаток); mkfs.fat/mkfs.ntfs"
             if firmware == "uefi" else
             f"parted … одна NTFS-партиция; mkfs.ntfs; set boot on"),
            f"wimlib-imagex apply <iso>/sources/install.wim {params.get('index')} <win>",
            ("загрузчик через BCD-SYS (копирует EFI-файлы + создаёт BCD)"
             if bcd_sys_script() and not bcd_sys_missing_tools()
             else "загрузчик: запасной способ (BCD-SYS недоступен) — "
                  "проверьте зависимости libhivex-bin/libwin-hivex-perl/attr/fatattr/xxd"),
        ]
        for step in plan:
            log("[dry-run] " + step)
        emit("done", ok=True, needs_bcd=True, dry_run=True)
        return

    require_tools(["parted", "partprobe", "mkfs.fat", "mkfs.ntfs", "wimlib-imagex"])

    tmp = tempfile.mkdtemp(prefix="wintogo-")
    iso_mp = os.path.join(tmp, "iso")
    win_mp = os.path.join(tmp, "win")
    esp_mp = os.path.join(tmp, "esp")
    for d in (iso_mp, win_mp, esp_mp):
        os.makedirs(d, exist_ok=True)

    needs_bcd = True
    try:
        stage("Монтирование ISO")
        run(["mount", "-o", "loop,ro", params["iso"], iso_mp])
        wim = find_wim(iso_mp)
        if not wim:
            raise RuntimeError("в ISO не найден install.wim/esd")

        esp, win = partition_device(device, firmware)
        format_partitions(esp, win, params.get("label"))

        run(["mount", win, win_mp])
        apply_image(wim, params.get("index", 1), win_mp)

        if firmware == "uefi":
            run(["mount", esp, esp_mp])
            needs_bcd = install_bootloader_uefi(esp_mp, win_mp, device, win)
        else:
            needs_bcd = install_bootloader_bios(win_mp, device, win)

        stage("Завершение (сброс кэшей)")
        run(["sync"])
        emit("done", ok=True, needs_bcd=needs_bcd)
    finally:
        for mp in (esp_mp, win_mp, iso_mp):
            run(["umount", mp], check=False)
        run(["sync"], check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def core_main(params_file):
    try:
        params = json.loads(Path(params_file).read_text())
        core_create(params)
    except Exception as e:
        emit("error", msg=str(e))
        sys.exit(1)


# ────────────────────────────────── GUI ─────────────────────────────────────

def run_gui():
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QCheckBox, QFileDialog, QFormLayout,
        QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
        QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
        QInputDialog,
    )

    class InspectWorker(QThread):
        done = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, iso):
            super().__init__()
            self.iso = iso

        def run(self):
            try:
                self.done.emit(inspect_iso(self.iso))
            except Exception as e:
                self.failed.emit(str(e))

    class CreateWorker(QThread):
        line = pyqtSignal(dict)
        finished_ok = pyqtSignal(bool)   # needs_bcd
        failed = pyqtSignal(str)

        def __init__(self, params):
            super().__init__()
            self.params = params

        def run(self):
            try:
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".json", delete=False) as f:
                    json.dump(self.params, f)
                    pf = f.name
                cmd = ["pkexec", sys.executable,
                       str(Path(__file__).resolve()), "--core", pf]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
                needs_bcd = True
                for raw in proc.stdout:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        self.line.emit({"t": "log", "msg": raw})
                        continue
                    if msg.get("t") == "done":
                        needs_bcd = msg.get("needs_bcd", True)
                    self.line.emit(msg)
                rc = proc.wait()
                if rc != 0:
                    err = proc.stderr.read().strip()
                    self.failed.emit(err or f"процесс завершился с кодом {rc} "
                                     "(отмена авторизации pkexec?)")
                else:
                    self.finished_ok.emit(needs_bcd)
                os.unlink(pf)
            except Exception as e:
                self.failed.emit(str(e))

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} {VERSION}")
            self.resize(720, 620)
            self.editions = []
            self.disks = []
            self.worker = None

            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)

            # — Источник (ISO)
            src_box = QGroupBox("1. Образ Windows (ISO)")
            src_l = QHBoxLayout(src_box)
            self.iso_edit = QLineEdit()
            self.iso_edit.setPlaceholderText("Путь к Windows 10/11 .iso")
            iso_btn = QPushButton("Обзор…")
            iso_btn.clicked.connect(self.pick_iso)
            src_l.addWidget(self.iso_edit)
            src_l.addWidget(iso_btn)
            root.addWidget(src_box)

            # — Редакция
            ed_box = QGroupBox("2. Редакция Windows")
            ed_l = QHBoxLayout(ed_box)
            self.edition_combo = QComboBox()
            self.edition_combo.setEnabled(False)
            self.inspect_btn = QPushButton("Прочитать редакции")
            self.inspect_btn.clicked.connect(self.inspect)
            ed_l.addWidget(self.edition_combo, 1)
            ed_l.addWidget(self.inspect_btn)
            root.addWidget(ed_box)

            # — Целевой диск
            disk_box = QGroupBox("3. Целевой диск (будет полностью стёрт!)")
            disk_v = QVBoxLayout(disk_box)
            disk_row = QHBoxLayout()
            self.disk_combo = QComboBox()
            refresh_btn = QPushButton("Обновить")
            refresh_btn.clicked.connect(self.refresh_disks)
            disk_row.addWidget(self.disk_combo, 1)
            disk_row.addWidget(refresh_btn)
            disk_v.addLayout(disk_row)
            self.usb_only = QCheckBox("Показывать только внешние/USB диски")
            self.usb_only.setChecked(True)
            self.usb_only.stateChanged.connect(self.refresh_disks)
            disk_v.addWidget(self.usb_only)
            root.addWidget(disk_box)

            # — Параметры
            opt_box = QGroupBox("4. Параметры")
            opt_l = QFormLayout(opt_box)
            self.firmware_combo = QComboBox()
            self.firmware_combo.addItem("UEFI / GPT (современные ПК)", "uefi")
            self.firmware_combo.addItem("BIOS / MBR (старые ПК, эксперим.)", "bios")
            self.label_edit = QLineEdit("Windows")
            self.dry_run = QCheckBox("Пробный прогон (не трогать диск, показать план)")
            opt_l.addRow("Режим загрузки:", self.firmware_combo)
            opt_l.addRow("Метка тома:", self.label_edit)
            opt_l.addRow("", self.dry_run)
            root.addWidget(opt_box)

            # — Запуск + прогресс
            self.start_btn = QPushButton("Создать загрузочный диск")
            self.start_btn.clicked.connect(self.start)
            root.addWidget(self.start_btn)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            root.addWidget(self.progress)

            self.stage_label = QLabel("")
            root.addWidget(self.stage_label)

            self.logview = QPlainTextEdit()
            self.logview.setReadOnly(True)
            self.logview.setMaximumBlockCount(2000)
            root.addWidget(self.logview, 1)

            self.check_deps()
            self.refresh_disks()

        # — вспомогательное
        def append_log(self, text):
            self.logview.appendPlainText(text)

        def check_deps(self):
            core = [t for t in ("wimlib-imagex", "parted", "mkfs.ntfs",
                                 "mkfs.fat", "pkexec") if not which(t)]
            if core:
                self.append_log(
                    "⚠ Нет основных утилит: " + ", ".join(core) +
                    "\n  sudo apt install wimtools gdisk dosfstools ntfs-3g pkexec")
            bcd_missing = bcd_sys_missing_tools()
            if not bcd_sys_script():
                self.append_log("⚠ Не найден third_party/bcd-sys — авто-BCD недоступен.")
            elif bcd_missing:
                self.append_log(
                    "⚠ Для авто-создания BCD (BCD-SYS) не хватает: " +
                    ", ".join(bcd_missing) +
                    "\n  sudo apt install libhivex-bin libwin-hivex-perl attr fatattr xxd"
                    "\n  (без них загрузчик придётся доделывать вручную)")

        def pick_iso(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Выберите ISO Windows", os.path.expanduser("~"),
                "ISO-образы (*.iso);;Все файлы (*)")
            if path:
                self.iso_edit.setText(path)
                self.edition_combo.clear()
                self.edition_combo.setEnabled(False)

        def inspect(self):
            iso = self.iso_edit.text().strip()
            if not iso or not os.path.exists(iso):
                QMessageBox.warning(self, APP_NAME, "Укажите существующий ISO-файл.")
                return
            self.inspect_btn.setEnabled(False)
            self.append_log("Читаю редакции из образа…")
            self._iw = InspectWorker(iso)
            self._iw.done.connect(self.on_editions)
            self._iw.failed.connect(self.on_inspect_failed)
            self._iw.start()

        def on_editions(self, info):
            self.inspect_btn.setEnabled(True)
            self.editions = info["editions"]
            self.edition_combo.clear()
            for e in self.editions:
                self.edition_combo.addItem(f"{e['index']}: {e['name']}", e["index"])
            self.edition_combo.setEnabled(bool(self.editions))
            self.append_log(f"Найдено редакций: {len(self.editions)}")

        def on_inspect_failed(self, err):
            self.inspect_btn.setEnabled(True)
            self.append_log("Ошибка чтения ISO: " + err)
            QMessageBox.critical(self, APP_NAME, "Не удалось прочитать ISO:\n" + err)

        def refresh_disks(self):
            try:
                self.disks = list_disks(usb_only=self.usb_only.isChecked())
            except Exception as e:
                self.append_log("Ошибка перечисления дисков: " + str(e))
                self.disks = []
            self.disk_combo.clear()
            for d in self.disks:
                flag = "  ⛔СИСТЕМНЫЙ" if d["is_system"] else ""
                mnt = "  (примонтирован)" if d["mountpoints"] else ""
                self.disk_combo.addItem(
                    f"{d['path']} — {d['model']}, {human_size(d['size'])}"
                    f" [{d['tran']}]{flag}{mnt}", d["path"])

        def selected_disk(self):
            path = self.disk_combo.currentData()
            for d in self.disks:
                if d["path"] == path:
                    return d
            return None

        def start(self):
            iso = self.iso_edit.text().strip()
            disk = self.selected_disk()
            dry = self.dry_run.isChecked()

            if not dry:
                if not iso or not os.path.exists(iso):
                    QMessageBox.warning(self, APP_NAME, "Укажите ISO-файл.")
                    return
            if not disk:
                QMessageBox.warning(self, APP_NAME, "Выберите целевой диск.")
                return
            if disk["is_system"]:
                QMessageBox.critical(
                    self, APP_NAME,
                    f"{disk['path']} — системный диск. Запись запрещена.")
                return

            index = self.edition_combo.currentData()
            if not dry and index is None:
                QMessageBox.warning(
                    self, APP_NAME,
                    "Сначала нажмите «Прочитать редакции» и выберите редакцию.")
                return

            if not dry:
                confirm, ok = QInputDialog.getText(
                    self, "Подтверждение стирания",
                    f"Диск будет ПОЛНОСТЬЮ СТЁРТ:\n\n"
                    f"  {disk['path']}\n  {disk['model']}, "
                    f"{human_size(disk['size'])}\n\n"
                    f"Для подтверждения введите имя устройства ({disk['name']}):")
                if not ok or confirm.strip() != disk["name"]:
                    self.append_log("Отменено (подтверждение не совпало).")
                    return

            params = {
                "device": disk["path"],
                "iso": iso,
                "index": index or 1,
                "firmware": self.firmware_combo.currentData(),
                "label": self.label_edit.text().strip() or "Windows",
                "dry_run": dry,
            }
            self.run_create(params)

        def run_create(self, params):
            self.start_btn.setEnabled(False)
            self.progress.setValue(0)
            self.append_log("─" * 40)
            self.append_log("Запуск (потребуется ввод пароля администратора)…")
            self.worker = CreateWorker(params)
            self.worker.line.connect(self.on_line)
            self.worker.finished_ok.connect(self.on_ok)
            self.worker.failed.connect(self.on_failed)
            self.worker.start()

        def on_line(self, msg):
            t = msg.get("t")
            if t == "log":
                self.append_log(msg.get("msg", ""))
            elif t == "stage":
                self.stage_label.setText("▶ " + msg.get("name", ""))
                self.append_log("▶ " + msg.get("name", ""))
            elif t == "progress":
                self.progress.setValue(int(msg.get("value", 0)))
            elif t == "warn":
                self.append_log("⚠ " + msg.get("msg", ""))
            elif t == "error":
                self.append_log("✗ " + msg.get("msg", ""))

        def on_ok(self, needs_bcd):
            self.start_btn.setEnabled(True)
            self.progress.setValue(100)
            self.stage_label.setText("Готово")
            if needs_bcd:
                QMessageBox.warning(
                    self, APP_NAME,
                    "Образ развёрнут, но BCD не создан автоматически — "
                    "см. инструкцию в логе, чтобы диск загрузился.")
            else:
                QMessageBox.information(
                    self, APP_NAME, "Готово! Загрузочный диск создан.")

        def on_failed(self, err):
            self.start_btn.setEnabled(True)
            self.stage_label.setText("Ошибка")
            self.append_log("✗ " + err)
            QMessageBox.critical(self, APP_NAME, "Не удалось:\n" + err)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG)
    icon = APP_DIR / "assets" / "wintogo.svg"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


# ────────────────────────────────── entry ───────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog=APP_ID, description=APP_NAME)
    ap.add_argument("--core", metavar="PARAMS_JSON",
                    help="внутренний привилегированный режим (вызывается GUI)")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()

    if args.version:
        print(f"{APP_NAME} {VERSION}")
        return
    if args.core:
        core_main(args.core)
        return
    run_gui()


if __name__ == "__main__":
    main()
