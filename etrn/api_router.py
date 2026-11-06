"""
api_router.py — FastAPI Router для интеграции отчетов ЭТРН в веб-сервер «ПУЛЬС — Транспортная логистика».

Предоставляет API:
1. POST /api/etrn/process-files — Сборка из 3 загруженных файлов (Drag-and-Drop в браузере).
2. POST /api/etrn/generate-task — Фоновый запуск автовыгрузки из 1С по датам.
3. GET  /api/etrn/task-status/{task_id} — Опрос прогресса формирования (0-100%).
4. GET  /api/etrn/download/{task_id} — Скачивание сформированного Excel-отчета.
5. POST /api/etrn/prepare-email — Подготовка письма с отчетом для локального Outlook сотрудника.
6. POST /api/etrn/send-email — Отправка письма с отчетом через сервер (fallback).
"""

from __future__ import annotations

import base64
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from .etrn_service import EtrnService
except ImportError:
    from etrn_service import EtrnService

router = APIRouter(prefix="/api/etrn", tags=["ЭТРН"])
service = EtrnService()

# Потокобезопасное хранилище задач
_TASKS_LOCK = threading.Lock()
_TASKS_STORE: Dict[str, Dict[str, Any]] = {}

ETRN_EMAIL_TO = "ab.smirnov@puls.ru; e.sokovnin@puls.ru; n.rozhkov@puls.ru"
ETRN_EMAIL_CC = "e.myalkin@puls.ru"


def _is_same_host_client(request: Request) -> bool:
    """Проверяет, открыт ли браузер непосредственно на том же ПК, где запущен сервер."""
    if not request.client:
        return False
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    client_ip = (request.client.host or "").strip()
    return client_ip in ("127.0.0.1", "::1", "localhost")


class GenerateReportRequest(BaseModel):
    """Тело запроса на фоновое формирование отчета."""
    date_from: str = Field(..., description="Дата начала (ДД.ММ.ГГГГ)", example="03.11.2026")
    date_to: Optional[str] = Field(None, description="Дата окончания (ДД.ММ.ГГГГ)", example="05.11.2026")


class EtrnEmailRequest(BaseModel):
    task_id: str
    mode: str = "draft"  # "draft" или "send"


# =============================================================================
# 1. Сборка отчета из файлов (Multipart Upload)
# =============================================================================
@router.post(
    "/process-files",
    summary="Собрать отчет из 3 файлов Excel",
    description="Принимает файлы «ЭТРН», «Модель расчетов» и «Процент» и формирует готовый сводный файл XLSX.",
)
async def process_files(
    request: Request,
    etrn_file: UploadFile = File(..., description="Файл «ЭТРН 1с.xlsx»"),
    model_file: UploadFile = File(..., description="Файл «Модель расчетов.xlsx»"),
    percent_file: UploadFile = File(..., description="Файл «Процент.xlsx»"),
    date_label: Optional[str] = Form(None, description="Метка даты для имени файла (напр. '04.11.2026')"),
    as_json: Optional[bool] = Form(False, description="Вернуть JSON с task_id вместо бинарного потока"),
):
    try:
        etrn_bytes = await etrn_file.read()
        model_bytes = await model_file.read()
        percent_bytes = await percent_file.read()

        # Генерация отчета
        report_bytes = service.build_report_from_sources(
            etrn_source=etrn_bytes,
            model_source=model_bytes,
            percent_source=percent_bytes,
        )

        # Имя файла для сохранения и скачивания
        label = (date_label or "").strip()
        if not label:
            label = datetime.now().strftime("%d.%m.%Y")
        clean_label = label.replace(" ", "_")
        out_filename = f"ЭТРН_{clean_label}.xlsx"

        task_id = str(uuid.uuid4())
        out_dir = service.cfg.output_dir
        os.makedirs(out_dir, exist_ok=True)
        out_filepath = os.path.join(out_dir, out_filename)

        with open(out_filepath, "wb") as f:
            f.write(report_bytes)

        with _TASKS_LOCK:
            _TASKS_STORE[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "progress": 100,
                "message": "Отчет успешно сформирован!",
                "filename": out_filename,
                "file_path": out_filepath,
                "date_label": label,
                "date_from": label,
                "date_to": label,
                "completed_at": datetime.now().isoformat(),
            }

        # Если клиент запросил JSON (для отображения кнопки отправки в Outlook)
        accept_header = request.headers.get("accept", "")
        if as_json or "application/json" in accept_header:
            return {
                "status": "success",
                "task_id": task_id,
                "filename": out_filename,
                "date_label": label,
                "download_url": f"/api/etrn/download/{task_id}",
            }

        return Response(
            content=report_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{out_filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ошибка при обработке файлов: {str(e)}",
        )


# =============================================================================
# 2. Фоновая автовыгрузка из 1С и сборка отчета
# =============================================================================
def _run_background_etrn_task(task_id: str, d_from: datetime, d_to: datetime, date_label: str) -> None:
    """Воркер фонового потока для выгрузки из 1С."""
    def progress_callback(pct: int, msg: str) -> None:
        with _TASKS_LOCK:
            if task_id in _TASKS_STORE:
                _TASKS_STORE[task_id]["progress"] = pct
                _TASKS_STORE[task_id]["message"] = msg

    try:
        result = service.generate_full_report(
            date_from=d_from,
            date_to=d_to,
            progress_cb=progress_callback,
        )
        with _TASKS_LOCK:
            _TASKS_STORE[task_id].update({
                "status": "completed",
                "progress": 100,
                "message": "Отчет успешно сформирован!",
                "filename": result["filename"],
                "file_path": result["output_filepath"],
                "date_label": date_label,
                "completed_at": datetime.now().isoformat(),
            })
    except Exception as e:
        with _TASKS_LOCK:
            _TASKS_STORE[task_id].update({
                "status": "failed",
                "progress": 100,
                "error": str(e),
                "message": f"Ошибка формирования: {str(e)}",
                "failed_at": datetime.now().isoformat(),
            })


@router.post(
    "/generate-task",
    summary="Запустить фоновую автовыгрузку отчета по ЭТРН",
    description="Ставит задачу в очередь фонового потока. Возвращает task_id для отслеживания статуса.",
)
async def generate_task(req: GenerateReportRequest):
    try:
        d_from = service.parse_date(req.date_from)
        d_to = service.parse_date(req.date_to) if req.date_to else d_from
        if d_from > d_to:
            d_from, d_to = d_to, d_from
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка в формате дат: {e}")

    task_id = str(uuid.uuid4())
    s_from = d_from.strftime("%d.%m.%Y")
    s_to = d_to.strftime("%d.%m.%Y")
    date_label = s_from if s_from == s_to else f"{s_from} - {s_to}"

    task_info = {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": "Инициализация задачи...",
        "date_from": s_from,
        "date_to": s_to,
        "date_label": date_label,
        "created_at": datetime.now().isoformat(),
    }

    with _TASKS_LOCK:
        _TASKS_STORE[task_id] = task_info

    th = threading.Thread(
        target=_run_background_etrn_task,
        args=(task_id, d_from, d_to, date_label),
        daemon=True,
    )
    th.start()

    return {"status": "accepted", "task_id": task_id, "message": "Задача успешно запущена"}


@router.get(
    "/task-status/{task_id}",
    summary="Получить статус фоновой задачи",
)
async def get_task_status(task_id: str):
    with _TASKS_LOCK:
        task = _TASKS_STORE.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    return task


@router.get(
    "/download/{task_id}",
    summary="Скачать сформированный Excel-отчет",
)
async def download_task_result(task_id: str):
    with _TASKS_LOCK:
        task = _TASKS_STORE.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if task.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Задача еще не завершена или завершилась с ошибкой")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл отчета не найден на диске")

    filename = task.get("filename", os.path.basename(file_path))

    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


# =============================================================================
# 3. Отправка отчета через Outlook
# =============================================================================
@router.post(
    "/prepare-email",
    summary="Подготовить пакет письма с отчетом ЭТРН для Outlook",
)
async def prepare_etrn_email(req: EtrnEmailRequest):
    with _TASKS_LOCK:
        task = _TASKS_STORE.get(req.task_id)

    if not task or task.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Отчет еще не сформирован")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл отчета не найден на диске")

    date_label = task.get("date_label") or task.get("date_from") or datetime.now().strftime("%d.%m.%Y")
    subject = f"ЭТРН отчет ({date_label})"
    html_body = "<p>Добрый день</p><p>Во вложении список с нарушениями</p>"

    filename = task.get("filename", os.path.basename(file_path))
    with open(file_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")

    email_item = {
        "to": ETRN_EMAIL_TO,
        "cc": ETRN_EMAIL_CC,
        "subject": subject,
        "html_body": html_body,
        "attachments": [
            {
                "filename": filename,
                "network_path": os.path.abspath(file_path),
                "content_b64": content_b64,
            }
        ],
    }

    return {
        "status": "success",
        "mode": req.mode,
        "emails": [email_item],
    }


@router.post(
    "/send-email",
    summary="Отправить отчет через Outlook на сервере (fallback)",
)
async def send_etrn_email(req: EtrnEmailRequest, request: Request):
    if not _is_same_host_client(request):
        return {
            "status": "bridge_required",
            "message": (
                "Чтобы черновики и письма создавались в вашем личном Outlook (на вашем компьютере), "
                "запустите на своем ПК помощник «outlook_bridge.py»."
            ),
        }

    prep = await prepare_etrn_email(req)
    if prep.get("status") != "success":
        return prep

    emails = prep.get("emails", [])
    draft_mode = (req.mode == "draft")

    try:
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()
        outlook = win32.Dispatch("outlook.application")

        for item in emails:
            mail = outlook.CreateItem(0)
            mail.To = item["to"]
            if item.get("cc"):
                mail.CC = item["cc"]
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

        action_text = "сохранен в Черновиках вашего Outlook" if draft_mode else "отправлен с вашей почты Outlook"
        return {
            "status": "success",
            "message": f"Отчет ЭТРН успешно {action_text}!",
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка Outlook: {str(e)}"}
