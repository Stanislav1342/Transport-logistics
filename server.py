"""
server.py — Единый корпоративный портал «ПУЛЬС — Транспортная логистика».

Объединяет в себе:
1. Ролевую систему авторизации по именным учеткам (формат s.khvostov + 6-значный PIN)
   и Админ-панель управления доступами по разделам (RBAC).
2. Раздел «ОПТ»:
   - Графики РК (недельные графики отгрузки филиалов + рассылка через Outlook)
   - Заявки перевозчикам (суточный реестр + генерация Word .docx + рассылка через Outlook)
3. Раздел «Температурные графики»:
   - Выгрузка термографиков из системы Advantum в PDF и ZIP (через DirectCDPDriver без chromedriver.exe).
"""

import base64
from collections import Counter, defaultdict
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from browser import create_driver, login_to_advantum
from config import Config, load_config
from export import ExportResult, export_thermographs
import generate_orders as orders_core
import generate_schedule as core

# ─── Инициализация путей и логов ──────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TASK_OUTPUT_DIR = os.path.join(BASE_DIR, "web_exports")
USERS_DB_PATH = os.path.join(BASE_DIR, "users_db.json")

os.makedirs(TASK_OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("puls_portal")

# Загрузка переменных окружения из .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

# ─── Настройка FastAPI и CORS ─────────────────────────────────────────────────

PORTAL_VERSION = "1.0"
app = FastAPI(title="ПУЛЬС — Транспортная логистика", version=f"v{PORTAL_VERSION}")

ALLOWED_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
    r"[a-z0-9\-]+\.[a-z0-9\-]+\.devtunnels\.ms)(:\d+)?$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ─── Безопасность, Учетные записи и Ролевой доступ (RBAC) ─────────────────────

DEFAULT_AUTH_PIN = "123456"
DEFAULT_ADMIN_LOGIN = "s.khvostov"
LOGIN_REGEX = re.compile(r"^[a-z]\.[a-z0-9_-]{2,30}$")
ALL_SECTIONS = ["opt_schedules", "opt_orders", "thermo"]


def _normalize_permissions(perms: list[str] | None) -> list[str]:
    if not perms:
        return []
    res: list[str] = []
    for p in perms:
        if p == "opt":
            for sub in ("opt_schedules", "opt_orders"):
                if sub not in res:
                    res.append(sub)
        elif p in ALL_SECTIONS and p not in res:
            res.append(p)
    return res

# Защита от перебора (Brute-force): учет неудачных попыток по IP
FAILED_AUTH_ATTEMPTS: dict[str, list[float]] = {}
AUTH_LOCKOUT_DURATION = 300.0  # Блокировка на 5 минут
MAX_FAILED_ATTEMPTS = 5        # Максимум 5 ошибок подряд

# Активные сессии в памяти: token -> {"login": str, "created_at": float}
SESSIONS_DB: dict[str, dict[str, Any]] = {}
USERS_LOCK = threading.Lock()


def get_master_admin_login() -> str:
    return (os.environ.get("ADMIN_LOGIN", "").strip().lower() or DEFAULT_ADMIN_LOGIN)


def get_master_admin_pin() -> str:
    return (
        os.environ.get("AUTH_PIN", "").strip()
        or os.environ.get("WEB_PIN_CODE", "").strip()
        or os.environ.get("AUTH_TOKEN", "").strip()
        or DEFAULT_AUTH_PIN
    )


def get_master_admin_name() -> str:
    return os.environ.get("ADMIN_NAME", "").strip() or "Хвостов Станислав"


def get_master_admin_position() -> str:
    return os.environ.get("ADMIN_POSITION", "").strip() or "Администратор системы"


def _hash_pin(pin: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return dk.hex()


def _verify_pin_hash(pin: str, salt: str, expected_hash: str) -> bool:
    actual = _hash_pin(pin, salt)
    return secrets.compare_digest(actual, expected_hash)


def _load_users_db() -> dict[str, dict[str, Any]]:
    """Загружает базу пользователей из users_db.json и синхронизирует мастер-админа из .env."""
    users: dict[str, dict[str, Any]] = {}
    if os.path.exists(USERS_DB_PATH):
        try:
            with open(USERS_DB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    users = data
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", USERS_DB_PATH, e)

    # Гарантируем наличие мастер-админа из .env
    admin_login = get_master_admin_login()
    admin_pin = get_master_admin_pin()
    admin_salt = users.get(admin_login, {}).get("salt") or secrets.token_hex(16)
    users[admin_login] = {
        "login": admin_login,
        "full_name": users.get(admin_login, {}).get("full_name") or get_master_admin_name(),
        "position": users.get(admin_login, {}).get("position") or get_master_admin_position(),
        "role": "admin",
        "permissions": list(ALL_SECTIONS),
        "is_active": True,
        "is_master": True,
        "salt": admin_salt,
        "pin_hash": _hash_pin(admin_pin, admin_salt),
        "created_at": users.get(admin_login, {}).get("created_at")
        or datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    return users


def _save_users_db(users: dict[str, dict[str, Any]]) -> None:
    try:
        with open(USERS_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка сохранения %s: %s", USERS_DB_PATH, e)


# Инициализируем базу при старте
with USERS_LOCK:
    _initial_users = _load_users_db()
    _save_users_db(_initial_users)

_env_pin = (
    os.environ.get("AUTH_PIN", "").strip()
    or os.environ.get("WEB_PIN_CODE", "").strip()
    or os.environ.get("AUTH_TOKEN", "").strip()
)
if not _env_pin:
    logger.warning("=" * 70)
    logger.warning(
        "⚠️  ВНИМАНИЕ: AUTH_PIN не задан в .env! Для админа '%s' используется дефолтный код 123456.",
        get_master_admin_login(),
    )
    logger.warning("=" * 70)
else:
    logger.info(
        "🔒 Портал защищен ролевой авторизацией. Главный администратор: %s",
        get_master_admin_login(),
    )


def _check_ip_rate_limit(client_ip: str) -> None:
    attempts = FAILED_AUTH_ATTEMPTS.get(client_ip)
    if not attempts:
        return
    now = time.time()
    valid_attempts = [t for t in attempts if now - t < AUTH_LOCKOUT_DURATION]
    if valid_attempts:
        FAILED_AUTH_ATTEMPTS[client_ip] = valid_attempts
    else:
        FAILED_AUTH_ATTEMPTS.pop(client_ip, None)
    if len(valid_attempts) >= MAX_FAILED_ATTEMPTS:
        logger.warning("Блокировка IP %s из-за превышения числа попыток входа", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много неверных попыток входа. Доступ временно заблокирован на 5 минут.",
        )


def _record_failed_attempt(client_ip: str) -> None:
    now = time.time()
    attempts = FAILED_AUTH_ATTEMPTS.setdefault(client_ip, [])
    attempts.append(now)


def _sanitize_user_public(u: dict[str, Any]) -> dict[str, Any]:
    return {
        "login": u["login"],
        "full_name": u.get("full_name", u["login"]),
        "position": u.get("position", "") or "Специалист отдела логистики",
        "role": u.get("role", "user"),
        "permissions": _normalize_permissions(u.get("permissions", [])),
        "is_active": bool(u.get("is_active", True)),
        "is_master": bool(u.get("is_master", False)),
        "created_at": u.get("created_at", ""),
    }


def check_auth(
    req: Request,
    required_section: str | None = None,
    require_admin: bool = False,
) -> dict[str, Any]:
    """
    Проверяет сессионный токен пользователя, его активность и права доступа к разделу.
    Возвращает словарь публичных данных пользователя.
    """
    client_ip = req.client.host if req.client else "unknown"
    _check_ip_rate_limit(client_ip)

    token = (
        req.headers.get("X-Auth-Token")
        or req.headers.get("X-Auth-PIN")
        or req.query_params.get("token")
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация в системе",
        )

    session = SESSIONS_DB.get(token.strip())
    if not session:
        _record_failed_attempt(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия недействительна или истекла. Войдите повторно.",
        )

    with USERS_LOCK:
        users = _load_users_db()
        user = users.get(session["login"])

    if not user or not user.get("is_active", True):
        SESSIONS_DB.pop(token.strip(), None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ваша учетная запись заблокирована или удалена администратором.",
        )

    if require_admin and user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ разрешен только администратору портала.",
        )

    if required_section and user.get("role") != "admin":
        perms = _normalize_permissions(user.get("permissions", []))
        if required_section == "opt_any":
            allowed = ("opt_schedules" in perms) or ("opt_orders" in perms)
        else:
            allowed = required_section in perms
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="У вашей учетной записи нет доступа к этому разделу.",
            )

    return _sanitize_user_public(user)


# ─── Эндпоинты Авторизации и Админ-панели ────────────────────────────────────


class LoginRequest(BaseModel):
    login: str = Field(..., min_length=2, max_length=40)
    pin: str = Field(..., min_length=6, max_length=6)


class CreateUserRequest(BaseModel):
    login: str = Field(..., min_length=3, max_length=35)
    full_name: str = Field(..., min_length=2, max_length=80)
    position: str = Field(default="Специалист отдела логистики", max_length=100)
    pin: str = Field(..., pattern=r"^\d{6}$")
    permissions: list[str] = Field(default_factory=lambda: ["opt_schedules", "opt_orders", "thermo"])
    role: str = Field(default="user")


class UpdateUserRequest(BaseModel):
    full_name: Optional[str] = None
    position: Optional[str] = None
    pin: Optional[str] = None
    permissions: Optional[list[str]] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


@app.post("/api/auth/login")
async def login_user(payload: LoginRequest, request: Request):
    """Авторизация по именному логину (формат s.khvostov) и 6-значному PIN-коду."""
    client_ip = request.client.host if request.client else "unknown"
    _check_ip_rate_limit(client_ip)

    login_clean = payload.login.strip().lower()
    pin_clean = payload.pin.strip()

    with USERS_LOCK:
        users = _load_users_db()
        user = users.get(login_clean)

    if not user:
        _record_failed_attempt(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или PIN-код",
        )

    if not _verify_pin_hash(pin_clean, user["salt"], user["pin_hash"]):
        _record_failed_attempt(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или PIN-код",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ваша учетная запись отключена администратором",
        )

    # Сбрасываем счетчик ошибок при успешном входе
    FAILED_AUTH_ATTEMPTS.pop(client_ip, None)

    token = secrets.token_urlsafe(32)
    SESSIONS_DB[token] = {
        "login": login_clean,
        "created_at": time.time(),
    }

    logger.info("Пользователь '%s' (%s) вошел в портал", login_clean, user.get("full_name"))
    return {
        "success": True,
        "token": token,
        "user": _sanitize_user_public(user),
    }


@app.post("/api/auth/logout")
async def logout_user(request: Request):
    token = request.headers.get("X-Auth-Token") or request.headers.get("X-Auth-PIN")
    if token:
        SESSIONS_DB.pop(token.strip(), None)
    return {"success": True}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    try:
        user = check_auth(request)
        return {"auth_required": True, "authenticated": True, "user": user}
    except HTTPException:
        return {"auth_required": True, "authenticated": False}


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    check_auth(request, require_admin=True)
    with USERS_LOCK:
        users = _load_users_db()
    result = [_sanitize_user_public(u) for u in users.values()]
    result.sort(key=lambda x: (not x["is_master"], x["login"]))
    return {"status": "success", "users": result}


@app.post("/api/admin/users")
async def admin_create_user(payload: CreateUserRequest, request: Request):
    check_auth(request, require_admin=True)
    login_clean = payload.login.strip().lower()

    if not LOGIN_REGEX.match(login_clean):
        raise HTTPException(
            status_code=400,
            detail="Логин должен быть в корпоративном формате 'и.фамилия' латиницей (например: s.khvostov, a.rozhkov)",
        )

    valid_perms = _normalize_permissions(payload.permissions)
    role = "admin" if payload.role == "admin" else "user"
    if role == "admin":
        valid_perms = list(ALL_SECTIONS)

    with USERS_LOCK:
        users = _load_users_db()
        if login_clean in users:
            raise HTTPException(
                status_code=400,
                detail=f"Учетная запись '{login_clean}' уже существует",
            )

        salt = secrets.token_hex(16)
        new_user = {
            "login": login_clean,
            "full_name": payload.full_name.strip(),
            "position": payload.position.strip() or "Специалист отдела логистики",
            "role": role,
            "permissions": valid_perms,
            "is_active": True,
            "is_master": False,
            "salt": salt,
            "pin_hash": _hash_pin(payload.pin.strip(), salt),
            "created_at": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        users[login_clean] = new_user
        _save_users_db(users)

    logger.info("Администратор создал учетную запись: %s (%s)", login_clean, payload.full_name)
    return {"status": "success", "user": _sanitize_user_public(new_user)}


@app.post("/api/admin/users/{target_login}/update")
async def admin_update_user(target_login: str, payload: UpdateUserRequest, request: Request):
    check_auth(request, require_admin=True)
    login_clean = target_login.strip().lower()

    with USERS_LOCK:
        users = _load_users_db()
        user = users.get(login_clean)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        if user.get("is_master"):
            # У мастер-админа нельзя забрать права или заблокировать его
            if payload.full_name is not None and payload.full_name.strip():
                user["full_name"] = payload.full_name.strip()
            if payload.position is not None and payload.position.strip():
                user["position"] = payload.position.strip()
            _save_users_db(users)
            return {"status": "success", "user": _sanitize_user_public(user)}

        if payload.full_name is not None and payload.full_name.strip():
            user["full_name"] = payload.full_name.strip()

        if payload.position is not None and payload.position.strip():
            user["position"] = payload.position.strip()

        if payload.role is not None:
            user["role"] = "admin" if payload.role == "admin" else "user"

        if payload.permissions is not None:
            user["permissions"] = (
                list(ALL_SECTIONS)
                if user["role"] == "admin"
                else _normalize_permissions(payload.permissions)
            )

        if payload.is_active is not None:
            user["is_active"] = bool(payload.is_active)
            if not user["is_active"]:
                # Мгновенно сбрасываем все активные сессии заблокированного сотрудника
                for tok, sess in list(SESSIONS_DB.items()):
                    if sess.get("login") == login_clean:
                        SESSIONS_DB.pop(tok, None)

        if payload.pin is not None and payload.pin.strip():
            pin_val = payload.pin.strip()
            if not re.match(r"^\d{6}$", pin_val):
                raise HTTPException(status_code=400, detail="PIN-код должен состоять ровно из 6 цифр")
            salt = secrets.token_hex(16)
            user["salt"] = salt
            user["pin_hash"] = _hash_pin(pin_val, salt)

        _save_users_db(users)

    logger.info("Обновлены параметры учетной записи: %s", login_clean)
    return {"status": "success", "user": _sanitize_user_public(user)}


@app.post("/api/admin/users/{target_login}/delete")
async def admin_delete_user(target_login: str, request: Request):
    check_auth(request, require_admin=True)
    login_clean = target_login.strip().lower()

    with USERS_LOCK:
        users = _load_users_db()
        user = users.get(login_clean)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if user.get("is_master"):
            raise HTTPException(
                status_code=400,
                detail="Нельзя удалить главную учетную запись администратора из .env",
            )
        users.pop(login_clean, None)
        _save_users_db(users)

    # Удаляем активные сессии пользователя
    for tok, sess in list(SESSIONS_DB.items()):
        if sess.get("login") == login_clean:
            SESSIONS_DB.pop(tok, None)

    logger.info("Удалена учетная запись: %s", login_clean)
    return {"status": "success"}


# ==============================================================================
# РАЗДЕЛ 1: ОПТ (ГРАФИКИ РК И ЗАЯВКИ ПЕРЕВОЗЧИКАМ)
# ==============================================================================


def get_schedules_dir() -> str:
    network_base = r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки\Готовые недельные графики"
    try:
        drive = os.path.splitdrive(network_base)[0]
        if drive and os.path.exists(drive + "\\"):
            os.makedirs(network_base, exist_ok=True)
            return network_base
        elif os.path.exists(network_base):
            return network_base
    except Exception:
        pass

    user_home = os.path.expanduser("~")
    desktop_candidates = [
        os.path.join(user_home, "OneDrive", "Рабочий стол"),
        os.path.join(user_home, "OneDrive", "Desktop"),
        os.path.join(user_home, "Рабочий стол"),
        os.path.join(user_home, "Desktop"),
    ]
    for c in desktop_candidates:
        if os.path.exists(c):
            folder = os.path.join(c, "Готовые недельные графики")
            os.makedirs(folder, exist_ok=True)
            return folder

    folder = os.path.join(user_home, "Desktop", "Готовые недельные графики")
    os.makedirs(folder, exist_ok=True)
    return folder


def get_orders_dir() -> str:
    return orders_core.get_orders_dir()


def load_schedules_contacts() -> dict[str, str]:
    net_dir = r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки"
    for folder in [BASE_DIR, net_dir]:
        for fname in ["contacts_schedules.json", "carriers_contacts.json"]:
            p = os.path.join(folder, fname)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning("Ошибка чтения %s: %s", p, e)
    return {}


def find_email_in_dict(carrier_name: str, contacts: dict, fallback_email: str = "") -> str:
    if not carrier_name or not contacts:
        return fallback_email
    carrier_name = carrier_name.strip()
    if carrier_name in contacts and contacts[carrier_name] and contacts[carrier_name].strip():
        return contacts[carrier_name].strip()

    c_lower = carrier_name.lower()
    for k, email in contacts.items():
        if k.lower() == c_lower and email and email.strip():
            return email.strip()

    def normalize_name(s: str) -> str:
        s = s.lower()
        s = re.sub(r'[\"«»\'\.,]', " ", s)
        for prefix in ["ип", "ооо", "ао", "зао", "пао", "тк"]:
            s = re.sub(rf"\b{prefix}\b", " ", s)
        return " ".join(s.split())

    c_norm = normalize_name(carrier_name)
    if c_norm:
        for k, email in contacts.items():
            if normalize_name(k) == c_norm and email and email.strip():
                return email.strip()
        tokens = [w for w in c_norm.split() if len(w) >= 3]
        if tokens:
            first_key_token = tokens[0]
            for k, email in contacts.items():
                k_norm = normalize_name(k)
                if first_key_token in k_norm.split() and email and email.strip():
                    return email.strip()

    return fallback_email


def get_schedule_carrier_email(carrier_name: str, fallback_email: str = "") -> str:
    return find_email_in_dict(carrier_name, load_schedules_contacts(), fallback_email)


def load_orders_contacts() -> dict[str, str]:
    net_dir = r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки"
    for folder in [BASE_DIR, net_dir]:
        p = os.path.join(folder, "contacts_orders.json")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Ошибка чтения %s: %s", p, e)
    return {}


def get_order_carrier_email(carrier_name: str, fallback_email: str = "") -> str:
    return find_email_in_dict(carrier_name, load_orders_contacts(), fallback_email)


CURRENT_STATE: dict[str, Any] = {
    "all_trips": [],
    "week_range": "",
    "total_trips": 0,
    "carriers": [],
    "last_output_path": None,
    "last_output_filename": None,
}

CURRENT_ORDERS_STATE: dict[str, Any] = {
    "output_dir": None,
    "summary_excel": None,
    "zip_file": None,
    "orders": [],
    "total_carriers": 0,
    "total_pallets": "0",
    "date_tag": "",
}


class SelectFileRequest(BaseModel):
    filename: str


class EmailRequest(BaseModel):
    mode: str  # 'draft' или 'send'
    carrier: Optional[str] = None
    email: Optional[str] = None


@app.get("/api/storage-info")
def get_storage_info(request: Request):
    check_auth(request, required_section="opt_any")
    sch_dir = get_schedules_dir()
    ord_dir = get_orders_dir()
    return {
        "schedules_dir": sch_dir,
        "schedules_is_network": sch_dir.upper().startswith("L:"),
        "orders_dir": ord_dir,
        "orders_is_network": ord_dir.upper().startswith("L:"),
    }


@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    check_auth(request, required_section="opt_schedules")
    try:
        temp_dir = tempfile.gettempdir()
        safe_name = os.path.basename(file.filename or "plan.xlsm")
        temp_path = os.path.join(temp_dir, safe_name)
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        base_name = os.path.splitext(safe_name)[0]
        schedules_dir = get_schedules_dir()

        is_edited_schedule = False
        all_trips = []
        if safe_name.endswith((".xlsx", ".xlsm")):
            try:
                test_trips = core.parse_final_schedule_file(temp_path)
                if len(test_trips) > 0:
                    is_edited_schedule = True
                    all_trips = test_trips
            except Exception:
                is_edited_schedule = False

        if is_edited_schedule and all_trips:
            out_filename = safe_name
            target_path = os.path.join(schedules_dir, out_filename)
            shutil.copy2(temp_path, target_path)
            core.refresh_schedule_file_summary(target_path)
            all_trips = core.parse_final_schedule_file(target_path)
        else:
            plan_trips, start_date = core.parse_plan_file(temp_path)
            end_date = start_date + datetime.timedelta(days=6)
            date_range_str = f"{start_date.strftime('%d.%m')} - {end_date.strftime('%d.%m.%Y')}"

            clean_name = base_name.replace(" (готовый)", "")
            out_filename = f"{clean_name} ({date_range_str}).xlsx"
            target_path = os.path.join(schedules_dir, out_filename)

            try:
                all_trips = core.build_schedule_from_plan(plan_trips, start_date, target_path)
            except PermissionError:
                out_filename = f"{clean_name} ({date_range_str})_{int(time.time())}.xlsx"
                target_path = os.path.join(schedules_dir, out_filename)
                all_trips = core.build_schedule_from_plan(plan_trips, start_date, target_path)

        carrier_counts = Counter([t["carrier"] for t in all_trips if t.get("carrier")])
        dates = [t["date"] for t in all_trips]
        week_range = f"{dates[0]} - {dates[-1]}" if dates else ""

        total_cost = sum(t.get("tariff", 0) for t in all_trips)
        total_cost_str = f"{total_cost:,}".replace(",", " ") + " ₽"

        carrier_costs: dict[str, int] = defaultdict(int)
        for t in all_trips:
            car = t.get("carrier")
            if car:
                carrier_costs[car] += t.get("tariff", 0)

        carriers_list = [
            {
                "name": k,
                "count": v,
                "cost": carrier_costs[k],
                "cost_str": f"{carrier_costs[k]:,}".replace(",", " ") + " ₽",
            }
            for k, v in carrier_counts.most_common()
        ]

        no_tariff_path = core.get_no_tariffs_copy_path(target_path)
        no_tariff_filename = os.path.basename(no_tariff_path) if no_tariff_path else ""
        manual_trips_count = sum(1 for t in all_trips if t.get("is_manual"))

        CURRENT_STATE["all_trips"] = all_trips
        CURRENT_STATE["week_range"] = week_range
        CURRENT_STATE["total_trips"] = len(all_trips)
        CURRENT_STATE["total_cost"] = total_cost
        CURRENT_STATE["total_cost_str"] = total_cost_str
        CURRENT_STATE["carriers"] = carriers_list
        CURRENT_STATE["last_output_path"] = target_path
        CURRENT_STATE["last_output_filename"] = out_filename

        return {
            "status": "success",
            "week_range": week_range,
            "total_trips": len(all_trips),
            "total_cost": total_cost_str,
            "carriers": carriers_list,
            "filename": out_filename,
            "no_tariff_filename": no_tariff_filename,
            "manual_trips": manual_trips_count,
            "is_edited": is_edited_schedule,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/list-ready-files")
def list_ready_files(request: Request):
    check_auth(request, required_section="opt_schedules")
    try:
        schedules_dir = get_schedules_dir()
        if not os.path.exists(schedules_dir):
            return {"status": "success", "files": []}

        files_info = []
        current_path = CURRENT_STATE.get("last_output_path")

        for fname in os.listdir(schedules_dir):
            if fname.startswith("~$") or not fname.endswith((".xlsx", ".xlsm")):
                continue
            if "(без тарифов)" in fname.lower():
                continue
            full_path = os.path.join(schedules_dir, fname)
            if not os.path.isfile(full_path):
                continue

            stat = os.stat(full_path)
            mtime = stat.st_mtime
            mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")

            size_bytes = stat.st_size
            if size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} КБ"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.2f} МБ"

            is_current = bool(
                current_path and os.path.abspath(full_path) == os.path.abspath(current_path)
            )
            files_info.append(
                {
                    "filename": fname,
                    "mtime": mtime,
                    "mtime_str": mtime_str,
                    "size": size_str,
                    "is_current": is_current,
                }
            )

        files_info.sort(key=lambda x: x["mtime"], reverse=True)
        return {"status": "success", "files": files_info}
    except Exception as e:
        return {"status": "error", "message": str(e), "files": []}


@app.post("/api/select-file")
def select_file(req: SelectFileRequest, request: Request):
    check_auth(request, required_section="opt_schedules")
    try:
        schedules_dir = get_schedules_dir()
        safe_name = os.path.basename(req.filename)
        target_path = os.path.join(schedules_dir, safe_name)
        if not os.path.exists(target_path):
            return {"status": "error", "message": f"Файл не найден: {safe_name}"}

        core.refresh_schedule_file_summary(target_path)
        trips = core.parse_final_schedule_file(target_path)
        if not trips:
            return {"status": "error", "message": "В выбранном файле не найдено строк с рейсами"}

        carrier_counts = Counter([t["carrier"] for t in trips if t.get("carrier")])
        dates = [t["date"] for t in trips]
        week_range = f"{dates[0]} - {dates[-1]}" if dates else ""

        total_cost = sum(t.get("tariff", 0) for t in trips)
        total_cost_str = f"{total_cost:,}".replace(",", " ") + " ₽"

        carrier_costs: dict[str, int] = defaultdict(int)
        for t in trips:
            car = t.get("carrier")
            if car:
                carrier_costs[car] += t.get("tariff", 0)

        carriers_list = [
            {
                "name": k,
                "count": v,
                "cost": carrier_costs[k],
                "cost_str": f"{carrier_costs[k]:,}".replace(",", " ") + " ₽",
            }
            for k, v in carrier_counts.most_common()
        ]

        no_tariff_path = core.get_no_tariffs_copy_path(target_path)
        no_tariff_filename = os.path.basename(no_tariff_path) if no_tariff_path else ""
        manual_trips_count = sum(1 for t in trips if t.get("is_manual"))

        CURRENT_STATE["all_trips"] = trips
        CURRENT_STATE["week_range"] = week_range
        CURRENT_STATE["total_trips"] = len(trips)
        CURRENT_STATE["total_cost"] = total_cost
        CURRENT_STATE["total_cost_str"] = total_cost_str
        CURRENT_STATE["carriers"] = carriers_list
        CURRENT_STATE["last_output_path"] = target_path
        CURRENT_STATE["last_output_filename"] = safe_name

        return {
            "status": "success",
            "filename": safe_name,
            "no_tariff_filename": no_tariff_filename,
            "manual_trips": manual_trips_count,
            "week_range": week_range,
            "total_trips": len(trips),
            "total_cost": total_cost_str,
            "carriers": carriers_list,
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка чтения файла: {str(e)}"}


def _build_schedule_email_packages(req: EmailRequest) -> dict[str, Any]:
    """Формирует готовые пакеты писем для Графиков РК."""
    target_file = CURRENT_STATE.get("last_output_path")
    if target_file and os.path.exists(target_file):
        try:
            disk_trips = core.parse_final_schedule_file(target_file)
            if disk_trips:
                CURRENT_STATE["all_trips"] = disk_trips
        except Exception:
            pass

    trips_to_send = CURRENT_STATE.get("all_trips", [])
    if not trips_to_send:
        return {
            "status": "error",
            "message": "Нет данных для отправки. Сначала загрузите и выберите файл плана!",
        }

    if req.carrier:
        target_trips = [t for t in trips_to_send if t["carrier"] == req.carrier]
    else:
        target_trips = trips_to_send

    if not target_trips:
        return {"status": "error", "message": "Рейсы для указанного перевозчика не найдены."}

    carrier_groups = defaultdict(list)
    for tr in target_trips:
        carrier_groups[tr["carrier"]].append(tr)

    dates = [tr["date"] for tr in trips_to_send]
    start_d = dates[0] if dates else ""
    end_d = dates[-1] if dates else ""

    emails = []
    missing_carriers = []
    for car, c_trips in carrier_groups.items():
        carrier_email = (req.email if req.email else None) or get_schedule_carrier_email(car)
        if not carrier_email or not carrier_email.strip():
            missing_carriers.append(car)
            continue

        sorted_trips = sorted(
            c_trips,
            key=lambda x: (
                x.get("dt") or datetime.datetime.strptime(x["date"], "%d.%m.%Y"),
                x["time"],
            ),
        )
        emails.append(
            {
                "carrier": car,
                "to": carrier_email,
                "subject": f"[{car}] График погрузки РЦ Черная Грязь ({start_d} - {end_d})",
                "html_body": core.generate_carrier_html(car, sorted_trips, start_d, end_d),
                "attachments": [],
            }
        )

    return {
        "status": "success",
        "mode": req.mode,
        "emails": emails,
        "missing": missing_carriers,
    }


@app.post("/api/prepare-emails")
def prepare_schedule_emails(req: EmailRequest, request: Request):
    """Возвращает сформированные письма для отправки через локальный Outlook сотрудника."""
    check_auth(request, required_section="opt_schedules")
    return _build_schedule_email_packages(req)


@app.post("/api/send-emails")
def send_emails(req: EmailRequest, request: Request):
    """Отправка писем по графикам через локальный Outlook сервера (fallback)."""
    check_auth(request, required_section="opt_schedules")
    pkg = _build_schedule_email_packages(req)
    if pkg.get("status") != "success":
        return pkg

    emails = pkg.get("emails", [])
    missing_carriers = pkg.get("missing", [])
    draft_mode = (req.mode == "draft")

    try:
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()
        outlook = win32.Dispatch("outlook.application")

        sent_count = 0
        for item in emails:
            mail = outlook.CreateItem(0)
            mail.To = item["to"]
            mail.Subject = item["subject"]
            mail.HTMLBody = item["html_body"]
            if draft_mode:
                mail.Save()
            else:
                mail.Send()
            sent_count += 1

        action_text = "сохранено в Черновиках" if draft_mode else "отправлено"
        missing_info = ""
        if missing_carriers:
            missing_msgs = [f"почта {c} не найдена в базе" for c in missing_carriers]
            missing_info = "\nВнимание: " + "; ".join(missing_msgs) + "."

        if sent_count > 0:
            return {
                "status": "success",
                "message": f"Успешно {action_text} писем перевозчикам: {sent_count} шт.{missing_info}",
                "count": sent_count,
                "missing": missing_carriers,
            }
        return {
            "status": "warning",
            "message": f"Письма не сформированы.{missing_info}",
            "count": 0,
            "missing": missing_carriers,
        }
    except ImportError:
        action_text = "черновиков" if draft_mode else "отправки"
        return {
            "status": "warning",
            "message": f"На сервере не установлен Outlook/pywin32 (подготовлено писем для {action_text}: {len(emails)} шт.). Для отправки со своего ПК запустите outlook_bridge.py.",
            "count": len(emails),
            "missing": missing_carriers,
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка Outlook: {str(e)}"}


@app.post("/api/orders/generate")
async def generate_orders_api(request: Request, file: UploadFile = File(...)):
    check_auth(request, required_section="opt_orders")
    try:
        temp_dir = tempfile.gettempdir()
        safe_name = os.path.basename(file.filename or "orders.xlsx")
        temp_path = os.path.join(temp_dir, safe_name)
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        template_candidates = [
            os.path.join(BASE_DIR, "Шаблон.docx"),
            r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки\Шаблон.docx",
            r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки\Заявки\Шаблон.docx",
        ]
        template_path = template_candidates[0]
        for cand in template_candidates:
            if os.path.exists(cand):
                template_path = cand
                break

        res = orders_core.process_daily_orders(
            input_excel_path=temp_path,
            template_docx_path=template_path,
            output_dir=get_orders_dir(),
        )
        CURRENT_ORDERS_STATE.update(res)
        return res
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/orders/download-file")
def download_order_file(filename: str, request: Request):
    check_auth(request, required_section="opt_orders")
    orders_dir = get_orders_dir()
    clean_name = os.path.basename(filename)
    target_path = os.path.join(orders_dir, clean_name)
    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(target_path, filename=clean_name, media_type="application/octet-stream")


@app.get("/api/orders/list-files")
def list_orders_files(request: Request):
    check_auth(request, required_section="opt_orders")
    try:
        orders_dir = get_orders_dir()
        if not os.path.exists(orders_dir):
            return {"status": "success", "files": []}
        files = []
        for f in os.listdir(orders_dir):
            if f.startswith("~$") or not f.endswith((".docx", ".xlsx", ".zip")):
                continue
            fp = os.path.join(orders_dir, f)
            if os.path.isfile(fp):
                stat = os.stat(fp)
                files.append(
                    {
                        "filename": f,
                        "mtime": stat.st_mtime,
                        "mtime_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime(
                            "%d.%m.%Y %H:%M"
                        ),
                        "size": f"{stat.st_size/1024:.1f} КБ"
                        if stat.st_size < 1024 * 1024
                        else f"{stat.st_size/(1024*1024):.2f} МБ",
                    }
                )
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return {"status": "success", "files": files, "output_dir": orders_dir}
    except Exception as e:
        return {"status": "error", "message": str(e), "files": []}


def _build_orders_email_packages(req: EmailRequest, include_base64: bool = False) -> dict[str, Any]:
    orders_to_send = CURRENT_ORDERS_STATE.get("orders", [])
    if not orders_to_send:
        return {
            "status": "error",
            "message": "Нет сформированных заявок для отправки. Сначала сформируйте заявки из файла реестра!",
        }

    if req.carrier:
        target_orders = [o for o in orders_to_send if o.get("carrier") == req.carrier]
    else:
        target_orders = orders_to_send

    if not target_orders:
        return {"status": "error", "message": "Заявки для указанного перевозчика не найдены."}

    carrier_groups = defaultdict(list)
    for ord_info in target_orders:
        carrier_groups[ord_info["carrier"]].append(ord_info)

    orders_dir = get_orders_dir()
    emails = []
    missing_carriers = []

    for car, c_orders in carrier_groups.items():
        carrier_email = (req.email if req.email else None) or get_order_carrier_email(car)
        if not carrier_email or not carrier_email.strip():
            missing_carriers.append(car)
            continue

        date_str = (
            c_orders[0].get("date")
            or CURRENT_ORDERS_STATE.get("date_tag")
            or datetime.datetime.now().strftime("%d.%m.%Y")
        )

        attachments = []
        for o in c_orders:
            fpath = o.get("path")
            if not fpath or not os.path.exists(fpath):
                fpath = os.path.join(orders_dir, o.get("filename", ""))
            if fpath and os.path.exists(fpath):
                att_entry: dict[str, Any] = {
                    "filename": os.path.basename(fpath),
                    "network_path": os.path.abspath(fpath),
                }
                if include_base64:
                    with open(fpath, "rb") as af:
                        att_entry["content_b64"] = base64.b64encode(af.read()).decode("ascii")
                attachments.append(att_entry)

        emails.append(
            {
                "carrier": car,
                "to": carrier_email,
                "subject": f"[{car}] Транспортная заявка на перевозку ({date_str})",
                "html_body": orders_core.generate_order_email_html(car, c_orders, date_str),
                "attachments": attachments,
            }
        )

    return {
        "status": "success",
        "mode": req.mode,
        "emails": emails,
        "missing": missing_carriers,
    }


@app.post("/api/orders/prepare-emails")
def prepare_orders_emails(req: EmailRequest, request: Request):
    """Возвращает пакет писем с вложениями .docx для отправки через локальный Outlook сотрудника."""
    check_auth(request, required_section="opt_orders")
    return _build_orders_email_packages(req, include_base64=True)


@app.post("/api/orders/send-emails")
def send_orders_emails(req: EmailRequest, request: Request):
    """Отправка индивидуальных заявок перевозчикам с вложением .docx (локальный Outlook сервера)."""
    check_auth(request, required_section="opt_orders")
    pkg = _build_orders_email_packages(req, include_base64=False)
    if pkg.get("status") != "success":
        return pkg

    emails = pkg.get("emails", [])
    missing_carriers = pkg.get("missing", [])
    draft_mode = (req.mode == "draft")

    try:
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()
        outlook = win32.Dispatch("outlook.application")

        sent_count = 0
        for item in emails:
            mail = outlook.CreateItem(0)
            mail.To = item["to"]
            mail.Subject = item["subject"]
            mail.HTMLBody = item["html_body"]
            for att in item.get("attachments", []):
                fpath = att.get("network_path")
                if fpath and os.path.exists(fpath):
                    mail.Attachments.Add(os.path.abspath(fpath))
            if draft_mode:
                mail.Save()
            else:
                mail.Send()
            sent_count += 1

        action_text = "сохранено в Черновиках" if draft_mode else "отправлено"
        missing_info = ""
        if missing_carriers:
            missing_msgs = [f"почта {c} не найдена в базе" for c in missing_carriers]
            missing_info = "\nВнимание: " + "; ".join(missing_msgs) + "."

        if sent_count > 0:
            return {
                "status": "success",
                "message": f"Успешно {action_text} писем с заявками перевозчикам: {sent_count} шт.{missing_info}",
                "count": sent_count,
                "missing": missing_carriers,
            }
        return {
            "status": "warning",
            "message": f"Письма не сформированы.{missing_info}",
            "count": 0,
            "missing": missing_carriers,
        }
    except ImportError:
        action_text = "черновиков" if draft_mode else "отправки"
        return {
            "status": "warning",
            "message": f"На сервере не установлен Outlook/pywin32 (подготовлено писем с заявками .docx для {action_text}: {len(emails)} шт.). Для отправки со своего ПК запустите outlook_bridge.py.",
            "count": len(emails),
            "missing": missing_carriers,
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка Outlook: {str(e)}"}


# ==============================================================================
# РАЗДЕЛ 2: ТЕМПЕРАТУРНЫЕ ГРАФИКИ (ADVANTUM)
# ==============================================================================


class ExportRequest(BaseModel):
    ml_numbers: list[str] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Список номеров МЛ (от 1 до 100 за одну задачу)",
    )
    date_from: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


jobs_db: dict[str, dict[str, Any]] = {}
job_queue: queue.Queue[str] = queue.Queue(maxsize=50)


def _cleanup_old_tasks(max_age_hours: int = 24) -> None:
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=max_age_hours)
    expired = []
    for tid, job in list(jobs_db.items()):
        try:
            created = datetime.datetime.fromisoformat(job.get("created_at", ""))
            if created < cutoff:
                expired.append(tid)
        except Exception:
            pass
    for tid in expired:
        jobs_db.pop(tid, None)

    try:
        if os.path.exists(TASK_OUTPUT_DIR):
            for folder in os.listdir(TASK_OUTPUT_DIR):
                folder_path = os.path.join(TASK_OUTPUT_DIR, folder)
                if os.path.isdir(folder_path) and folder.startswith("task_"):
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(folder_path))
                    if mtime < cutoff:
                        shutil.rmtree(folder_path, ignore_errors=True)
                        logger.info("Удалена устаревшая папка задачи: %s", folder)
    except Exception as e:
        logger.warning("Ошибка при очистке старых задач: %s", e)


def get_local_ip() -> str:
    """Возвращает реальный LAN IP компьютера в офисной сети, игнорируя виртуальные адаптеры VPN (198.18.x.x)."""
    candidates: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("198.18."):
                continue
            candidates.append(ip)
    except Exception:
        pass

    # На macOS/Linux также проверяем вывод ifconfig на случай активного VPN-туннеля
    if not candidates and sys.platform != "win32":
        try:
            import subprocess
            out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
            for m in re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out):
                if not m.startswith("127.") and not m.startswith("169.254.") and not m.startswith("198.18."):
                    candidates.append(m)
        except Exception:
            pass

    # Также проверяем маршрут по умолчанию, если он не ведет в виртуальный туннель VPN (198.18.x.x)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        route_ip = s.getsockname()[0]
        if not route_ip.startswith("198.18.") and not route_ip.startswith("127."):
            return route_ip
    except Exception:
        pass
    finally:
        s.close()

    # Приоритет корпоративным подсетям 10.x.x.x, 192.168.x.x, 172.x.x.x
    for ip in candidates:
        if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("172."):
            return ip
    return candidates[0] if candidates else "127.0.0.1"


def _task_worker():
    logger.info("Фоновый воркер задач Advantum запущен.")
    while True:
        try:
            _cleanup_old_tasks(max_age_hours=24)
        except Exception:
            pass

        task_id = job_queue.get()
        if not task_id or task_id not in jobs_db:
            job_queue.task_done()
            continue

        job = jobs_db[task_id]
        job["status"] = "running"
        task_dir = os.path.join(TASK_OUTPUT_DIR, f"task_{task_id}")
        os.makedirs(task_dir, exist_ok=True)

        def log_msg(msg: str):
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            job["logs"].append(entry)
            logger.info("[%s] %s", task_id[:8], msg)

        base_config = load_config()
        task_config = dataclasses.replace(base_config, download_dir=task_dir, headless=True)

        driver = None
        try:
            log_msg("Подготовка к запуску...")
            d_from = datetime.date.fromisoformat(job["date_from"])
            d_to = datetime.date.fromisoformat(job["date_to"])
            date_filter = (d_from, d_to)

            log_msg("Запуск браузера через Direct CDP (в фоновом режиме)...")
            driver = create_driver(task_config, headless=True)

            log_msg("Авторизация в системе Advantum...")
            wait = login_to_advantum(driver, task_config)

            def on_progress(idx: int, total: int, ml: str, result: ExportResult | None):
                job["total_count"] = total
                job["current_ml"] = ml
                if result is None:
                    job["progress_current"] = idx - 1
                    log_msg(f"МЛ ({idx}/{total}): начало обработки {ml}...")
                else:
                    job["progress_current"] = idx
                    res_dict = {
                        "ml_number": result.ml_number,
                        "success": result.success,
                        "filename": os.path.basename(result.filepath) if result.filepath else "",
                        "error": result.error,
                        "driver_name": getattr(result, "driver_name", "") or "",
                        "tc_number": getattr(result, "tc_number", "") or "",
                    }
                    job["results"].append(res_dict)
                    if result.success:
                        log_msg(
                            f"МЛ {ml}: успешно выгружен ({res_dict['driver_name']}, {res_dict['tc_number']})"
                        )
                    else:
                        log_msg(f"МЛ {ml}: не выгружен ({result.error})")

            log_msg("Применение фильтра дат и начало экспорта...")
            export_thermographs(
                driver,
                wait,
                job["ml_numbers"],
                task_config,
                date_filter=date_filter,
                on_progress=on_progress,
            )

            pdf_files = [
                os.path.join(task_dir, f)
                for f in os.listdir(task_dir)
                if f.lower().endswith(".pdf")
            ]

            if pdf_files:
                zip_filename = f"advantum_charts_{job['date_from']}_{job['date_to']}.zip"
                zip_path = os.path.join(task_dir, zip_filename)
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in pdf_files:
                        zf.write(p, arcname=os.path.basename(p))

                job["has_zip"] = True
                job["zip_filename"] = zip_filename
                job["zip_path"] = zip_path
                log_msg(f"Создан архив: {zip_filename} ({len(pdf_files)} файлов)")
            else:
                job["has_zip"] = False
                log_msg("Файлы графиков не были созданы.")

            job["status"] = "completed"
            job["success_count"] = sum(1 for r in job["results"] if r.get("success"))
            job["fail_count"] = len(job["results"]) - job["success_count"]
            log_msg(
                f"Готово! Успешно: {job['success_count']}, не выгружено: {job['fail_count']}"
            )
        except Exception:
            job["status"] = "failed"
            logger.exception("Критический сбой при выполнении задачи %s", task_id)
            job["error_message"] = (
                "Внутренняя ошибка сервера при обработке задачи. Обратитесь к администратору."
            )
            log_msg("Критический сбой выполнения задачи.")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            job_queue.task_done()


def _validate_task_id(task_id: str) -> None:
    try:
        uuid.UUID(task_id)
    except Exception:
        raise HTTPException(
            status_code=400, detail="Некорректный формат идентификатора задачи"
        )


worker_thread = threading.Thread(target=_task_worker, daemon=True)
worker_thread.start()


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Файл шаблона index.html не найден")
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content)


@app.post("/api/tasks")
async def create_export_task(req: ExportRequest, request: Request):
    user = check_auth(request, required_section="thermo")

    if job_queue.full():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Очередь задач переполнена (максимум 50). Пожалуйста, повторите попытку позже.",
        )

    clean_mls = [ml.strip()[:35] for ml in req.ml_numbers if ml.strip()]
    if not clean_mls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Список номеров МЛ не должен быть пустым",
        )
    if len(clean_mls) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Превышен лимит: максимум 100 МЛ за одну задачу",
        )

    try:
        datetime.date.fromisoformat(req.date_from)
        datetime.date.fromisoformat(req.date_to)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный формат даты (требуется YYYY-MM-DD)",
        )

    task_id = str(uuid.uuid4())
    job_data = {
        "id": task_id,
        "owner": user["login"],
        "created_at": datetime.datetime.now().isoformat(),
        "status": "queued",
        "date_from": req.date_from,
        "date_to": req.date_to,
        "ml_numbers": clean_mls,
        "total_count": len(clean_mls),
        "progress_current": 0,
        "current_ml": "",
        "results": [],
        "logs": [f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Задача поставлена в очередь."],
        "has_zip": False,
        "zip_filename": None,
        "zip_path": None,
        "success_count": 0,
        "fail_count": 0,
        "error_message": None,
    }

    jobs_db[task_id] = job_data
    job_queue.put(task_id)
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request):
    check_auth(request, required_section="thermo")
    _validate_task_id(task_id)
    job = jobs_db.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    return {
        "id": job["id"],
        "status": job["status"],
        "date_from": job["date_from"],
        "date_to": job["date_to"],
        "total_count": job["total_count"],
        "progress_current": job["progress_current"],
        "current_ml": job["current_ml"],
        "results": job["results"],
        "logs": job["logs"],
        "has_zip": job["has_zip"],
        "success_count": job["success_count"],
        "fail_count": job["fail_count"],
        "error_message": job["error_message"],
    }


@app.get("/api/tasks/{task_id}/download")
async def download_zip(task_id: str, request: Request):
    check_auth(request, required_section="thermo")
    _validate_task_id(task_id)
    job = jobs_db.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    zip_path = job.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(
            status_code=404, detail="Архив с файлами еще не сформирован или отсутствует"
        )

    filename = job.get("zip_filename") or f"advantum_charts_{task_id[:8]}.zip"
    encoded_filename = urllib.parse.quote(filename)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@app.get("/api/tasks/{task_id}/files/{filename}")
async def download_single_pdf(task_id: str, filename: str, request: Request):
    check_auth(request, required_section="thermo")
    _validate_task_id(task_id)

    safe_filename = os.path.basename(filename)
    if not safe_filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Разрешено скачивание только PDF-файлов",
        )

    job = jobs_db.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    file_path = os.path.join(TASK_OUTPUT_DIR, f"task_{task_id}", safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл графика не найден")

    encoded_filename = urllib.parse.quote(safe_filename)
    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=safe_filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@app.get("/api/status")
async def server_status():
    return {
        "status": "online",
        "version": PORTAL_VERSION,
        "queue_size": job_queue.qsize(),
        "total_jobs": len(jobs_db),
    }


def main():
    local_ip = get_local_ip()
    port = 8000

    print("=" * 68)
    print(f"    🚀 ПУЛЬС — ТРАНСПОРТНАЯ ЛОГИСТИКА (Версия {PORTAL_VERSION}) — ЗАПУЩЕН!    ")
    print("=" * 68)
    print(f"  • Ссылка на этом компьютере:   http://localhost:{port}")
    print(f"  • Ссылка для коллег в офисе:   http://{local_ip}:{port}")
    print(f"  • Главный администратор:       {get_master_admin_login()}")
    print("=" * 68)
    print("  Для завершения работы нажмите Ctrl + C в этом окне.")
    print("=" * 68)

    def _open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
