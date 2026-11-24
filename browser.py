"""
browser.py — Инициализация WebDriver и CDP-экспорт в PDF.

Содержит:
- create_driver() — настройка Яндекс.Браузера с kiosk-printing
- save_page_as_pdf() — сохранение страницы в PDF через CDP (без Ctrl+P)
- login_to_advantum() — авторизация (задержки из v22)
"""

import base64
import json
import logging
import os
import sys
import time

import requests

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from cdp_driver import DirectCDPDriver
from config import Config

# ─── Настройки окружения ────────────────────────────────────────────────────────
os.environ['WDM_LOCAL'] = '1'

logger = logging.getLogger(__name__)


def _resolve_browser_path(config: Config) -> str:
    """Определяет путь к исполняемому файлу Яндекс.Браузера."""
    # 1. Из конфига (если указан явно в .env)
    if config.browser_path and os.path.exists(config.browser_path):
        return config.browser_path

    # 2. Для Windows (Яндекс.Браузер)
    yandex_path = os.path.expandvars(
        r"%LocalAppData%\Yandex\YandexBrowser\Application\browser.exe"
    )
    if os.path.exists(yandex_path):
        return yandex_path

    fallback = r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe"
    if os.path.exists(fallback):
        return fallback

    if config.browser_path_fallback and os.path.exists(config.browser_path_fallback):
        return config.browser_path_fallback

    # 3. Для тестов на Mac
    if sys.platform == "darwin":
        mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(mac_chrome):
            return mac_chrome
        mac_yandex = "/Applications/Yandex.app/Contents/MacOS/Yandex"
        if os.path.exists(mac_yandex):
            return mac_yandex

    raise FileNotFoundError(
        "Браузер не найден. Укажите путь в .env (BROWSER_PATH)."
    )


def _get_local_chromedriver(config: Config) -> str | None:
    """Ищет webdriver в .wdm/ (как это делает webdriver_manager)."""
    wdm_dir = os.path.join(os.getcwd(), ".wdm", "drivers", "chromedriver")
    if not os.path.exists(wdm_dir):
        return None

    # Ищем правильный бинарник в зависимости от ОС
    target_exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    
    for root, _, files in os.walk(wdm_dir):
        for f in files:
            if f == target_exe:
                return os.path.join(root, f)
    return None


def _resolve_chromedriver(config: Config) -> str:
    """Определяет путь к ChromeDriver: локальный или скачивает (как в v22)."""
    # 1. Сначала ищем в текущей папке со скриптом
    local_exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    current_dir_driver = os.path.join(os.getcwd(), local_exe)
    if os.path.exists(current_dir_driver):
        print(f"[Запуск] ChromeDriver найден в текущей папке: {current_dir_driver}")
        return current_dir_driver

    # 2. Скачиваем / получаем актуальный через ChromeDriverManager
    print("[Запуск] Проверка / загрузка ChromeDriver... (ожидание до 15 сек)")
    
    import socket
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(15)
    
    try:
        path = ChromeDriverManager(
            driver_version=config.chromedriver_version
        ).install()
        return path
    except Exception as e:
        # 3. Fallback: если сети нет, проверяем локальный кэш .wdm
        local_path = _get_local_chromedriver(config)
        if local_path and os.path.exists(local_path):
            print(f"[Запуск] ChromeDriver найден в кэше: {local_path}")
            return local_path

        raise RuntimeError(
            f"Не удалось скачать ChromeDriver (возможно блокирует сеть).\n"
            f"ОШИБКА: {e}\n\n"
            f"РЕШЕНИЕ: Скачайте ChromeDriver версии {config.chromedriver_version or 'под ваш браузер'} вручную "
            f"и положите файл '{local_exe}' рядом с файлом main.py!"
        ) from e
    finally:
        socket.setdefaulttimeout(old_timeout)


def create_driver(config: Config, headless: bool | None = None) -> DirectCDPDriver:
    """
    Создаёт и настраивает экземпляр DirectCDPDriver для Яндекс.Браузера / Chrome.

    Работает напрямую через встроенный протокол отладки браузера (CDP WebSocket)
    БЕЗ использования chromedriver.exe, что исключает блокировку групповыми
    политиками Windows (WinError 1260).
    """
    browser_path = _resolve_browser_path(config)
    print(f"[Путь] Браузер: {browser_path}")
    print(f"[Путь] Найден:  {os.path.exists(browser_path)}")

    is_headless = headless if headless is not None else config.headless
    if is_headless:
        print("[Запуск] Режим: ФОНОВЫЙ (Headless, без всплывающего окна)")

    print_settings = {
        "recentDestinations": [{
            "id": "Save as PDF",
            "origin": "local",
            "account": "",
        }],
        "selectedDestinationId": "Save as PDF",
        "version": 2,
        "isLandscapeEnabled": False,
        "isCssBackgroundEnabled": True,
        "scalingType": 3,
        "scaling": "100",
        "isHeaderFooterEnabled": False,
        "mediaSize": {
            "name": "ISO_A4",
            "width_microns": 210000,
            "height_microns": 297000,
        },
    }

    prefs = {
        "download.default_directory": config.download_dir,
        "download.prompt_for_download": False,
        "savefile.default_directory": config.download_dir,
        "printing.print_preview_sticky_settings.appState": json.dumps(print_settings),
        "printing.default_destination_selection_rules": json.dumps({
            "kind": "local",
            "namePattern": "Save as PDF",
        }),
        "plugins.always_open_pdf_externally": True,
    }

    print("[Запуск] Прямое подключение к браузеру через CDP (без chromedriver.exe)...")
    driver = DirectCDPDriver(
        browser_path=browser_path,
        headless=is_headless,
        prefs=prefs,
    )
    logger.info("DirectCDPDriver запущен.")
    return driver


def save_page_as_pdf(
    driver: webdriver.Chrome,
    filepath: str,
    landscape: bool = False,
    print_background: bool = True,
) -> str:
    """
    Сохраняет текущую страницу в PDF через Chrome DevTools Protocol.

    Полностью заменяет цепочку:
    PyAutoGUI Ctrl+P → Enter → Ctrl+A → Ctrl+V имя файла → Enter

    Работает в Яндекс.Браузере (Chromium-based) без headless-режима.

    Args:
        driver: WebDriver-инстанс.
        filepath: Полный путь для сохранения PDF (включая .pdf).
        landscape: Альбомная ориентация.
        print_background: Печатать фоновые цвета/изображения.

    Returns:
        Полный путь к сохранённому файлу.

    Raises:
        WebDriverException: Если CDP-команда не поддерживается.
    """
    # Убеждаемся, что директория существует
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

    try:
        pdf_data = driver.execute_cdp_cmd("Page.printToPDF", {
            "landscape": landscape,
            "printBackground": print_background,
            "preferCSSPageSize": True,
            "scale": 1.0,
            "paperWidth": 8.27,     # A4 в дюймах
            "paperHeight": 11.69,   # A4 в дюймах
            "marginTop": 0.4,
            "marginBottom": 0.4,
            "marginLeft": 0.4,
            "marginRight": 0.4,
            "displayHeaderFooter": False,
        })

        pdf_bytes = base64.b64decode(pdf_data["data"])
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)

        file_size_kb = len(pdf_bytes) / 1024
        print(f"[CDP] PDF сохранён: {filepath} ({file_size_kb:.1f} KB)")
        return filepath

    except WebDriverException as e:
        error_msg = str(e)
        if "printToPDF" in error_msg or "CDP" in error_msg:
            print(
                f"[CDP] Page.printToPDF не поддерживается. "
                f"Убедитесь, что Яндекс.Браузер обновлён. Ошибка: {e}"
            )
        raise


def login_to_advantum(
    driver: webdriver.Chrome,
    config: Config,
) -> WebDriverWait:
    """
    Авторизуется в системе Advantum.
    Задержки полностью из v22 — проверены и работают.

    Args:
        driver: WebDriver-инстанс.
        config: Конфигурация с логином и паролем.

    Returns:
        WebDriverWait для дальнейшего использования.
    """
    wait = WebDriverWait(driver, 20)

    print("[Запуск] Открываем Advantum...")
    driver.get(config.advantum_url)
    time.sleep(5)  # ← из v22: ждём загрузки SPA

    inputs = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "input")))
    inputs[0].send_keys(config.login)
    inputs[1].send_keys(config.password)
    driver.find_element(By.XPATH, "//button[@type='submit']").click()
    time.sleep(3)  # ← из v22: ждём авторизации

    print("[Успех] Авторизация пройдена.")
    return wait
