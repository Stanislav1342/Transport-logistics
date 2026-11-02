"""
main.py — Точка входа автоматизации Advantum (Puls).

v24 — Рефакторинг на базе v22:
- Модульная архитектура (config, browser, utils, export)
- stable_click с обходом MUI overlay/iframe
- CDP Page.printToPDF вместо PyAutoGUI + Ctrl+P
- Credentials в .env
- Все задержки из v22 сохранены
"""

import logging
import sys
import time

from config import load_config
from browser import create_driver, login_to_advantum
from export import export_thermographs
from utils import parse_date_range

VERSION = "v42"


def setup_logging() -> None:
    """Настраивает логирование в консоль и файл."""
    # Milliseconds are handled by %(msecs)03d in format, or simply by omitting datefmt and relying on default, but we'll add it to format
    log_format = "[%(asctime)s.%(msecs)03d] [%(levelname)s] %(name)s: %(message)s"
    date_format = "%H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("advantum.log", encoding="utf-8", mode="a"),
        ],
    )
    # Приглушаем verbose-логи от Selenium и urllib3
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("WDM").setLevel(logging.WARNING)


def task_export_by_route_numbers() -> None:
    """Массовый экспорт термографиков по списку маршрутных листов."""
    config = load_config()

    print("\n--- Настройка массового поиска по Маршрутным Листам ---")
    raw_routes = input("Введите номера МЛ ЧЕРЕЗ ЗАПЯТУЮ (например: МСК521499, МСК521239): ")
    routes_list = [r.strip() for r in raw_routes.split(",") if r.strip()]

    if not routes_list:
        print("[Ошибка] Список пуст!")
        return

    # Запрос периода дат
    date_range = None
    while True:
        raw_dates = input(
            "Введите период дат (ДД.ММ.ГГГГ - ДД.ММ.ГГГГ, например: 01.09.2026 - 15.09.2026, или Enter для пропуска): "
        ).strip()
        if not raw_dates:
            print("[Инфо] Фильтр по дате не задан (используется текущий фильтр системы).")
            break
        try:
            date_range = parse_date_range(raw_dates)
            if date_range:
                d_from, d_to = date_range
                print(f"[Инфо] Выбран период: с {d_from.strftime('%d.%m.%Y')} по {d_to.strftime('%d.%m.%Y')}")
            break
        except ValueError as err:
            print(f"[Ошибка] {err}. Попробуйте снова.")

    print(f"\n[Инфо] Будет обработано МЛ: {len(routes_list)}")
    for i, ml in enumerate(routes_list, 1):
        print(f"  {i}. {ml}")
    print()

    driver = None
    try:
        # Инициализация и авторизация
        driver = create_driver(config)
        wait = login_to_advantum(driver, config)

        # Экспорт
        results = export_thermographs(driver, wait, routes_list, config, date_filter=date_range)

        # Вывод итогов
        print(f"\n{'=' * 50}")
        print(f"ЗАВЕРШЕНО: {sum(1 for r in results if r.success)}/{len(results)} успешно")
        for r in results:
            status = "✓" if r.success else "✗"
            detail = r.filepath if r.success else r.error
            print(f"  {status} {r.ml_number}: {detail}")
        print(f"{'=' * 50}")

    except Exception as e:
        print(f"[Критическая ошибка]: {e}")
        import traceback
        traceback.print_exc()
    finally:
        time.sleep(2)  # ← из v22
        print("\n[Завершение] Закрываем браузер.")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def main_menu() -> None:
    """Главное меню программы."""
    print(f"[ВЕРСИЯ ФАЙЛА] {VERSION} — рефакторинг на базе v22")
    setup_logging()

    while True:
        print(f"\n{'=' * 45}")
        print("      АВТОМАТИЗАЦИЯ ADVANTUM (ПО МЛ)")
        print(f"{'=' * 45}")
        print("1. Выгрузить термографики по списку Маршрутных Листов")
        print("0. Выйти из программы")
        print(f"{'=' * 45}")

        choice = input("Выберите действие (0-1): ").strip()
        if choice == "1":
            task_export_by_route_numbers()
        elif choice == "0":
            print("До свидания!")
            break
        else:
            print("Неверный выбор. Попробуйте снова.")


if __name__ == "__main__":
    main_menu()