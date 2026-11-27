"""
config.py — Загрузка конфигурации из .env файла.

Все чувствительные данные (логин, пароль) и пути
хранятся в .env, не в исходном коде.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    # Если python-dotenv не установлен — работаем без него
    def load_dotenv(*args, **kwargs):
        pass


@dataclass
class Config:
    """Конфигурация приложения."""

    login: str = ""
    password: str = ""
    advantum_url: str = "https://atms.advantum.ru/#/control/requests"
    browser_path: str = ""
    browser_path_fallback: str = ""
    download_dir: str = ""
    chromedriver_version: str | None = None
    headless: bool = False

    # Таймауты (секунды)
    default_timeout: int = 20
    overlay_timeout: int = 15
    page_load_pause: float = 3.0
    click_retry_count: int = 3

    # Селекторы overlay/loader (MUI-специфичные)
    overlay_selectors: list = field(default_factory=lambda: [
        ".MuiBackdrop-root",
        ".MuiDialog-container .MuiBackdrop-root",
        "[class*='overlay']",
        "[class*='spinner']",
        "[class*='loader']",
        ".MuiCircularProgress-root",
    ])


def load_config(env_path: str | None = None) -> Config:
    """
    Загружает конфигурацию из .env файла.

    Args:
        env_path: Путь к .env файлу. Если None — ищет в директории скрипта.

    Returns:
        Config с заполненными полями.
    """
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    load_dotenv(env_path)

    download_dir = os.environ.get("DOWNLOAD_DIR", "~/Downloads")
    download_dir = os.path.expanduser(download_dir)

    browser_path = os.environ.get("BROWSER_PATH", "")
    browser_path = os.path.expandvars(browser_path)

    browser_path_fallback = os.environ.get(
        "BROWSER_PATH_FALLBACK",
        r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe",
    )

    headless_val = os.environ.get("HEADLESS", "").strip().lower() in ("true", "1", "yes")

    return Config(
        login=os.environ.get("ADVANTUM_LOGIN", ""),
        password=os.environ.get("ADVANTUM_PASSWORD", ""),
        advantum_url=os.environ.get("ADVANTUM_URL", "https://atms.advantum.ru/#/control/requests"),
        browser_path=browser_path,
        browser_path_fallback=browser_path_fallback,
        download_dir=download_dir,
        chromedriver_version=os.environ.get("CHROMEDRIVER_VERSION"),
        headless=headless_val,
    )
