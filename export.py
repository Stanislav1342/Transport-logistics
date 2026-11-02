"""
export.py — Логика экспорта термографиков из Advantum.

Основной модуль, содержащий:
- export_thermographs() — массовый экспорт по списку МЛ
- export_single_ml() — экспорт одного маршрутного листа

Все задержки time.sleep() взяты из v22 — проверены на продакшене.
"""

import base64
import datetime
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from typing import NamedTuple, Dict, Any

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

    def __init__(self, config: Config):
        self.config = config
        self.driver: DirectCDPDriver | None = None

    def __enter__(self):
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
        return self

    def print_html_to_pdf(self, html_path: str, output_pdf: str) -> bool:
        """Печатает локальный HTML-файл в PDF через открытую сессию."""
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
        driver_name = "НеизвестныйВодитель"
        tc_number = "НеизвестноеТС"
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
        wait_overlays_gone(driver, timeout=5)
        print_btn = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(@class, 'MuiButton-root') and contains(., 'Версия для печати')] | //button[contains(text(), 'Версия для печати')] | //*[contains(text(), 'Версия для печати')]",
                )
            )
        )
        driver.execute_script("arguments[0].click();", print_btn)
        time.sleep(1)

        print("[Шаг 6.1] Нажимаем 'Простая'...")
        wait_overlays_gone(driver, timeout=5)
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
    driver: WebDriver,
    wait: WebDriverWait,
    ml_numbers: list[str],
    config: Config,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
    on_progress: Any | None = None,
) -> list[ExportResult]:
    """
    Массовый экспорт термографиков по списку номеров маршрутных листов.
    Использует единую сессию headless PDF-печати и быстрый сброс между МЛ.
    
    on_progress: опциональный callback(idx, total, ml_number, result_or_none)
    """
    results: list[ExportResult] = []

    # 1. Единоразовая подготовка таблицы 'Рейсы' и установка фильтра дат
    ensure_table_view(driver, wait, config, date_filter=date_filter)

    # 2. Пакетная обработка в рамках единой сессии PdfPrinterSession
    with PdfPrinterSession(config) as printer:
        for idx, ml in enumerate(ml_numbers, 1):
            print(f"\n{'='*50}\nМЛ ({idx}/{len(ml_numbers)}): {ml}\n{'='*50}")
            if on_progress:
                try:
                    on_progress(idx, len(ml_numbers), ml, None)
                except Exception:
                    pass

            result = export_single_ml(
                driver, wait, ml, config, date_filter=date_filter, printer=printer
            )
            results.append(result)

            if on_progress:
                try:
                    on_progress(idx, len(ml_numbers), ml, result)
                except Exception:
                    pass

            # Если произошел непредвиденный сбой (не связанный с отсутствием рейса)
            if not result.success and "Не найден" not in result.error:
                recover_after_error(driver, wait, config, date_filter=date_filter)

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
