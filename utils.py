"""
utils.py — Утилиты отказоустойчивой автоматизации.

Содержит:
- stable_click() — клик с обходом overlay, iframe, stale-элементов
- wait_overlays_gone() — ожидание исчезновения MUI-оверлеев
- find_element_deep() — поиск элемента с обходом iframe
- smart_wait() — замена time.sleep() на условное ожидание
- format_car_number() / parse_ts_from_print_page() — парсинг данных
"""

import datetime
import logging
import re
import time

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

# ─── Overlay / Loader ──────────────────────────────────────────────────────────

# Типичные CSS-селекторы overlay/loader в MUI и других фреймворках
DEFAULT_OVERLAY_SELECTORS = [
    ".MuiBackdrop-root[style*='opacity: 1']",
    ".MuiCircularProgress-root",
    "[class*='loading-overlay']",
    "[class*='spinner']",
    "[class*='loader']:not([class*='MuiButton'])",
]


def wait_overlays_gone(
    driver: WebDriver,
    selectors: list[str] | None = None,
    timeout: int = 15,
) -> None:
    """
    Ожидает исчезновения всех известных overlay/loader элементов.

    Типичная проблема MUI: при открытии модального окна рендерится
    MuiBackdrop-root, который перехватывает клики даже после
    визуального исчезновения (opacity transition).

    Args:
        driver: WebDriver-инстанс.
        selectors: CSS-селекторы overlay-элементов.
        timeout: Максимальное время ожидания (секунды).
    """
    if selectors is None:
        selectors = DEFAULT_OVERLAY_SELECTORS

    wait = WebDriverWait(driver, timeout, poll_frequency=0.3)

    for selector in selectors:
        try:
            wait.until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, selector))
            )
            logger.debug("Overlay исчез: %s", selector)
        except TimeoutException:
            # Overlay не найден или всё ещё виден — проверяем, блокирует ли
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed():
                    logger.warning(
                        "Overlay всё ещё виден после %d сек: %s", timeout, selector
                    )
                    # Принудительно удаляем через JS
                    driver.execute_script(
                        "arguments[0].remove();", el
                    )
                    logger.info("Overlay скрыт принудительно: %s", selector)
            except (NoSuchElementException, StaleElementReferenceException):
                pass  # Overlay отсутствует — OK


# ─── Поиск элементов (включая iframe) ─────────────────────────────────────────


def _find_in_iframes(
    driver: WebDriver,
    by: str,
    selector: str,
    depth: int = 0,
    max_depth: int = 3,
) -> WebElement | None:
    """
    Рекурсивно обходит все iframe в поисках элемента.

    Args:
        driver: WebDriver-инстанс.
        by: Тип локатора (By.XPATH, By.CSS_SELECTOR, ...).
        selector: Строка селектора.
        depth: Текущая глубина рекурсии.
        max_depth: Максимальная глубина вложенности iframe.

    Returns:
        WebElement если найден, иначе None.
    """
    if depth >= max_depth:
        return None

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for iframe in iframes:
        try:
            driver.switch_to.frame(iframe)
        except WebDriverException:
            continue

        try:
            el = driver.find_element(by, selector)
            if el.is_displayed():
                logger.info(
                    "Элемент найден в iframe (глубина %d): %s", depth + 1, selector
                )
                return el
        except NoSuchElementException:
            pass

        # Рекурсия в вложенные iframe
        result = _find_in_iframes(driver, by, selector, depth + 1, max_depth)
        if result:
            return result

        driver.switch_to.parent_frame()

    return None


def find_element_deep(
    driver: WebDriver,
    by: str,
    selector: str,
    check_iframes: bool = True,
) -> WebElement | None:
    """
    Ищет элемент в основном DOM, затем (если не найден) во всех iframe.

    Args:
        driver: WebDriver-инстанс.
        by: Тип локатора.
        selector: Строка селектора.
        check_iframes: Если True — проверяет iframe при ненахождении.

    Returns:
        WebElement если найден, иначе None.
    """
    # Сначала — основной DOM
    try:
        el = driver.find_element(by, selector)
        if el.is_displayed():
            return el
    except NoSuchElementException:
        pass

    # Если не найден и разрешён обход iframe
    if check_iframes:
        driver.switch_to.default_content()
        result = _find_in_iframes(driver, by, selector)
        if result:
            return result
        # Возвращаемся в основной контекст, если ничего не нашли
        driver.switch_to.default_content()

    return None


# ─── Stable Click ──────────────────────────────────────────────────────────────


def stable_click(
    driver: WebDriver,
    selector: str,
    by: str = By.XPATH,
    timeout: int = 20,
    retries: int = 3,
    overlay_selectors: list[str] | None = None,
    check_iframes: bool = True,
    description: str = "",
) -> WebElement:
    """
    Отказоустойчивый клик по элементу с обходом типичных проблем SPA.

    Стратегия:
    1. Ждёт исчезновения overlay/loader (MUI Backdrop, спиннеры)
    2. Ищет элемент в основном DOM
    3. Если не найден — рекурсивно обходит все iframe
    4. scrollIntoView + нативный клик
    5. Если ElementClickInterceptedException — JS-клик
    6. Если StaleElementReferenceException — повторяет поиск (до retries раз)

    Args:
        driver: WebDriver-инстанс.
        selector: XPath или CSS-селектор элемента.
        by: Тип локатора (By.XPATH по умолчанию).
        timeout: Время ожидания появления элемента (секунды).
        retries: Количество повторных попыток при stale/перехвате.
        overlay_selectors: CSS-селекторы overlay для ожидания их исчезновения.
        check_iframes: Проверять ли iframe при ненахождении.
        description: Описание для логов (например, «Версия для печати»).

    Returns:
        WebElement по которому кликнули.

    Raises:
        TimeoutException: Элемент не найден за timeout секунд.
        Exception: Все retries исчерпаны.
    """
    desc = description or selector[:60]
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            # ── Шаг 1: Ждём исчезновения overlay ──
            logger.debug("[%s] Попытка %d/%d — ждём overlay...", desc, attempt, retries)
            wait_overlays_gone(driver, overlay_selectors, timeout=min(timeout, 10))

            # ── Шаг 2: Ищем элемент (DOM + iframe) ──
            element = None
            deadline = time.time() + timeout

            while time.time() < deadline:
                element = find_element_deep(driver, by, selector, check_iframes)
                if element:
                    break
                time.sleep(0.5)

            if not element:
                raise TimeoutException(
                    f"Элемент не найден за {timeout}с: {desc}"
                )

            # ── Шаг 3: Scroll + нативный клик ──
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
                element,
            )
            time.sleep(0.3)  # Микропауза для settle после scroll

            try:
                element.click()
                logger.info("[%s] Нативный клик — успех.", desc)
                return element

            except ElementClickInterceptedException:
                # ── Шаг 4: JS-клик (обход перекрытия) ──
                logger.warning(
                    "[%s] Нативный клик перехвачен — JS-клик...", desc
                )
                driver.execute_script("arguments[0].click();", element)
                logger.info("[%s] JS-клик — успех.", desc)
                return element

            except ElementNotInteractableException:
                # Элемент есть, но не интерактивен — JS-клик
                logger.warning(
                    "[%s] Элемент не интерактивен — JS-клик...", desc
                )
                driver.execute_script("arguments[0].click();", element)
                logger.info("[%s] JS-клик (not interactable) — успех.", desc)
                return element

        except StaleElementReferenceException:
            logger.warning(
                "[%s] Stale element (попытка %d/%d) — повтор...",
                desc, attempt, retries,
            )
            last_error = StaleElementReferenceException(
                f"Элемент устарел после {retries} попыток: {desc}"
            )
            time.sleep(1)
            continue

        except TimeoutException as e:
            last_error = e
            logger.warning(
                "[%s] Timeout (попытка %d/%d): %s",
                desc, attempt, retries, e,
            )
            time.sleep(1)
            continue

    raise last_error or Exception(f"stable_click не удался: {desc}")


# ─── Smart Wait (замена time.sleep) ────────────────────────────────────────────


def smart_wait(
    driver: WebDriver,
    condition_fn,
    timeout: int = 15,
    poll: float = 0.5,
    message: str = "",
) -> bool:
    """
    Умное ожидание с пользовательским условием.

    Вместо `time.sleep(5)` — ждём конкретного условия.

    Args:
        driver: WebDriver-инстанс.
        condition_fn: Callable, возвращающий True когда условие выполнено.
                      Принимает driver как аргумент.
        timeout: Максимальное время ожидания.
        poll: Интервал опроса.
        message: Описание для логов.

    Returns:
        True если условие выполнено, False если timeout.

    Example:
        # Ждём, пока таблица не станет непустой
        smart_wait(driver, lambda d: len(d.find_elements(By.XPATH, "//tr")) > 1,
                   timeout=10, message="Ожидание загрузки таблицы")
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condition_fn(driver):
                logger.debug("smart_wait: условие выполнено — %s", message)
                return True
        except Exception:
            pass
        time.sleep(poll)

    logger.warning("smart_wait: timeout (%dс) — %s", timeout, message)
    return False


def wait_for_new_window(
    driver: WebDriver,
    known_handles: set,
    timeout: int = 15,
) -> str | None:
    """
    Ожидает появления нового окна/вкладки браузера.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = driver.execute_cdp_cmd("Target.getTargets", {})
            current_handles = {f"CDwindow-{t['targetId']}" for t in targets.get("targetInfos", []) if t["type"] == "page"}
            new_handles = current_handles - known_handles
            if new_handles:
                handle = new_handles.pop()
                logger.info("Новое окно: %s", handle)
                return handle
        except Exception as e:
            logger.debug(f"Ошибка при получении window_handles: {e}")
        time.sleep(0.5)

    logger.warning("Новое окно не появилось за %dс.", timeout)
    return None


# ─── Парсинг данных ────────────────────────────────────────────────────────────


def format_car_number(raw: str) -> str:
    """
    Форматирует гос. номер ТС в читаемый вид: A 123 BC 77.

    Args:
        raw: Сырая строка номера (например, 'A123BC77').

    Returns:
        Отформатированный номер или исходная строка.
    """
    s = re.sub(r'\s+', '', raw).upper()
    m = re.match(r'^([A-ZА-ЯЁ])(\d{3})([A-ZА-ЯЁ]{2})(\d{2,3})$', s)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)}"
    return s


def parse_ts_from_print_page(text: str) -> str:
    """
    Извлекает номер ТС из текста страницы about:blank.

    Args:
        text: Полный текст body страницы.

    Returns:
        Отформатированный номер ТС или 'ТС'.
    """
    clean = " ".join(text.split())
    m = re.search(
        r'\bТС[:\s]+([А-ЯЁA-Z]\d{3}[А-ЯЁA-Z]{2}\d{2,3})\b',
        clean,
        re.IGNORECASE,
    )
    if m:
        return format_car_number(m.group(1))
    return "ТС"


def parse_date_token(s: str) -> datetime.date:
    """
    Парсит отдельную дату из строки (ДД.ММ.ГГГГ или ДД.ММ.ГГ).
    """
    s = s.strip()
    m = re.match(r"^(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})$", s)
    if not m:
        raise ValueError(f"Неверный формат даты '{s}'. Ожидается ДД.ММ.ГГГГ")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    return datetime.date(year, month, day)


def parse_date_range(raw: str) -> tuple[datetime.date, datetime.date] | None:
    """
    Парсит диапазон дат из пользовательского ввода.

    Поддерживает форматы:
    - '15.09.2026 - 18.09.2026' (с дефисом/тире)
    - '15.09.26 - 18.09.26' (двухзначный год)
    - '15.09.2026, 18.09.2026' или через пробел
    - '15.09.2026' (один день -> диапазон с 15 по 15)

    Returns:
        Кортеж (start_date, end_date) или None, если строка пуста.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    dates_found = re.findall(r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b", raw)
    if not dates_found:
        raise ValueError(f"Не удалось распознать даты в строке: '{raw}'")

    if len(dates_found) >= 2:
        start_date = parse_date_token(dates_found[0])
        end_date = parse_date_token(dates_found[1])
    else:
        start_date = parse_date_token(dates_found[0])
        end_date = start_date

    if start_date > end_date:
        raise ValueError(
            f"Дата начала ({start_date.strftime('%d.%m.%Y')}) не может быть "
            f"позже даты окончания ({end_date.strftime('%d.%m.%Y')})"
        )

    return start_date, end_date


def parse_driver_name(driver: WebDriver) -> str:
    """
    Извлекает ФИО водителя из заголовка модального окна.

    Args:
        driver: WebDriver-инстанс (должен быть на странице с модальным окном).

    Returns:
        ФИО водителя или 'Водитель'.
    """
    try:
        modal_text = driver.find_element(
            By.XPATH, "//*[contains(text(), 'водитель:')]"
        ).text
        logger.debug("Шапка модалки: %s", modal_text)
        if "водитель:" in modal_text.lower():
            raw = modal_text.lower().split("водитель:")[1].strip()
            name = " ".join([w.capitalize() for w in raw.split()[:3]])
            logger.info("Водитель: %s", name)
            return name
    except (NoSuchElementException, IndexError, AttributeError) as e:
        logger.debug("Не удалось извлечь ФИО водителя: %s", e)
    return "Водитель"

