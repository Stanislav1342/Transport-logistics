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
    "schedule_baselines.json",
    ".schedule_baselines.json",
    "advantum_shipments_cache.db",
    "advantum_shipments_cache.db-wal",
    "advantum_shipments_cache.db-shm",
    "mail_contacts.db",
    "mail_contacts.db-wal",
    "mail_contacts.db-shm",
    "portal.db",
    "portal.db-wal",
    "portal.db-shm",
    "РЕЧЬ_ДЛЯ_ПРЕЗЕНТАЦИИ.md",
    "PROJECT_CONTEXT.md",
    "AGENTS.md",
    "GEMINI.md",
    "STATE.md",
    "Тарифы.xlsx",
    "ТАРИФЫ РК ТАБЛИЦА.xlsx",
    "Последовательность направлений.xlsx",
    "Исключения для алгоритма распределения транспорта.docx",
    "Шаблон.docx",
    "contacts_schedules.json",
    "contacts_orders.json",
    "carriers_contacts.json",
}

LEGACY_MOVED_FILES = {
    "generate_schedule.py": "opt/generate_schedule.py",
    "generate_orders.py": "opt/generate_orders.py",
    "outlook_bridge.py": "opt/outlook_bridge.py",
    "ml_waybills_router.py": "apt/ml_waybills_router.py",
    "export.py": "monitoring/advantum/export.py",
    "browser.py": "monitoring/advantum/browser.py",
    "cdp_driver.py": "monitoring/advantum/cdp_driver.py",
    "config.py": "monitoring/advantum/config.py",
    "utils.py": "monitoring/advantum/utils.py",
    "main.py": "monitoring/advantum/main.py",
    "mail_contacts_db.py": "core/portal_db.py",
}


def update_from_github() -> None:
    print("=" * 68)
    print("  🔄 ОБНОВЛЕНИЕ ПОРТАЛА «ПУЛЬС — ТРАНСПОРТНАЯ ЛОГИСТИКА» С GITHUB")
    print("=" * 68)
    print(f"Репозиторий: {REPO_ZIP_URL}")
    print("Скачивание актуальной версии...")

    req = urllib.request.Request(
        REPO_ZIP_URL,
        headers={"User-Agent": "Puls-Transport-Logistics-Updater/1.2"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        zip_bytes = resp.read()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    updated_count = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue

            parts = member.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel_path = parts[1].replace("\\", "/")

            if rel_path.startswith("data/") or os.path.basename(rel_path) in PROTECTED_FILES:
                continue

            dest_path = os.path.join(base_dir, rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            with zf.open(member) as src, open(dest_path, "wb") as dst:
                dst.write(src.read())
            updated_count += 1
            print(f"  [Обновлен] {rel_path}")

    for old_rel, new_rel in LEGACY_MOVED_FILES.items():
        old_path = os.path.join(base_dir, old_rel)
        new_path = os.path.join(base_dir, new_rel)
        if os.path.isfile(old_path) and os.path.isfile(new_path):
            try:
                os.remove(old_path)
                print(f"  [Удалён дубль] {old_rel} -> {new_rel}")
            except Exception as exc:
                print(f"  [Предупреждение] Не удалось удалить {old_rel}: {exc}")

    print("=" * 68)
    print(f"  ✅ Обновление успешно завершено! Обновлено файлов: {updated_count}")
    print("  🔒 Ваши локальные базы (data/), .env и шаблоны сохранены.")
    print("=" * 68)


if __name__ == "__main__":
    update_from_github()
