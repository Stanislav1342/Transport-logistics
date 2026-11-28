"""
config.py — Конфигурация путей, ссылок 1С (e1cib) и параметров отчета ЭТРН.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Tuple


@dataclass
class Config:
    """Конфигурация проекта ЭТРН."""

    # Папка на рабочем столе для входных и выходных файлов
    desktop_dir: str = field(
        default_factory=lambda: os.path.join(os.path.expanduser("~"), "Desktop")
    )
    output_dir_name: str = "ЭТРН"

    # Имена выгружаемых из 1С файлов
    input_etrn_name: str = "ЭТРН 1с.xlsx"
    input_model_name: str = "Модель расчетов.xlsx"
    input_percent_name: str = "Процент.xlsx"

    # Постоянные прямые ссылки 1С (e1cib) для перехода через Shift + F11
    url_marshrut: str = "e1cib/list/Документ.МаршрутныйЛист"
    url_etrn: str = "e1cib/list/Документ.ЭлектроннаяТранспортнаяНакладная"
    url_percent: str = field(
        default_factory=lambda: os.environ.get("ETRN_1C_PERCENT_URL", "").strip()
    )

    # Человекочитаемые названия разделов для логов
    title_marshrut: str = "Маршрутный лист"
    title_etrn: str = "Электронные транспортные накладные"
    title_percent: str = "Контроль подписания перевозчиков по ЭТРН"

    # Заголовок главного окна 1С (регулярное выражение)
    onec_window_regex: str = (
        ".*(1С:Предприятие|ЕИС МСК|Корпоративная информационная система).*"
    )

    # Паузы и таймауты (секунды)
    action_pause: float = 1.0
    report_gen_timeout: int = 40

    @property
    def output_dir(self) -> str:
        if (
            os.environ.get("ETRN_OUTPUT_DIR", "").strip()
            or os.environ.get("PORTAL_DATA_DIR", "").strip()
            or os.environ.get("SERVER_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        ):
            try:
                from core import server_config
                portal_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                return server_config.get_etrn_data_dir(portal_root)
            except ImportError:
                pass
        d = os.path.join(self.desktop_dir, self.output_dir_name)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    @property
    def desktop_path(self) -> str:
        return self.output_dir

    @property
    def etrn_file(self) -> str:
        return os.path.join(self.desktop_dir, self.input_etrn_name)

    @property
    def model_file(self) -> str:
        return os.path.join(self.desktop_dir, self.input_model_name)

    @property
    def percent_file(self) -> str:
        return os.path.join(self.desktop_dir, self.input_percent_name)

    def get_output_file(self, date_label: str) -> str:
        return os.path.join(self.output_dir, f"ЭТРН_{date_label}.xlsx")

    @staticmethod
    def get_marshrut_period() -> Tuple[datetime, datetime]:
        """Для «Маршрутного листа» период: сегодня и 30 дней до него."""
        today = datetime.now()
        start = today - timedelta(days=30)
        return start, today


def get_config() -> Config:
    return Config()
