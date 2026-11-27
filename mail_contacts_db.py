#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mail_contacts_db.py — Единая SQLite-база почтовых контактов и получателей по разделам портала.

Поддерживает разделы:
- opt_schedules (ОПТ / Графики РК)
- opt_orders    (ОПТ / Заявки перевозчикам)
- thermo        (Мониторинг / Температурные графики)
- etrn          (Мониторинг / Отчет по ЭТРН)
- apt_waybills  (АПТ / Маршрутные листы)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
import threading
from typing import Any, Optional

logger = logging.getLogger("puls_portal.mail_contacts")

MAIL_SECTIONS: list[dict[str, str]] = [
    {"key": "opt_schedules", "group": "ОПТ", "label": "Графики РК", "mode": "carriers"},
    {"key": "opt_orders", "group": "ОПТ", "label": "Заявки перевозчикам", "mode": "carriers"},
    {"key": "thermo", "group": "Мониторинг", "label": "Температурные графики", "mode": "recipients"},
    {"key": "etrn", "group": "Мониторинг", "label": "Отчет по ЭТРН", "mode": "recipients"},
    {"key": "apt_waybills", "group": "АПТ", "label": "Маршрутные листы", "mode": "recipients"},
]

MAIL_SECTIONS_BY_KEY: dict[str, dict[str, str]] = {s["key"]: s for s in MAIL_SECTIONS}

_DB_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_db_path(base_dir: str) -> str:
    """Возвращает путь к SQLite-файлу базы почт (поддерживает MAIL_CONTACTS_DB_PATH)."""
    override = os.environ.get("MAIL_CONTACTS_DB_PATH", "").strip()
    if override:
        return override
    return os.path.join(base_dir, "mail_contacts.db")


def _connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS mail_contacts_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def normalize_and_validate_emails(raw: str) -> str:
    """Разбирает, валидирует и склеивает список email-адресов через '; '."""
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


def _validate_section_key(section_key: str) -> str:
    key = str(section_key or "").strip()
    if key not in MAIL_SECTIONS_BY_KEY:
        raise ValueError(f"Неизвестный подраздел: {section_key}")
    return key


def _validate_mail_role(mail_role: Optional[str]) -> str:
    role = str(mail_role or "to").strip().lower()
    if role not in ("to", "cc"):
        raise ValueError("Тип адресата должен быть 'to' (Кому) или 'cc' (Копия).")
    return role


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json_contacts(folders: list[str], filenames: list[str]) -> dict[str, str]:
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
                        return {str(k): str(v) for k, v in data.items() if v is not None}
                except Exception as exc:
                    logger.warning("Ошибка чтения %s при миграции почт: %s", p, exc)
    return {}


def init_db(base_dir: str, network_base_dir: str = "") -> None:
    """Создаёт схему SQLite и при первом запуске импортирует контакты из JSON и .env."""
    db_path = get_db_path(base_dir)
    folders = [base_dir]
    if network_base_dir:
        folders.append(network_base_dir)

    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            done_row = conn.execute(
                "SELECT value FROM mail_contacts_meta WHERE key = 'initial_import_done'"
            ).fetchone()
            count_row = conn.execute("SELECT COUNT(*) AS cnt FROM mail_contacts").fetchone()
            if done_row or (count_row and count_row["cnt"] > 0):
                if not done_row:
                    conn.execute(
                        "INSERT OR REPLACE INTO mail_contacts_meta(key, value) VALUES ('initial_import_done', '1')"
                    )
                conn.commit()
                return

            now = _now_iso()
            rows_to_insert: list[tuple[str, str, str, str, str, str, str]] = []

            sch_json = _load_json_contacts(folders, ["contacts_schedules.json", "carriers_contacts.json"])
            for carrier, raw_email in sch_json.items():
                if not str(raw_email or "").strip():
                    continue
                try:
                    norm_email = normalize_and_validate_emails(raw_email)
                except ValueError:
                    continue
                rows_to_insert.append(
                    ("opt_schedules", carrier.strip(), norm_email, "to", "Импорт из JSON", now, "system")
                )

            ord_json = _load_json_contacts(folders, ["contacts_orders.json"])
            for carrier, raw_email in ord_json.items():
                if not str(raw_email or "").strip():
                    continue
                try:
                    norm_email = normalize_and_validate_emails(raw_email)
                except ValueError:
                    continue
                rows_to_insert.append(
                    ("opt_orders", carrier.strip(), norm_email, "to", "Импорт из JSON", now, "system")
                )

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
                except ValueError:
                    continue
                rows_to_insert.append(
                    (sec_key, default_label, norm_email, role, f"Импорт из {env_name}", now, "system")
                )

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO mail_contacts(section_key, recipient_name, email, mail_role, note, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
            conn.execute(
                "INSERT OR REPLACE INTO mail_contacts_meta(key, value) VALUES ('initial_import_done', '1')"
            )
            conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
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


def list_contacts(base_dir: str, section_key: Optional[str] = None, query: str = "") -> list[dict[str, Any]]:
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
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows]


def count_by_section(base_dir: str) -> dict[str, int]:
    db_path = get_db_path(base_dir)
    counts = {s["key"]: 0 for s in MAIL_SECTIONS}
    with _DB_LOCK:
        with _connect(db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT section_key, COUNT(*) AS cnt FROM mail_contacts GROUP BY section_key"
            ).fetchall()
            for r in rows:
                counts[r["section_key"]] = int(r["cnt"])
    return counts


def add_contact(
    base_dir: str,
    section_key: str,
    recipient_name: str,
    email: str,
    mail_role: str = "to",
    note: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    sec_key = _validate_section_key(section_key)
    name = str(recipient_name or "").strip()
    if not name:
        raise ValueError("Укажите, к кому относится почта.")
    norm_email = normalize_and_validate_emails(email)
    role = _validate_mail_role(mail_role)
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
            return _row_to_dict(row)


def update_contact(
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

            new_sec = _validate_section_key(section_key) if section_key is not None else existing["section_key"]
            new_name = str(recipient_name).strip() if recipient_name is not None else existing["recipient_name"]
            if not new_name:
                raise ValueError("Укажите, к кому относится почта.")
            new_email = normalize_and_validate_emails(email) if email is not None else existing["email"]
            new_role = _validate_mail_role(mail_role) if mail_role is not None else existing["mail_role"]
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
            return _row_to_dict(row) if row else None


def delete_contact(base_dir: str, contact_id: int) -> bool:
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
    """Возвращает словарь {recipient_name: 'a@b.ru; c@d.ru'} для заданного раздела."""
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
    """Возвращает (to_str, cc_str) для разделов с общими получателями (thermo, etrn, apt_waybills)."""
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
