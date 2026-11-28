"""
core/server_config.py — Единая 12-factor конфигурация серверного режима (k8s / Docker)
и путей постоянного хранилища (PORTAL_DATA_DIR / PVC).

При SERVER_MODE=0 (значение по умолчанию на локальном Windows-ПК) сохраняется
100% обратная совместимость с локальными путями.
При SERVER_MODE=1 (Docker / Kubernetes) все БД, кэши, графики, заявки, отчёты
и временные файлы строго изолируются внутри PORTAL_DATA_DIR (/data).
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_server_mode() -> bool:
    return _env_bool("SERVER_MODE", default=False)


def get_data_dir(base_dir: str) -> str:
    override = (os.environ.get("PORTAL_DATA_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(base_dir, "data")


def get_web_exports_dir(base_dir: str) -> str:
    override = (os.environ.get("TASK_OUTPUT_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if is_server_mode() or (os.environ.get("PORTAL_DATA_DIR") or "").strip():
        return os.path.join(get_data_dir(base_dir), "web_exports")
    return os.path.join(base_dir, "web_exports")


def get_schedules_data_dir(base_dir: str) -> str:
    d = os.path.join(get_data_dir(base_dir), "schedules")
    os.makedirs(d, exist_ok=True)
    return d


def get_orders_data_dir(base_dir: str) -> str:
    d = os.path.join(get_data_dir(base_dir), "orders")
    os.makedirs(d, exist_ok=True)
    return d


def get_etrn_data_dir(base_dir: str) -> str:
    override = (os.environ.get("ETRN_OUTPUT_DIR") or "").strip()
    if override:
        d = os.path.abspath(os.path.expanduser(override))
    else:
        d = os.path.join(get_data_dir(base_dir), "etrn_exports")
    os.makedirs(d, exist_ok=True)
    return d


def get_baselines_path(base_dir: str) -> str:
    return os.path.join(get_data_dir(base_dir), "schedule_baselines.json")


def get_advantum_cache_db_path(base_dir: str) -> str:
    return os.path.join(get_data_dir(base_dir), "advantum_shipments_cache.db")


def get_cors_allowed_origins() -> list[str]:
    raw = (os.environ.get("CORS_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return []
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def is_trust_proxy_headers() -> bool:
    return _env_bool("TRUST_PROXY_HEADERS", default=is_server_mode())


def get_session_ttl_seconds() -> int:
    raw = (os.environ.get("SESSION_TTL_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val >= 60:
            return val
    except ValueError:
        pass
    return 12 * 3600


def is_etrn_1c_enabled() -> bool:
    return _env_bool("ETRN_1C_AUTOMATION_ENABLED", default=not is_server_mode())


def is_smtp_configured() -> bool:
    return bool((os.environ.get("SMTP_HOST") or "").strip())


def is_ml_remote_mode() -> bool:
    return _env_bool("ML_REMOTE_MODE", default=False)


def is_notebook_configured() -> bool:
    explicit = (os.environ.get("ORDERS_NOTEBOOK_PATH") or "").strip()
    if explicit:
        return True
    if is_server_mode():
        return False
    return bool((os.environ.get("NETWORK_BASE_DIR") or "").strip())
