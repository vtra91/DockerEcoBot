#!/usr/bin/env python3
"""
Загрузка архива с фотографиями на сервер.

Пользователь загружает архив на Яндекс.Диск (или даёт прямой HTTPS-URL),
заходит на сервер по SSH и запускает скрипт. Скрипт скачивает архив,
безопасно распаковывает его во временную папку и переносит изображения
в salut_bot/images с сохранением структуры папок из архива.

Пример:
    sudo python3 upload_user_images.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEST_DIR = PROJECT_ROOT / "salut_bot" / "images"
TMP_ROOT = PROJECT_ROOT / "uploads_tmp"

MAX_BYTES = 2048 * 1024 * 1024          # 2 ГБ на архив
MAX_UNPACKED = MAX_BYTES * 4            # 8 ГБ после распаковки
MAX_FILES = 5000                        # файлов в архиве
ENCODING = "auto"                       # utf-8 / cp1251 / cp866 / cp437

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}

YANDEX_HOSTS = {"disk.yandex.ru", "disk.yandex.com", "yadi.sk", "yandex.ru"}

# Частые русские буквы и «мусорные» символы для выбора cp866/cp1251.
_RU_COMMON = set("оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё")
_RU_BAD = set(
    "ЂЃ‚ѓ„…†‡€‰Љ‹ЊЌЋЏђ‘’“”•–—™љ›њќћџЎўЈ¤Ґ¦§Ё©Є«¬®Ї°±Ііґµ¶·№є»јЅѕї"
)


class UploadError(Exception):
    """Понятная пользователю ошибка."""


@dataclass
class CopyStats:
    files: int = 0
    bytes: int = 0
    skipped: int = 0
    overwritten: int = 0


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def human_size(value: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{value} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def ask(prompt: str) -> str:
    return input(f"{prompt}: ").strip()


def normalize_url(url: str) -> str:
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UploadError("Нужна полная http(s)-ссылка.")
    if parsed.username or parsed.password:
        raise UploadError("Ссылки с логином/паролем не поддерживаются.")
    return url


def resolve_yandex_disk(public_url: str) -> str:
    public_url = normalize_url(public_url)
    api = (
        "https://cloud-api.yandex.net/v1/disk/public/resources/download"
        "?public_key=" + urllib.parse.quote(public_url, safe="")
    )
    request = urllib.request.Request(
        api,
        headers={"User-Agent": "EcoBotImageUploader/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise UploadError(f"Яндекс.Диск не отдал ссылку на скачивание: {exc}") from exc

    href = payload.get("href")
    if not href:
        raise UploadError(
            "В ответе Яндекс.Диска нет поля href. "
            "Проверьте, что ссылка публичная и файл ещё существует."
        )
    return href


def is_private_host(host: str) -> bool:
    """Не ходим на localhost и приватные IP."""
    import ipaddress
    import socket

    host = host.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def download_archive(url: str, target: Path) -> int:
    url = normalize_url(url)
    host = urllib.parse.urlparse(url).hostname or ""
    if is_private_host(host):
        raise UploadError(f"Ссылка ведёт на приватный/локальный адрес: {host}")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "EcoBotImageUploader/1.0"},
    )
    written = 0
    started = time.monotonic()
    last_report = 0.0

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_BYTES:
                raise UploadError(
                    f"Архив больше лимита: {human_size(int(length))} > {human_size(MAX_BYTES)}"
                )

            with target.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise UploadError(
                            f"Архив превысил лимит {human_size(MAX_BYTES)}."
                        )
                    out.write(chunk)

                    now = time.monotonic()
                    if now - last_report >= 2:
                        last_report = now
                        print(
                            f"\rСкачано: {human_size(written)}"
                            f" ({now - started:.0f} c)",
                            end="",
                            flush=True,
                        )
    except UploadError:
        raise
    except Exception as exc:
        raise UploadError(f"Не удалось скачать архив: {exc}") from exc

    print(f"\rСкачано: {human_size(written)} за {time.monotonic() - started:.0f} c")
    if written == 0:
        raise UploadError("Скачался пустой файл.")
    return written


def _cyrillic_score(text: str) -> int:
    letters = [ch.lower() for ch in text if ch.isalpha()]
    if not letters:
        return 0
    common = sum(ch in _RU_COMMON for ch in letters)
    cyrillic = sum("а" <= ch <= "я" or ch == "ё" for ch in letters)
    bad = sum(ch in _RU_BAD for ch in text)
    return common * 3 + cyrillic - bad * 4


def decode_name(raw: bytes) -> str:
    """Декодирует имя из архива с учётом русских кодировок Windows."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates = []
    for enc in ("cp866", "cp1251"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        candidates.append((_cyrillic_score(text), enc, text))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][2]

    return raw.decode("cp437", errors="replace")


def zip_entry_name(info: zipfile.ZipInfo) -> str:
    # Бит 11 означает, что имя уже в UTF-8.
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    return decode_name(raw)


def tar_member_name(member: tarfile.TarInfo) -> str:
    raw = member.name.encode("utf-8", "surrogateescape")
    return decode_name(raw)


def _assert_inside(base: Path, target: Path) -> None:
    base = base.resolve()
    target = target.resolve()
    if target != base and base not in target.parents:
        raise UploadError(f"Небезопасный путь в архиве: {target}")


def _check_name(name: str, base: Path) -> Path:
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadError(f"Абсолютный путь в архиве: {name}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UploadError(f"Path traversal в архиве: {name}")
    target = base.joinpath(*parts)
    _assert_inside(base, target)
    return target


def extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise UploadError(f"В архиве больше {MAX_FILES} файлов.")
        total = sum(info.file_size for info in infos if not info.is_dir())
        if total > MAX_UNPACKED:
            raise UploadError(
                f"Распакованный размер больше лимита: "
                f"{human_size(total)} > {human_size(MAX_UNPACKED)}"
            )

        for info in infos:
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UploadError(f"Символическая ссылка в архиве: {info.filename}")
            target = _check_name(zip_entry_name(info), dest)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)


def extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*", encoding="utf-8", errors="surrogateescape") as tf:
        members = tf.getmembers()
        if len(members) > MAX_FILES:
            raise UploadError(f"В архиве больше {MAX_FILES} файлов.")
        total = sum(member.size for member in members if member.isfile())
        if total > MAX_UNPACKED:
            raise UploadError(
                f"Распакованный размер больше лимита: "
                f"{human_size(total)} > {human_size(MAX_UNPACKED)}"
            )

        for member in members:
            if member.issym() or member.islnk() or member.isdev():
                raise UploadError(f"Ссылка/устройство в архиве: {member.name}")
            if member.isdir():
                continue
            target = _check_name(tar_member_name(member), dest)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)


def extract_archive(archive: Path, dest: Path) -> None:
    if zipfile.is_zipfile(archive):
        extract_zip(archive, dest)
        return
    if tarfile.is_tarfile(archive):
        extract_tar(archive, dest)
        return
    raise UploadError(
        "Неизвестный формат архива. Поддерживаются .zip, .tar, .tar.gz, .tgz, "
        ".tar.bz2, .tar.xz."
    )


def image_by_magic(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if head.startswith(b"\xff\xd8\xff"):
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if head.startswith((b"GIF87a", b"GIF89a")):
        return True
    if head.startswith(b"BM"):
        return True
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif", b"avis"}:
            return True
    return False


def copy_images(source: Path, dest_root: Path) -> CopyStats:
    """Копирует только изображения, сохраняя относительные пути."""
    stats = CopyStats()
    dest_base = dest_root.resolve()
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS or not image_by_magic(path):
            stats.skipped += 1
            continue

        rel = path.relative_to(source)
        target = dest_root / rel

        parent_resolved = target.parent.resolve()
        if parent_resolved != dest_base and dest_base not in parent_resolved.parents:
            raise UploadError(f"Путь выходит за пределы images: {target}")

        if target.is_symlink():
            raise UploadError(f"Не перезаписываю символическую ссылку: {target}")

        if target.exists():
            if target.is_dir():
                raise UploadError(f"На месте файла уже есть папка: {target}")
            stats.overwritten += 1

        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copy2(path, target)
        target.chmod(0o644)
        stats.files += 1
        stats.bytes += target.stat().st_size

    return stats


def main() -> int:
    print("=" * 72)
    print("Загрузка архива с фотографиями")
    print("=" * 72)

    raw_url = ask("Ссылка на архив (Яндекс.Диск или прямой URL)")
    if not raw_url:
        eprint("Ссылка не указана.")
        return 1

    try:
        print("\nПолучаю ссылку на скачивание...")
        host = (urllib.parse.urlparse(normalize_url(raw_url)).hostname or "").lower()
        if host in YANDEX_HOSTS:
            download_url = resolve_yandex_disk(raw_url)
        else:
            download_url = normalize_url(raw_url)

        parsed = urllib.parse.urlparse(download_url)
        safe_href = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        print(f"Источник: {safe_href}")
        print(f"Куда: {DEST_DIR}")
        print("Структура папок из архива сохраняется.")
        print("Существующие файлы будут перезаписаны.")

        answer = input("\nПродолжить? Введите yes: ").strip().lower()
        if answer not in ("yes", "y", "да"):
            print("Отменено.")
            return 0

        DEST_DIR.mkdir(parents=True, exist_ok=True)
        if not os.access(DEST_DIR, os.W_OK):
            raise UploadError(
                f"Нет прав на запись в {DEST_DIR}. Запустите через sudo."
            )

        free = shutil.disk_usage(DEST_DIR).free
        if free < MAX_BYTES * 2:
            print(
                f"Предупреждение: свободно {human_size(free)}, "
                f"может не хватить."
            )

        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=".upload-", dir=TMP_ROOT))
        archive_path = work_dir / "archive.bin"
        extracted_dir = work_dir / "extracted"
        extracted_dir.mkdir()

        try:
            archive_bytes = download_archive(download_url, archive_path)
            print(f"Архив: {human_size(archive_bytes)}")

            print("Распаковываю...")
            try:
                extract_archive(archive_path, extracted_dir)
            except UploadError:
                raise
            except Exception as exc:
                raise UploadError(f"Не удалось распаковать архив: {exc}") from exc

            print("Копирую изображения в images...")
            stats = copy_images(extracted_dir, DEST_DIR)
            if stats.files == 0:
                raise UploadError("В архиве не найдено ни одного изображения.")

            print("\nГотово.")
            print(f"Изображений: {stats.files}")
            print(f"Объём: {human_size(stats.bytes)}")
            print(f"Пропущено: {stats.skipped}")
            if stats.overwritten:
                print(f"Перезаписано: {stats.overwritten}")
            print(f"Папка: {DEST_DIR}")
            return 0
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    except UploadError as exc:
        eprint(f"\nОшибка: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        eprint(f"\nНепредвиденная ошибка: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
