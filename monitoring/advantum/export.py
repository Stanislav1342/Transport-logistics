"""
export.py — Логика экспорта термографиков из Advantum.

Основной модуль, содержащий:
- export_thermographs() — массовый экспорт по списку МЛ
- export_single_ml() — экспорт одного маршрутного листа

Все задержки time.sleep() взяты из v22 — проверены на продакшене.
"""

import base64
import datetime
import html as html_lib
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import statistics
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple, Dict, Any

import requests
import requests.adapters

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from .browser import (
        save_page_as_pdf,
        _resolve_browser_path,
        _resolve_chromedriver,
    )
    from .cdp_driver import DirectCDPDriver
    from .config import Config
    from .utils import (
        parse_driver_name,
        parse_ts_from_print_page,
        stable_click,
        wait_for_new_window,
        wait_overlays_gone,
    )
except ImportError:
    from browser import (
        save_page_as_pdf,
        _resolve_browser_path,
        _resolve_chromedriver,
    )
    from cdp_driver import DirectCDPDriver
    from config import Config
    from utils import (
        parse_driver_name,
        parse_ts_from_print_page,
        stable_click,
        wait_for_new_window,
        wait_overlays_gone,
    )

logger = logging.getLogger(__name__)


# ─── MUI-специфичные селекторы для Advantum ────────────────────────────────────

# Кнопка «Версия для печати» — MUI outlined button
# Из предоставленного HTML:
# <button class="MuiButtonBase-root MuiButton-root MuiButton-outlined
#   MuiButton-sizeMedium MuiButton-colorPrimary css-1jlcw5i">
#   Версия для печати<span class="MuiTouchRipple-root css-4mb1j7"></span>
# </button>
PRINT_VERSION_SELECTORS = [
    (By.XPATH, "//button[contains(@class, 'MuiButton-root') and contains(., 'Версия для печати')]"),
    (By.CSS_SELECTOR, "button.MuiButton-outlined.MuiButton-colorPrimary"),
    (By.XPATH, "//button[contains(text(), 'Версия для печати')]"),
    (By.XPATH, "//*[contains(text(), 'Версия для печати')]"),
]

# Кнопка «Простая» (выпадающее меню после «Версия для печати»)
SIMPLE_VERSION_SELECTORS = [
    (By.XPATH, "//li[contains(@class, 'MuiMenuItem') and contains(., 'Простая')]"),
    (By.XPATH, "//div[contains(@class, 'MuiMenu')]//li[contains(., 'Простая')]"),
    (By.XPATH, "//button[contains(., 'Простая')]"),
    (By.XPATH, "//*[text()='Простая']"),
    (By.XPATH, "//*[contains(text(), 'Простая')]"),
]

# Иконка температурного графика (из v22)
THERMO_ICON_SELECTORS = [
    (By.XPATH, "//span[@aria-label='Температурный график']"),
    (By.XPATH, "//span[text()='multiline_chart']"),
    (By.CSS_SELECTOR, "span[aria-label='Температурный график']"),
]


class ExportResult(NamedTuple):
    """Результат экспорта одного МЛ."""
    ml_number: str
    success: bool
    filepath: str
    error: str
    driver_name: str = ""
    tc_number: str = ""


def normalize_ml_number(ml: str) -> str:
    """
    Нормализует номер маршрутного листа:
    - Заменяет латинские M, C, K, m, c, k на русскую кириллицу М, С, К
    - Удаляет лишние пробелы и приводит к верхнему регистру
    """
    tr = str.maketrans({
        'M': 'М', 'm': 'М',
        'C': 'С', 'c': 'С',
        'K': 'К', 'k': 'К',
    })
    return ml.strip().translate(tr).upper()


# ─── Вспомогательные функции ───────────────────────────────────────────────────


def _find_and_click_button(
    driver: WebDriver,
    selectors: list[tuple[str, str]],
    description: str,
    timeout: int = 20,
) -> None:
    """
    Пробует несколько селекторов по очереди для поиска и клика.
    Использует stable_click для каждого — с обходом overlay и JS-кликом.
    """
    last_error = None

    for by, selector in selectors:
        try:
            logger.debug("[%s] Пробуем: %s = %s", description, by, selector)
            stable_click(
                driver,
                selector,
                by=by,
                timeout=timeout,
                description=description,
            )
            print(f"[{description}] Нажата.")
            return
        except (TimeoutException, NoSuchElementException, Exception) as e:
            last_error = e
            logger.debug("[%s] Селектор %s не сработал: %s", description, selector, e)
            continue

    raise last_error or Exception(f"Ни один селектор не сработал для: {description}")


# ─── Headless генерация PDF ───────────────────────────────────────────────────


class PdfPrinterSession:
    """
    Контекстный менеджер для пакетной печати PDF через единый фоновый (headless) браузер.
    Инициализирует один headless-браузер через DirectCDPDriver (без chromedriver.exe)
    для всех МЛ, исключая оверхед на многократный перезапуск браузера.
    """

    def __init__(self, config: Config, lazy: bool = False):
        self.config = config
        self.lazy = lazy
        self.driver: DirectCDPDriver | None = None
        self._init_attempted = False

    def _ensure_driver(self) -> None:
        if self.driver is not None or self._init_attempted:
            return
        self._init_attempted = True
        try:
            browser_path = _resolve_browser_path(self.config)
            self.driver = DirectCDPDriver(
                browser_path=browser_path,
                headless=True,
                disable_js=True,
            )
            logger.info("Постоянный headless-браузер (Direct CDP) для PDF инициализирован.")
        except Exception as e:
            logger.warning(
                "Не удалось запустить постоянный headless-браузер: %s. Будет использован fallback.",
                e,
            )
            self.driver = None

    def __enter__(self):
        if not self.lazy:
            self._ensure_driver()
        return self

    def print_html_to_pdf(self, html_path: str, output_pdf: str) -> bool:
        """Печатает локальный HTML-файл в PDF через открытую сессию."""
        if self.driver is None and self.lazy:
            self._ensure_driver()
        if not self.driver:
            print_html_to_pdf_headless(html_path, output_pdf, self.config)
            return True

        try:
            abs_path = "file://" + os.path.abspath(html_path)
            if os.name == "nt":
                abs_path = "file:///" + os.path.abspath(html_path).replace("\\", "/")

            self.driver.get(abs_path)
            print_options = {
                "landscape": True,
                "displayHeaderFooter": False,
                "printBackground": True,
                "preferCSSPageSize": True,
            }
            result = self.driver.execute_cdp_cmd("Page.printToPDF", print_options)
            if os.path.exists(output_pdf):
                try:
                    os.remove(output_pdf)
                except Exception:
                    pass

            with open(output_pdf, "wb") as f:
                f.write(base64.b64decode(result["data"]))
            return True
        except Exception as e:
            logger.error("Ошибка печати PDF через постоянную сессию: %s. Пробуем fallback...", e)
            print_html_to_pdf_headless(html_path, output_pdf, self.config)
            return True

    def render_html_to_pdf(self, html_content: str, output_pdf: str) -> bool:
        """Сохраняет HTML во временный файл и печатает в PDF без внешних зависимостей."""
        os.makedirs(os.path.dirname(os.path.abspath(output_pdf)), exist_ok=True)
        temp_html_path = os.path.join(
            os.path.dirname(os.path.abspath(output_pdf)),
            f".tmp_render_{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}.html",
        )
        try:
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            return self.print_html_to_pdf(temp_html_path, output_pdf)
        finally:
            if os.path.exists(temp_html_path):
                try:
                    os.remove(temp_html_path)
                except Exception:
                    pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


def print_html_to_pdf_headless(html_path: str, output_pdf: str, config: Config) -> None:
    """Разовый fallback: генерирует PDF из локального HTML через DirectCDPDriver (без chromedriver.exe)."""
    browser_path = _resolve_browser_path(config)

    try:
        headless_driver = DirectCDPDriver(
            browser_path=browser_path,
            headless=True,
            disable_js=True,
        )
    except Exception as e:
        print(f"[Ошибка] Не удалось запустить fallback headless браузер: {e}")
        return

    try:
        abs_path = "file://" + os.path.abspath(html_path)
        if os.name == "nt":
            abs_path = "file:///" + os.path.abspath(html_path).replace("\\", "/")

        headless_driver.get(abs_path)
        print_options = {
            "landscape": True,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
        }
        result = headless_driver.execute_cdp_cmd("Page.printToPDF", print_options)
        if os.path.exists(output_pdf):
            try:
                os.remove(output_pdf)
            except Exception:
                pass

        with open(output_pdf, "wb") as f:
            f.write(base64.b64decode(result["data"]))
    finally:
        headless_driver.quit()




RUSSIAN_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _parse_month_header(text: str) -> tuple[int, int]:
    """
    Парсит заголовок месяца календаря вида 'Сентябрь 2026' в (year, month).
    """
    parts = text.strip().split()
    if len(parts) >= 2:
        m_name = parts[0].lower()
        y_num = int(parts[1])
        m_num = RUSSIAN_MONTHS.get(m_name)
        if m_num:
            return y_num, m_num
    raise ValueError(f"Не удалось распознать заголовок месяца: '{text}'")


def apply_date_filter(
    driver: WebDriver,
    wait: WebDriverWait,
    start_date: datetime.date,
    end_date: datetime.date,
) -> bool:
    """
    Устанавливает фильтр по датам в системе Advantum.

    1. Проверяет, развернута ли панель фильтров (если нет — кликает значок фильтра).
    2. Проверяет текущее значение в поле периода: если нужные даты уже стоят, не трогает.
    3. Кликает на значок календаря.
    4. Считывает открытый месяц и переключает стрелочками '<' / '>' на нужный месяц.
    5. Выбирает указанные пользователем даты начала и окончания.
    6. Нажимает кнопку 'Применить' и ждёт обновления данных.
    """
    print(
        f"[Фильтр дат] Применение периода: {start_date.strftime('%d.%m.%Y')} — "
        f"{end_date.strftime('%d.%m.%Y')}..."
    )

    # 1. Проверяем, развернута ли панель фильтров
    filter_fields = driver.find_elements(
        By.XPATH, "//div[contains(@class, 'MuiPickersTextField-root')]"
    )
    if not filter_fields or not filter_fields[0].is_displayed():
        print("[Фильтр дат] Раскрываем панель фильтров...")
        expand_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//div[contains(@class, 'extended-grid__grid-filter-expand-button')]")
            )
        )
        driver.execute_script("arguments[0].click();", expand_btn)
        time.sleep(2)

    # 2. Проверяем, установлен ли уже нужный период
    try:
        period_input = driver.find_element(
            By.XPATH, "//div[contains(@class, 'MuiPickersTextField-root')]//input"
        )
        cur_val = period_input.get_attribute("value") or ""
        start_short = start_date.strftime("%d.%m.%y")
        end_short = end_date.strftime("%d.%m.%y")
        if start_short in cur_val and end_short in cur_val:
            print(f"[Фильтр дат] Период уже установлен: {cur_val}")
            return True
    except Exception as e:
        logger.debug("Не удалось прочитать текущий период: %s", e)

    # 3. Открываем календарь
    print("[Фильтр дат] Открываем окно календаря...")
    cal_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//div[contains(@class, 'MuiPickersTextField-root')]//button")
        )
    )
    driver.execute_script("arguments[0].click();", cal_btn)
    time.sleep(1.5)

    # 4. Ожидаем появления календаря и считываем отображаемый месяц
    wait.until(
        EC.presence_of_all_elements_located(
            (By.XPATH, "//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')]")
        )
    )

    left_lbl = wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][1]//span[contains(@class, 'MuiTypography-subtitle1')]",
            )
        )
    ).text
    cur_year, cur_month = _parse_month_header(left_lbl)
    month_delta = (start_date.year - cur_year) * 12 + (start_date.month - cur_month)
    logger.info(
        "Текущий месяц в календаре: %s (%d.%d), целевой: %d.%d, смещение: %d",
        left_lbl, cur_month, cur_year, start_date.month, start_date.year, month_delta
    )

    # Переключаем назад, если требуется
    if month_delta < 0:
        for _ in range(abs(month_delta)):
            prev_btn = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][1]//button[contains(@class, 'MuiPickersArrowSwitcher-previousIconButton')]",
                    )
                )
            )
            driver.execute_script("arguments[0].click();", prev_btn)
            time.sleep(0.3)
    # Переключаем вперед, если требуется
    elif month_delta > 0:
        for _ in range(month_delta):
            next_btn = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][2]//button[contains(@class, 'MuiPickersArrowSwitcher-nextIconButton')]",
                    )
                )
            )
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(0.3)

    time.sleep(0.5)

    # 5. Кликаем день даты начала в Container 0 (левый месяц)
    start_day_xpath = (
        f"//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][1]"
        f"//button[@role='gridcell' and normalize-space(text())='{start_date.day}']"
    )
    start_day_btn = wait.until(EC.element_to_be_clickable((By.XPATH, start_day_xpath)))
    driver.execute_script("arguments[0].click();", start_day_btn)
    time.sleep(0.4)

    # 6. Кликаем день даты окончания
    end_delta = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_delta == 0:
        # Тот же месяц — Container 0
        end_day_xpath = (
            f"//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][1]"
            f"//button[@role='gridcell' and normalize-space(text())='{end_date.day}']"
        )
        end_day_btn = wait.until(EC.element_to_be_clickable((By.XPATH, end_day_xpath)))
        driver.execute_script("arguments[0].click();", end_day_btn)
    elif end_delta == 1:
        # Следующий месяц — Container 1 (он уже открыт справа)
        end_day_xpath = (
            f"//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][2]"
            f"//button[@role='gridcell' and normalize-space(text())='{end_date.day}']"
        )
        end_day_btn = wait.until(EC.element_to_be_clickable((By.XPATH, end_day_xpath)))
        driver.execute_script("arguments[0].click();", end_day_btn)
    else:
        # Более одного месяца вперед — доматываем правый контейнер
        for _ in range(end_delta - 1):
            next_btn = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][2]//button[contains(@class, 'MuiPickersArrowSwitcher-nextIconButton')]",
                    )
                )
            )
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(0.3)
        time.sleep(0.4)
        end_day_xpath = (
            f"//div[contains(@class, 'MuiDateRangeCalendar-monthContainer')][2]"
            f"//button[@role='gridcell' and normalize-space(text())='{end_date.day}']"
        )
        end_day_btn = wait.until(EC.element_to_be_clickable((By.XPATH, end_day_xpath)))
        driver.execute_script("arguments[0].click();", end_day_btn)

    time.sleep(0.5)

    # 7. Нажимаем кнопку «Применить»
    print("[Фильтр дат] Нажимаем 'Применить'...")
    apply_btn = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[contains(@class, 'MuiButton-root') and contains(text(), 'Применить')]",
            )
        )
    )
    driver.execute_script("arguments[0].click();", apply_btn)
    time.sleep(2)

    # 8. Ждём снятия оверлеев и обновления таблицы
    wait_overlays_gone(driver, timeout=10)
    time.sleep(1)

    try:
        new_val = driver.find_element(
            By.XPATH, "//div[contains(@class, 'MuiPickersTextField-root')]//input"
        ).get_attribute("value")
        print(f"[Фильтр дат] Установлен период: {new_val}")
    except Exception:
        pass

    return True


def _clear_search_input(driver: WebDriver) -> None:
    """Быстро очищает строку поиска рейсов без перезагрузки страницы."""
    try:
        wait_overlays_gone(driver, timeout=3)
    except Exception:
        pass

    try:
        clear_btns = driver.find_elements(
            By.XPATH, "//div[contains(@class, 'search-field__clear')]//button"
        )
        for btn in clear_btns:
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.4)
                return
    except Exception:
        pass

    try:
        search_inputs = driver.find_elements(
            By.XPATH, "//div[contains(@class, 'extended-grid__grid-filter-search')]//input"
        )
        if search_inputs:
            inp = search_inputs[0]
            driver.execute_script("arguments[0].value = '';", inp)
            modifier = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
            try:
                inp.send_keys(modifier + "a")
                inp.send_keys(Keys.BACKSPACE)
                inp.send_keys(Keys.ENTER)
            except Exception:
                pass
            time.sleep(0.4)
    except Exception:
        pass


def get_advantum_bearer_token(driver: WebDriver) -> str:
    """Извлекает JWT Bearer-токен Advantum из localStorage авторизованного браузера."""
    auth_raw = driver.execute_script("return localStorage.getItem('auth_data');")
    if not auth_raw:
        raise RuntimeError("Данные авторизации (auth_data) не найдены в localStorage")
    try:
        parsed = json.loads(auth_raw)
        token = (parsed.get("authData") or {}).get("access_token", "")
    except Exception as e:
        raise RuntimeError(f"Не удалось разобрать auth_data из localStorage: {e}") from e
    if not token:
        raise RuntimeError("Поле access_token отсутствует в auth_data")
    return token


def _is_pure_shipment_id(token_ml: str) -> bool:
    """Проверяет, является ли строка внутренним числовым ID рейса Advantum (7-9 цифр)."""
    return bool(re.fullmatch(r"\d{7,9}", token_ml.strip()))


def _index_shipment_record(item: dict[str, Any], index: dict[str, dict[str, Any]]) -> None:
    """Индексирует запись рейса в словаре по id, нормализованному routeNum и цифрам routeNum."""
    sid = item.get("id")
    if sid is not None:
        index[f"id:{sid}"] = item
    r_num = item.get("routeNum")
    if r_num:
        norm = normalize_ml_number(str(r_num))
        index[f"ml:{norm}"] = item
        digits = "".join(re.findall(r"\d+", norm))
        if digits:
            d_key = f"digits:{digits}"
            if d_key not in index or item.get("hasTelematicEquipment"):
                index[d_key] = item


def _lookup_indexed_shipment(ml: str, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Ищет рейс в индексе по точному нормализованному номеру МЛ, ID или цифрам."""
    norm = normalize_ml_number(ml)
    if f"ml:{norm}" in index:
        return index[f"ml:{norm}"]
    if _is_pure_shipment_id(norm) and f"id:{norm}" in index:
        return index[f"id:{norm}"]
    digits = "".join(re.findall(r"\d+", norm))
    if digits and f"digits:{digits}" in index:
        return index[f"digits:{digits}"]
    return None


# ─── Глобальное ограничение конкурентности API, SQLite-кэш и JWT-менеджер ──────

_GLOBAL_ADVANTUM_API_SEMAPHORE = threading.Semaphore(4)
_PORTAL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIPMENTS_CACHE_DB_PATH = os.path.join(_PORTAL_ROOT, "data", "advantum_shipments_cache.db")
_LEGACY_CACHE_DB_PATH = os.path.join(_PORTAL_ROOT, "advantum_shipments_cache.db")
if os.path.isfile(_LEGACY_CACHE_DB_PATH) and not os.path.exists(SHIPMENTS_CACHE_DB_PATH):
    try:
        os.makedirs(os.path.dirname(SHIPMENTS_CACHE_DB_PATH), exist_ok=True)
        import shutil as _shutil
        _shutil.move(_LEGACY_CACHE_DB_PATH, SHIPMENTS_CACHE_DB_PATH)
    except Exception:
        pass
DEFAULT_ML_RATE_PER_DAY = 160.0


def _extract_shipment_date(item: dict[str, Any]) -> str | None:
    """Извлекает дату рейса YYYY-MM-DD из полей API ответа."""
    for field in ("scheduledStartInstant", "factStartInstant", "startDate", "createdDate"):
        raw = item.get(field)
        if isinstance(raw, str) and len(raw.strip()) >= 10:
            cand = raw.strip()[:10]
            try:
                datetime.date.fromisoformat(cand)
                return cand
            except Exception:
                continue
    return None


def _extract_ml_prefix_num(ml_norm: str) -> tuple[str, int] | None:
    """Разбирает нормализованный номер МЛ вида 'МСК548799' -> ('МСК', 548799)."""
    m = re.fullmatch(r"([А-ЯЁA-Z]{2,6})(\d{4,9})", (ml_norm or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


class _ShipmentsCache:
    """
    Пассивный потокобезопасный SQLite WAL-кэш поискового индекса рейсов Advantum.
    Хранит метаданные рейсов (номер МЛ -> id, водитель, ТС, флаг телематики) и
    список полностью закрытых прошлых дней, но НИКОГДА не закрывает последние 3 дня
    (сегодня, вчера, позавчера) и всегда требует живой загрузки данных датчиков.
    """

    def __init__(self, db_path: str = SHIPMENTS_CACHE_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_prune_ts = 0.0
        try:
            with self._lock:
                self._init_db()
        except Exception as e:
            logger.warning("[Кэш] Не удалось инициализировать %s: %s", self.db_path, e)

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    def _reset_corrupt_db(self) -> None:
        logger.warning("[Кэш] Обнаружено повреждение БД %s, пересоздаем...", self.db_path)
        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        try:
            self._init_db(allow_reset=False)
        except Exception as e:
            logger.warning("[Кэш] Повторная инициализация не удалась: %s", e)

    def _init_db(self, allow_reset: bool = True) -> None:
        conn = None
        try:
            conn = self._connect()
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS shipments (
                        ml_norm TEXT PRIMARY KEY,
                        digits TEXT,
                        shipment_id TEXT NOT NULL,
                        route_num TEXT,
                        ship_date TEXT,
                        driver_fio TEXT,
                        vehicle_gos_num TEXT,
                        has_telematic INTEGER,
                        payload_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_shipments_digits ON shipments(digits);
                    CREATE INDEX IF NOT EXISTS idx_shipments_sid ON shipments(shipment_id);
                    CREATE INDEX IF NOT EXISTS idx_shipments_date ON shipments(ship_date);
                    CREATE TABLE IF NOT EXISTS indexed_days (
                        day_iso TEXT PRIMARY KEY,
                        items_count INTEGER NOT NULL DEFAULT 0,
                        indexed_at TEXT NOT NULL
                    );
                    """
                )
        except sqlite3.DatabaseError:
            if allow_reset:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
            else:
                raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def lookup(self, ml_numbers: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not ml_numbers:
            return result
        with self._lock:
            conn = None
            try:
                conn = self._connect()
                cur = conn.cursor()
                for ml in ml_numbers:
                    norm = normalize_ml_number(ml)
                    if not norm:
                        continue
                    row = cur.execute(
                        "SELECT payload_json FROM shipments WHERE ml_norm = ? LIMIT 1",
                        (norm,),
                    ).fetchone()
                    if row is None and _is_pure_shipment_id(norm):
                        row = cur.execute(
                            "SELECT payload_json FROM shipments WHERE shipment_id = ? "
                            "ORDER BY COALESCE(has_telematic, 0) DESC, updated_at DESC LIMIT 1",
                            (norm,),
                        ).fetchone()
                    if row is None:
                        digits = "".join(re.findall(r"\d+", norm))
                        if digits:
                            row = cur.execute(
                                "SELECT payload_json FROM shipments WHERE digits = ? "
                                "ORDER BY COALESCE(has_telematic, 0) DESC, updated_at DESC LIMIT 1",
                                (digits,),
                            ).fetchone()
                    if row and row[0]:
                        try:
                            payload = json.loads(row[0])
                            if isinstance(payload, dict):
                                rec = dict(payload)
                                rec["_from_cache"] = True
                                result[norm] = rec
                        except Exception:
                            pass
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
                return {}
            except Exception as e:
                logger.debug("[Кэш] Ошибка lookup: %s", e)
                return {}
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return result

    def upsert_shipments(self, items: list[dict[str, Any]], fallback_day: str | None = None) -> int:
        if not items:
            return 0
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows: list[tuple[Any, ...]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sid = item.get("id")
            if sid is None:
                continue
            sid_str = str(sid).strip()
            if not sid_str:
                continue
            raw_route = str(item.get("routeNum") or "").strip()
            norm = normalize_ml_number(raw_route) if raw_route else f"ID:{sid_str}"
            digits = "".join(re.findall(r"\d+", norm)) if raw_route else ""
            ship_date = _extract_shipment_date(item) or fallback_day
            driver_fio = str(item.get("driverFio") or "").strip()
            vehicle_gos_num = str(
                item.get("vehicleGosNums") or item.get("vehicleGosNum") or ""
            ).strip()
            has_tel_raw = item.get("hasTelematicEquipment")
            has_tel = None if has_tel_raw is None else (1 if has_tel_raw else 0)
            clean_item = {k: v for k, v in item.items() if k != "_from_cache"}
            payload_json = json.dumps(clean_item, ensure_ascii=False)
            rows.append(
                (
                    norm,
                    digits,
                    sid_str,
                    raw_route,
                    ship_date,
                    driver_fio,
                    vehicle_gos_num,
                    has_tel,
                    payload_json,
                    now_iso,
                )
            )
        if not rows:
            return 0

        with self._lock:
            conn = None
            try:
                conn = self._connect()
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO shipments (
                            ml_norm, digits, shipment_id, route_num, ship_date,
                            driver_fio, vehicle_gos_num, has_telematic, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                return len(rows)
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
                return 0
            except Exception as e:
                logger.debug("[Кэш] Ошибка upsert_shipments: %s", e)
                return 0
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def mark_days_indexed(
        self, days: set[datetime.date] | list[datetime.date], items_count: int = 0
    ) -> None:
        """Фиксирует в кэше полностью опрошенные прошлые дни (строго старше последних 3 дней)."""
        cutoff_safe = datetime.date.today() - datetime.timedelta(days=3)
        safe_days = sorted({d for d in days if isinstance(d, datetime.date) and d <= cutoff_safe})
        if not safe_days:
            return
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = [(d.isoformat(), int(items_count), now_iso) for d in safe_days]
        with self._lock:
            conn = None
            try:
                conn = self._connect()
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO indexed_days (day_iso, items_count, indexed_at) VALUES (?, ?, ?)",
                        rows,
                    )
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
            except Exception as e:
                logger.debug("[Кэш] Ошибка mark_days_indexed: %s", e)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def get_indexed_days(self) -> set[str]:
        cutoff_safe_iso = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        with self._lock:
            conn = None
            try:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT day_iso FROM indexed_days WHERE day_iso <= ?",
                    (cutoff_safe_iso,),
                ).fetchall()
                return {r[0] for r in rows if r and r[0]}
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
                return set()
            except Exception as e:
                logger.debug("[Кэш] Ошибка get_indexed_days: %s", e)
                return set()
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def get_ml_anchors(
        self, prefixes: set[str] | None = None, limit: int = 1000
    ) -> list[tuple[str, int, datetime.date]]:
        """Возвращает калибровочные пары (префикс, число, дата) с покрытием разных дат."""
        anchors: list[tuple[str, int, datetime.date]] = []
        with self._lock:
            conn = None
            try:
                conn = self._connect()
                cur = conn.cursor()
                date_rows = cur.execute(
                    "SELECT DISTINCT ship_date FROM shipments "
                    "WHERE ship_date IS NOT NULL AND ship_date != '' AND ml_norm NOT LIKE 'ID:%' "
                    "ORDER BY ship_date DESC LIMIT 60"
                ).fetchall()
                dates_list = [r[0] for r in date_rows if r and r[0]]
                per_day_limit = max(20, min(60, limit // max(1, len(dates_list))))
                for d_iso in dates_list:
                    try:
                        d_obj = datetime.date.fromisoformat(d_iso)
                    except Exception:
                        continue
                    ml_rows = cur.execute(
                        "SELECT ml_norm FROM shipments "
                        "WHERE ship_date = ? AND ml_norm NOT LIKE 'ID:%' LIMIT ?",
                        (d_iso, per_day_limit),
                    ).fetchall()
                    for (ml_norm,) in ml_rows:
                        parsed = _extract_ml_prefix_num(ml_norm)
                        if parsed is None:
                            continue
                        p, num = parsed
                        if prefixes is not None and p not in prefixes:
                            continue
                        anchors.append((p, num, d_obj))
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
                return []
            except Exception as e:
                logger.debug("[Кэш] Ошибка get_ml_anchors: %s", e)
                return []
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return anchors

    def prune_old(self, max_age_days: int = 90, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_prune_ts) < 3600.0:
            return
        cutoff_iso = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
        with self._lock:
            self._last_prune_ts = now
            conn = None
            try:
                conn = self._connect()
                with conn:
                    conn.execute(
                        "DELETE FROM shipments "
                        "WHERE (ship_date IS NOT NULL AND ship_date != '' AND ship_date < ?) "
                        "OR ((ship_date IS NULL OR ship_date = '') AND updated_at < ?)",
                        (cutoff_iso, cutoff_iso),
                    )
                    conn.execute(
                        "DELETE FROM indexed_days WHERE day_iso < ?",
                        (cutoff_iso,),
                    )
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self._reset_corrupt_db()
            except Exception as e:
                logger.debug("[Кэш] Ошибка prune_old: %s", e)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


_SHIPMENTS_CACHE = _ShipmentsCache()


def _parse_jwt_exp(token: str) -> float | None:
    """Извлекает timestamp истечения (exp) из payload JWT без проверки подписи."""
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        exp = payload.get("exp")
        if exp is not None:
            return float(exp)
    except Exception:
        pass
    return None


class AdvantumTokenManager:
    """
    Потокобезопасный менеджер общего Bearer JWT-токена Advantum.
    Исключает лишние параллельные логины через браузер и сразу закрывает браузер
    авторизации после получения токена.
    """

    def __init__(self, safety_margin_sec: float = 120.0):
        self._lock = threading.Lock()
        self._token: str = ""
        self._exp_ts: float = 0.0
        self._safety_margin_sec = float(safety_margin_sec)

    def _is_valid_unlocked(self) -> bool:
        return bool(self._token and time.time() < (self._exp_ts - self._safety_margin_sec))

    def is_valid(self) -> bool:
        with self._lock:
            return self._is_valid_unlocked()

    def set_token(self, token: str) -> None:
        if not token:
            return
        exp = _parse_jwt_exp(token)
        with self._lock:
            self._token = token
            self._exp_ts = exp if exp is not None else (time.time() + 1800.0)

    def invalidate(self, bad_token: str | None = None) -> None:
        with self._lock:
            if bad_token is None or self._token == bad_token:
                self._token = ""
                self._exp_ts = 0.0

    def get_token(
        self,
        config: Config,
        force_refresh: bool = False,
        login_fn: Any | None = None,
    ) -> str:
        with self._lock:
            if not force_refresh and self._is_valid_unlocked():
                return self._token

            if login_fn is not None:
                tok = str(login_fn(config) or "")
            else:
                from browser import create_driver, login_to_advantum

                drv = None
                try:
                    logger.info("[Auth] Получение свежего JWT-токена Advantum через браузер...")
                    drv = create_driver(config, headless=True)
                    login_to_advantum(drv, config)
                    tok = get_advantum_bearer_token(drv)
                finally:
                    if drv is not None:
                        try:
                            drv.quit()
                        except Exception:
                            pass

            if not tok:
                raise RuntimeError("Не удалось получить access_token Advantum")

            exp = _parse_jwt_exp(tok)
            self._token = tok
            self._exp_ts = exp if exp is not None else (time.time() + 1800.0)
            return tok


ADVANTUM_TOKEN_MANAGER = AdvantumTokenManager()


def _build_os_ssl_context() -> ssl.SSLContext:
    """
    Создает SSLContext на базе системного хранилища сертификатов ОС (включая корневые
    сертификаты корпоративного прокси в Windows) и при наличии подгружает бандл certifi.
    """
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    try:
        import certifi

        ca_bundle = certifi.where()
        if ca_bundle and os.path.exists(ca_bundle):
            ctx.load_verify_locations(cafile=ca_bundle)
    except Exception:
        pass
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class _SystemSSLContextAdapter(requests.adapters.HTTPAdapter):
    """HTTP-адаптер requests, использующий системное хранилище сертификатов ОС."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None, **kwargs: Any):
        self._ssl_context = ssl_context or _build_os_ssl_context()
        super().__init__(**kwargs)

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        pool_kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def build_connection_pool_key_attributes(
        self,
        request: requests.PreparedRequest,
        verify: Any,
        cert: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        if verify is True and host_params.get("scheme") == "https":
            pool_kwargs["ssl_context"] = self._ssl_context
        return host_params, pool_kwargs


class _AdvantumApiClient:
    """
    Потокобезопасный HTTP-клиент Advantum API:
    1. Каждый рабочий поток получает собственный requests.Session с системным хранилищем
       сертификатов ОС; общее число одновременных запросов ограничено _GLOBAL_ADVANTUM_API_SEMAPHORE.
    2. Автоматически обновляет протухший токен при HTTP 401 (через token_manager) и делает
       вежливый retry с backoff при HTTP 429/503.
    3. Если корпоративный прокси/антивирус блокирует TLS на уровне Python (SSLError/ProxyError),
       переключается на same-origin fetch() внутри браузера (с ленивым запуском при необходимости).
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://atms.advantum.ru",
        driver: WebDriver | None = None,
        config: Config | None = None,
        token_manager: AdvantumTokenManager | None = None,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.driver = driver
        self.config = config
        self.token_manager = token_manager
        self._use_browser_fetch = False

        self._ssl_context = _build_os_ssl_context()
        self._tls = threading.local()
        self._token_lock = threading.Lock()
        self._browser_lock = threading.Lock()
        self._owned_driver: WebDriver | None = None

    def _get_session(self) -> requests.Session:
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.mount("https://", _SystemSSLContextAdapter(ssl_context=self._ssl_context))
            sess.headers.update({"Accept": "application/json"})
            self._tls.session = sess
        return sess

    @property
    def session(self) -> requests.Session:
        return self._get_session()

    def _current_token(self) -> str:
        with self._token_lock:
            return self.token

    def _ensure_browser_driver(self) -> WebDriver | None:
        with self._browser_lock:
            if self.driver is not None:
                return self.driver
            if self.config is None:
                return None
            try:
                from browser import create_driver, login_to_advantum

                drv = create_driver(self.config, headless=True)
                login_to_advantum(drv, self.config)
                try:
                    fresh_tok = get_advantum_bearer_token(drv)
                    if fresh_tok:
                        with self._token_lock:
                            self.token = fresh_tok
                        if self.token_manager is not None:
                            self.token_manager.set_token(fresh_tok)
                except Exception:
                    pass
                self.driver = drv
                self._owned_driver = drv
                return self.driver
            except Exception as e:
                logger.warning("[API] Не удалось лениво запустить браузер для fallback fetch(): %s", e)
                return None

    def close(self) -> None:
        with self._browser_lock:
            if self._owned_driver is not None:
                try:
                    self._owned_driver.quit()
                except Exception:
                    pass
                if self.driver is self._owned_driver:
                    self.driver = None
                self._owned_driver = None

    def _browser_fetch_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = 25,
        token: str | None = None,
    ) -> tuple[bool, int, Any, bool, str | None]:
        active_token = token if token is not None else self._current_token()
        with self._browser_lock:
            if not self.driver:
                return False, 0, None, False, "Браузер недоступен для fallback fetch()"

            rel_path = path if path.startswith("/") else f"/{path}"
            if params:
                qs = urllib.parse.urlencode(params)
                sep = "&" if "?" in rel_path else "?"
                rel_path = f"{rel_path}{sep}{qs}"

            timeout_ms = min(max(int(timeout * 1000), 1000), 22000)

            try:
                cur_url = str(getattr(self.driver, "current_url", "") or "")
                if not cur_url.startswith(self.base_url):
                    self.driver.get(f"{self.base_url}/pre-load.js")
            except Exception:
                pass

            async_core = """
                const url = arguments[0];
                const token = arguments[1];
                const timeoutMs = arguments[2];
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const resp = await fetch(url, {
                        method: 'GET',
                        headers: {
                            'Authorization': 'Bearer ' + token,
                            'Accept': 'application/json'
                        },
                        signal: controller.signal
                    });
                    const text = await resp.text();
                    let data = null;
                    try { data = text ? JSON.parse(text) : null; } catch (e) {}
                    return {
                        ok: !!resp.ok,
                        status: Number(resp.status || 0),
                        data: data,
                        timeout: false,
                        error: resp.ok ? null : ('HTTP ' + resp.status)
                    };
                } catch (e) {
                    const msg = String(e || 'Fetch failed');
                    const isTimeout = Boolean(e && (e.name === 'AbortError' || msg.includes('AbortError')));
                    return {
                        ok: false,
                        status: 0,
                        data: null,
                        timeout: isTimeout,
                        error: msg
                    };
                } finally {
                    clearTimeout(timer);
                }
            """

            try:
                if isinstance(self.driver, DirectCDPDriver) or not hasattr(
                    self.driver, "execute_async_script"
                ):
                    script = f"return (async function() {{ {async_core} }})();"
                    res = self.driver.execute_script(script, rel_path, active_token, timeout_ms)
                else:
                    script = (
                        "const done = arguments[arguments.length - 1];\n"
                        f"(async function() {{ {async_core} }})().then(done).catch(err => "
                        "done({ok: false, status: 0, data: null, timeout: false, error: String(err)}));"
                    )
                    res = self.driver.execute_async_script(script, rel_path, active_token, timeout_ms)

                if not isinstance(res, dict):
                    return False, 0, None, False, f"Некорректный ответ browser fetch: {type(res).__name__}"
                return (
                    bool(res.get("ok")),
                    int(res.get("status") or 0),
                    res.get("data"),
                    bool(res.get("timeout")),
                    res.get("error"),
                )
            except Exception as e:
                err_str = str(e)
                is_to = "timed out" in err_str.lower() or "timeout" in err_str.lower()
                return False, 0, None, is_to, err_str

    def get_json(
        self, path: str, params: dict[str, Any] | None = None, timeout: int = 30
    ) -> tuple[bool, int, Any, bool, str | None]:
        """Возвращает кортеж (ok, status_code, json_data, is_timeout, error_msg)."""
        rel_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{rel_path}"
        refreshed_401 = False
        rate_retries = 0

        while True:
            cur_token = self._current_token()
            resp = None

            if self._use_browser_fetch:
                if self.driver is None and self._ensure_browser_driver() is None:
                    return False, 0, None, False, "Браузер недоступен для fallback fetch()"
                cur_token = self._current_token()
                with _GLOBAL_ADVANTUM_API_SEMAPHORE:
                    ok, status_code, data, is_timeout, err_msg = self._browser_fetch_json(
                        path, params=params, timeout=timeout, token=cur_token
                    )
            else:
                sess = self._get_session()
                headers = {"Authorization": f"Bearer {cur_token}", "Accept": "application/json"}
                try:
                    with _GLOBAL_ADVANTUM_API_SEMAPHORE:
                        resp = sess.get(url, params=params, headers=headers, timeout=timeout)
                    status_code = resp.status_code
                    if resp.ok:
                        try:
                            return True, status_code, resp.json(), False, None
                        except Exception as e:
                            return False, status_code, None, False, f"Ошибка JSON: {e}"
                    ok, data, is_timeout, err_msg = False, None, False, f"HTTP {status_code}"
                except requests.exceptions.Timeout as exc:
                    return False, 0, None, True, str(exc)
                except (requests.exceptions.SSLError, requests.exceptions.ProxyError) as exc:
                    if self.driver is not None or self.config is not None:
                        logger.warning(
                            "[API] Прямой Python-запрос к Advantum заблокирован TLS/прокси (%s). Переключаемся на browser fetch().",
                            exc,
                        )
                        print("[API] Корпоративный прокси/TLS: переключение запросов на браузерный fetch()...")
                        if self._ensure_browser_driver() is not None:
                            self._use_browser_fetch = True
                            continue
                    return False, 0, None, False, str(exc)
                except requests.exceptions.RequestException as exc:
                    return False, 0, None, False, str(exc)

            if ok:
                return True, status_code, data, False, None

            if (
                status_code == 401
                and not refreshed_401
                and self.token_manager is not None
                and self.config is not None
            ):
                refreshed_401 = True
                try:
                    self.token_manager.invalidate(cur_token)
                    new_tok = self.token_manager.get_token(self.config)
                    with self._token_lock:
                        self.token = new_tok
                    continue
                except Exception as tok_err:
                    logger.warning("[API] Не удалось обновить токен после HTTP 401: %s", tok_err)
                    return False, 401, None, False, err_msg or "HTTP 401"

            if status_code in (429, 503) and rate_retries < 2:
                rate_retries += 1
                backoff = 1.5 * rate_retries
                if resp is not None:
                    try:
                        ra = resp.headers.get("Retry-After")
                        if ra:
                            backoff = min(5.0, max(0.5, float(ra)))
                    except Exception:
                        pass
                time.sleep(backoff)
                continue

            return False, status_code, data, is_timeout, err_msg


def _fetch_shipments_chunk_detailed(
    api_client: _AdvantumApiClient | requests.Session,
    chunk_from: datetime.date,
    chunk_to: datetime.date,
    base_url: str = "https://atms.advantum.ru",
) -> tuple[list[dict[str, Any]], set[datetime.date]]:
    """
    Запрашивает список рейсов через GET /api/v1/shipments за интервал chunk_from..chunk_to.
    Возвращает (items, succeeded_days). Использует дифференцированный таймаут (12с для
    многодневных окон и 28с для однодневных) и при таймауте дробит окно пополам до 1 дня.
    """
    params = {
        "scheduledStartInstantFrom": f"{chunk_from.isoformat()}T00:00:00.000Z",
        "scheduledStartInstantTo": f"{chunk_to.isoformat()}T23:59:59.999Z",
    }
    span_days = (chunk_to - chunk_from).days
    req_timeout = 12 if span_days > 0 else 28

    if isinstance(api_client, _AdvantumApiClient):
        ok, status_code, data, is_timeout, err_msg = api_client.get_json(
            "/api/v1/shipments", params=params, timeout=req_timeout
        )
    else:
        ok, status_code, data, is_timeout, err_msg = False, 0, None, False, None
        try:
            with _GLOBAL_ADVANTUM_API_SEMAPHORE:
                resp = api_client.get(
                    f"{base_url.rstrip('/')}/api/v1/shipments", params=params, timeout=req_timeout
                )
            status_code = resp.status_code
            if resp.ok:
                ok = True
                data = resp.json()
            else:
                err_msg = f"HTTP {resp.status_code}"
        except requests.exceptions.Timeout as exc:
            is_timeout = True
            err_msg = str(exc)
        except requests.exceptions.RequestException as exc:
            err_msg = str(exc)

    if ok:
        days_ok = {
            chunk_from + datetime.timedelta(days=i)
            for i in range(max(0, span_days) + 1)
        }
        return (data if isinstance(data, list) else []), days_ok

    should_split = span_days > 0 and (
        is_timeout or status_code in (400, 408, 500, 502, 503, 504)
    )
    if should_split:
        mid = chunk_from + datetime.timedelta(days=span_days // 2)
        reason = err_msg or f"HTTP {status_code}"
        logger.info(
            "[API] Окно %s..%s (%s), дробим на 2 под-окна...",
            chunk_from,
            chunk_to,
            reason,
        )
        part1, d1 = _fetch_shipments_chunk_detailed(
            api_client, chunk_from, mid, base_url=base_url
        )
        part2, d2 = _fetch_shipments_chunk_detailed(
            api_client, mid + datetime.timedelta(days=1), chunk_to, base_url=base_url
        )
        return part1 + part2, d1 | d2

    if status_code:
        logger.warning(
            "[API] GET /api/v1/shipments (%s..%s) вернул статус %s",
            chunk_from,
            chunk_to,
            status_code,
        )
    else:
        logger.warning(
            "[API] Ошибка запроса /api/v1/shipments за %s..%s: %s",
            chunk_from,
            chunk_to,
            err_msg,
        )
    return [], set()


def _fetch_shipments_chunk_http(
    api_client: _AdvantumApiClient | requests.Session,
    chunk_from: datetime.date,
    chunk_to: datetime.date,
    base_url: str = "https://atms.advantum.ru",
) -> list[dict[str, Any]]:
    """Совместимая обертка, возвращающая только список рейсов без набора закрытых дней."""
    items, _ = _fetch_shipments_chunk_detailed(
        api_client, chunk_from, chunk_to, base_url=base_url
    )
    return items


def _group_days_into_windows(
    days_desc: list[datetime.date], max_window_days: int = 2
) -> list[tuple[datetime.date, datetime.date]]:
    """Группирует отсортированный по убыванию список дат в окна длиной не более max_window_days."""
    if not days_desc:
        return []
    ordered = sorted(set(days_desc), reverse=True)
    windows: list[tuple[datetime.date, datetime.date]] = []
    win_to = ordered[0]
    win_from = ordered[0]
    for d in ordered[1:]:
        if (win_from - d).days == 1 and (win_to - d).days < max_window_days:
            win_from = d
        else:
            windows.append((win_from, win_to))
            win_to = d
            win_from = d
    windows.append((win_from, win_to))
    return windows


def _estimate_target_dates(
    missing_targets: list[str],
    anchors: list[tuple[str, int, datetime.date]],
    min_date: datetime.date,
    max_date: datetime.date,
) -> list[datetime.date]:
    """
    Оценивает вероятные даты рейсов по прогрессии номеров МЛ (напр. МСКxxxxxx -> ~160/день
    или реальный темп по калибровочным якорям). Возвращает список ориентировочных дат.
    """
    if not missing_targets or not anchors:
        return []

    by_prefix_date: dict[str, dict[datetime.date, list[int]]] = {}
    for p, num, d in anchors:
        by_prefix_date.setdefault(p, {}).setdefault(d, []).append(num)

    prefix_models: dict[str, tuple[datetime.date, float, float]] = {}
    for p, date_map in by_prefix_date.items():
        day_medians = sorted(
            ((d, float(statistics.median(nums))) for d, nums in date_map.items()),
            key=lambda x: x[0],
        )
        if len(day_medians) >= 2:
            d0, m0 = day_medians[0]
            d1, m1 = day_medians[-1]
            span = (d1 - d0).days
            if span >= 1 and m1 > m0:
                rate = min(1500.0, max(30.0, (m1 - m0) / float(span)))
            else:
                rate = DEFAULT_ML_RATE_PER_DAY
            prefix_models[p] = (d1, m1, rate)
        elif len(day_medians) == 1:
            d1, m1 = day_medians[0]
            prefix_models[p] = (d1, m1, DEFAULT_ML_RATE_PER_DAY)

    est_dates: list[datetime.date] = []
    min_ord = min_date.toordinal()
    max_ord = max_date.toordinal()
    for t in missing_targets:
        parsed = _extract_ml_prefix_num(t)
        if not parsed:
            continue
        p, target_num = parsed
        model = prefix_models.get(p)
        if not model:
            continue
        ref_date, ref_num, rate = model
        delta_days = (target_num - ref_num) / rate
        est_ord = round(ref_date.toordinal() + delta_days)
        clamped_ord = max(min_ord, min(max_ord, est_ord))
        est_dates.append(datetime.date.fromordinal(clamped_ord))
    return est_dates


def _prioritize_windows(
    windows: list[tuple[datetime.date, datetime.date]],
    estimated_dates: list[datetime.date],
) -> list[tuple[datetime.date, datetime.date]]:
    """
    Переупорядочивает оставшиеся окна поиска так, чтобы ближайшие к оценочным датам
    проверялись первыми, не отбрасывая при этом ни одного резервного окна.
    """
    if not windows:
        return []
    if not estimated_dates:
        return sorted(windows, key=lambda w: w[1], reverse=True)

    def _window_distance(w: tuple[datetime.date, datetime.date]) -> tuple[int, int]:
        w_from, w_to = w
        best = min(
            0
            if w_from <= ed <= w_to
            else min(abs((ed - w_from).days), abs((ed - w_to).days))
            for ed in estimated_dates
        )
        return (best, -w_to.toordinal())

    return sorted(windows, key=_window_distance)


def find_shipments_by_ml_api(
    token: str,
    ml_numbers: list[str],
    date_filter: tuple[datetime.date, datetime.date] | None = None,
    max_lookback_days: int = 60,
    base_url: str = "https://atms.advantum.ru",
    driver: WebDriver | None = None,
    api_client: _AdvantumApiClient | None = None,
    cache: _ShipmentsCache | None = None,
    use_cache: bool = True,
    max_workers: int = 3,
) -> dict[str, dict[str, Any]]:
    """
    Ускоренный каскадный поиск рейсов по номерам МЛ (routeNum) или ID рейсов через REST API:
    1. Проверяет пассивный SQLite WAL-кэш (мгновенный возврат для ранее встречавшихся старых рейсов).
    2. Пропускает полностью проиндексированные старые дни (> 3 дней назад).
    3. Оценивает целевую дату по темпу номеров МЛ (Smart Jump) и проверяет ближайшие 2-дневные окна первыми.
    4. Запрашивает окна волнами по 3 потока (последовательно в режиме browser fetch) с авто-дроблением
       и мгновенной остановкой, как только все искомые МЛ найдены.
    """
    base_url = base_url.rstrip("/")
    index: dict[str, dict[str, Any]] = {}
    owns_client = api_client is None
    client = api_client or _AdvantumApiClient(
        token=token,
        base_url=base_url,
        driver=driver,
        token_manager=ADVANTUM_TOKEN_MANAGER if driver is None else None,
    )
    active_cache = (cache or _SHIPMENTS_CACHE) if use_cache else None

    try:
        seen_norms: set[str] = set()
        normalized_targets: list[str] = []
        for ml in ml_numbers:
            if not ml or not ml.strip():
                continue
            norm = normalize_ml_number(ml)
            if norm not in seen_norms:
                seen_norms.add(norm)
                normalized_targets.append(norm)

        if not normalized_targets:
            return {}

        def _all_found() -> bool:
            return all(_lookup_indexed_shipment(t, index) is not None for t in normalized_targets)

        def _build_result_map() -> dict[str, dict[str, Any]]:
            res: dict[str, dict[str, Any]] = {}
            for t in normalized_targets:
                found = _lookup_indexed_shipment(t, index)
                if found is not None:
                    res[t] = found
            return res

        # 0. Пассивный SQLite-кэш поискового индекса
        if active_cache is not None:
            active_cache.prune_old(max_age_days=90)
            cached_hits = active_cache.lookup(normalized_targets)
            if cached_hits:
                for item in cached_hits.values():
                    _index_shipment_record(item, index)
                print(
                    f"[Кэш] Найдено в локальном индексе: {len(cached_hits)}/{len(normalized_targets)} МЛ."
                )
            if _all_found():
                return _build_result_map()

        # 1. Прямая проверка внутренних числовых ID рейсов (7-9 цифр)
        for norm in normalized_targets:
            if _lookup_indexed_shipment(norm, index) is not None:
                continue
            if _is_pure_shipment_id(norm):
                ok, _, item, _, err_msg = client.get_json(f"/api/v1/shipments/{norm}", timeout=15)
                if ok and isinstance(item, dict) and item.get("id"):
                    _index_shipment_record(item, index)
                    if active_cache is not None:
                        active_cache.upsert_shipments([item])
                    print(f"[API] Рейс ID {norm} найден напрямую (МЛ: {item.get('routeNum', '—')}).")
                elif err_msg:
                    logger.debug("Ошибка прямого запроса ID %s: %s", norm, err_msg)

        if _all_found():
            return _build_result_map()

        # 2. Строим 2-дневные окна поиска (отбрасывая уже закрытые прошлые дни из кэша)
        if date_filter:
            start_d, end_d = date_filter
        else:
            end_d = datetime.date.today() + datetime.timedelta(days=1)
            start_d = end_d - datetime.timedelta(days=max_lookback_days)

        total_span = max(0, (end_d - start_d).days)
        all_days = [end_d - datetime.timedelta(days=i) for i in range(total_span + 1)]
        closed_days = active_cache.get_indexed_days() if active_cache is not None else set()
        days_to_scan = [d for d in all_days if d.isoformat() not in closed_days]
        remaining_windows = _group_days_into_windows(days_to_scan, max_window_days=2)

        if not remaining_windows:
            return _build_result_map()

        missing_now = [t for t in normalized_targets if _lookup_indexed_shipment(t, index) is None]
        target_prefixes = {
            p for p_opt in (_extract_ml_prefix_num(t) for t in missing_now) if p_opt for p, _ in [p_opt]
        }
        anchors: list[tuple[str, int, datetime.date]] = (
            active_cache.get_ml_anchors(prefixes=target_prefixes or None)
            if active_cache is not None
            else []
        )

        def _ingest_window_results(items: list[dict[str, Any]], days_ok: set[datetime.date]) -> None:
            for item in items:
                _index_shipment_record(item, index)
                r_num = item.get("routeNum")
                s_date = _extract_shipment_date(item)
                if r_num and s_date:
                    parsed = _extract_ml_prefix_num(normalize_ml_number(str(r_num)))
                    if parsed:
                        try:
                            d_obj = datetime.date.fromisoformat(s_date)
                            if not target_prefixes or parsed[0] in target_prefixes:
                                anchors.append((parsed[0], parsed[1], d_obj))
                        except Exception:
                            pass
            if active_cache is not None:
                if items:
                    active_cache.upsert_shipments(items)
                if days_ok:
                    active_cache.mark_days_indexed(days_ok, items_count=len(items))

        # 3. Если нет свежего якоря (за последние 2 дня), сначала быстро снимаем новейшее 2-дневное окно
        recent_cutoff = datetime.date.today() - datetime.timedelta(days=2)
        has_recent_anchor = any(d >= recent_cutoff for _, _, d in anchors)
        scanned_windows = 0
        total_windows_initial = len(remaining_windows)

        if not has_recent_anchor and remaining_windows:
            w_from, w_to = remaining_windows.pop(0)
            scanned_windows += 1
            print(
                f"[API] Поиск МЛ в периоде {w_from.strftime('%d.%m.%Y')} — {w_to.strftime('%d.%m.%Y')} "
                f"(свежее окно 1/{total_windows_initial})..."
            )
            items, days_ok = _fetch_shipments_chunk_detailed(
                client, w_from, w_to, base_url=base_url
            )
            _ingest_window_results(items, days_ok)
            found_now = sum(1 for t in normalized_targets if _lookup_indexed_shipment(t, index) is not None)
            print(
                f"[API] Получено {len(items)} рейсов; найдено искомых МЛ: {found_now}/{len(normalized_targets)}."
            )
            if found_now == len(normalized_targets):
                return _build_result_map()

        # 4. Smart Jump: сортируем оставшиеся окна ближе к оценочным датам и обходим волнами
        missing_now = [t for t in normalized_targets if _lookup_indexed_shipment(t, index) is None]
        est_dates = _estimate_target_dates(missing_now, anchors, start_d, end_d)
        remaining_windows = _prioritize_windows(remaining_windows, est_dates)
        if est_dates and remaining_windows:
            est_str = ", ".join(sorted({d.strftime("%d.%m.%Y") for d in est_dates}))
            print(f"[API] Ориентир Smart Jump: {est_str} (осталось окон: {len(remaining_windows)}).")

        while remaining_windows:
            wave_size = (
                1
                if (client._use_browser_fetch or max_workers <= 1)
                else min(max_workers, len(remaining_windows))
            )
            wave = remaining_windows[:wave_size]
            remaining_windows = remaining_windows[wave_size:]

            if wave_size == 1:
                w_from, w_to = wave[0]
                scanned_windows += 1
                print(
                    f"[API] Поиск МЛ в периоде {w_from.strftime('%d.%m.%Y')} — {w_to.strftime('%d.%m.%Y')} "
                    f"(шаг {scanned_windows}/{total_windows_initial})..."
                )
                items, days_ok = _fetch_shipments_chunk_detailed(
                    client, w_from, w_to, base_url=base_url
                )
                _ingest_window_results(items, days_ok)
                total_wave_items = len(items)
            else:
                desc_parts = [f"{w0.strftime('%d.%m')}–{w1.strftime('%d.%m')}" for w0, w1 in wave]
                print(
                    f"[API] Параллельный опрос {len(wave)} окон: {', '.join(desc_parts)}..."
                )
                total_wave_items = 0
                with ThreadPoolExecutor(max_workers=wave_size) as pool:
                    fut_map = {
                        pool.submit(
                            _fetch_shipments_chunk_detailed,
                            client,
                            w0,
                            w1,
                            base_url,
                        ): (w0, w1)
                        for w0, w1 in wave
                    }
                    for fut in as_completed(fut_map):
                        scanned_windows += 1
                        try:
                            items, days_ok = fut.result()
                            total_wave_items += len(items)
                            _ingest_window_results(items, days_ok)
                        except Exception as w_err:
                            w0, w1 = fut_map[fut]
                            logger.warning("[API] Ошибка параллельного окна %s..%s: %s", w0, w1, w_err)

            found_now = sum(1 for t in normalized_targets if _lookup_indexed_shipment(t, index) is not None)
            print(
                f"[API] Получено {total_wave_items} рейсов за волну; найдено искомых МЛ: {found_now}/{len(normalized_targets)}."
            )
            if found_now == len(normalized_targets):
                break

            if remaining_windows and anchors:
                missing_now = [t for t in normalized_targets if _lookup_indexed_shipment(t, index) is None]
                est_dates = _estimate_target_dates(missing_now, anchors, start_d, end_d)
                remaining_windows = _prioritize_windows(remaining_windows, est_dates)

        return _build_result_map()
    finally:
        if owns_client:
            client.close()


def fetch_shipments_api_by_interval(
    driver: WebDriver,
    date_from: datetime.date,
    date_to: datetime.date,
) -> dict[str, dict[str, Any]]:
    """
    Совместимая обертка: запрашивает данные о рейсах за интервал дат через нативный REST API.
    """
    try:
        token = get_advantum_bearer_token(driver)
        client = _AdvantumApiClient(token=token, driver=driver)
        index: dict[str, dict[str, Any]] = {}
        cur_from = date_from
        while cur_from <= date_to:
            cur_to = min(cur_from + datetime.timedelta(days=3), date_to)
            items = _fetch_shipments_chunk_http(client, cur_from, cur_to)
            for item in items:
                r_num = item.get("routeNum")
                if r_num:
                    norm = normalize_ml_number(str(r_num))
                    index[norm] = item
                    digits = "".join(re.findall(r"\d+", norm))
                    if digits and digits not in index:
                        index[digits] = item
            cur_from = cur_to + datetime.timedelta(days=1)
        return index
    except Exception as e:
        logger.warning("Не удалось выполнить предварительный опрос API Advantum: %s", e)
        return {}


# ─── Рендеринг печатной формы термографика из данных API ──────────────────────

ADVANTUM_SENSOR_PALETTE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]


def _parse_tz_offset(offset_str: str | None) -> tuple[datetime.timezone, str]:
    """Парсит строку смещения вроде '+03:00' в (timezone, отображаемая_строка)."""
    raw = (offset_str or "+03:00").strip()
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", raw)
    if not m:
        return datetime.timezone(datetime.timedelta(hours=3)), "+03:00"
    sign = 1 if m.group(1) == "+" else -1
    hours = int(m.group(2))
    mins = int(m.group(3))
    td = datetime.timedelta(minutes=sign * (hours * 60 + mins))
    return datetime.timezone(td), f"{m.group(1)}{m.group(2)}:{m.group(3)}"


def _parse_iso_utc(dt_str: str | None) -> datetime.datetime | None:
    """Парсит ISO-дату/время в timezone-aware datetime в UTC."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    s = dt_str.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _format_duration_ru(start_dt: datetime.datetime, finish_dt: datetime.datetime) -> str:
    """Форматирует длительность периода чтения как в Advantum."""
    total_sec = max(0, int((finish_dt - start_dt).total_seconds()))
    total_mins = total_sec // 60
    days = total_mins // 1440
    hours = (total_mins % 1440) // 60
    mins = total_mins % 60
    parts = []
    if days > 0:
        parts.append(f"{days} д.")
    if hours > 0 or days > 0:
        parts.append(f"{hours} ч.")
    parts.append(f"{mins} мин.")
    return " ".join(parts)


def _compute_nice_ticks(y_lo: float, y_hi: float, target_count: int = 8) -> list[float]:
    """Подбирает аккуратный шаг делений оси температуры Y."""
    span = y_hi - y_lo
    if span <= 0:
        return [y_lo]
    raw_step = span / max(2, target_count)
    mag = 10 ** math.floor(math.log10(raw_step))
    norm = raw_step / mag
    if norm <= 1.0:
        step = 1.0 * mag
    elif norm <= 2.0:
        step = 2.0 * mag
    elif norm <= 2.5 and mag >= 1:
        step = 2.5 * mag
    elif norm <= 5.0:
        step = 5.0 * mag
    else:
        step = 10.0 * mag
    step = max(step, 0.5)
    start_tick = math.ceil(y_lo / step) * step
    ticks: list[float] = []
    cur = start_tick
    while cur <= y_hi + 1e-9:
        ticks.append(round(cur, 2))
        cur += step
    return ticks


def _format_temp_short(val: float) -> str:
    """Форматирует число температуры для подписи диапазона (+2, +15, -18, 0)."""
    f = float(val)
    if abs(f) < 1e-6:
        return "0"
    if abs(f - round(f)) < 1e-6:
        i = int(round(f))
        return f"+{i}" if i > 0 else str(i)
    return f"+{f:g}" if f > 0 else f"{f:g}"


def _get_wholesale_dual_regime(
    shipment_full: dict[str, Any],
    sensors_by_sec: dict[int, list[dict[str, Any]]],
    active_sections: list[int],
    sec1_t_min: float | None,
    sec1_t_max: float | None,
    sec1_restr: dict[str, Any],
    chart_data: dict[str, Any],
) -> tuple[float, float, dict[str, Any]] | None:
    """
    Возвращает (t2_min, t2_max, secondSectionRestrictions) для отрисовки второй
    зелёной полосы нормы СТРОГО для одноотсечных оптовых рейсов с двумя разными
    температурными режимами (как МСК554534).
    """
    # 1. Одноотсечная машина (нет датчиков от 2-го и других отсеков, даже пустых)
    if set(sensors_by_sec.keys()) != {1} or active_sections != [1]:
        return None

    # 2. Строго оптовый рейс
    ct_id = shipment_full.get("commercialTypeId")
    ct_name = str(shipment_full.get("commercialTypeName") or "").lower()
    if not (ct_id == 6 or "опт" in ct_name):
        return None

    # 3. Заданы два разных валидных температурных режима
    t2_min = shipment_full.get("secondChamberTemperatureMin")
    if t2_min is None:
        t2_min = shipment_full.get("secondChamberTemperatureMinInCelsius")
    t2_max = shipment_full.get("secondChamberTemperatureMax")
    if t2_max is None:
        t2_max = shipment_full.get("secondChamberTemperatureMaxInCelsius")

    if not (
        isinstance(sec1_t_min, (int, float))
        and not isinstance(sec1_t_min, bool)
        and isinstance(sec1_t_max, (int, float))
        and not isinstance(sec1_t_max, bool)
        and isinstance(t2_min, (int, float))
        and not isinstance(t2_min, bool)
        and isinstance(t2_max, (int, float))
        and not isinstance(t2_max, bool)
    ):
        return None

    f1_min, f1_max = float(sec1_t_min), float(sec1_t_max)
    f2_min, f2_max = float(t2_min), float(t2_max)
    if f2_min >= f2_max or (f1_min == f2_min and f1_max == f2_max):
        return None

    # 4. Нет плечевых интервалов от самого Advantum
    if sec1_restr.get("sectionTemperatureLegControlParams"):
        return None

    sec2_restr = chart_data.get("secondSectionRestrictions") or {}
    return f2_min, f2_max, sec2_restr


def _build_compartment_svg(
    sec_sensors: list[dict[str, Any]],
    t_min: float | None,
    t_max: float | None,
    restr: dict[str, Any],
    start_dt: datetime.datetime,
    finish_dt: datetime.datetime,
    tz_obj: datetime.timezone,
    extra_regimes: list[tuple[float, float, dict[str, Any] | None]] | None = None,
) -> tuple[str, float | None, float | None]:
    """Строит SVG-график температуры одного отсека в точном стиле Advantum."""
    width = 960
    height = 410
    m_left, m_top, m_right, m_bottom = 68, 12, 20, 68
    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    parsed_lines: list[tuple[int, dict[str, Any], list[tuple[datetime.datetime, float]]]] = []
    all_vals: list[float] = []

    for idx, sen in enumerate(sec_sensors):
        pts: list[tuple[datetime.datetime, float]] = []
        for item in sen.get("values") or []:
            dt = _parse_iso_utc(item.get("dateInstance"))
            val = item.get("value")
            if dt is not None and val is not None:
                try:
                    fval = float(val)
                    if not math.isnan(fval):
                        pts.append((dt, fval))
                        all_vals.append(fval)
                except Exception:
                    pass
        pts.sort(key=lambda p: p[0])
        parsed_lines.append((idx, sen, pts))

    if not all_vals:
        return "", None, None

    temp_min_val = min(all_vals)
    temp_max_val = max(all_vals)

    domain_vals = list(all_vals)

    def _extend_domain(r_min: float | None, r_max: float | None, r_restr: dict[str, Any] | None) -> None:
        r_dict = r_restr if r_restr is not None else restr
        r_tol = r_dict.get("toleranceInDegreesCelsius")
        r_dev_up = r_dict.get("sensorDeviationUpper")
        r_dev_lo = r_dict.get("sensorDeviationLower")
        if isinstance(r_min, (int, float)):
            domain_vals.append(float(r_min))
            if isinstance(r_tol, (int, float)):
                domain_vals.append(float(r_min) - float(r_tol))
            if isinstance(r_dev_lo, (int, float)):
                domain_vals.append(
                    float(r_min)
                    - float(r_dev_lo)
                    - (float(r_tol) if isinstance(r_tol, (int, float)) else 0.0)
                )
        if isinstance(r_max, (int, float)):
            domain_vals.append(float(r_max))
            if isinstance(r_tol, (int, float)):
                domain_vals.append(float(r_max) + float(r_tol))
            if isinstance(r_dev_up, (int, float)):
                domain_vals.append(
                    float(r_max)
                    + float(r_dev_up)
                    + (float(r_tol) if isinstance(r_tol, (int, float)) else 0.0)
                )

    _extend_domain(t_min, t_max, restr)
    for ex_min, ex_max, ex_restr in extra_regimes or []:
        _extend_domain(ex_min, ex_max, ex_restr if ex_restr is not None else restr)

    raw_lo = min(domain_vals)
    raw_hi = max(domain_vals)
    if abs(raw_hi - raw_lo) < 0.5:
        raw_lo -= 2.0
        raw_hi += 2.0
    span_y = raw_hi - raw_lo
    y_lo = raw_lo - span_y * 0.2
    y_hi = raw_hi + span_y * 0.1
    if y_hi <= y_lo:
        y_hi = y_lo + 1.0

    start_ts = start_dt.timestamp()
    finish_ts = finish_dt.timestamp()
    if finish_ts <= start_ts:
        finish_ts = start_ts + 3600.0

    def x_px(dt: datetime.datetime) -> float:
        return ((dt.timestamp() - start_ts) / (finish_ts - start_ts)) * plot_w

    def y_px(val: float) -> float:
        return plot_h - ((val - y_lo) / (y_hi - y_lo)) * plot_h

    svg_parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="display:block;font-family:Arial,Helvetica,sans-serif;">',
        f'<g transform="translate({m_left},{m_top})">',
        f'<defs><clipPath id="clip-plot"><rect x="0" y="0" width="{plot_w}" height="{plot_h}"/></clipPath></defs>',
    ]

    # 1. Температурные коридоры (допустимая зона и допуски)
    def _draw_band(b_min: float, b_max: float, fill_col: str, border_col: str, x_start: float = 0.0, x_w: float | None = None) -> None:
        bw = plot_w if x_w is None else max(0.0, min(plot_w - x_start, x_w))
        if bw <= 0 or b_max < b_min:
            return
        y_top = y_px(b_max)
        y_bot = y_px(b_min)
        bh = max(0.0, y_bot - y_top)
        svg_parts.append(
            f'<g clip-path="url(#clip-plot)">'
            f'<rect x="{x_start:.2f}" y="{y_top:.2f}" width="{bw:.2f}" height="{bh:.2f}" fill="{fill_col}" />'
            f'<line x1="{x_start:.2f}" y1="{y_bot:.2f}" x2="{x_start + bw:.2f}" y2="{y_bot:.2f}" stroke="{border_col}" stroke-width="2" />'
            f'<line x1="{x_start:.2f}" y1="{y_top:.2f}" x2="{x_start + bw:.2f}" y2="{y_top:.2f}" stroke="{border_col}" stroke-width="2" />'
            f'</g>'
        )

    def _draw_regime_block(
        r_min: float | None,
        r_max: float | None,
        x_start: float = 0.0,
        x_w: float | None = None,
        regime_restr: dict[str, Any] | None = None,
    ) -> None:
        if not (isinstance(r_min, (int, float)) and isinstance(r_max, (int, float))):
            return
        active_restr = regime_restr if regime_restr is not None else restr
        r_tol = active_restr.get("toleranceInDegreesCelsius")
        r_dev_up = active_restr.get("sensorDeviationUpper")
        r_dev_lo = active_restr.get("sensorDeviationLower")
        fmin, fmax = float(r_min), float(r_max)
        if isinstance(r_tol, (int, float)):
            ftol = float(r_tol)
            if isinstance(r_dev_up, (int, float)) or isinstance(r_dev_lo, (int, float)):
                du = float(r_dev_up or 0.0)
                dl = float(r_dev_lo or 0.0)
                _draw_band(fmin - dl - ftol, fmax + du + ftol, "#fef3c7", "#ffa300", x_start, x_w)
            if ftol < 0:
                _draw_band(fmin, fmax, "#F3FFCC", "#00CC00", x_start, x_w)
                _draw_band(fmin - ftol, fmax + ftol, "#CCFFCC", "#DDDD00", x_start, x_w)
            else:
                _draw_band(fmin - ftol, fmax + ftol, "#FFFFCC", "#DDDD00", x_start, x_w)
                _draw_band(fmin, fmax, "#CCFFCC", "#00CC00", x_start, x_w)
        else:
            _draw_band(fmin, fmax, "#CCFFCC", "#00CC00", x_start, x_w)

    leg_params = restr.get("sectionTemperatureLegControlParams") or []
    valid_legs = [
        lg for lg in leg_params if lg.get("start") and lg.get("finish") and _parse_iso_utc(lg.get("start")) and _parse_iso_utc(lg.get("finish"))
    ]
    drawn_band_labels: list[tuple[float, float]] = []
    if valid_legs:
        valid_legs.sort(key=lambda lg: _parse_iso_utc(lg["start"]))  # type: ignore[arg-type]
        for lg in valid_legs:
            l_s = _parse_iso_utc(lg["start"])
            l_f = _parse_iso_utc(lg["finish"])
            if l_s and l_f:
                xs = max(0.0, min(plot_w, x_px(l_s)))
                xf = max(0.0, min(plot_w, x_px(l_f)))
                if xf > xs:
                    _draw_regime_block(lg.get("temperatureMin"), lg.get("temperatureMax"), xs, xf - xs)
    else:
        _draw_regime_block(t_min, t_max, regime_restr=restr)
        if extra_regimes:
            if isinstance(t_min, (int, float)) and isinstance(t_max, (int, float)):
                drawn_band_labels.append((float(t_min), float(t_max)))
            for ex_min, ex_max, ex_restr in extra_regimes:
                _draw_regime_block(ex_min, ex_max, regime_restr=ex_restr if ex_restr is not None else restr)
                drawn_band_labels.append((float(ex_min), float(ex_max)))

    # 2. Сетка и метки оси Y
    for tick in _compute_nice_ticks(y_lo, y_hi):
        yp = y_px(tick)
        if 0 <= yp <= plot_h:
            svg_parts.append(
                f'<line x1="0" y1="{yp:.2f}" x2="{plot_w}" y2="{yp:.2f}" stroke="#e2e8f0" stroke-width="1" />'
            )
            svg_parts.append(
                f'<line x1="-5" y1="{yp:.2f}" x2="0" y2="{yp:.2f}" stroke="#64748b" stroke-width="1" />'
            )
            svg_parts.append(
                f'<text x="-8" y="{yp + 4:.2f}" text-anchor="end" font-size="11" fill="#1e293b">{tick:.1f} °C</text>'
            )

    # 3. Вертикальная сетка и метки времени по оси X (повернуты на -45°)
    n_xticks = 10
    for i in range(n_xticks + 1):
        frac = i / n_xticks
        t_cur = start_ts + frac * (finish_ts - start_ts)
        dt_loc = datetime.datetime.fromtimestamp(t_cur, tz=tz_obj)
        xp = frac * plot_w
        svg_parts.append(
            f'<line x1="{xp:.2f}" y1="0" x2="{xp:.2f}" y2="{plot_h}" stroke="#e2e8f0" stroke-width="0.8" stroke-dasharray="2 2" />'
        )
        svg_parts.append(
            f'<line x1="{xp:.2f}" y1="{plot_h}" x2="{xp:.2f}" y2="{plot_h + 5}" stroke="#64748b" stroke-width="1" />'
        )
        lbl = dt_loc.strftime("%d.%m %H:%M")
        svg_parts.append(
            f'<text x="{xp:.2f}" y="{plot_h + 10}" transform="rotate(-45 {xp:.2f},{plot_h + 10})" '
            f'text-anchor="end" font-size="10.5" fill="#1e293b">{lbl}</text>'
        )

    # Оси координат
    svg_parts.append(
        f'<line x1="0" y1="0" x2="0" y2="{plot_h}" stroke="#475569" stroke-width="1.2" />'
    )
    svg_parts.append(
        f'<line x1="0" y1="{plot_h}" x2="{plot_w}" y2="{plot_h}" stroke="#475569" stroke-width="1.2" />'
    )

    if drawn_band_labels:
        for b_min, b_max in drawn_band_labels:
            y_mid = (y_px(b_min) + y_px(b_max)) / 2.0 + 4.0
            lbl = f"{_format_temp_short(b_min)}..{_format_temp_short(b_max)} °C"
            svg_parts.append(
                f'<text x="{plot_w - 6:.2f}" y="{y_mid:.2f}" text-anchor="end" '
                f'font-size="10.5" font-weight="600" fill="#166534">{html_lib.escape(lbl)}</text>'
            )

    # 4. Разрывы данных > 60 минут ("нет показаний") и кривые датчиков
    gap_threshold_sec = 3600.0
    for idx, sen, pts in parsed_lines:
        color = ADVANTUM_SENSOR_PALETTE[idx % len(ADVANTUM_SENSOR_PALETTE)]
        if not pts:
            continue

        # Выделяем непрерывные сегменты
        segments: list[list[tuple[datetime.datetime, float]]] = [[pts[0]]]
        for j in range(1, len(pts)):
            dt_prev, _ = pts[j - 1]
            dt_curr, _ = pts[j]
            diff = (dt_curr - dt_prev).total_seconds()
            if diff > gap_threshold_sec:
                g_x1 = max(0.0, min(plot_w, x_px(dt_prev)))
                g_x2 = max(0.0, min(plot_w, x_px(dt_curr)))
                gw = g_x2 - g_x1
                if gw > 4:
                    svg_parts.append(
                        f'<g clip-path="url(#clip-plot)">'
                        f'<rect x="{g_x1:.2f}" y="0" width="{gw:.2f}" height="{plot_h}" fill="#cbd5e1" fill-opacity="0.45" />'
                        f'<line x1="{g_x1:.2f}" y1="0" x2="{g_x1:.2f}" y2="{plot_h}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 3" />'
                        f'<line x1="{g_x2:.2f}" y1="0" x2="{g_x2:.2f}" y2="{plot_h}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 3" />'
                        f'</g>'
                    )
                    if gw >= 70:
                        svg_parts.append(
                            f'<text x="{(g_x1 + g_x2) / 2:.2f}" y="{plot_h / 2:.2f}" text-anchor="middle" '
                            f'font-size="11" fill="#475569">нет показаний</text>'
                        )
                segments.append([])
            segments[-1].append(pts[j])

        # Рисуем ломаные по сегментам
        for seg in segments:
            if len(seg) < 2:
                if len(seg) == 1:
                    cx = x_px(seg[0][0])
                    cy = y_px(seg[0][1])
                    svg_parts.append(
                        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="2.2" fill="{color}" clip-path="url(#clip-plot)" />'
                    )
                continue
            d_cmds = []
            for p_idx, (p_dt, p_val) in enumerate(seg):
                px = x_px(p_dt)
                py = y_px(p_val)
                cmd = "M" if p_idx == 0 else "L"
                d_cmds.append(f"{cmd}{px:.2f},{py:.2f}")
            svg_parts.append(
                f'<path d="{" ".join(d_cmds)}" fill="none" stroke="{color}" stroke-width="1.6" '
                f'stroke-linejoin="round" stroke-linecap="round" clip-path="url(#clip-plot)" />'
            )

    svg_parts.append("</g></svg>")
    return "\n".join(svg_parts), temp_min_val, temp_max_val


def build_advantum_print_html(shipment_full: dict[str, Any], chart_data: dict[str, Any]) -> str:
    """
    Формирует чистую HTML+SVG печатную форму температурного графика по данным API Advantum
    (соответствует «Версия для печати -> Простая» в веб-интерфейсе Advantum).
    """
    sensors = chart_data.get("sensors") or []
    tz_obj, offset_label = _parse_tz_offset(chart_data.get("firstLoadingPointOffset"))

    start_dt = _parse_iso_utc(chart_data.get("requestTimeStart"))
    finish_dt = _parse_iso_utc(chart_data.get("requestTimeFinish"))

    all_point_dts: list[datetime.datetime] = []
    sensors_by_sec: dict[int, list[dict[str, Any]]] = {}
    for sen in sensors:
        sec_num = int(sen.get("sectionNumber") or 1)
        sensors_by_sec.setdefault(sec_num, []).append(sen)
        for v in sen.get("values") or []:
            dt = _parse_iso_utc(v.get("dateInstance"))
            if dt is not None:
                all_point_dts.append(dt)

    if not start_dt and all_point_dts:
        start_dt = min(all_point_dts)
    if not finish_dt and all_point_dts:
        finish_dt = max(all_point_dts)
    if not start_dt:
        start_dt = datetime.datetime.now(datetime.timezone.utc)
    if not finish_dt:
        finish_dt = start_dt + datetime.timedelta(hours=1)

    start_local_str = start_dt.astimezone(tz_obj).strftime("%d.%m.%Y %H:%M")
    finish_local_str = finish_dt.astimezone(tz_obj).strftime("%d.%m.%Y %H:%M")
    duration_str = _format_duration_ru(start_dt, finish_dt)

    route_num = shipment_full.get("routeNum") or str(shipment_full.get("id") or "")
    customer_name = shipment_full.get("customerOrganizationName") or ""
    carrier_name = shipment_full.get("carrierOrganizationName") or ""
    tc_num = shipment_full.get("vehicleGosNums") or shipment_full.get("vehicleGosNum") or chart_data.get("gosNum") or ""
    trailer_num = shipment_full.get("trailerGosNums") or ""
    driver_fio = shipment_full.get("driverFio") or ""

    active_sections = [s for s in sorted(sensors_by_sec.keys()) if any(sen.get("values") for sen in sensors_by_sec[s])]
    if not active_sections:
        raise ValueError("Данные температурного графика отсутствуют (нет показаний датчиков)")

    esc = html_lib.escape
    page_blocks: list[str] = []

    for sec_idx, sec_num in enumerate(active_sections):
        sec_sensors = sensors_by_sec[sec_num]
        if sec_num == 1:
            t_min = shipment_full.get("temperatureMin")
            if t_min is None:
                t_min = shipment_full.get("temperatureMinInCelsius")
            t_max = shipment_full.get("temperatureMax")
            if t_max is None:
                t_max = shipment_full.get("temperatureMaxInCelsius")
            restr = chart_data.get("firstSectionRestrictions") or {}
        else:
            t_min = shipment_full.get("secondChamberTemperatureMin")
            if t_min is None:
                t_min = shipment_full.get("secondChamberTemperatureMinInCelsius")
            t_max = shipment_full.get("secondChamberTemperatureMax")
            if t_max is None:
                t_max = shipment_full.get("secondChamberTemperatureMaxInCelsius")
            restr = chart_data.get("secondSectionRestrictions") or {}

        dual_regime = None
        if sec_num == 1:
            dual_regime = _get_wholesale_dual_regime(
                shipment_full=shipment_full,
                sensors_by_sec=sensors_by_sec,
                active_sections=active_sections,
                sec1_t_min=t_min,
                sec1_t_max=t_max,
                sec1_restr=restr,
                chart_data=chart_data,
            )
        extra_regimes = [dual_regime] if dual_regime else None

        svg_markup, min_temp, max_temp = _build_compartment_svg(
            sec_sensors,
            t_min,
            t_max,
            restr,
            start_dt,
            finish_dt,
            tz_obj,
            extra_regimes=extra_regimes,
        )
        if not svg_markup:
            continue

        serials = [str(sen.get("serialNumber") or sen.get("id") or "") for sen in sec_sensors if (sen.get("serialNumber") or sen.get("id"))]
        has_violation = any(bool(sen.get("hasViolation")) for sen in sec_sensors)
        min_temp_str = f"{min_temp:.1f}" if min_temp is not None else ""
        max_temp_str = f"{max_temp:.1f}" if max_temp is not None else ""
        if dual_regime is not None and t_min is not None and t_max is not None:
            pairs = sorted(
                [(float(t_min), float(t_max)), (dual_regime[0], dual_regime[1])],
                key=lambda p: p[0],
            )
            bounds_str = " / ".join(
                f"{_format_temp_short(a)}..{_format_temp_short(b)} °C" for a, b in pairs
            )
        else:
            bounds_str = f"{t_min if t_min is not None else ''} {t_max if t_max is not None else ''}".strip()

        legend_items: list[str] = []
        for s_i, sen in enumerate(sec_sensors):
            col = ADVANTUM_SENSOR_PALETTE[s_i % len(ADVANTUM_SENSOR_PALETTE)]
            ser_lbl = sen.get("serialNumber") or sen.get("name") or f"id {sen.get('id')}"
            legend_items.append(
                f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;">'
                f'<span style="display:inline-block;width:14px;height:3px;background:{col};"></span>'
                f'<span>Датчик {esc(str(ser_lbl))}</span>'
                f'</span>'
            )

        page_break = "always" if sec_idx < len(active_sections) - 1 else "avoid"
        block_html = f"""
        <div style="page-break-after:{page_break};break-inside:avoid;page-break-inside:avoid;margin-bottom:4px;">
          <div style="margin-bottom:10px;font-size:11.5px;line-height:1.32;color:#111827;">
            <div>Юридическое лицо: {esc(str(customer_name))}</div>
            <div>Транспортная компания: {esc(str(carrier_name))}</div>
            <div>Номер рейса: {esc(str(route_num))}</div>
            <div>TC: {esc(str(tc_num))} Прицеп: {esc(str(trailer_num))}</div>
            <div>Водитель: {esc(str(driver_fio))}</div>
            <div>Интервал записи: 1 мин</div>
            <div>Отсек №: {sec_num}</div>
            <div>Терморегистратор №: {esc(', '.join(serials))}</div>
            <div>Первое считывание: {esc(start_local_str)} {esc(offset_label)} Последнее считывание: {esc(finish_local_str)} {esc(offset_label)} Период считывания: {esc(duration_str)}</div>
            <div>Минимальная температура: {esc(min_temp_str)}</div>
            <div>Максимальная температура: {esc(max_temp_str)}</div>
            <div>Температурные границы отсека {sec_num}: {esc(bounds_str)}</div>
            <div>Отклонение: {'были' if has_violation else 'нет'}</div>
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;">
              {''.join(legend_items)}
              <span style="display:inline-flex;align-items:center;gap:4px;margin-left:8px;">
                <span style="display:inline-block;width:14px;height:10px;background:#CCFFCC;border:1px solid #00CC00;"></span>
                <span>Норма</span>
              </span>
              <span style="display:inline-flex;align-items:center;gap:4px;">
                <span style="display:inline-block;width:14px;height:10px;background:#FFFFCC;border:1px solid #DDDD00;"></span>
                <span>Допуск</span>
              </span>
            </div>
          </div>
          <div>{svg_markup}</div>
        </div>
        """
        page_blocks.append(block_html)

    if not page_blocks:
        raise ValueError("Данные температурного графика отсутствуют (нет показаний датчиков)")

    title_str = esc(f"Температурный график {route_num} для печати")
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>{title_str}</title>
  <style>
    @media print {{
      @page {{ size: A4 landscape; margin: 0.7cm 1cm; }}
    }}
    body {{
      margin: 0;
      padding: 4px 8px;
      background: #ffffff;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      color: #0f172a;
    }}
  </style>
</head>
<body>
  {''.join(page_blocks)}
</body>
</html>"""


def fetch_and_render_chart_html(
    shipment_id: int | str,
    token: str,
    shipment_hint: dict[str, Any] | None = None,
    base_url: str = "https://atms.advantum.ru",
    driver: WebDriver | None = None,
    api_client: _AdvantumApiClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Запрашивает карточку рейса и сырые данные температурного графика через API Advantum
    и возвращает (html_content, shipment_full).
    """
    base_url = base_url.rstrip("/")
    client = api_client or _AdvantumApiClient(token=token, base_url=base_url, driver=driver)

    shipment_full = dict(shipment_hint or {})
    ok_ship, _, data_ship, _, err_ship = client.get_json(
        f"/api/v1/shipments/{shipment_id}", timeout=20
    )
    if ok_ship and isinstance(data_ship, dict):
        shipment_full.update(data_ship)
    elif err_ship:
        logger.debug("Ошибка получения карточки /api/v1/shipments/%s: %s", shipment_id, err_ship)

    ok_chart, status_chart, chart_data, _, err_chart = client.get_json(
        f"/api/v1/shipment-temperature-chart/with-different-settings/{shipment_id}",
        params={"useAggregatorSettings": "false"},
        timeout=25,
    )
    if not ok_chart:
        detail = f"HTTP {status_chart}" if status_chart else (err_chart or "неизвестная ошибка")
        raise RuntimeError(f"Ошибка API графика Advantum: {detail}")

    if not isinstance(chart_data, dict):
        raise ValueError("Некорректный ответ API графика Advantum")

    html_content = build_advantum_print_html(shipment_full, chart_data)
    return html_content, shipment_full


def export_single_ml_via_api(
    token: str,
    ml_number: str,
    shipment_info: dict[str, Any],
    config: Config,
    printer: PdfPrinterSession,
    driver: WebDriver | None = None,
    api_client: _AdvantumApiClient | None = None,
) -> ExportResult:
    """
    Прямая выгрузка термографика одного МЛ в PDF по данным REST API Advantum без UI-кликов.
    """
    norm_ml = normalize_ml_number(ml_number)
    shipment_id = shipment_info.get("id")
    if not shipment_id:
        return ExportResult(ml_number, False, "", "Не найден идентификатор рейса")

    html_content, shipment_full = fetch_and_render_chart_html(
        shipment_id=shipment_id,
        token=token,
        shipment_hint=shipment_info,
        driver=driver,
        api_client=api_client,
    )

    route_display = normalize_ml_number(str(shipment_full.get("routeNum") or norm_ml))
    driver_name = str(shipment_full.get("driverFio") or "НеизвестныйВодитель").strip()
    tc_number = str(
        shipment_full.get("vehicleGosNums")
        or shipment_full.get("vehicleGosNum")
        or "НеизвестноеТС"
    ).strip()

    clean_ml = re.sub(r'[<>:"/\\|?*]', "_", route_display).strip()
    clean_driver = re.sub(r'[<>:"/\\|?*]', "_", driver_name).strip()
    clean_tc = re.sub(r'[<>:"/\\|?*]', "_", tc_number).strip()

    target_pdf = os.path.join(
        config.download_dir,
        f"Температурный график {clean_ml} {clean_driver} {clean_tc}.pdf",
    )

    print(f"[Генерация API->PDF] Сохраняем PDF: {os.path.basename(target_pdf)}...")
    printer.render_html_to_pdf(html_content, target_pdf)

    return ExportResult(
        ml_number=ml_number,
        success=True,
        filepath=target_pdf,
        error="",
        driver_name=clean_driver,
        tc_number=clean_tc,
    )


def ensure_table_view(
    driver: WebDriver,
    wait: WebDriverWait,
    config: Config,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
) -> None:
    """
    Гарантирует, что открыта таблица 'Рейсы' -> 'Все' и применен фильтр по датам.
    Вызывается один раз перед началом пакетного экспорта списка МЛ.
    """
    print("[Подготовка] Открываем вкладку 'Рейсы'...")
    try:
        current_url = driver.current_url
        if "control/shipments" not in current_url:
            driver.get(config.advantum_url)
            time.sleep(2)

        # Клик 'Рейсы'
        reisy_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//span[text()='Рейсы'] | //div[contains(text(), 'Рейсы')]")
            )
        )
        driver.execute_script("arguments[0].click();", reisy_btn)
        time.sleep(1.5)

        # Клик 'Все'
        vse_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[text()='Все']")))
        driver.execute_script("arguments[0].click();", vse_btn)
        time.sleep(1.5)

        # Фильтр дат
        if date_filter:
            print("[Подготовка] Проверка и установка фильтра по датам...")
            apply_date_filter(driver, wait, date_filter[0], date_filter[1])
            time.sleep(1.5)

        print("[Подготовка] Таблица рейсов готова к работе.")
    except Exception as e:
        logger.error("Ошибка при подготовке таблицы рейсов: %s", e)
        raise


def export_single_ml(
    driver: WebDriver,
    wait: WebDriverWait,
    ml_number: str,
    config: Config,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
    printer: PdfPrinterSession | None = None,
    shipment_info: dict[str, Any] | None = None,
) -> ExportResult:
    """
    Экспортирует термографик одного маршрутного листа в PDF.
    Быстрый цикл: поиск -> выбор рейса -> график -> печать -> закрытие модалки -> очистка поиска.
    """
    try:
        norm_ml = normalize_ml_number(ml_number)
        digits_ml = "".join(re.findall(r"\d+", norm_ml))

        # Ждем, пока исчезнут любые оверлеи/backdrop перед взаимодействием с поиском
        wait_overlays_gone(driver, timeout=6)

        # 1. Поле поиска рейсов
        search_input = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(@class, 'extended-grid__grid-filter-search')]//input")
            )
        )

        # Очищаем перед новым поиском и ждем сброса таблицы к общему списку
        _clear_search_input(driver)
        try:
            WebDriverWait(driver, 4).until(
                lambda d: (
                    "| 0 /"
                    not in d.find_element(
                        By.XPATH,
                        "//div[contains(@class, 'extended-grid__grid-title')]//span[contains(@class, 'extended-grid__grid-length')]",
                    ).text
                    and "| 1 /"
                    not in d.find_element(
                        By.XPATH,
                        "//div[contains(@class, 'extended-grid__grid-title')]//span[contains(@class, 'extended-grid__grid-length')]",
                    ).text
                )
            )
        except Exception:
            pass

        print(f"[Поиск] Ввод '{norm_ml}'...")
        wait_overlays_gone(driver, timeout=4)
        try:
            search_input.click()
        except Exception:
            driver.execute_script("arguments[0].focus();", search_input)

        driver.execute_script("arguments[0].value = '';", search_input)
        search_input.send_keys(norm_ml)
        search_input.send_keys(Keys.ENTER)

        # Ожидаем применения поиска: либо появление рейса в строке / счетчике '| 1 /', либо '0 /'
        target_row = None
        driver_name = (
            shipment_info.get("driverFio")
            if shipment_info and shipment_info.get("driverFio")
            else "НеизвестныйВодитель"
        )
        tc_number = (
            shipment_info.get("vehicleGosNum")
            if shipment_info and shipment_info.get("vehicleGosNum")
            else "НеизвестноеТС"
        )
        poll_start = time.time()
        fallback_tried = False

        while time.time() - poll_start < 12:
            try:
                # Читаем счетчик длины таблицы
                len_text = ""
                try:
                    len_el = driver.find_element(
                        By.XPATH,
                        "//div[contains(@class, 'extended-grid__grid-title')]//span[contains(@class, 'extended-grid__grid-length')]",
                    )
                    len_text = len_el.text.strip()
                except Exception:
                    pass

                rows = driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class, 'extended-grid__grid')]//div[contains(@class, 'public_fixedDataTable_main')][1]//div[contains(@class, 'public_fixedDataTableRow_main') and not(contains(@class, 'public_fixedDataTable_header'))]",
                )

                # Если поиск завершился и показал 0 строк
                if ("| 0 /" in len_text or len_text == "| 0") and (time.time() - poll_start >= 1.5):
                    # Попробуем запасной поиск по чистым цифрам (если не совпадает с norm_ml и еще не пробовали)
                    if not fallback_tried and digits_ml and digits_ml != norm_ml:
                        fallback_tried = True
                        print(f"[Поиск] Не найдено по '{norm_ml}', пробуем поиск по цифрам '{digits_ml}'...")
                        _clear_search_input(driver)
                        time.sleep(0.3)
                        search_input.send_keys(digits_ml)
                        search_input.send_keys(Keys.ENTER)
                        poll_start = time.time()
                        continue
                    else:
                        print(f"[Поиск] Рейс {norm_ml} не найден за выбранный период.")
                        _clear_search_input(driver)
                        return ExportResult(
                            ml_number, False, "", "Не найден в таблице за выбранный период"
                        )

                # Если найдена ровно 1 строка или счетчик подтверждает ровно 1 рейс
                if rows and ("| 1 /" in len_text or len(rows) == 1):
                    # Даем минимум 1 секунду на debounce / обновление или сразу берем если | 1 /
                    if time.time() - poll_start >= 1.0 or "| 1 /" in len_text:
                        target_row = rows[0]
                        row_texts = driver.execute_script(
                            """
                            return Array.from(arguments[0].querySelectorAll('.public_fixedDataTableCell_cellContent')).map(el => (el.innerText || '').trim());
                            """,
                            rows[0],
                        )
                        if len(row_texts) > 48 and row_texts[48]:
                            driver_name = row_texts[48]
                        if len(row_texts) > 18 and row_texts[18]:
                            tc_number = row_texts[18]
                        print(f"[Найдено] Рейс найден в таблице! (ФИО: {driver_name}, ТС: {tc_number})")
                        break

                # Если строк несколько, проверяем точное совпадение по тексту ячеек
                if rows and len(rows) > 1:
                    matched = False
                    for r in rows:
                        r_texts = driver.execute_script(
                            """
                            return Array.from(arguments[0].querySelectorAll('.public_fixedDataTableCell_cellContent')).map(el => (el.innerText || '').trim());
                            """,
                            r,
                        )
                        if norm_ml in r_texts or (digits_ml and digits_ml in r_texts):
                            target_row = r
                            if len(r_texts) > 48 and r_texts[48]:
                                driver_name = r_texts[48]
                            if len(r_texts) > 18 and r_texts[18]:
                                tc_number = r_texts[18]
                            matched = True
                            print(f"[Найдено] Точное совпадение среди нескольких строк! (ФИО: {driver_name}, ТС: {tc_number})")
                            break
                    if matched:
                        break
            except Exception:
                pass
            time.sleep(0.3)

        if not target_row:
            print(f"[Поиск] Рейс {norm_ml} не найден в таблице за выбранный период.")
            _clear_search_input(driver)
            return ExportResult(
                ml_number, False, "", "Не найден в таблице за выбранный период"
            )

        print("[Шаг 5] Кликаем по строке рейса...")
        wait_overlays_gone(driver, timeout=5)
        driver.execute_script("arguments[0].click();", target_row)
        time.sleep(1)

        print("[Шаг 5.1] Нажимаем иконку температурного графика...")
        wait_overlays_gone(driver, timeout=5)
        icon = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "span[aria-label='Температурный график']")
            )
        )
        driver.execute_script("arguments[0].click();", icon)
        # Ждём загрузки данных графика в модальном окне
        time.sleep(3)

        # Пробуем спарсить ФИО водителя из модального окна, если еще не определено
        try:
            modal_driver = parse_driver_name(driver)
            if driver_name in ("", "НеизвестныйВодитель", "Водитель") and modal_driver != "Водитель":
                driver_name = modal_driver
        except Exception:
            pass

        # Перехват window.open и отслеживание target ID до клика по меню печати
        targets_before = driver.execute_cdp_cmd("Target.getTargets", {})
        target_ids_before = {
            t["targetId"] for t in targets_before["targetInfos"] if t["type"] == "page"
        }

        driver.execute_script("""
            if (!window.__origOpen) {
                window.__origOpen = window.open;
            }
            window.__popupWin = null;
            window.open = function(url, name, feats) {
                console.log("window.open called!");
                const w = window.__origOpen(url, name, feats);
                window.__popupWin = w;
                return w;
            };
        """)

        print("[Шаг 6] Нажимаем кнопку 'Версия для печати'...")
        time.sleep(0.5)
        print_btn = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(@class, 'MuiButton-root') and contains(., 'Версия для печати')] | //button[contains(text(), 'Версия для печати')] | //*[contains(text(), 'Версия для печати')]",
                )
            )
        )
        driver.execute_script("arguments[0].click();", print_btn)
        time.sleep(0.8)

        print("[Шаг 6.1] Нажимаем 'Простая'...")
        time.sleep(0.3)
        simple_btn = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[normalize-space(text())='Простая'] | //li[normalize-space(text())='Простая']",
                )
            )
        )
        driver.execute_script("arguments[0].click();", simple_btn)

        # Динамический опрос готовности HTML печатной формы (до 25 сек)
        html = None
        poll_start = time.time()
        while time.time() - poll_start < 25:
            html = driver.execute_script("""
                if (window.__popupWin && window.__popupWin.document && window.__popupWin.document.documentElement) {
                    const h = window.__popupWin.document.documentElement.outerHTML;
                    if (h && h.length > 500) {
                        return h;
                    }
                }
                return null;
            """)
            if html:
                break
            time.sleep(0.5)

        # Fallback: если через window.__popupWin не удалось, но открылась новая вкладка/окно
        if not html and len(driver.window_handles) > 1:
            try:
                main_handle = driver.current_window_handle
                for handle in driver.window_handles:
                    if handle != main_handle:
                        driver.switch_to.window(handle)
                        h_poll = time.time()
                        while time.time() - h_poll < 10:
                            src = driver.page_source
                            if src and len(src) > 500:
                                html = src
                                break
                            time.sleep(0.5)
                        driver.close()
                driver.switch_to.window(main_handle)
            except Exception:
                pass

        # Закрываем popup через CDP
        targets_after = driver.execute_cdp_cmd("Target.getTargets", {})
        target_ids_after = {
            t["targetId"] for t in targets_after["targetInfos"] if t["type"] == "page"
        }
        new_targets = target_ids_after - target_ids_before
        for tid in new_targets:
            try:
                driver.execute_cdp_cmd("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass

        # Быстро закрываем модальное окно графика клавишей Escape
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        wait_overlays_gone(driver, timeout=4)

        if not html:
            _clear_search_input(driver)
            return ExportResult(
                ml_number, False, "", "Не удалось перехватить HTML печатной формы"
            )

        # Дополнительно извлекаем данные ТС и водителя из HTML печатной формы, если отсутствуют
        if tc_number in ("", "НеизвестноеТС", "ТС"):
            try:
                parsed_tc = parse_ts_from_print_page(html)
                if parsed_tc and parsed_tc != "ТС":
                    tc_number = parsed_tc
            except Exception:
                pass

        if driver_name in ("", "НеизвестныйВодитель", "Водитель"):
            try:
                m_drv = re.search(r'водитель[:\s]+([А-ЯЁа-яё\s\.\-]+?)(?:<|,|\n|;|\d|\bТС)', html, re.IGNORECASE)
                if m_drv:
                    driver_name = " ".join(m_drv.group(1).strip().split()[:3])
            except Exception:
                pass

        # Санитизация имени файла (запрещенные символы в Windows/macOS)
        clean_ml = re.sub(r'[<>:"/\\|?*]', "_", norm_ml).strip()
        clean_driver = re.sub(r'[<>:"/\\|?*]', "_", driver_name).strip()
        clean_tc = re.sub(r'[<>:"/\\|?*]', "_", tc_number).strip()

        target_pdf = os.path.join(
            config.download_dir,
            f"Температурный график {clean_ml} {clean_driver} {clean_tc}.pdf",
        )
        temp_html_path = os.path.join(
            config.download_dir, f"temp_thermo_{clean_ml}.html"
        )

        # Удаляем <script>, <iframe>, <object>, <embed> перед рендерингом через file:// для защиты от XSS и LFI
        clean_html = re.sub(
            r"<(script|iframe|object|embed)\b[^>]*>(?:.*?</\1>)?",
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(clean_html)

        print(f"[Генерация] Сохраняем PDF: {os.path.basename(target_pdf)}...")
        if printer:
            printer.print_html_to_pdf(temp_html_path, target_pdf)
        else:
            print_html_to_pdf_headless(temp_html_path, target_pdf, config)

        if os.path.exists(temp_html_path):
            try:
                os.remove(temp_html_path)
            except Exception:
                pass

        # Быстро очищаем строку поиска для следующего МЛ
        _clear_search_input(driver)

        return ExportResult(
            ml_number,
            True,
            target_pdf,
            "",
            driver_name=clean_driver,
            tc_number=clean_tc,
        )

    except Exception as e:
        import traceback

        logger.error("Ошибка при экспорте МЛ %s: %s", ml_number, e)
        traceback.print_exc()

        # Попытка быстро закрыть модальное окно и очистить поиск
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        wait_overlays_gone(driver, timeout=4)
        _clear_search_input(driver)

        err_str = str(e)
        if "Не найден" in err_str:
            user_msg = "Не найден в списке рейсов"
        elif "Timeout" in err_str or "Таймаут" in err_str:
            user_msg = "Превышено время ожидания ответа страницы Advantum"
        else:
            user_msg = "Техническая ошибка при выгрузке графика"

        return ExportResult(ml_number, False, "", user_msg)


def recover_after_error(
    driver: WebDriver,
    wait: WebDriverWait,
    config: Config,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
) -> None:
    """Восстанавливает страницу Advantum после непредвиденной ошибки."""
    print("[Восстановление] Перезагрузка страницы Advantum...")
    try:
        driver.get(config.advantum_url)
        time.sleep(3)
        wait_overlays_gone(driver, timeout=6)
        ensure_table_view(driver, wait, config, date_filter=date_filter)
    except Exception as e:
        print(f"[Восстановление] Не удалось восстановить страницу: {e}")


def export_thermographs(
    driver: WebDriver | None,
    wait: WebDriverWait | None,
    ml_numbers: list[str],
    config: Config,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
    on_progress: Any | None = None,
) -> list[ExportResult]:
    """
    Массовый экспорт термографиков по списку номеров маршрутных листов.
    Использует прямой каскадный поиск через REST API Advantum (без ввода дат
    и работы с UI-таблицей), прямой сбор данных датчиков и единую сессию PDF-печати.

    Если driver=None (фоновые воркеры портала), использует общий потокобезопасный
    AdvantumTokenManager и лениво запускает headless-браузер только при реальной
    необходимости (fallback).

    on_progress: опциональный callback(idx, total, ml_number, result_or_none)
    """
    results: list[ExportResult] = []
    token = ""
    api_shipments: dict[str, dict[str, Any]] = {}
    api_client: _AdvantumApiClient | None = None
    ui_ready = False
    owned_ui_driver: WebDriver | None = None
    active_driver: WebDriver | None = driver
    active_wait: WebDriverWait | None = wait

    # 1. Получаем Bearer-токен: либо из переданного браузера, либо из общего AdvantumTokenManager
    if driver is not None:
        try:
            token = get_advantum_bearer_token(driver)
            ADVANTUM_TOKEN_MANAGER.set_token(token)
            try:
                driver.get("https://atms.advantum.ru/pre-load.js")
            except Exception:
                pass
        except Exception as e:
            logger.warning("[API] Не удалось извлечь токен из браузера: %s. Переход в UI-режим.", e)
    else:
        token = ADVANTUM_TOKEN_MANAGER.get_token(config)

    try:
        # 2. Каскадный поиск всех запрошенных МЛ через REST API Advantum (кэш + Smart Jump + параллельные чанки)
        if token:
            api_client = _AdvantumApiClient(
                token=token,
                driver=driver,
                config=config,
                token_manager=ADVANTUM_TOKEN_MANAGER,
            )
            print("[API] Поиск маршрутных листов через нативный REST API Advantum...")
            try:
                api_shipments = find_shipments_by_ml_api(
                    token=token,
                    ml_numbers=ml_numbers,
                    date_filter=date_filter,
                    driver=driver,
                    api_client=api_client,
                )
                print(f"[API] Поиск завершен: найдено {len(api_shipments)}/{len(ml_numbers)} МЛ.")
            except Exception as e:
                logger.warning("[API] Ошибка каскадного поиска МЛ: %s", e)

        def _ensure_ui_session() -> tuple[WebDriver, WebDriverWait]:
            nonlocal active_driver, active_wait, owned_ui_driver, ui_ready
            if active_driver is None:
                from browser import create_driver, login_to_advantum

                owned_ui_driver = create_driver(config, headless=True)
                active_driver = owned_ui_driver
                active_wait = login_to_advantum(active_driver, config)
            elif active_wait is None:
                active_wait = WebDriverWait(active_driver, getattr(config, "default_timeout", 20))
            if not ui_ready:
                ensure_table_view(active_driver, active_wait, config, date_filter=date_filter)
                ui_ready = True
            return active_driver, active_wait

        # 3. Пакетная обработка и генерация PDF в рамках единой сессии PdfPrinterSession (ленивый запуск)
        with PdfPrinterSession(config, lazy=True) as printer:
            for idx, ml in enumerate(ml_numbers, 1):
                print(f"\n{'='*50}\nМЛ ({idx}/{len(ml_numbers)}): {ml}\n{'='*50}")
                if on_progress:
                    try:
                        on_progress(idx, len(ml_numbers), ml, None)
                    except Exception:
                        pass

                norm_ml = normalize_ml_number(ml)
                shipment_info = api_shipments.get(norm_ml)
                result: ExportResult | None = None

                # Защита от устаревшего кэша по флагу телематики: уточняем карточку вживую
                if (
                    shipment_info
                    and shipment_info.get("hasTelematicEquipment") is False
                    and shipment_info.get("_from_cache")
                    and api_client is not None
                ):
                    sid = shipment_info.get("id")
                    if sid:
                        ok_live, _, live_item, _, _ = api_client.get_json(
                            f"/api/v1/shipments/{sid}", timeout=15
                        )
                        if ok_live and isinstance(live_item, dict) and live_item.get("id"):
                            merged = dict(shipment_info)
                            merged.update(live_item)
                            merged.pop("_from_cache", None)
                            shipment_info = merged
                            _SHIPMENTS_CACHE.upsert_shipments([merged])

                if token:
                    if not shipment_info:
                        print(f"[API] Рейс {ml} не найден в системе Advantum.")
                        result = ExportResult(
                            ml_number=ml,
                            success=False,
                            filepath="",
                            error="Рейс не найден в системе Advantum",
                        )
                    elif shipment_info.get("hasTelematicEquipment") is False:
                        print(
                            f"[API] У автомобиля {shipment_info.get('vehicleGosNum', '')} нет телематики (датчиков). Пропуск."
                        )
                        result = ExportResult(
                            ml_number=ml,
                            success=False,
                            filepath="",
                            error="Телематическое оборудование (датчики) отсутствует",
                            driver_name=shipment_info.get("driverFio", ""),
                            tc_number=shipment_info.get("vehicleGosNum", ""),
                        )
                    else:
                        try:
                            cur_token = api_client._current_token() if api_client else token
                            result = export_single_ml_via_api(
                                token=cur_token,
                                ml_number=ml,
                                shipment_info=shipment_info,
                                config=config,
                                printer=printer,
                                driver=active_driver,
                                api_client=api_client,
                            )
                        except ValueError as ve:
                            # Нет показаний датчиков или пустой график — UI здесь также пуст
                            print(f"[API] {ml}: {ve}")
                            result = ExportResult(
                                ml_number=ml,
                                success=False,
                                filepath="",
                                error=str(ve),
                                driver_name=shipment_info.get("driverFio", "") if shipment_info else "",
                                tc_number=shipment_info.get("vehicleGosNum", "") if shipment_info else "",
                            )
                        except Exception as api_err:
                            logger.warning(
                                "[API] Прямая генерация графика для %s не удалась (%s). Пробуем UI fallback...",
                                ml,
                                api_err,
                            )

                # Fallback через UI-таблицу только если прямой API путь не дал результата из-за сбоя или отсутствия токена
                if result is None:
                    ui_drv, ui_w = _ensure_ui_session()
                    result = export_single_ml(
                        ui_drv,
                        ui_w,
                        ml,
                        config,
                        date_filter=date_filter,
                        printer=printer,
                        shipment_info=shipment_info,
                    )
                    if not result.success and "Не найден" not in result.error:
                        recover_after_error(ui_drv, ui_w, config, date_filter=date_filter)

                results.append(result)

                if on_progress:
                    try:
                        on_progress(idx, len(ml_numbers), ml, result)
                    except Exception:
                        pass
    finally:
        if api_client is not None:
            api_client.close()
        if owned_ui_driver is not None:
            try:
                owned_ui_driver.quit()
            except Exception:
                pass

    # 3. Итоговый отчёт
    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count

    print(f"\n{'='*50}")
    print(f"ИТОГО: {success_count}/{len(results)} успешно, {fail_count} не выгружено")

    if fail_count > 0:
        print("Подробности:")
        for r in results:
            if not r.success:
                print(f"  - {r.ml_number}: {r.error}")

    print(f"Файлы сохранены в: {config.download_dir}")
    print("=" * 50)

    return results
