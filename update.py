"""
update.py — Скрипт обновления проекта с GitHub без использования git.exe.

Используется на корпоративных компьютерах, где запуск git.exe и chromedriver.exe
заблокирован групповыми политиками Windows (AppLocker / WinError 1260).

Запуск:
    python update.py
"""

import io
import os
import urllib.request
import zipfile

REPO_ZIP_URL = "https://github.com/Stanislav1342/Transport-logistics/archive/refs/heads/main.zip"
PROTECTED_FILES = {
    ".env",
    "users_db.json",
    "РЕЧЬ_ДЛЯ_ПРЕЗЕНТАЦИИ.md",
    "PROJECT_CONTEXT.md",
    "Тарифы.xlsx",
    "ТАРИФЫ РК ТАБЛИЦА.xlsx",
    "Последовательность направлений.xlsx",
    "Исключения для алгоритма распределения транспорта.docx",
    "Шаблон.docx",
    "contacts_schedules.json",
    "contacts_orders.json",
    "carriers_contacts.json",
}


def update_from_github() -> None:
    print("=" * 64)
    print("  🔄 ОБНОВЛЕНИЕ ПРОЕКТА ADVANTUM С GITHUB (БЕЗ GIT.EXE)")
    print("=" * 64)
    print(f"Скачивание архива: {REPO_ZIP_URL} ...")

    req = urllib.request.Request(
        REPO_ZIP_URL,
        headers={"User-Agent": "Advantum-Updater/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        zip_bytes = resp.read()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    updated_count = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue

            # Убираем корневую папку архива (Advantum-main/)
            parts = member.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel_path = parts[1]

            if os.path.basename(rel_path) in PROTECTED_FILES:
                continue

            dest_path = os.path.join(base_dir, rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            with zf.open(member) as src, open(dest_path, "wb") as dst:
                dst.write(src.read())
            updated_count += 1
            print(f"  [Обновлен] {rel_path}")

    print("=" * 64)
    print(f"  ✅ Обновление успешно завершено! Обновлено файлов: {updated_count}")
    print("  🔒 Ваш файл .env с паролями и PIN-кодом сохранен без изменений.")
    print("=" * 64)


if __name__ == "__main__":
    update_from_github()
