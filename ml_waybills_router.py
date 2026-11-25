"""
Модуль интеграции веб-портала «ПУЛЬС — Транспортная логистика» с локальным ML-сервисом
проверки маршрутных листов (`../ML` на Windows или `../Маршрутные листы` на macOS).

Зона ответственности модуля:
- кроссплатформенный автозапуск и контроль жизни процесса review_service.py (в .venv/venv ML-проекта или через общий Python);
- строго локальный HTTP-прокси (loopback 127.0.0.1 / localhost / ::1) в обход корпоративных системных прокси;
- проверка прав доступа (apt_waybills для операторов, role == "admin" для админ-маршрутов);
- принудительная серверная подстановка user, user_name и admin_user из текущей сессии портала;
- передача заголовка X-ML-Token при наличии ML_API_TOKEN.
"""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

import requests
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger("server.ml_waybills")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ML_HOST = "127.0.0.1"
DEFAULT_ML_PORT = 8765
MAX_PDF_UPLOAD_BYTES = 600 * 1024 * 1024
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_QUARANTINE_STATUSES = frozenset({"pending", "approved", "rejected", "all"})

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_ML_START_LOCK = threading.Lock()
_ML_PROC: Optional[subprocess.Popen] = None
_ML_PROC_URL: Optional[str] = None
_ML_WIN_JOB_HANDLE: Any = None
_ML_STDERR_TAIL: deque[str] = deque(maxlen=40)
_THREAD_LOCAL = threading.local()
_ATEXIT_REGISTERED = False


def _assign_win32_kill_on_close(proc: subprocess.Popen) -> None:
    """На Windows привязывает дочерний ML-процесс к Job Object, чтобы он умирал вместе с server.py."""
    global _ML_WIN_JOB_HANDLE
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        if _ML_WIN_JOB_HANDLE is None:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(job)
                return
            _ML_WIN_JOB_HANDLE = job

        proc_handle = getattr(proc, "_handle", None)
        if proc_handle:
            kernel32.AssignProcessToJobObject(_ML_WIN_JOB_HANDLE, int(proc_handle))
    except Exception as exc:
        logger.debug("Не удалось привязать ML-процесс к Win32 Job Object: %s", exc)


def _read_expected_ml_version(ml_dir: Optional[str]) -> Optional[str]:
    """Читает SERVICE_VERSION из review_service.py на диске."""
    if not ml_dir:
        return None
    script_path = os.path.join(ml_dir, "review_service.py")
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4096)
        m = re.search(r'^\s*SERVICE_VERSION\s*=\s*["\']([^"\']+)["\']', head, re.MULTILINE)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _stop_stale_ml_on_port(port: int) -> None:
    """Аккуратно завершает зависший процесс review_service.py на указанном локальном порту."""
    shutdown_ml_service()
    if sys.platform == "win32":
        killed_by_netstat = False
        try:
            ns = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=6,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pids: set[str] = set()
            for line in (ns.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING":
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port}") and (
                        local_addr.startswith("127.0.0.1:") or local_addr.startswith("[::1]:")
                    ):
                        pid = parts[4]
                        if pid.isdigit() and int(pid) > 0 and int(pid) != os.getpid():
                            pids.add(pid)
            for pid in pids:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/T", "/F"],
                    capture_output=True,
                    timeout=6,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                killed_by_netstat = True
        except Exception as exc:
            logger.debug("Не удалось остановить старый процесс через netstat/taskkill: %s", exc)

        if not killed_by_netstat:
            try:
                ps_cmd = (
                    f"Get-CimInstance Win32_Process | "
                    f"Where-Object {{ $_.CommandLine -like '*review_service.py*--port*{port}*' -or "
                    f"($_.CommandLine -like '*review_service.py*serve*' -and '{port}' -eq '8765') }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                logger.debug("Не удалось остановить старый review_service.py через PowerShell: %s", exc)
    else:
        try:
            subprocess.run(["pkill", "-f", f"review_service.py.*--port {port}"], capture_output=True, timeout=5)
        except Exception:
            pass
    time.sleep(0.4)


def _get_ml_port() -> int:
    raw = (os.environ.get("ML_SERVICE_PORT") or str(DEFAULT_ML_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Некорректный ML_SERVICE_PORT: {raw!r}",
        ) from exc
    if not (1 <= port <= 65535):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Порт ML_SERVICE_PORT вне диапазона 1..65535: {port}",
        )
    return port


def _get_ml_base_url() -> tuple[str, str, int]:
    """Возвращает (base_url, host, port) и гарантирует строго loopback-адрес."""
    default_url = f"http://{DEFAULT_ML_HOST}:{_get_ml_port()}"
    raw_url = (os.environ.get("ML_SERVICE_URL") or default_url).strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http",) or host not in ALLOWED_LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Из соображений ИБ ML_SERVICE_URL должен указывать строго на loopback-адрес (127.0.0.1 / localhost).",
        )
    port = parsed.port or _get_ml_port()
    normalized_host = "127.0.0.1" if host == "::1" else host
    return raw_url, normalized_host, port


def _find_ml_service_dir() -> tuple[Optional[str], list[str]]:
    """
    Ищет каталог ML-проекта с файлом review_service.py в порядке приоритета:
    1) ML_SERVICE_DIR из окружения / .env (если задан);
    2) соседняя папка ../ML (корпоративный Windows-ПК);
    3) соседняя папка ../Маршрутные листы (macOS).
    """
    parent_dir = os.path.dirname(BASE_DIR)
    candidates: list[str] = []

    env_dir = (os.environ.get("ML_SERVICE_DIR") or "").strip()
    if env_dir:
        candidates.append(os.path.abspath(os.path.expanduser(env_dir)))

    for folder_name in ("ML", "Маршрутные листы"):
        cand = os.path.abspath(os.path.join(parent_dir, folder_name))
        if cand not in candidates:
            candidates.append(cand)

    for idx, cand in enumerate(candidates):
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "review_service.py")):
            return cand, candidates
        if idx == 0 and env_dir:
            logger.warning(
                "ML_SERVICE_DIR=%r задан, но review_service.py не найден — проверяем соседние каталоги.",
                cand,
            )

    return None, candidates


def _get_ml_service_dir() -> str:
    found, candidates = _find_ml_service_dir()
    return found or candidates[0]


def _get_ml_api_token() -> str:
    return (os.environ.get("ML_API_TOKEN") or "").strip()


def _is_inside_prefix(exe_path: str, prefix_dir: str) -> bool:
    try:
        norm_exe = os.path.normcase(os.path.abspath(exe_path))
        norm_pref = os.path.normcase(os.path.abspath(prefix_dir))
        return os.path.commonpath([norm_exe, norm_pref]) == norm_pref
    except Exception:
        return False


def _resolve_ml_python(ml_dir: str) -> str:
    """
    Выбирает интерпретатор Python для автозапуска review_service.py:
    1) ML_PYTHON_EXE из окружения / .env (если задан);
    2) <ML_SERVICE_DIR>/.venv/(bin/python | Scripts/python.exe);
    3) <ML_SERVICE_DIR>/venv/(bin/python | Scripts/python.exe);
    4) Если портал запущен в своём .venv (sys.prefix != sys.base_prefix) —
       глобальный/базовый Python (sys.base_prefix, sys._base_executable, PATH вне sys.prefix);
    5) sys.executable (текущий Python, под которым запущен server.py).
    """
    env_python = (os.environ.get("ML_PYTHON_EXE") or "").strip()
    if env_python:
        expanded = os.path.expanduser(env_python)
        has_sep = (os.sep in expanded) or ("/" in expanded) or ("\\" in expanded) or os.path.isabs(expanded)
        if has_sep:
            abs_py = os.path.abspath(expanded)
            if os.path.isfile(abs_py):
                return abs_py
        else:
            found_which = shutil.which(expanded)
            if found_which:
                return found_which
            if os.path.isfile(expanded):
                return os.path.abspath(expanded)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Указанный в ML_PYTHON_EXE интерпретатор Python не найден: {env_python}",
        )

    is_win = sys.platform == "win32"
    rel_parts = ("Scripts", "python.exe") if is_win else ("bin", "python")
    for venv_name in (".venv", "venv"):
        cand = os.path.join(ml_dir, venv_name, *rel_parts)
        if os.path.isfile(cand):
            return cand

    in_portal_venv = (
        os.path.normcase(os.path.abspath(sys.prefix))
        != os.path.normcase(os.path.abspath(sys.base_prefix))
    )
    if in_portal_venv:
        base_candidates: list[str] = []
        if is_win:
            base_candidates.append(os.path.join(sys.base_prefix, "python.exe"))
        else:
            base_candidates.append(os.path.join(sys.base_prefix, "bin", "python3"))
            base_candidates.append(os.path.join(sys.base_prefix, "bin", "python"))

        base_exe = getattr(sys, "_base_executable", None)
        if base_exe:
            base_candidates.append(base_exe)

        for cand in base_candidates:
            if cand and os.path.isfile(cand) and not _is_inside_prefix(cand, sys.prefix):
                return os.path.abspath(cand)

        for cmd_name in (("python", "python3") if is_win else ("python3", "python")):
            found = shutil.which(cmd_name)
            if found and os.path.isfile(found) and not _is_inside_prefix(found, sys.prefix):
                return os.path.abspath(found)

    return sys.executable


def _get_loopback_session() -> requests.Session:
    """Возвращает потокобезопасную requests.Session с полным обходом системных HTTP-прокси."""
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.trust_env = False
        sess.proxies = {"http": None, "https": None}
        _THREAD_LOCAL.session = sess
    return sess


def _validate_id(value: str, field_name: str = "id") -> str:
    if not value or not SAFE_ID_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Некорректный идентификатор '{field_name}'.",
        )
    return value


def _validate_filename(value: str, field_name: str = "crop_file") -> str:
    if (
        not value
        or value in (".", "..")
        or ".." in value
        or value.startswith(".")
        or not SAFE_FILENAME_RE.match(value)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Некорректное имя файла '{field_name}'.",
        )
    return value


def _drain_stderr(pipe: Any) -> None:
    try:
        for raw_line in iter(pipe.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                _ML_STDERR_TAIL.append(line)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def shutdown_ml_service() -> None:
    """Корректно завершает дочерний ML-процесс, если его запускал портал."""
    global _ML_PROC, _ML_PROC_URL
    proc = _ML_PROC
    if proc is None:
        return
    if proc.poll() is None:
        logger.info("Остановка дочернего ML-сервиса (PID=%s)...", proc.pid)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _ML_PROC = None
    _ML_PROC_URL = None


def is_ml_service_healthy(base_url: Optional[str] = None, timeout: float = 1.5) -> tuple[bool, Optional[dict[str, Any]]]:
    """Проверяет доступность GET /api/ml/health на loopback-адресе."""
    if base_url is None:
        base_url, _, _ = _get_ml_base_url()
    sess = _get_loopback_session()
    try:
        resp = sess.get(
            f"{base_url}/api/ml/health",
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "ok" and "class_names" in data:
            return True, data
    except Exception:
        pass
    return False, None


def _check_outlook_bridge_conflict(base_url: str) -> bool:
    """Проверяет, не занят ли локальный порт сервисом outlook_bridge.py."""
    sess = _get_loopback_session()
    try:
        resp = sess.get(
            f"{base_url}/ping",
            timeout=0.8,
            proxies={"http": None, "https": None},
        )
        if resp.status_code == 200:
            text_low = (resp.text or "").lower()
            if "outlook" in text_low or "bridge" in text_low or "status" in text_low:
                return True
    except Exception:
        pass
    return False


def ensure_ml_service_running(wait_timeout: float = 35.0) -> dict[str, Any]:
    """
    Гарантирует, что локальный ML-сервис review_service.py запущен и отвечает на /api/ml/health.
    При необходимости автоматически поднимает его (из .venv/venv ML-проекта или через sys.executable / ML_PYTHON_EXE).
    """
    global _ML_PROC, _ML_PROC_URL, _ATEXIT_REGISTERED

    base_url, host, port = _get_ml_base_url()
    ml_dir_found, _ = _find_ml_service_dir()
    expected_ver = _read_expected_ml_version(ml_dir_found)

    ok, data = is_ml_service_healthy(base_url, timeout=1.5)
    if ok and data is not None:
        if not expected_ver or str(data.get("version") or "") == expected_ver:
            return data

    with _ML_START_LOCK:
        ok, data = is_ml_service_healthy(base_url, timeout=1.5)
        if ok and data is not None:
            if not expected_ver or str(data.get("version") or "") == expected_ver:
                return data
            logger.warning(
                "На %s работает устаревший ML-сервис v%s (ожидается v%s). Перезапускаем...",
                base_url,
                data.get("version"),
                expected_ver,
            )
            _stop_stale_ml_on_port(port)

        if _check_outlook_bridge_conflict(base_url):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Локальный порт {port} занят другим сервисом (вероятно, outlook_bridge.py). "
                    "Задайте ML_SERVICE_PORT=8766 в .env или закройте outlook_bridge на сервере."
                ),
            )

        proc_alive_for_same_url = (
            _ML_PROC is not None
            and _ML_PROC.poll() is None
            and _ML_PROC_URL == base_url
        )

        if not proc_alive_for_same_url:
            if _ML_PROC is not None and _ML_PROC.poll() is None and _ML_PROC_URL != base_url:
                shutdown_ml_service()

            ml_dir, checked_dirs = _find_ml_service_dir()
            if not ml_dir:
                checked_str = ", ".join(checked_dirs)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"Не найден каталог ML-проекта с файлом review_service.py (проверено: {checked_str}). "
                        "Укажите путь в переменной окружения ML_SERVICE_DIR."
                    ),
                )

            review_script = os.path.join(ml_dir, "review_service.py")
            ml_python = _resolve_ml_python(ml_dir)

            _ML_STDERR_TAIL.clear()
            child_env = os.environ.copy()
            child_env["PYTHONUNBUFFERED"] = "1"
            child_env["PYTHONUTF8"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
            token = _get_ml_api_token()
            if token:
                child_env["ML_API_TOKEN"] = token

            popen_kwargs: dict[str, Any] = {
                "cwd": ml_dir,
                "env": child_env,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            cmd = [
                ml_python,
                review_script,
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ]
            logger.info("Автозапуск ML-сервиса: %s", " ".join(cmd))
            try:
                _ML_PROC = subprocess.Popen(cmd, **popen_kwargs)
                _ML_PROC_URL = base_url
                _assign_win32_kill_on_close(_ML_PROC)
            except Exception as exc:
                _ML_PROC = None
                _ML_PROC_URL = None
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Не удалось запустить ML-сервис: {exc}",
                ) from exc

            if _ML_PROC.stderr is not None:
                threading.Thread(
                    target=_drain_stderr,
                    args=(_ML_PROC.stderr,),
                    name="ml-service-stderr",
                    daemon=True,
                ).start()

            if not _ATEXIT_REGISTERED:
                atexit.register(shutdown_ml_service)
                _ATEXIT_REGISTERED = True

        deadline = time.monotonic() + max(5.0, wait_timeout)
        while time.monotonic() < deadline:
            ok, data = is_ml_service_healthy(base_url, timeout=1.0)
            if ok and data is not None:
                logger.info("ML-сервис готов на %s (version=%s)", base_url, data.get("version"))
                return data
            if _ML_PROC is not None and _ML_PROC.poll() is not None:
                code = _ML_PROC.returncode
                tail = " | ".join(list(_ML_STDERR_TAIL)[-8:])
                _ML_PROC = None
                _ML_PROC_URL = None
                logger.error("ML-сервис аварийно завершился (код=%s): %s", code, tail)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"ML-сервис завершился с ошибкой (код {code}). {tail or 'Проверьте логи review_service.py.'}",
                )
            time.sleep(0.3)

    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=f"Превышено время ожидания запуска ML-сервиса на {base_url}.",
    )


def start_ml_service_background() -> None:
    """Асинхронный фоновый прогрев/запуск ML-сервиса при старте server.py."""
    def _runner() -> None:
        try:
            ensure_ml_service_running(wait_timeout=45.0)
        except Exception as exc:
            logger.warning("Фоновый автозапуск ML-сервиса не выполнен (повторится при обращении в раздел АПТ): %s", exc)

    threading.Thread(target=_runner, name="ml-service-autostart", daemon=True).start()


def _safe_content_disposition(upstream_header: Optional[str], fallback_ascii: str) -> str:
    if upstream_header:
        try:
            upstream_header.encode("latin-1")
            return upstream_header
        except UnicodeEncodeError:
            match = re.search(r'filename="([^"]+)"', upstream_header)
            if match:
                orig_name = os.path.basename(match.group(1))
                quoted = urllib.parse.quote(orig_name, safe="")
                return f'attachment; filename="{fallback_ascii}"; filename*=UTF-8\'\'{quoted}'
    return f'attachment; filename="{fallback_ascii}"'


def _extract_upstream_error(resp: requests.Response) -> str:
    try:
        err_json = resp.json()
        if isinstance(err_json, dict):
            msg = err_json.get("error") or err_json.get("detail")
            if msg:
                return str(msg)
    except Exception:
        pass
    text = (resp.text or "").strip()
    return text[:300] or f"Ошибка ML-сервиса (HTTP {resp.status_code})."


def _upstream_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    raw_body: Any = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 30.0,
    include_token: bool = True,
    stream: bool = False,
) -> requests.Response:
    base_url, _, _ = _get_ml_base_url()
    req_headers: dict[str, str] = dict(headers or {})
    if include_token:
        token = _get_ml_api_token()
        if token:
            req_headers["X-ML-Token"] = token

    if isinstance(raw_body, (bytes, bytearray)) and "Content-Length" not in req_headers:
        req_headers["Content-Length"] = str(len(raw_body))

    url = f"{base_url}{path}"
    sess = requests.Session() if stream else _get_loopback_session()
    if stream:
        sess.trust_env = False
        sess.proxies = {"http": None, "https": None}

    def _do_call() -> requests.Response:
        return sess.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            data=raw_body,
            headers=req_headers,
            timeout=timeout,
            proxies={"http": None, "https": None},
            stream=stream,
        )

    try:
        resp = _do_call()
    except requests.ConnectionError:
        ensure_ml_service_running()
        try:
            resp = _do_call()
        except requests.RequestException as exc:
            if stream:
                sess.close()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"ML-сервис недоступен: {exc}",
            ) from exc
    except requests.Timeout as exc:
        if stream:
            sess.close()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Превышено время ожидания ответа от ML-сервиса.",
        ) from exc
    except requests.RequestException as exc:
        if stream:
            sess.close()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка связи с локальным ML-сервисом: {exc}",
        ) from exc

    if resp.status_code >= 400:
        err_detail = _extract_upstream_error(resp)
        if stream:
            try:
                resp.close()
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
        raise HTTPException(
            status_code=resp.status_code,
            detail=err_detail,
        )
    if stream:
        setattr(resp, "_owned_session", sess)
    return resp


async def _parse_json_object(request: Request, allow_empty: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        if allow_empty:
            return {}
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Тело запроса не может быть пустым.")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный JSON в теле запроса.") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ожидается JSON-объект.")
    return parsed


def create_ml_router(check_auth_fn: Callable[..., dict[str, Any]]) -> APIRouter:
    """Создаёт FastAPI APIRouter со всеми прокси-маршрутами раздела «АПТ → Маршрутные листы»."""
    router = APIRouter(tags=["APT Waybills ML"])

    # ─── ОПЕРАТОРСКИЕ МАРШРУТЫ (required_section="apt_waybills") ──────────

    @router.get("/api/ml/health")
    def ml_health(request: Request):
        check_auth_fn(request, required_section="apt_waybills")
        data = ensure_ml_service_running()
        base_url, _, _ = _get_ml_base_url()
        result = dict(data)
        result["ml_service_url"] = base_url
        result["managed_by_portal"] = _ML_PROC is not None and _ML_PROC.poll() is None
        return result

    @router.get("/api/ml/sessions")
    def ml_list_sessions(request: Request, limit: int = Query(5, ge=1, le=50)):
        check_auth_fn(request, required_section="apt_waybills")
        resp = _upstream_request("GET", "/api/ml/sessions", params={"limit": limit}, timeout=15.0)
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.post("/api/ml/sessions/upload")
    async def ml_upload_pdf(
        request: Request,
        filename: Optional[str] = Query(None),
        max_pages: int = Query(3500, ge=1, le=5000),
    ):
        check_auth_fn(request, required_section="apt_waybills")
        content_type = (request.headers.get("content-type") or "").lower()

        resolved_name = (filename or "").strip()
        tmp_path: Optional[str] = None
        raw_bytes: Optional[bytes] = None
        total_size = 0

        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                file_field = form.get("file")
                if file_field is None or not hasattr(file_field, "read"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Не передан PDF-файл (поле 'file').",
                    )
                if not resolved_name:
                    resolved_name = str(getattr(file_field, "filename", "") or "waybills.pdf")
                if "max_pages" in form:
                    try:
                        max_pages = max(1, min(5000, int(str(form.get("max_pages")))))
                    except ValueError:
                        pass

                tmp_fd, tmp_path = tempfile.mkstemp(prefix="puls_pdf_", suffix=".pdf")
                os.close(tmp_fd)
                first_chunk = True
                with open(tmp_path, "wb") as tmp_out:
                    while True:
                        chunk = await file_field.read(1024 * 1024)
                        if not chunk:
                            break
                        if first_chunk:
                            first_chunk = False
                            if not chunk.startswith(b"%PDF-"):
                                raise HTTPException(
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Файл должен быть корректным PDF-документом (ожидается заголовок %PDF-).",
                                )
                        total_size += len(chunk)
                        if total_size > MAX_PDF_UPLOAD_BYTES:
                            raise HTTPException(
                                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail="Размер PDF превышает допустимый лимит 600 МБ.",
                            )
                        tmp_out.write(chunk)

                if total_size == 0:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Загруженный файл пуст.")
            else:
                raw_bytes = await request.body()
                if not resolved_name:
                    resolved_name = "waybills.pdf"
                total_size = len(raw_bytes)
                if total_size == 0:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Загруженный файл пуст.")
                if total_size > MAX_PDF_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Размер PDF превышает допустимый лимит 600 МБ.",
                    )
                if not raw_bytes.startswith(b"%PDF-"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Файл должен быть корректным PDF-документом (ожидается заголовок %PDF-).",
                    )

            safe_filename = os.path.basename(resolved_name.replace("\\", "/")) or "waybills.pdf"

            def _send_with_pages(mp: int) -> requests.Response:
                headers = {
                    "Content-Type": "application/pdf",
                    "Content-Length": str(total_size),
                }
                if tmp_path is not None:
                    with open(tmp_path, "rb") as f_stream:
                        return _upstream_request(
                            "POST",
                            "/api/ml/sessions/upload",
                            params={"filename": safe_filename, "max_pages": mp},
                            raw_body=f_stream,
                            headers=headers,
                            timeout=300.0,
                        )
                return _upstream_request(
                    "POST",
                    "/api/ml/sessions/upload",
                    params={"filename": safe_filename, "max_pages": mp},
                    raw_body=raw_bytes,
                    headers=headers,
                    timeout=300.0,
                )

            def _forward_upload() -> requests.Response:
                try:
                    return _send_with_pages(max_pages)
                except HTTPException as exc:
                    if exc.status_code == 400 and max_pages > 250 and "250" in str(exc.detail):
                        logger.warning(
                            "ML-сервис отклонил max_pages=%s (старый лимит 250) — повторяем с max_pages=250.",
                            max_pages,
                        )
                        return _send_with_pages(250)
                    raise

            resp = await asyncio.to_thread(_forward_upload)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @router.get("/api/ml/sessions/{session_id}/status")
    def ml_session_status(request: Request, session_id: str):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        resp = _upstream_request("GET", f"/api/ml/sessions/{sid}/status", timeout=15.0)
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.get("/api/ml/sessions/{session_id}")
    def ml_get_session(request: Request, session_id: str):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        resp = _upstream_request("GET", f"/api/ml/sessions/{sid}", timeout=90.0)
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.get("/api/ml/sessions/{session_id}/report")
    def ml_download_report(request: Request, session_id: str):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        resp = _upstream_request("GET", f"/api/ml/sessions/{sid}/report", timeout=30.0)
        cd = _safe_content_disposition(resp.headers.get("Content-Disposition"), f"report_{sid}.txt")
        return Response(
            content=resp.content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": cd,
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/ml/sessions/{session_id}/archive-pdf")
    def ml_download_archive_pdf(request: Request, session_id: str):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        resp = _upstream_request("GET", f"/api/ml/sessions/{sid}/archive-pdf", timeout=180.0)
        cd = _safe_content_disposition(resp.headers.get("Content-Disposition"), f"checked_{sid}.pdf")
        return Response(
            content=resp.content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": cd,
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/ml/sessions/{session_id}/export-zip")
    def ml_download_export_zip(request: Request, session_id: str):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        try:
            resp = _upstream_request("GET", f"/api/ml/sessions/{sid}/export-zip", timeout=300.0, stream=True)
        except HTTPException as exc:
            if exc.status_code == 404:
                ml_dir_found, _ = _find_ml_service_dir()
                disk_ver = _read_expected_ml_version(ml_dir_found)
                hint = (
                    f"В папке ML установлена старая версия сервиса (v{disk_ver}). Запустите python update.py также в папке ML и перезапустите сервер."
                    if disk_ver == "1.0.0"
                    else "Маршрут export-zip не найден в запущенном ML-сервисе. Обновите папку ML (python update.py) и перезапустите сервер."
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=hint,
                ) from exc
            raise
        cd = _safe_content_disposition(resp.headers.get("Content-Disposition"), f"waybills_{sid}.zip")
        headers = {
            "Content-Disposition": cd,
            "Cache-Control": "no-store",
        }
        cl = resp.headers.get("Content-Length")
        if cl:
            headers["Content-Length"] = cl

        def _stream_chunks():
            owned_sess = getattr(resp, "_owned_session", None)
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                if owned_sess is not None:
                    try:
                        owned_sess.close()
                    except Exception:
                        pass

        return StreamingResponse(
            _stream_chunks(),
            media_type="application/zip",
            headers=headers,
        )

    @router.post("/api/ml/sessions/{session_id}/finalize")
    async def ml_finalize_session(request: Request, session_id: str):
        user = check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        payload = await _parse_json_object(request, allow_empty=True)
        payload.pop("actor", None)
        payload["user"] = user["login"]
        payload["user_name"] = user.get("full_name") or user["login"]

        raw_manual_ids = payload.get("manual_markup_page_ids")
        manual_page_ids: list[str] = []
        if isinstance(raw_manual_ids, list):
            seen_manual: set[str] = set()
            for item in raw_manual_ids:
                if item is None:
                    continue
                pid = _validate_id(str(item), "page_id")
                if pid not in seen_manual:
                    seen_manual.add(pid)
                    manual_page_ids.append(pid)
        if manual_page_ids:
            payload["manual_markup_page_ids"] = manual_page_ids
        else:
            payload.pop("manual_markup_page_ids", None)

        def _do_finalize() -> dict[str, Any]:
            result: dict[str, Any]
            try:
                resp = _upstream_request(
                    "POST",
                    f"/api/ml/sessions/{sid}/finalize",
                    json_body=payload,
                    timeout=120.0,
                )
                result = resp.json()
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise

                # Fallback для старой версии review_service.py без /finalize:
                # отправляем в карантин только страницы с правками оператора, где не осталось ошибок.
                sess_resp = _upstream_request("GET", f"/api/ml/sessions/{sid}", timeout=90.0)
                sess_data = sess_resp.json()
                submitted_pages = 0
                submitted_crops = 0
                last_summary = sess_data.get("summary")

                for page in sess_data.get("pages") or []:
                    if page.get("quarantine_item_id"):
                        continue
                    windows = page.get("windows") or []
                    has_diff = any(
                        (
                            (w.get("original_elements") is not None and (w.get("original_elements") or []) != (w.get("elements") or []))
                            or w.get("review_status") in ("fixed_clean", "fixed_with_errors")
                            or any(e.get("source") == "operator" for e in (w.get("elements") or []))
                        )
                        for w in windows
                    )
                    has_dismissed = any(w.get("review_status") == "dismissed_clean" for w in windows)
                    all_clean = all(not (w.get("errors") or []) and not (w.get("warnings") or []) for w in windows)
                    if has_diff and not has_dismissed and all_clean and page.get("page_id"):
                        sub_resp = _upstream_request(
                            "POST",
                            f"/api/ml/sessions/{sid}/pages/{page['page_id']}/submit-quarantine",
                            json_body={
                                "user": payload["user"],
                                "user_name": payload["user_name"],
                                "comment": payload.get("comment") or "",
                            },
                            timeout=30.0,
                        )
                        sub_json = sub_resp.json()
                        submitted_pages += 1
                        submitted_crops += int(sub_json.get("digit_crops_count") or 0)
                        if sub_json.get("session_summary"):
                            last_summary = sub_json["session_summary"]

                result = {
                    "status": "finalized",
                    "fallback": True,
                    "submitted_pages": submitted_pages,
                    "submitted_crops": submitted_crops,
                    "session_summary": last_summary,
                }

            # Гарантируем попадание страниц с ручной правкой класса/рамки в очередь дообучения,
            # даже если окно осталось со статусом confirmed_error (например: Печать -> Плохая печать).
            if manual_page_ids:
                sess_resp = _upstream_request("GET", f"/api/ml/sessions/{sid}", timeout=90.0)
                sess_data = sess_resp.json()
                manual_set = set(manual_page_ids)
                extra_pages = 0
                extra_crops = 0
                last_summary = result.get("session_summary") or sess_data.get("summary")

                for page in sess_data.get("pages") or []:
                    pid = page.get("page_id")
                    if not pid or pid not in manual_set or page.get("quarantine_item_id"):
                        continue
                    windows = page.get("windows") or []
                    has_dismissed = any(w.get("review_status") == "dismissed_clean" for w in windows)
                    has_unresolved = any(w.get("review_status") in ("pending", "fixed_with_errors") for w in windows)
                    if has_dismissed or has_unresolved:
                        continue
                    try:
                        sub_resp = _upstream_request(
                            "POST",
                            f"/api/ml/sessions/{sid}/pages/{pid}/submit-quarantine",
                            json_body={
                                "user": payload["user"],
                                "user_name": payload["user_name"],
                                "comment": "[РУЧНАЯ ПРАВКА] Ручное исправление класса/рамки на изображении",
                            },
                            timeout=30.0,
                        )
                        sub_json = sub_resp.json()
                        extra_pages += 1
                        extra_crops += int(sub_json.get("digit_crops_count") or 0)
                        if sub_json.get("session_summary"):
                            last_summary = sub_json["session_summary"]
                    except HTTPException as sub_exc:
                        if sub_exc.status_code == 400:
                            continue
                        raise

                if extra_pages > 0:
                    base_pages = int(result.get("submitted_pages") or result.get("auto_quarantined_pages") or result.get("quarantined_pages") or 0)
                    base_crops = int(result.get("submitted_crops") or 0)
                    result["submitted_pages"] = base_pages + extra_pages
                    result["submitted_crops"] = base_crops + extra_crops
                    if last_summary is not None:
                        result["session_summary"] = last_summary

            return result

        data = await asyncio.to_thread(_do_finalize)
        return JSONResponse(status_code=200, content=data)

    @router.get("/api/ml/sessions/{session_id}/pages/{page_id}/image")
    def ml_get_page_image(
        request: Request,
        session_id: str,
        page_id: str,
        annotated: int = Query(0, ge=0, le=1),
    ):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        pid = _validate_id(page_id, "page_id")
        resp = _upstream_request(
            "GET",
            f"/api/ml/sessions/{sid}/pages/{pid}/image",
            params={"annotated": annotated},
            timeout=30.0,
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/png"),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/ml/sessions/{session_id}/pages/{page_id}/windows/{window_idx}/image")
    def ml_get_window_image(
        request: Request,
        session_id: str,
        page_id: str,
        window_idx: int,
    ):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        pid = _validate_id(page_id, "page_id")
        if window_idx < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный индекс окна.")
        resp = _upstream_request(
            "GET",
            f"/api/ml/sessions/{sid}/pages/{pid}/windows/{window_idx}/image",
            timeout=30.0,
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/png"),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/ml/sessions/{session_id}/pages/{page_id}/elements/{element_id}/crop")
    def ml_get_element_crop(
        request: Request,
        session_id: str,
        page_id: str,
        element_id: str,
    ):
        check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        pid = _validate_id(page_id, "page_id")
        eid = _validate_id(element_id, "element_id")
        resp = _upstream_request(
            "GET",
            f"/api/ml/sessions/{sid}/pages/{pid}/elements/{eid}/crop",
            timeout=30.0,
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/ml/sessions/{session_id}/pages/{page_id}/windows/{window_idx}/action")
    async def ml_window_action(
        request: Request,
        session_id: str,
        page_id: str,
        window_idx: int,
    ):
        user = check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        pid = _validate_id(page_id, "page_id")
        if window_idx < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный индекс окна.")

        payload = await _parse_json_object(request, allow_empty=False)
        payload.pop("actor", None)
        payload["user"] = user["login"]
        payload["user_name"] = user.get("full_name") or user["login"]

        try:
            resp = await asyncio.to_thread(
                _upstream_request,
                "POST",
                f"/api/ml/sessions/{sid}/pages/{pid}/windows/{window_idx}/action",
                json_body=payload,
                timeout=30.0,
            )
        except HTTPException as exc:
            if (
                exc.status_code == 400
                and payload.get("action") == "mark_valid"
                and "mark_valid" in str(exc.detail)
            ):
                ml_dir_found, _ = _find_ml_service_dir()
                disk_ver = _read_expected_ml_version(ml_dir_found)
                hint = (
                    f"В папке ML установлена старая версия сервиса (v{disk_ver}). Запустите python update.py также в папке ML и перезапустите сервер."
                    if disk_ver == "1.0.0"
                    else f"{exc.detail}. Обновите ML-модуль (review_service.py) и перезапустите сервер."
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=hint,
                ) from exc
            raise
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.post("/api/ml/sessions/{session_id}/pages/{page_id}/submit-quarantine")
    async def ml_submit_quarantine(
        request: Request,
        session_id: str,
        page_id: str,
    ):
        user = check_auth_fn(request, required_section="apt_waybills")
        sid = _validate_id(session_id, "session_id")
        pid = _validate_id(page_id, "page_id")

        payload = await _parse_json_object(request, allow_empty=True)
        payload.pop("actor", None)
        payload["user"] = user["login"]
        payload["user_name"] = user.get("full_name") or user["login"]

        resp = await asyncio.to_thread(
            _upstream_request,
            "POST",
            f"/api/ml/sessions/{sid}/pages/{pid}/submit-quarantine",
            json_body=payload,
            timeout=30.0,
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    # ─── АДМИНСКИЕ МАРШРУТЫ (строго require_admin=True) ────────────────────

    @router.post("/api/ml/sessions")
    async def ml_create_session_from_path(request: Request):
        check_auth_fn(request, require_admin=True)
        payload = await _parse_json_object(request, allow_empty=False)
        resp = await asyncio.to_thread(
            _upstream_request,
            "POST",
            "/api/ml/sessions",
            json_body=payload,
            timeout=30.0,
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.post("/api/ml/sessions/{session_id}/delete")
    @router.delete("/api/ml/sessions/{session_id}")
    def ml_delete_session(request: Request, session_id: str):
        check_auth_fn(request, require_admin=True)
        sid = _validate_id(session_id, "session_id")
        try:
            resp = _upstream_request("DELETE", f"/api/ml/sessions/{sid}", timeout=20.0)
        except HTTPException as exc:
            if exc.status_code == 404 and "маршрут" in str(exc.detail).lower():
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="Удаление сессий поддерживается после обновления ML-модуля (review_service.py).",
                ) from exc
            raise
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.get("/api/ml/admin/quarantine")
    def ml_admin_list_quarantine(request: Request, status_filter: str = Query("pending", alias="status")):
        check_auth_fn(request, require_admin=True)
        st = (status_filter or "pending").strip().lower()
        if st not in ALLOWED_QUARANTINE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Недопустимый фильтр status (pending|approved|rejected|all).",
            )
        resp = _upstream_request(
            "GET",
            "/api/ml/admin/quarantine",
            params={"status": st},
            timeout=20.0,
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.get("/api/ml/admin/quarantine/{item_id}")
    def ml_admin_get_quarantine_item(request: Request, item_id: str):
        check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        resp = _upstream_request("GET", f"/api/ml/admin/quarantine/{qid}", timeout=20.0)
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.get("/api/ml/admin/quarantine/{item_id}/image")
    def ml_admin_quarantine_image(request: Request, item_id: str):
        check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        resp = _upstream_request("GET", f"/api/ml/admin/quarantine/{qid}/image", timeout=30.0)
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/png"),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/ml/admin/quarantine/{item_id}/windows/{window_idx}/image")
    def ml_admin_quarantine_window_image(request: Request, item_id: str, window_idx: int):
        check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        if window_idx < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный индекс окна.")
        resp = _upstream_request(
            "GET",
            f"/api/ml/admin/quarantine/{qid}/windows/{window_idx}/image",
            timeout=30.0,
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/png"),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/ml/admin/quarantine/{item_id}/crops/{crop_file}")
    def ml_admin_quarantine_crop(request: Request, item_id: str, crop_file: str):
        check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        safe_crop = _validate_filename(crop_file, "crop_file")
        resp = _upstream_request(
            "GET",
            f"/api/ml/admin/quarantine/{qid}/crops/{safe_crop}",
            timeout=20.0,
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/ml/admin/quarantine/{item_id}/windows/{window_idx}/edit")
    async def ml_admin_quarantine_window_edit(request: Request, item_id: str, window_idx: int):
        user = check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        if window_idx < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный индекс окна.")

        payload = await _parse_json_object(request, allow_empty=False)
        edits = payload.get("edits")
        allowed_actions = {"add_box", "delete_box", "update_box", "relabel_box", "fix_digit"}
        if not isinstance(edits, list) or not (1 <= len(edits) <= 200):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Поле 'edits' должно быть списком от 1 до 200 правок.",
            )
        for idx, item in enumerate(edits):
            if not isinstance(item, dict) or item.get("action") not in allowed_actions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Некорректная правка #{idx + 1}: допустимы только add_box, delete_box, update_box, relabel_box, fix_digit.",
                )

        upstream_payload = {
            "edits": edits,
            "admin_user": user["login"],
            "admin_name": user.get("full_name") or user["login"],
        }
        try:
            resp = await asyncio.to_thread(
                _upstream_request,
                "POST",
                f"/api/ml/admin/quarantine/{qid}/windows/{window_idx}/edit",
                json_body=upstream_payload,
                timeout=60.0,
            )
        except HTTPException as exc:
            detail_low = str(exc.detail).lower()
            if exc.status_code == 404 and ("маршрут" in detail_low or "unknown" in detail_low):
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="Правка разметки в очереди дообучения поддерживается после обновления ML-модуля (review_service.py v1.3.0).",
                ) from exc
            raise
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.post("/api/ml/admin/quarantine/{item_id}/decision")
    async def ml_admin_quarantine_decision(request: Request, item_id: str):
        user = check_auth_fn(request, require_admin=True)
        qid = _validate_id(item_id, "item_id")
        payload = await _parse_json_object(request, allow_empty=False)
        payload["admin_user"] = user["login"]

        resp = await asyncio.to_thread(
            _upstream_request,
            "POST",
            f"/api/ml/admin/quarantine/{qid}/decision",
            json_body=payload,
            timeout=20.0,
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @router.post("/api/ml/admin/export-approved")
    async def ml_admin_export_approved(request: Request):
        check_auth_fn(request, require_admin=True)
        payload = await _parse_json_object(request, allow_empty=True)
        resp = await asyncio.to_thread(
            _upstream_request,
            "POST",
            "/api/ml/admin/export-approved",
            json_body={"include_exported": bool(payload.get("include_exported", False))},
            timeout=120.0,
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    return router
