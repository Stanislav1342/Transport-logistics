#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/portal_db.py — Единая SQLite-база данных портала «ПУЛЬС — Транспортная логистика».

Файл БД по умолчанию: <BASE_DIR>/data/portal.db (переопределяется через PORTAL_DB_PATH).
Содержит 6 таблиц:
  1. portal_meta           — флаги одноразовых миграций
  2. users                 — учётные записи и права доступа
  3. mail_contacts         — база почт по разделам и подразделам
  4. opt_tariffs           — матрица тарифов РК
  5. opt_city_sequence     — эталонная последовательность направлений
  6. opt_distribution_rules — правила распределения перевозчиков по городам
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("puls_portal.db")

_DB_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

MAIL_SECTIONS: list[dict[str, str]] = [
    {"key": "opt_schedules", "group": "ОПТ", "label": "Графики РК", "mode": "carriers"},
    {"key": "opt_orders", "group": "ОПТ", "label": "Заявки перевозчикам", "mode": "carriers"},
    {"key": "thermo", "group": "Мониторинг", "label": "Температурные графики", "mode": "recipients"},
    {"key": "etrn", "group": "Мониторинг", "label": "Отчет по ЭТРН", "mode": "recipients"},
    {"key": "apt_waybills", "group": "АПТ", "label": "Маршрутные листы", "mode": "recipients"},
]
MAIL_SECTIONS_BY_KEY: dict[str, dict[str, str]] = {s["key"]: s for s in MAIL_SECTIONS}

RULE_TYPES: list[dict[str, str]] = [
    {"key": "fixed_only", "label": "Только закреплённый перевозчик"},
    {"key": "first_then_second", "label": "1-я машина закреплена, 2-я+ другой перевозчик"},
    {"key": "first_then_pool", "label": "1-я машина закреплена, 2-я+ баланс по пулу"},
    {"key": "first_second_then_cheapest", "label": "1-я и 2-я закреплены, 3-я+ рентабельный тариф"},
    {"key": "first_second_then_pool", "label": "1-я и 2-я закреплены, 3-я+ баланс по пулу"},
    {"key": "cycle", "label": "Цикличное чередование по списку"},
    {"key": "pool_balanced", "label": "Равномерный баланс по пулу"},
    {"key": "custom_code", "label": "Сложная логика в коде"},
]
RULE_TYPES_BY_KEY: dict[str, dict[str, str]] = {r["key"]: r for r in RULE_TYPES}

DEFAULT_CITY_SEQUENCE: list[str] = [
    "ПУЛЬС Ярославль",
    "ПУЛЬС Казань",
    "ПУЛЬС Воронеж",
    "ПУЛЬС Краснодар",
    "ПУЛЬС Волгоград",
    "ПУЛЬС Самара",
    "ПУЛЬС Екатеринбург",
    "ПУЛЬС Новосибирск",
    "ПУЛЬС Красноярск",
    "ПУЛЬС Иркутск",
    "ПУЛЬС Хабаровск",
    "ПУЛЬС СПБ",
    "ПУЛЬС Брянск",
]

DEFAULT_DISTRIBUTION_RULES: list[dict[str, Any]] = [
    {
        "city": "Ярославль",
        "rule_type": "custom_code",
        "first_carrier": "ИП Мельник",
        "second_carrier": "ИП Гусманов",
        "pool_carriers": "ИП Мельник; ИП Коршунов; ИП Гусманов",
        "notes": "Чередование 1-й машины по дням недели (Мельник/Коршунов), 2-я+ ИП Гусманов (в коде)",
    },
    {
        "city": "Брянск",
        "rule_type": "first_second_then_cheapest",
        "first_carrier": "НОРДЛАЙН",
        "second_carrier": "ТК Сияние",
        "pool_carriers": "",
        "notes": "Пятничный порядок (после Ярославля) остаётся в коде",
    },
    {
        "city": "Казань",
        "rule_type": "first_then_pool",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "НОРДЛАЙН; АО Национальный; Буш-Авто",
        "notes": "1-я ТК Сияние, 2-я+ баланс по пулу",
    },
    {
        "city": "Уфа",
        "rule_type": "first_then_pool",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "АО Национальный; Агро-Авто; Буш-Авто",
        "notes": "1-я ТК Сияние, 2-я+ баланс по пулу",
    },
    {
        "city": "Воронеж",
        "rule_type": "fixed_only",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "",
        "notes": "Только ТК Сияние",
    },
    {
        "city": "Ростов-на-Дону",
        "rule_type": "fixed_only",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "",
        "notes": "Только ТК Сияние",
    },
    {
        "city": "Краснодар",
        "rule_type": "cycle",
        "first_carrier": "ТК Сияние",
        "second_carrier": "Буш-Авто",
        "pool_carriers": "ТК Сияние; Буш-Авто; Агро-Авто",
        "notes": "Цикличное чередование 1-я/2-я/3-я",
    },
    {
        "city": "Волгоград",
        "rule_type": "fixed_only",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "",
        "notes": "Только ТК Сияние",
    },
    {
        "city": "Самара",
        "rule_type": "first_then_second",
        "first_carrier": "ТК Сияние",
        "second_carrier": "АО Национальный",
        "pool_carriers": "",
        "notes": "1-я ТК Сияние, 2-я+ АО Национальный",
    },
    {
        "city": "Екатеринбург",
        "rule_type": "custom_code",
        "first_carrier": "ТК Сияние",
        "second_carrier": "АО Национальный",
        "pool_carriers": "",
        "notes": "3-я+ рентабельный тариф; ограничение НОРДЛАЙН <=1/нед только Чт/Пт (в коде)",
    },
    {
        "city": "Новосибирск",
        "rule_type": "first_second_then_pool",
        "first_carrier": "ТК Сияние",
        "second_carrier": "АО Национальный",
        "pool_carriers": "ЕманТрансАвто; Виллайн; АО Национальный; ТК Сияние",
        "notes": "1-я ТК Сияние, 2-я АО Национальный, 3-я+ равномерно по пулу",
    },
    {
        "city": "Красноярск",
        "rule_type": "pool_balanced",
        "first_carrier": "",
        "second_carrier": "",
        "pool_carriers": "ЕманТрансАвто; Виллайн",
        "notes": "Равномерный недельный баланс",
    },
    {
        "city": "Иркутск",
        "rule_type": "pool_balanced",
        "first_carrier": "",
        "second_carrier": "",
        "pool_carriers": "Азимут; ЕманТрансАвто",
        "notes": "Равномерный недельный баланс",
    },
    {
        "city": "Хабаровск",
        "rule_type": "custom_code",
        "first_carrier": "Азимут",
        "second_carrier": "Авангард",
        "pool_carriers": "",
        "notes": "1-2 нед Азимут, 3-я+ Авангард в Сб/Пт, всегда 40 паллет (в коде)",
    },
    {
        "city": "Санкт-Петербург",
        "rule_type": "custom_code",
        "first_carrier": "ТК Сияние",
        "second_carrier": "",
        "pool_carriers": "НОРДЛАЙН; Караван",
        "daily_limit": 1,
        "weekly_limit": 3,
        "notes": "Лимиты НОРДЛАЙН <=1/день и Караван <=1/день, <=3/нед (в коде)",
    },
]

_FALLBACK_TARIFFS: dict[str, dict[str, int]] = {
    "Брянск": {"Васино": 50000, "НОРДЛАЙН": 51000, "ЕманТрансАвто": 51500, "ТК Сияние": 57000, "Олимп": 61800, "ТДМ*": 70000, "Агро-Авто": 73000},
    "Волгоград": {"ЕманТрансАвто": 134000, "Васино": 135000, "ТК Сияние": 150000, "АО Национальный": 155000, "Агро-Авто": 157000, "Олимп": 160800, "Веб-Логистика": 190000},
    "Воронеж": {"Васино": 75000, "ЕманТрансАвто": 78000, "Буш-Авто": 81120, "ТК Сияние": 83000, "Олимп": 87000, "Агро-Авто": 95000},
    "Екатеринбург": {"НОРДЛАЙН": 210000, "Васино": 210000, "Буш-Авто": 219000, "Олимп": 230000, "ТК Сияние": 240000, "Азимут": 245000, "АО Национальный": 250000, "ТДМ*": 250000, "Агро-Авто": 265000, "Веб-Логистика": 280000, "УТК": 295000},
    "Иркутск": {"Азимут": 520000, "ЕманТрансАвто": 550000, "Олимп": 570000, "Агро-Авто": 589000, "ТДМ*": 600000, "Веб-Логистика": 600000, "ТК Сияние": 620000},
    "Казань": {"НОРДЛАЙН": 100000, "Буш-Авто": 108000, "АО Национальный": 115000, "Азимут": 115000, "Олимп": 116400, "ТК Сияние": 122000, "ТДМ*": 125000, "Агро-Авто": 135000},
    "Краснодар": {"Буш-Авто": 182000, "ТК Сияние": 185000, "Агро-Авто": 195000, "Олимп": 195600, "НОРДЛАЙН": 200000, "АО Национальный": 215000, "Веб-Логистика": 240000},
    "Красноярск": {"ЕманТрансАвто": 440000, "Виллайн": 450000, "Азимут": 450000, "Агро-Авто": 470000, "Олимп": 470000, "ТДМ*": 500000, "Веб-Логистика": 500000, "ТК Сияние": 530000},
    "Новосибирск": {"ЕманТрансАвто": 385000, "Виллайн": 395000, "Азимут": 400000, "АО Национальный": 405000, "Олимп": 410000, "ТК Сияние": 420000, "ТДМ*": 420000, "Агро-Авто": 430000, "Веб-Логистика": 445000},
    "Ростов-на-Дону": {"Васино": 135000, "ЕманТрансАвто": 140000, "Буш-Авто": 145000, "ТК Сияние": 150000, "АО Национальный": 155000, "Олимп": 160800, "Агро-Авто": 165000, "Веб-Логистика": 185000},
    "Самара": {"Васино": 135000, "Буш-Авто": 140000, "ТК Сияние": 145000, "АО Национальный": 150000, "Олимп": 152400, "Агро-Авто": 160000, "ТДМ*": 165000, "Веб-Логистика": 180000},
    "Санкт-Петербург": {"НОРДЛАЙН": 82000, "Караван": 85000, "ТК Сияние": 89000, "АО Национальный": 92000, "Буш-Авто": 95000, "Олимп": 96000, "Агро-Авто": 105000},
    "Уфа": {"Васино": 170000, "Буш-Авто": 179000, "ТДМ*": 200000, "Олимп": 200000, "ТК Сияние": 217000, "Азимут": 220000, "АО Национальный": 225000, "Агро-Авто": 225000, "Веб-Логистика": 240000, "УТК": 250000},
    "Хабаровск": {"ЕманТрансАвто": 972000, "Васино": 980000, "Агро-Авто": 997000, "ТДМ*": 1000000, "Веб-Логистика": 1000000, "Азимут": 1050000, "Авангард": 1050000, "Гринлайн": 1050000, "Виллайн": 1050000, "ТК Сияние": 1150000, "Олимп": 1166400, "700 Дорог": 1200000},
    "Ярославль": {"ИП Гусманов": 47000, "ИП Коршунов": 52000, "ИП Мельник": 52000, "Буш-Авто": 53680, "ЕманТрансАвто": 56500, "Олимп": 56500, "ТК Сияние": 57000, "Васино": 60000, "Агро-Авто": 77000},
}


def get_db_path(base_dir: str) -> str:
    """Возвращает путь к единой SQLite-базе портала (PORTAL_DB_PATH / MAIL_CONTACTS_DB_PATH / PORTAL_DATA_DIR/portal.db)."""
    for env_name in ("PORTAL_DB_PATH", "MAIL_CONTACTS_DB_PATH"):
        override = os.environ.get(env_name, "").strip()
        if override:
            return override
    data_dir = os.environ.get("PORTAL_DATA_DIR", "").strip()
    if data_dir:
        return os.path.join(os.path.abspath(os.path.expanduser(data_dir)), "portal.db")
    return os.path.join(base_dir, "data", "portal.db")


def _connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return conn


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portal_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            permissions_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            is_master INTEGER NOT NULL DEFAULT 0,
            salt TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            pin_plain TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_key TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            email TEXT NOT NULL,
            mail_role TEXT NOT NULL DEFAULT 'to',
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mail_contacts_section ON mail_contacts(section_key)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL DEFAULT 'Черная Грязь',
            destination TEXT NOT NULL,
            carrier TEXT NOT NULL,
            tariff INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            UNIQUE(origin, destination, carrier)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_city_sequence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL,
            city_name TEXT NOT NULL UNIQUE,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_distribution_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL UNIQUE,
            rule_type TEXT NOT NULL,
            first_carrier TEXT NOT NULL DEFAULT '',
            second_carrier TEXT NOT NULL DEFAULT '',
            pool_carriers TEXT NOT NULL DEFAULT '',
            daily_limit INTEGER DEFAULT NULL,
            weekly_limit INTEGER DEFAULT NULL,
            notes TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_login ON auth_sessions(login)"
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM portal_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str = "1") -> None:
    conn.execute("INSERT OR REPLACE INTO portal_meta(key, value) VALUES (?, ?)", (key, value))


def _normalize_carrier_name_for_db(raw: str) -> str:
    c = str(raw or "").strip()
    cl = c.lower()
    if "национальн" in cl:
        return "АО Национальный"
    if "сияние" in cl:
        return "ТК Сияние"
    if "емантранс" in cl or "еман" in cl:
        return "ЕманТрансАвто"
    if "агро" in cl:
        return "Агро-Авто"
    if "буш" in cl:
        return "Буш-Авто"
    if "норд" in cl:
        return "НОРДЛАЙН"
    if "веб" in cl:
        return "Веб-Логистика"
    if "олимп" in cl:
        return "Олимп"
    if "тдм" in cl:
        return "ТДМ*"
    if "виллайн" in cl:
        return "Виллайн"
    if "азимут" in cl:
        return "Азимут"
    if "васино" in cl:
        return "Васино"
    if "авангард" in cl:
        return "Авангард"
    if "караван" in cl:
        return "Караван"
    if "гриnl" in cl or "гринлайн" in cl:
        return "Гринлайн"
    if "700" in cl:
        return "700 Дорог"
    if "утк" in cl:
        return "УТК"
    return c


def _normalize_city_name_for_db(raw: str) -> str:
    c = str(raw or "").strip()
    cl = c.lower()
    if "спб" in cl or "петербург" in cl:
        return "Санкт-Петербург"
    if "ростов" in cl:
        return "Ростов-на-Дону"
    return c


def normalize_and_validate_emails(raw: str) -> str:
    parts = [p.strip() for p in re.split(r"[;,\n\r]+", str(raw or "")) if p.strip()]
    if not parts:
        raise ValueError("Укажите хотя бы один email-адрес.")
    seen: set[str] = set()
    cleaned: list[str] = []
    for addr in parts:
        if not _EMAIL_RE.match(addr):
            raise ValueError(f"Некорректный email-адрес: {addr}")
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(addr)
    return "; ".join(cleaned)


def _load_json_dict(folders: list[str], filenames: list[str]) -> dict[str, Any]:
    for folder in folders:
        if not folder:
            continue
        for fname in filenames:
            p = os.path.join(folder, fname)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data
                except Exception as exc:
                    logger.warning("Ошибка чтения %s при миграции: %s", p, exc)
    return {}


def _find_first_existing(folders: list[str], filenames: list[str]) -> Optional[str]:
    for folder in folders:
        if not folder:
            continue
        for fname in filenames:
            p = os.path.join(folder, fname)
            if os.path.isfile(p):
                return p
    return None


def _migrate_users_if_needed(conn: sqlite3.Connection, base_dir: str) -> None:
    if _meta_get(conn, "users_migrated"):
        return
    cnt = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if cnt == 0:
        users_path = os.path.join(base_dir, "users_db.json")
        if os.path.isfile(users_path):
            try:
                with open(users_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    rows = []
                    for login_key, u in data.items():
                        if not isinstance(u, dict):
                            continue
                        login = str(u.get("login") or login_key).strip()
                        rows.append(
                            (
                                login,
                                str(u.get("full_name") or login),
                                str(u.get("position") or "Специалист отдела логистики"),
                                str(u.get("role") or "user"),
                                json.dumps(u.get("permissions") or [], ensure_ascii=False),
                                1 if u.get("is_active", True) else 0,
                                1 if u.get("is_master", False) else 0,
                                str(u.get("salt") or ""),
                                str(u.get("pin_hash") or ""),
                                str(u.get("pin_plain") or ""),
                                str(u.get("created_at") or _now_iso()),
                            )
                        )
                    if rows:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO users(
                                login, full_name, position, role, permissions_json,
                                is_active, is_master, salt, pin_hash, pin_plain, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            rows,
                        )
            except Exception as exc:
                logger.warning("Ошибка миграции users_db.json: %s", exc)
    _meta_set(conn, "users_migrated", "1")


def _migrate_mail_contacts_if_needed(
    conn: sqlite3.Connection, base_dir: str, network_base_dir: str, db_path: str
) -> None:
    if _meta_get(conn, "mail_contacts_migrated"):
        return
    cnt = conn.execute("SELECT COUNT(*) AS c FROM mail_contacts").fetchone()["c"]
    if cnt == 0:
        old_db = os.path.join(base_dir, "mail_contacts.db")
        migrated_from_old_db = False
        if os.path.isfile(old_db) and os.path.abspath(old_db) != os.path.abspath(db_path):
            try:
                old_conn = sqlite3.connect(old_db, timeout=5)
                old_conn.row_factory = sqlite3.Row
                old_rows = old_conn.execute(
                    "SELECT section_key, recipient_name, email, mail_role, note, updated_at, updated_by FROM mail_contacts"
                ).fetchall()
                old_conn.close()
                if old_rows:
                    conn.executemany(
                        """
                        INSERT INTO mail_contacts(section_key, recipient_name, email, mail_role, note, updated_at, updated_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                r["section_key"],
                                r["recipient_name"],
                                r["email"],
                                r["mail_role"],
                                r["note"],
                                r["updated_at"],
                                r["updated_by"],
                            )
                            for r in old_rows
                        ],
                    )
                    migrated_from_old_db = True
            except Exception as exc:
                logger.warning("Ошибка переноса из старой mail_contacts.db: %s", exc)

        if not migrated_from_old_db:
            folders = [base_dir, network_base_dir]
            now = _now_iso()
            rows_to_insert: list[tuple[str, str, str, str, str, str, str]] = []

            sch_json = _load_json_dict(folders, ["contacts_schedules.json", "carriers_contacts.json"])
            for carrier, raw_email in sch_json.items():
                if not str(raw_email or "").strip():
                    continue
                try:
                    norm_email = normalize_and_validate_emails(str(raw_email))
                    rows_to_insert.append(
                        ("opt_schedules", str(carrier).strip(), norm_email, "to", "Импорт из JSON", now, "system")
                    )
                except ValueError:
                    continue

            ord_json = _load_json_dict(folders, ["contacts_orders.json"])
            for carrier, raw_email in ord_json.items():
                if not str(raw_email or "").strip():
                    continue
                try:
                    norm_email = normalize_and_validate_emails(str(raw_email))
                    rows_to_insert.append(
                        ("opt_orders", str(carrier).strip(), norm_email, "to", "Импорт из JSON", now, "system")
                    )
                except ValueError:
                    continue

            env_pairs = [
                ("thermo", "to", "THERMO_EMAIL_TO", "Получатели термографиков (Кому)"),
                ("thermo", "cc", "THERMO_EMAIL_CC", "Получатели термографиков (Копия)"),
                ("etrn", "to", "ETRN_EMAIL_TO", "Получатели ЭТРН (Кому)"),
                ("etrn", "cc", "ETRN_EMAIL_CC", "Получатели ЭТРН (Копия)"),
            ]
            for sec_key, role, env_name, default_label in env_pairs:
                raw_env = os.environ.get(env_name, "").strip()
                if not raw_env:
                    continue
                try:
                    norm_email = normalize_and_validate_emails(raw_env)
                    rows_to_insert.append(
                        (sec_key, default_label, norm_email, role, f"Импорт из {env_name}", now, "system")
                    )
                except ValueError:
                    continue

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO mail_contacts(section_key, recipient_name, email, mail_role, note, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
    _meta_set(conn, "mail_contacts_migrated", "1")


def _migrate_tariffs_if_needed(conn: sqlite3.Connection, base_dir: str, network_base_dir: str) -> None:
    if _meta_get(conn, "tariffs_migrated"):
        return
    cnt = conn.execute("SELECT COUNT(*) AS c FROM opt_tariffs").fetchone()["c"]
    if cnt == 0:
        now = _now_iso()
        rows_map: dict[tuple[str, str, str], int] = {}
        xlsx_path = _find_first_existing(
            [base_dir, os.path.join(base_dir, "data"), network_base_dir],
            ["Тарифы.xlsx"],
        )
        if xlsx_path:
            try:
                import openpyxl

                wb = openpyxl.load_workbook(xlsx_path, data_only=True)
                ws = wb.active
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if not row or len(row) < 4:
                        continue
                    origin = str(row[0] or "Черная Грязь").strip() or "Черная Грязь"
                    dest = _normalize_city_name_for_db(str(row[1] or ""))
                    carrier = _normalize_carrier_name_for_db(str(row[2] or ""))
                    raw_val = row[3]
                    if not dest or not carrier or raw_val is None:
                        continue
                    try:
                        val = int(round(float(raw_val)))
                    except (TypeError, ValueError):
                        continue
                    if val <= 0:
                        continue
                    rows_map[(origin, dest, carrier)] = val
            except Exception as exc:
                logger.warning("Ошибка миграции Тарифы.xlsx: %s", exc)

        if not rows_map:
            for dest, c_map in _FALLBACK_TARIFFS.items():
                for carrier, val in c_map.items():
                    rows_map[("Черная Грязь", dest, carrier)] = int(val)

        conn.executemany(
            """
            INSERT OR REPLACE INTO opt_tariffs(origin, destination, carrier, tariff, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(o, d, c, t, now, "system") for (o, d, c), t in rows_map.items()],
        )
    _meta_set(conn, "tariffs_migrated", "1")


def _migrate_city_sequence_if_needed(conn: sqlite3.Connection, base_dir: str, network_base_dir: str) -> None:
    if _meta_get(conn, "city_sequence_migrated"):
        return
    cnt = conn.execute("SELECT COUNT(*) AS c FROM opt_city_sequence").fetchone()["c"]
    if cnt == 0:
        now = _now_iso()
        cities: list[str] = []
        xlsx_path = _find_first_existing(
            [base_dir, os.path.join(base_dir, "data"), network_base_dir],
            ["Последовательность направлений.xlsx", "Последовательность направлений .xlsx"],
        )
        if xlsx_path:
            try:
                import openpyxl

                wb = openpyxl.load_workbook(xlsx_path, data_only=True)
                ws = wb.active
                for row in ws.iter_rows(values_only=True):
                    val = str(row[0] or "").strip() if row else ""
                    if val and val not in cities:
                        cities.append(val)
            except Exception as exc:
                logger.warning("Ошибка миграции Последовательность направлений.xlsx: %s", exc)

        if not cities:
            cities = list(DEFAULT_CITY_SEQUENCE)

        conn.executemany(
            """
            INSERT OR IGNORE INTO opt_city_sequence(position, city_name, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            """,
            [(idx, name, now, "system") for idx, name in enumerate(cities, 1)],
        )
    _meta_set(conn, "city_sequence_migrated", "1")


def _migrate_rules_if_needed(conn: sqlite3.Connection) -> None:
    if _meta_get(conn, "rules_migrated"):
        return
    cnt = conn.execute("SELECT COUNT(*) AS c FROM opt_distribution_rules").fetchone()["c"]
    if cnt == 0:
        now = _now_iso()
        conn.executemany(
            """
            INSERT OR IGNORE INTO opt_distribution_rules(
                city, rule_type, first_carrier, second_carrier, pool_carriers,
                daily_limit, weekly_limit, notes, is_active, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'system')
            """,
            [
                (
                    r["city"],
                    r["rule_type"],
                    r.get("first_carrier", ""),
                    r.get("second_carrier", ""),
                    r.get("pool_carriers", ""),
                    r.get("daily_limit"),
                    r.get("weekly_limit"),
                    r.get("notes", ""),
                    now,
                )
                for r in DEFAULT_DISTRIBUTION_RULES
            ],
        )
    _meta_set(conn, "rules_migrated", "1")


def init_db(base_dir: str, network_base_dir: str = "") -> None:
    """Инициализирует единую SQLite-базу портала и выполняет одноразовые миграции."""
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            _migrate_users_if_needed(conn, base_dir)
            _migrate_mail_contacts_if_needed(conn, base_dir, network_base_dir, db_path)
            _migrate_tariffs_if_needed(conn, base_dir, network_base_dir)
            _migrate_city_sequence_if_needed(conn, base_dir, network_base_dir)
            _migrate_rules_if_needed(conn)
            conn.commit()


# ─── USERS CRUD ───────────────────────────────────────────────────────────────

def load_all_users(base_dir: str) -> dict[str, dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT * FROM users").fetchall()
            result: dict[str, dict[str, Any]] = {}
            for r in rows:
                try:
                    perms = json.loads(r["permissions_json"] or "[]")
                except Exception:
                    perms = []
                result[r["login"]] = {
                    "login": r["login"],
                    "full_name": r["full_name"],
                    "position": r["position"],
                    "role": r["role"],
                    "permissions": perms if isinstance(perms, list) else [],
                    "is_active": bool(r["is_active"]),
                    "is_master": bool(r["is_master"]),
                    "salt": r["salt"],
                    "pin_hash": r["pin_hash"],
                    "pin_plain": r["pin_plain"],
                    "created_at": r["created_at"],
                }
            return result


def save_all_users(base_dir: str, users: dict[str, dict[str, Any]]) -> None:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            conn.execute("DELETE FROM users")
            rows = []
            for login_key, u in users.items():
                login = str(u.get("login") or login_key).strip()
                rows.append(
                    (
                        login,
                        str(u.get("full_name") or login),
                        str(u.get("position") or "Специалист отдела логистики"),
                        str(u.get("role") or "user"),
                        json.dumps(u.get("permissions") or [], ensure_ascii=False),
                        1 if u.get("is_active", True) else 0,
                        1 if u.get("is_master", False) else 0,
                        str(u.get("salt") or ""),
                        str(u.get("pin_hash") or ""),
                        str(u.get("pin_plain") or ""),
                        str(u.get("created_at") or _now_iso()),
                    )
                )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO users(
                        login, full_name, position, role, permissions_json,
                        is_active, is_master, salt, pin_hash, pin_plain, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()


# ─── MAIL CONTACTS CRUD ───────────────────────────────────────────────────────

def _mail_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    sec = MAIL_SECTIONS_BY_KEY.get(row["section_key"], {})
    return {
        "id": row["id"],
        "section_key": row["section_key"],
        "section_group": sec.get("group", ""),
        "section_label": sec.get("label", row["section_key"]),
        "recipient_name": row["recipient_name"],
        "email": row["email"],
        "mail_role": row["mail_role"],
        "note": row["note"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def list_mail_contacts(base_dir: str, section_key: Optional[str] = None, query: str = "") -> list[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            sql = "SELECT * FROM mail_contacts WHERE 1=1"
            params: list[Any] = []
            if section_key:
                sql += " AND section_key = ?"
                params.append(section_key.strip())
            q = str(query or "").strip().lower()
            if q:
                sql += " AND (LOWER(recipient_name) LIKE ? OR LOWER(email) LIKE ? OR LOWER(note) LIKE ?)"
                like_q = f"%{q}%"
                params.extend([like_q, like_q, like_q])
            sql += " ORDER BY section_key ASC, LOWER(recipient_name) ASC, id ASC"
            return [_mail_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def count_mail_contacts_by_section(base_dir: str) -> dict[str, int]:
    db_path = get_db_path(base_dir)
    counts = {s["key"]: 0 for s in MAIL_SECTIONS}
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            for r in conn.execute("SELECT section_key, COUNT(*) AS cnt FROM mail_contacts GROUP BY section_key").fetchall():
                counts[r["section_key"]] = int(r["cnt"])
    return counts


def add_mail_contact(
    base_dir: str,
    section_key: str,
    recipient_name: str,
    email: str,
    mail_role: str = "to",
    note: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    sec_key = str(section_key or "").strip()
    if sec_key not in MAIL_SECTIONS_BY_KEY:
        raise ValueError(f"Неизвестный подраздел: {section_key}")
    name = str(recipient_name or "").strip()
    if not name:
        raise ValueError("Укажите, к кому относится почта.")
    norm_email = normalize_and_validate_emails(email)
    role = str(mail_role or "to").strip().lower()
    if role not in ("to", "cc"):
        raise ValueError("Тип адресата должен быть 'to' или 'cc'.")
    now = _now_iso()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO mail_contacts(section_key, recipient_name, email, mail_role, note, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sec_key, name, norm_email, role, str(note or "").strip(), now, str(updated_by or "").strip()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM mail_contacts WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _mail_row_to_dict(row)


def update_mail_contact(
    base_dir: str,
    contact_id: int,
    section_key: Optional[str] = None,
    recipient_name: Optional[str] = None,
    email: Optional[str] = None,
    mail_role: Optional[str] = None,
    note: Optional[str] = None,
    updated_by: str = "",
) -> Optional[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            existing = conn.execute("SELECT * FROM mail_contacts WHERE id = ?", (contact_id,)).fetchone()
            if not existing:
                return None
            new_sec = str(section_key).strip() if section_key is not None else existing["section_key"]
            if new_sec not in MAIL_SECTIONS_BY_KEY:
                raise ValueError(f"Неизвестный подраздел: {new_sec}")
            new_name = str(recipient_name).strip() if recipient_name is not None else existing["recipient_name"]
            if not new_name:
                raise ValueError("Укажите, к кому относится почта.")
            new_email = normalize_and_validate_emails(email) if email is not None else existing["email"]
            new_role = str(mail_role).strip().lower() if mail_role is not None else existing["mail_role"]
            if new_role not in ("to", "cc"):
                raise ValueError("Тип адресата должен быть 'to' или 'cc'.")
            new_note = str(note).strip() if note is not None else existing["note"]
            now = _now_iso()
            conn.execute(
                """
                UPDATE mail_contacts
                SET section_key = ?, recipient_name = ?, email = ?, mail_role = ?, note = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                (new_sec, new_name, new_email, new_role, new_note, now, str(updated_by or "").strip(), contact_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM mail_contacts WHERE id = ?", (contact_id,)).fetchone()
            return _mail_row_to_dict(row) if row else None


def delete_mail_contact(base_dir: str, contact_id: int) -> bool:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM mail_contacts WHERE id = ?", (contact_id,))
            conn.commit()
            return cur.rowcount > 0


def _merge_unique_emails(values: list[str]) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for raw in values:
        for part in re.split(r"[;,\n\r]+", str(raw or "")):
            addr = part.strip()
            if not addr:
                continue
            key = addr.lower()
            if key not in seen:
                seen.add(key)
                merged.append(addr)
    return "; ".join(merged)


def get_contacts_dict(base_dir: str, section_key: str) -> dict[str, str]:
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return {}
    grouped: dict[str, list[str]] = {}
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT recipient_name, email FROM mail_contacts WHERE section_key = ? ORDER BY id ASC",
                (section_key,),
            ).fetchall()
            for r in rows:
                grouped.setdefault(r["recipient_name"], []).append(r["email"])
    return {name: _merge_unique_emails(emails) for name, emails in grouped.items()}


def get_recipients_to_cc(base_dir: str, section_key: str) -> tuple[str, str]:
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return "", ""
    to_list: list[str] = []
    cc_list: list[str] = []
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT mail_role, email FROM mail_contacts WHERE section_key = ? ORDER BY id ASC",
                (section_key,),
            ).fetchall()
            for r in rows:
                if r["mail_role"] == "cc":
                    cc_list.append(r["email"])
                else:
                    to_list.append(r["email"])
    return _merge_unique_emails(to_list), _merge_unique_emails(cc_list)


# ─── TARIFFS CRUD ─────────────────────────────────────────────────────────────

def list_tariffs(base_dir: str, destination: Optional[str] = None, query: str = "") -> list[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            sql = "SELECT * FROM opt_tariffs WHERE 1=1"
            params: list[Any] = []
            if destination:
                sql += " AND destination = ?"
                params.append(destination.strip())
            q = str(query or "").strip().lower()
            if q:
                sql += " AND (LOWER(origin) LIKE ? OR LOWER(destination) LIKE ? OR LOWER(carrier) LIKE ?)"
                like_q = f"%{q}%"
                params.extend([like_q, like_q, like_q])
            sql += " ORDER BY destination ASC, tariff ASC, carrier ASC"
            return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_tariffs_matrix(base_dir: str) -> dict[str, dict[str, int]]:
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return {}
    matrix: dict[str, dict[str, int]] = {}
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            for r in conn.execute("SELECT destination, carrier, tariff FROM opt_tariffs").fetchall():
                matrix.setdefault(r["destination"], {})[r["carrier"]] = int(r["tariff"])
    return matrix


def add_tariff(
    base_dir: str,
    destination: str,
    carrier: str,
    tariff: int,
    origin: str = "Черная Грязь",
    updated_by: str = "",
) -> dict[str, Any]:
    dest = _normalize_city_name_for_db(destination)
    car = _normalize_carrier_name_for_db(carrier)
    orig = str(origin or "Черная Грязь").strip() or "Черная Грязь"
    if not dest or not car:
        raise ValueError("Укажите направление и перевозчика.")
    val = int(tariff)
    if val <= 0:
        raise ValueError("Тариф должен быть положительным числом.")
    now = _now_iso()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            try:
                cur = conn.execute(
                    """
                    INSERT INTO opt_tariffs(origin, destination, carrier, tariff, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (orig, dest, car, val, now, str(updated_by or "").strip()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Тариф для {orig} → {dest} ({car}) уже существует.")
            conn.commit()
            return dict(conn.execute("SELECT * FROM opt_tariffs WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_tariff(
    base_dir: str,
    tariff_id: int,
    destination: Optional[str] = None,
    carrier: Optional[str] = None,
    tariff: Optional[int] = None,
    origin: Optional[str] = None,
    updated_by: str = "",
) -> Optional[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            ex = conn.execute("SELECT * FROM opt_tariffs WHERE id = ?", (tariff_id,)).fetchone()
            if not ex:
                return None
            new_orig = str(origin).strip() if origin is not None else ex["origin"]
            new_dest = _normalize_city_name_for_db(destination) if destination is not None else ex["destination"]
            new_car = _normalize_carrier_name_for_db(carrier) if carrier is not None else ex["carrier"]
            new_val = int(tariff) if tariff is not None else int(ex["tariff"])
            if not new_dest or not new_car or new_val <= 0:
                raise ValueError("Проверьте направление, перевозчика и положительный тариф.")
            now = _now_iso()
            try:
                conn.execute(
                    """
                    UPDATE opt_tariffs
                    SET origin = ?, destination = ?, carrier = ?, tariff = ?, updated_at = ?, updated_by = ?
                    WHERE id = ?
                    """,
                    (new_orig, new_dest, new_car, new_val, now, str(updated_by or "").strip(), tariff_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Тариф для {new_orig} → {new_dest} ({new_car}) уже существует.")
            conn.commit()
            return dict(conn.execute("SELECT * FROM opt_tariffs WHERE id = ?", (tariff_id,)).fetchone())


def delete_tariff(base_dir: str, tariff_id: int) -> bool:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM opt_tariffs WHERE id = ?", (tariff_id,))
            conn.commit()
            return cur.rowcount > 0


# ─── CITY SEQUENCE CRUD ───────────────────────────────────────────────────────

def list_city_sequence(base_dir: str) -> list[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT * FROM opt_city_sequence ORDER BY position ASC, id ASC").fetchall()
            return [dict(r) for r in rows]


def get_city_sequence_names(base_dir: str) -> list[str]:
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return []
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT city_name FROM opt_city_sequence ORDER BY position ASC, id ASC").fetchall()
            return [r["city_name"] for r in rows]


def add_city_sequence(base_dir: str, city_name: str, updated_by: str = "") -> dict[str, Any]:
    name = str(city_name or "").strip()
    if not name:
        raise ValueError("Укажите название направления.")
    now = _now_iso()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            max_pos = conn.execute("SELECT COALESCE(MAX(position), 0) AS m FROM opt_city_sequence").fetchone()["m"]
            try:
                cur = conn.execute(
                    "INSERT INTO opt_city_sequence(position, city_name, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                    (int(max_pos) + 1, name, now, str(updated_by or "").strip()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Направление '{name}' уже есть в списке.")
            conn.commit()
            return dict(conn.execute("SELECT * FROM opt_city_sequence WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_city_sequence(base_dir: str, item_id: int, city_name: str, updated_by: str = "") -> Optional[dict[str, Any]]:
    name = str(city_name or "").strip()
    if not name:
        raise ValueError("Укажите название направления.")
    now = _now_iso()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            ex = conn.execute("SELECT * FROM opt_city_sequence WHERE id = ?", (item_id,)).fetchone()
            if not ex:
                return None
            try:
                conn.execute(
                    "UPDATE opt_city_sequence SET city_name = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                    (name, now, str(updated_by or "").strip(), item_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Направление '{name}' уже есть в списке.")
            conn.commit()
            return dict(conn.execute("SELECT * FROM opt_city_sequence WHERE id = ?", (item_id,)).fetchone())


def move_city_sequence(base_dir: str, item_id: int, direction: str) -> bool:
    if direction not in ("up", "down"):
        raise ValueError("Направление должно быть 'up' или 'down'.")
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT id, position FROM opt_city_sequence ORDER BY position ASC, id ASC").fetchall()
            idx = next((i for i, r in enumerate(rows) if r["id"] == item_id), None)
            if idx is None:
                return False
            swap_idx = idx - 1 if direction == "up" else idx + 1
            if 0 <= swap_idx < len(rows):
                a, b = rows[idx], rows[swap_idx]
                conn.execute("UPDATE opt_city_sequence SET position = ? WHERE id = ?", (b["position"], a["id"]))
                conn.execute("UPDATE opt_city_sequence SET position = ? WHERE id = ?", (a["position"], b["id"]))
                conn.commit()
            return True


def delete_city_sequence(base_dir: str, item_id: int) -> bool:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM opt_city_sequence WHERE id = ?", (item_id,))
            conn.commit()
            return cur.rowcount > 0


# ─── DISTRIBUTION RULES CRUD ──────────────────────────────────────────────────

def list_distribution_rules(base_dir: str, query: str = "") -> list[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            sql = "SELECT * FROM opt_distribution_rules WHERE 1=1"
            params: list[Any] = []
            q = str(query or "").strip().lower()
            if q:
                sql += " AND (LOWER(city) LIKE ? OR LOWER(first_carrier) LIKE ? OR LOWER(pool_carriers) LIKE ? OR LOWER(notes) LIKE ?)"
                like_q = f"%{q}%"
                params.extend([like_q, like_q, like_q, like_q])
            sql += " ORDER BY city ASC"
            out = []
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                d["rule_type_label"] = RULE_TYPES_BY_KEY.get(d["rule_type"], {}).get("label", d["rule_type"])
                out.append(d)
            return out


def get_distribution_rules_map(base_dir: str) -> dict[str, dict[str, Any]]:
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return {}
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT * FROM opt_distribution_rules WHERE is_active = 1").fetchall()
            return {r["city"]: dict(r) for r in rows}


def add_distribution_rule(
    base_dir: str,
    city: str,
    rule_type: str,
    first_carrier: str = "",
    second_carrier: str = "",
    pool_carriers: str = "",
    daily_limit: Optional[int] = None,
    weekly_limit: Optional[int] = None,
    notes: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    c_name = _normalize_city_name_for_db(city)
    if not c_name:
        raise ValueError("Укажите город.")
    rt = str(rule_type or "").strip()
    if rt not in RULE_TYPES_BY_KEY:
        raise ValueError(f"Неизвестный тип правила: {rule_type}")
    now = _now_iso()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            try:
                cur = conn.execute(
                    """
                    INSERT INTO opt_distribution_rules(
                        city, rule_type, first_carrier, second_carrier, pool_carriers,
                        daily_limit, weekly_limit, notes, is_active, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        c_name,
                        rt,
                        str(first_carrier or "").strip(),
                        str(second_carrier or "").strip(),
                        str(pool_carriers or "").strip(),
                        daily_limit,
                        weekly_limit,
                        str(notes or "").strip(),
                        now,
                        str(updated_by or "").strip(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Правило для города '{c_name}' уже существует.")
            conn.commit()
            d = dict(conn.execute("SELECT * FROM opt_distribution_rules WHERE id = ?", (cur.lastrowid,)).fetchone())
            d["rule_type_label"] = RULE_TYPES_BY_KEY.get(d["rule_type"], {}).get("label", d["rule_type"])
            return d


def update_distribution_rule(
    base_dir: str,
    rule_id: int,
    city: Optional[str] = None,
    rule_type: Optional[str] = None,
    first_carrier: Optional[str] = None,
    second_carrier: Optional[str] = None,
    pool_carriers: Optional[str] = None,
    daily_limit: Optional[int] = None,
    weekly_limit: Optional[int] = None,
    notes: Optional[str] = None,
    is_active: Optional[bool] = None,
    updated_by: str = "",
) -> Optional[dict[str, Any]]:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            ex = conn.execute("SELECT * FROM opt_distribution_rules WHERE id = ?", (rule_id,)).fetchone()
            if not ex:
                return None
            new_city = _normalize_city_name_for_db(city) if city is not None else ex["city"]
            new_rt = str(rule_type).strip() if rule_type is not None else ex["rule_type"]
            if new_rt not in RULE_TYPES_BY_KEY:
                raise ValueError(f"Неизвестный тип правила: {new_rt}")
            now = _now_iso()
            try:
                conn.execute(
                    """
                    UPDATE opt_distribution_rules
                    SET city = ?, rule_type = ?, first_carrier = ?, second_carrier = ?, pool_carriers = ?,
                        daily_limit = ?, weekly_limit = ?, notes = ?, is_active = ?, updated_at = ?, updated_by = ?
                    WHERE id = ?
                    """,
                    (
                        new_city,
                        new_rt,
                        str(first_carrier).strip() if first_carrier is not None else ex["first_carrier"],
                        str(second_carrier).strip() if second_carrier is not None else ex["second_carrier"],
                        str(pool_carriers).strip() if pool_carriers is not None else ex["pool_carriers"],
                        daily_limit if daily_limit is not None else ex["daily_limit"],
                        weekly_limit if weekly_limit is not None else ex["weekly_limit"],
                        str(notes).strip() if notes is not None else ex["notes"],
                        (1 if is_active else 0) if is_active is not None else ex["is_active"],
                        now,
                        str(updated_by or "").strip(),
                        rule_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Правило для города '{new_city}' уже существует.")
            conn.commit()
            d = dict(conn.execute("SELECT * FROM opt_distribution_rules WHERE id = ?", (rule_id,)).fetchone())
            d["rule_type_label"] = RULE_TYPES_BY_KEY.get(d["rule_type"], {}).get("label", d["rule_type"])
            return d


def delete_distribution_rule(base_dir: str, rule_id: int) -> bool:
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM opt_distribution_rules WHERE id = ?", (rule_id,))
            conn.commit()
            return cur.rowcount > 0


# ─── Персистентные сессии авторизации (переживают рестарт пода в k8s) ────────


def create_auth_session(base_dir: str, token: str, login: str, ttl_seconds: int = 43200) -> dict[str, Any]:
    now = time.time()
    expires_at = now + max(60, int(ttl_seconds))
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO auth_sessions(token, login, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, login, now, expires_at),
            )
            conn.commit()
    return {"login": login, "created_at": now, "expires_at": expires_at}


def get_auth_session(base_dir: str, token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    now = time.time()
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT token, login, created_at, expires_at FROM auth_sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if float(row["expires_at"]) <= now:
                conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
                conn.commit()
                return None
            return {
                "login": row["login"],
                "created_at": float(row["created_at"]),
                "expires_at": float(row["expires_at"]),
            }


def delete_auth_session(base_dir: str, token: str) -> None:
    if not token:
        return
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            conn.commit()


def delete_user_auth_sessions(base_dir: str, login: str) -> int:
    if not login:
        return 0
    db_path = get_db_path(base_dir)
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM auth_sessions WHERE login = ?", (login,))
            conn.commit()
            return cur.rowcount


def check_db_health(base_dir: str) -> bool:
    db_path = get_db_path(base_dir)
    try:
        with _DB_LOCK:
            with _connect(db_path) as conn:
                _ensure_schema(conn)
                row = conn.execute("SELECT 1").fetchone()
                return bool(row and row[0] == 1)
    except Exception:
        return False
