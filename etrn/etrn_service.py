"""
etrn_service.py — Высокоуровневый сервисный слой для работы с отчетами ЭТРН.

Служит единой точкой входа для:
1. Веб-сервера (FastAPI / server.py):
   - Генерация отчета из загруженных файлов через браузер (Multipart Upload).
   - Запуск фонового формирования за выбранный диапазон дат.
2. CLI и планировщика задач:
   - Автоматический сбор за вчера/произвольный период.

Архитектура:
- Потокобезопасная сборка Excel в памяти (io.BytesIO).
- Поддержка колбэков прогресса (для индикатора выполнения в веб-интерфейсе).
- Полная изоляция от деталей GUI / 1С.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Callable, Dict, Optional, Tuple, Union

try:
    from .config import Config, get_config
    from .etrn_optimizer import process_and_style_etrn
    from .onec_exporter import BaseOneCExporter, get_onec_exporter
except ImportError:
    from config import Config, get_config
    from etrn_optimizer import process_and_style_etrn
    from onec_exporter import BaseOneCExporter, get_onec_exporter


ProgressCallback = Callable[[int, str], None]


class EtrnService:
    """Единый сервис генерации и обработки отчетов ЭТРН."""

    def __init__(self, cfg: Optional[Config] = None, exporter: Optional[BaseOneCExporter] = None):
        self.cfg = cfg or get_config()
        self._exporter = exporter

    @property
    def exporter(self) -> BaseOneCExporter:
        """Ленивая инициализация экспортера 1С."""
        if self._exporter is None:
            self._exporter = get_onec_exporter(self.cfg)
        return self._exporter

    # -------------------------------------------------------------------------
    # 1. Работа с датами и именами файлов
    # -------------------------------------------------------------------------
    @staticmethod
    def parse_date(date_str: str) -> datetime:
        """Парсит строку даты вида '04.11.2026' или '04.11'."""
        s = date_str.strip()
        parts = s.split(".")
        today = datetime.now()
        if len(parts) == 2:
            return datetime(today.year, int(parts[1]), int(parts[0]))
        elif len(parts) == 3:
            year = int(parts[2])
            if year < 100:
                year += 2000
            return datetime(year, int(parts[1]), int(parts[0]))
        raise ValueError(f"Некорректный формат даты: '{date_str}'. Ожидается ДД.ММ.ГГГГ")

    @classmethod
    def parse_date_range(cls, text: Optional[str] = None) -> Tuple[datetime, datetime]:
        """
        Парсит одиночную дату или диапазон:
        - None / '' -> вчерашний день
        - '04.11.2026' -> (04.11.2026, 04.11.2026)
        - '03.11.2026-05.11.2026' -> (03.11.2026, 05.11.2026)
        """
        if not text or not text.strip():
            yesterday = datetime.now() - timedelta(days=1)
            d = datetime(yesterday.year, yesterday.month, yesterday.day)
            return (d, d)

        cleaned = text.strip()
        if "-" in cleaned:
            parts = [p.strip() for p in cleaned.split("-", 1)]
            d_from = cls.parse_date(parts[0])
            d_to = cls.parse_date(parts[1])
            if d_from > d_to:
                d_from, d_to = d_to, d_from
            return (d_from, d_to)

        d = cls.parse_date(cleaned)
        return (d, d)

    @staticmethod
    def format_filename(date_from: datetime, date_to: datetime) -> str:
        """Формирует имя итогового файла отчета."""
        d_from_str = date_from.strftime("%d.%m.%Y")
        d_to_str = date_to.strftime("%d.%m.%Y")
        if d_from_str == d_to_str:
            return f"ЭТРН_{d_from_str}.xlsx"
        return f"ЭТРН_{d_from_str}_{d_to_str}.xlsx"

    # -------------------------------------------------------------------------
    # 2. Обработка загруженных файлов (Web-ready / In-Memory)
    # -------------------------------------------------------------------------
    def build_report_from_sources(
        self,
        etrn_source: Union[str, os.PathLike, BinaryIO, bytes],
        model_source: Union[str, os.PathLike, BinaryIO, bytes],
        percent_source: Union[str, os.PathLike, BinaryIO, bytes],
        output_filepath: Optional[Union[str, os.PathLike]] = None,
    ) -> bytes:
        """
        Собирает стилизованный отчет из трех источников данных.
        Работает полностью в памяти (подходит для FastAPI UploadFile / BytesIO).
        Возвращает байты XLSX. Если передан output_filepath — также сохраняет на диск.
        """
        return process_and_style_etrn(
            input_filepath=etrn_source,
            model_filepath=model_source,
            percent_filepath=percent_source,
            output_filepath=output_filepath,
        )

    # -------------------------------------------------------------------------
    # 3. Полный цикл (1С Выгрузка + Сборка отчета) с поддержкой прогресса
    # -------------------------------------------------------------------------
    def generate_full_report(
        self,
        date_from: datetime,
        date_to: datetime,
        output_filepath: Optional[str] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """
        Выполняет полный цикл генерации отчета:
        1. Выгружает 3 таблицы из 1С (МЛ, ЭТРН, Процент).
        2. Обрабатывает и стилизует сводный отчет.
        3. Возвращает статус, пути к файлам и имя отчета.
        """
        def report_progress(percent: int, message: str) -> None:
            if progress_cb:
                try:
                    progress_cb(percent, message)
                except Exception:
                    pass

        # Период для МЛ: 30 дней до даты окончания
        marshrut_from = date_to - timedelta(days=30)
        marshrut_period = (marshrut_from, date_to)
        etrn_period = (date_from, date_to)

        filename = self.format_filename(date_from, date_to)
        if not output_filepath:
            output_filepath = os.path.join(self.cfg.desktop_path, filename)

        report_progress(5, f"Запуск выгрузки из 1С за период {date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}...")

        # 1. Выгрузка из 1С
        exported_files = self.exporter.export_all(
            marshrut_period=marshrut_period,
            etrn_period=etrn_period,
        )

        report_progress(75, "Выгрузка из 1С завершена. Обработка данных и сборка отчета...")

        # 2. Обработка и сохранение
        report_bytes = self.build_report_from_sources(
            etrn_source=exported_files["etrn"],
            model_source=exported_files["model"],
            percent_source=exported_files["percent"],
            output_filepath=output_filepath,
        )

        report_progress(100, f"Отчет готов: {filename}")

        return {
            "status": "success",
            "filename": filename,
            "output_filepath": output_filepath,
            "bytes_length": len(report_bytes),
            "date_from": date_from.strftime("%d.%m.%Y"),
            "date_to": date_to.strftime("%d.%m.%Y"),
            "exported_files": exported_files,
        }
