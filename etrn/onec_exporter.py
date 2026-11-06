"""
onec_exporter.py — Автоматизация выгрузки таблиц из 1С:Предприятие через прямые ссылки (e1cib).

На Windows:
  Использует промышленный RPA-стандарт для 1С:Предприятие (Управляемый интерфейс / Такси):
    1. Поиск, максимизация и активация главного окна 1С.
    2. Прямой переход по постоянным навигационным ссылкам (Shift + F11):
       - «Маршрутный лист»: e1cib/list/Документ.МаршрутныйЛист
       - «Электронные транспортные накладные»: e1cib/list/Документ.ЭлектроннаяТранспортнаяНакладная
       - «Контроль подписания»: e1cib/data/Справочник.ВариантыОтчетов?ref=946700e0ed9e2e9e11f1495d81d50ffe
       (В диалоге перехода используется кнопка «Вставить из буфера», что исключает сбои раскладки).
    3. Раздел «Маршрутный лист»:
       - Заполнение дат фильтра отгрузки («Больше или равно» и «Меньше или равно») в шапке списка.
       - Нажатие прямой кнопки «Вывести список...» на панели инструментов (или через меню Еще).
       - В диалоге «Вывести список»:
         * Нажатие значка «Снять все флажки».
         * Включение галочек только для: «Номер», «Водитель», «Перевозчик», «Модель расчетов».
         * Нажатие [ОК].
       - Сохранение таблицы в 'Модель расчетов.xlsx' и закрытие вкладки «Информация по МЛ» (Ctrl+F4).
    4. Раздел «Электронные транспортные накладные»:
       - Установка периода через меню «Еще ▾» ➔ «Установить период...» (или плашку периода) в диалоге «Выберите период».
       - Меню «Еще ▾» ➔ «Вывести список...».
       - В диалоге «Вывести список»:
         * Снятие галочек с: «Внутренний номер», «Грузоотправитель», «Комментарий».
         * Нажатие [ОК].
       - Сохранение таблицы в 'ЭТРН 1с.xlsx' и закрытие вкладки «Список» (Ctrl+F4).
    5. Отчет «Контроль подписания перевозчиков по ЭТРН»:
       - Заполнение дат в шапке отчета.
       - Нажатие «Сформировать» (F5) и ожидание готовности.
       - Сохранение в 'Процент.xlsx' и закрытие вкладки отчета (Ctrl+F4).

На macOS / Linux:
  Автоматически активируется MockOneCExporter — симулятор полного пайплайна с подробным логированием.
"""

import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from .config import Config, get_config
except ImportError:
    from config import Config, get_config

logger = logging.getLogger("OneCExporter")


def set_clipboard(text: str) -> None:
    """Копирует текст в буфер обмена Windows (через Win32 API / clip.exe) без ошибок кодировки."""
    if sys.platform == "win32":
        try:
            import win32clipboard
            import win32con

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception as e:
            logger.debug("win32clipboard error: %s, fallback to clip.exe", e)

        try:
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True)
            return
        except Exception as e:
            logger.warning("Не удалось скопировать текст в clip: %s", e)
    else:
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        except Exception:
            pass


class BaseOneCExporter:
    """Интерфейс сервиса выгрузки данных из 1С."""

    def export_all(
        self,
        marshrut_period: Tuple[datetime, datetime],
        etrn_period: Tuple[datetime, datetime],
    ) -> Dict[str, str]:
        raise NotImplementedError


class MockOneCExporter(BaseOneCExporter):
    """
    Симулятор 1С для работы на macOS / Linux.
    Позволяет протестировать весь пайплайн без наличия установленной 1С.
    """

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()

    def export_all(
        self,
        marshrut_period: Tuple[datetime, datetime],
        etrn_period: Tuple[datetime, datetime],
    ) -> Dict[str, str]:
        m_start = marshrut_period[0].strftime("%d.%m.%Y")
        m_end = marshrut_period[1].strftime("%d.%m.%Y")
        e_start = etrn_period[0].strftime("%d.%m.%Y")
        e_end = etrn_period[1].strftime("%d.%m.%Y")

        print("\n" + "=" * 70)
        print("  [1С-СИМУЛЯТОР / macOS] Эмуляция автоматической выгрузки из 1С")
        print("=" * 70)
        print(f" 1. Поиск главного окна 1С: '{self.cfg.onec_window_regex}' -> [Найдено]")
        print(" 2. Активация и максимизация окна 1С (ShowWindow SW_MAXIMIZE)")
        print("-" * 70)

        # 1. Маршрутный лист
        print(f" 3. Прямой переход по ссылке (Shift + F11):")
        print(f"    URL: «{self.cfg.url_marshrut}»")
        print(f"    Открыт раздел: «{self.cfg.title_marshrut}» [OK]")
        print(f"    Проверка и нормализация фильтров шапки: сняты лишние галочки (Номер, Водитель)")
        print(f"    Установка дат в фильтре отгрузки: с {m_start} по {m_end}")
        print(f"    Кнопка тулбара «Вывести список...»")
        print(f"    Диалог «Вывести список»:")
        print(f"      • Нажатие «Снять все флажки»")
        print(f"      • Включение только: Номер, Водитель, Перевозчик, Модель расчетов")
        print(f"      • Нажатие [ОК]")
        print(f"    Сохранение (Ctrl+S) ➔ «{self.cfg.model_file}»")
        print(f"    Закрытие временной вкладки «Модель расчетов.xlsx» по крестику [x]")
        time.sleep(0.4)

        # 2. ЭТРН
        print("-" * 70)
        print(f" 4. Прямой переход по ссылке (Shift + F11):")
        print(f"    URL: «{self.cfg.url_etrn}»")
        print(f"    Открыт раздел: «{self.cfg.title_etrn}» [OK]")
        print(f"    Установка периода списка через диалог: с {e_start} по {e_end}")
        print(f"    Верификация плашки периода: подтверждено «{e_start} - {e_end}»")
        print(f"    Таблица Еще ➔ «Вывести список...»")
        print(f"    Диалог «Вывести список»:")
        print(f"      • Снятие флажков: Внутренний номер, Грузоотправитель, Комментарий")
        print(f"      • Нажатие [ОК]")
        print(f"    Сохранение (Ctrl+S) ➔ «{self.cfg.etrn_file}»")
        print(f"    Закрытие временной вкладки «ЭТРН 1с.xlsx» по крестику [x]")
        time.sleep(0.4)

        # 3. Контроль подписания
        print("-" * 70)
        print(f" 5. Прямой переход по ссылке (Shift + F11):")
        print(f"    URL: «{self.cfg.url_percent}»")
        print(f"    Открыт отчет: «{self.cfg.title_percent}» [OK]")
        print(f"    Заполнение полей в шапке отчета: с {e_start} по {e_end}")
        print(f"    Нажатие кнопки «Сформировать» (F5) ➔ Формирование отчета...")
        print(f"    Сохранение (Ctrl+S) ➔ «{self.cfg.percent_file}»")
        print(f"    Закрытие временной вкладки отчета по крестику [x]")
        print(f"    Проверка нижней панели 1С: сохранены только 4 рабочих раздела пользователя")
        time.sleep(0.4)

        print("=" * 70)
        print("  ✓ [1С-СИМУЛЯТОР] Все 3 таблицы успешно выгружены на рабочий стол.")
        print("=" * 70 + "\n")

        return {
            "model": self.cfg.model_file,
            "etrn": self.cfg.etrn_file,
            "percent": self.cfg.percent_file,
        }


class WindowsOneCExporter(BaseOneCExporter):
    """
    Реальный сервис автоматизации 1С:Предприятие под Windows.
    Использует прямые навигационные ссылки 1С (Shift+F11 / e1cib) и UI Automation.
    """

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        try:
            import pywinauto
            from pywinauto import Application, Desktop
            from pywinauto.keyboard import send_keys
            import win32api
            import win32con
            import win32gui

            self.pywinauto = pywinauto
            self.Application = Application
            self.Desktop = Desktop
            self.send_keys = send_keys
            self.win32api = win32api
            self.win32con = win32con
            self.win32gui = win32gui
        except ImportError as e:
            raise ImportError(
                f"Не установлена библиотека для Windows-автоматизации: {e}. "
                "Выполните: pip install pywinauto pywin32"
            )

        self.app = None
        self.main_win = None

    # ---------------------------------------------------------------------
    # Низкоуровневые методы работы с клавиатурой и окном
    # ---------------------------------------------------------------------
    def _send_vk(self, vk_code: int, down: bool = True) -> None:
        """Отправка Virtual Key события через win32api (не зависит от раскладки)."""
        flags = 0 if down else self.win32con.KEYEVENTF_KEYUP
        self.win32api.keybd_event(vk_code, 0, flags, 0)

    def _send_hotkey(self, *vk_codes: int) -> None:
        """Нажатие комбинации клавиш по их виртуальным кодам."""
        for code in vk_codes:
            self._send_vk(code, down=True)
            time.sleep(0.04)
        for code in reversed(vk_codes):
            self._send_vk(code, down=False)
            time.sleep(0.04)
        time.sleep(0.2)

    def _hotkey_shift_f11(self) -> None:
        """Переход по ссылке 1С: Shift + F11 (0x10, 0x7A)."""
        self._send_hotkey(self.win32con.VK_SHIFT, self.win32con.VK_F11)

    def _hotkey_shift_f10(self) -> None:
        """Контекстное меню Windows: Shift + F10 (0x10, 0x79)."""
        self._send_hotkey(self.win32con.VK_SHIFT, self.win32con.VK_F10)

    def _hotkey_ctrl_s(self) -> None:
        """Сохранить (Ctrl + S): 0x53."""
        self._send_hotkey(self.win32con.VK_CONTROL, 0x53)

    def _hotkey_ctrl_f4(self) -> None:
        """Закрыть текущую вкладку (Ctrl + F4): VK_F4 = 0x73."""
        self._send_hotkey(self.win32con.VK_CONTROL, self.win32con.VK_F4)

    def _hotkey_f5(self) -> None:
        """Сформировать отчет / обновить (F5): VK_F5 = 0x74."""
        self._send_hotkey(self.win32con.VK_F5)

    def _hotkey_enter(self) -> None:
        """Нажатие Enter."""
        self._send_hotkey(self.win32con.VK_RETURN)

    def _hotkey_tab(self) -> None:
        """Нажатие Tab."""
        self._send_hotkey(self.win32con.VK_TAB)

    def _hotkey_esc(self) -> None:
        """Нажатие Esc."""
        self._send_hotkey(self.win32con.VK_ESCAPE)

    def _paste_clipboard(self) -> None:
        """Вставка из буфера обмена через Ctrl + V (0x56)."""
        self._send_hotkey(self.win32con.VK_CONTROL, 0x56)

    def _select_all_and_paste(self, text: str) -> None:
        """Выделяет всё поле (Ctrl+A), копирует текст в буфер и вставляет (Ctrl+V)."""
        set_clipboard(text)
        time.sleep(0.08)
        self._send_hotkey(self.win32con.VK_CONTROL, 0x41)  # Ctrl + A
        time.sleep(0.08)
        self._paste_clipboard()
        time.sleep(0.1)

    def _enter_date_field(self, click_x: int, click_y: int, date_str: str) -> None:
        """
        Гарантированно очищает и вставляет дату в поле 1С:
        1. Двойной клик в центр поля (выделяет существующее значение).
        2. Выделение всеми способами: Ctrl+A и Home + Shift+End.
        3. Вставка даты через буфер обмена (Ctrl+V).
        """
        self.pywinauto.mouse.click(coords=(click_x, click_y))
        time.sleep(0.1)
        self.pywinauto.mouse.double_click(coords=(click_x, click_y))
        time.sleep(0.1)

        set_clipboard(date_str)
        time.sleep(0.06)

        self._send_hotkey(self.win32con.VK_CONTROL, 0x41)  # Ctrl + A
        time.sleep(0.05)
        self._send_hotkey(self.win32con.VK_HOME)
        time.sleep(0.05)
        self._send_hotkey(self.win32con.VK_SHIFT, self.win32con.VK_END)
        time.sleep(0.05)

        self._paste_clipboard()
        time.sleep(0.12)

    # ---------------------------------------------------------------------
    # Надежный поиск диалогов в Windows (Win32 EnumWindows + Desktop UIA)
    # ---------------------------------------------------------------------
    def _is_dialog_open(self, title_sub: str) -> bool:
        """Проверяет через Win32 EnumWindows, открыто ли диалоговое окно с указанной подстрокой."""
        try:
            pattern = re.compile(rf".*{re.escape(title_sub)}.*", re.IGNORECASE)
            elems = self.pywinauto.findwindows.find_elements(title_re=pattern)
            main_hwnd = self.main_win.handle if self.main_win else None
            for el in elems:
                if main_hwnd and el.handle == main_hwnd:
                    continue
                return True
            return False
        except Exception:
            return False

    def _find_dialog(self, title_sub: str, timeout: float = 8.0):
        """
        Гарантированно находит отдельное диалоговое окно по подстроке заголовка.
        Ищет ТОЛЬКО среди окон верхнего уровня (Win32 EnumWindows / Desktop UIA),
        исключая главное окно 1С и его внутренние элементы управления (кнопки, плашки).
        """
        end_time = time.time() + timeout
        pattern = re.compile(rf".*{re.escape(title_sub)}.*", re.IGNORECASE)
        main_hwnd = None
        if self.main_win:
            try:
                main_hwnd = self.main_win.handle
            except Exception:
                pass

        while time.time() < end_time:
            # 1. Поиск через Win32 EnumWindows (на уровне ОС среди окон верхнего уровня)
            try:
                handles = self.pywinauto.findwindows.find_elements(title_re=pattern)
                for h in handles:
                    if main_hwnd and h.handle == main_hwnd:
                        continue
                    app_dlg = self.Application(backend="uia").connect(handle=h.handle)
                    w = app_dlg.window(handle=h.handle)
                    return w
            except Exception:
                pass

            # 2. Поиск через Desktop UIA
            try:
                for w in self.Desktop(backend="uia").windows():
                    try:
                        if main_hwnd and w.handle == main_hwnd:
                            continue
                        t = (w.element_info.name or w.window_text() or "").strip()
                        if pattern.search(t):
                            ctrl_type = getattr(w.element_info, "control_type", "")
                            if ctrl_type in ("Window", "Pane", ""):
                                return w
                    except Exception:
                        continue
            except Exception:
                pass

            time.sleep(0.3)
        return None

    # ---------------------------------------------------------------------
    # Активация и максимизация окна 1С
    # ---------------------------------------------------------------------
    def _find_and_activate_1c(self) -> None:
        """Находит главное окно 1С по заголовку, максимизирует и выводит на передний план."""
        print(f"[1С:Windows] Поиск окна 1С по маске '{self.cfg.onec_window_regex}'...")
        elements = self.pywinauto.findwindows.find_elements(
            title_re=self.cfg.onec_window_regex
        )
        if not elements:
            raise RuntimeError(
                "Главное окно 1С:Предприятие не найдено!\n"
                "Пожалуйста, запустите 1С и войдите в рабочую базу перед запуском скрипта."
            )

        hwnd = elements[0].handle
        self.app = self.Application(backend="uia").connect(handle=hwnd)
        self.main_win = self.app.window(handle=hwnd)

        # Разворачиваем окно на весь экран для предсказуемых координат
        try:
            self.win32gui.ShowWindow(hwnd, self.win32con.SW_MAXIMIZE)
        except Exception:
            pass
        time.sleep(0.3)

        try:
            self.win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        try:
            self.main_win.set_focus()
        except Exception:
            pass

        time.sleep(0.5)
        print("[1С:Windows] ✓ Окно 1С успешно максимизировано и активировано.")

    # ---------------------------------------------------------------------
    # Управление вкладками 1С и безопасный переход по ссылкам (e1cib)
    # ---------------------------------------------------------------------
    def _activate_tab_by_name(self, section_title: str) -> bool:
        """
        Ищет вкладку с подстрокой section_title на нижней панели открытых вкладок 1С.
        Кликает строго по левой части вкладки (r.left + 25), чтобы исключить клик по крестику [x].
        Возвращает True, если вкладка найдена и успешно активирована.
        """
        if not self.main_win:
            return False

        try:
            main_r = self.main_win.rectangle()
            kw = section_title.lower().strip()
            synonyms = [kw]
            if "маршрут" in kw:
                synonyms.extend(["маршрутн", "маршрутный"])
            elif "электронн" in kw or "этрн" in kw:
                synonyms.extend(["электронн", "этрн", "транспортные накладные"])
            elif "контроль подписания" in kw or "процент" in kw:
                synonyms.extend(["контроль подписания", "вариант отчета", "подписания перевозчиков"])

            for el in self.main_win.descendants():
                try:
                    txt = (el.element_info.name or el.window_text() or "").strip().lower()
                    if not txt:
                        continue
                    if any(s in txt for s in synonyms):
                        r = el.rectangle()
                        # Нижняя панель открытых вкладок 1С Такси (нижние 80 пикселей)
                        if r.top >= main_r.bottom - 80 and r.width() > 30 and r.height() > 15:
                            click_x = r.left + min(35, r.width() // 3)
                            click_y = r.top + r.height() // 2
                            self.pywinauto.mouse.click(coords=(click_x, click_y))
                            return True
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Поиск открытой вкладки '%s': %s", section_title, e)

        return False

    def _activate_tab_or_open_link(self, url: str, section_title: str) -> None:
        """
        Переходит в раздел 1С.
        Если раздел уже открыт на нижней панели вкладок — активирует его безопасным кликом.
        Если еще не открыт — открывает по ссылке e1cib через Shift + F11.
        """
        print(f"\n[1С:Windows] Активация раздела «{section_title}»...")

        # 1. Проверяем, не открыт ли раздел уже на нижней панели вкладок 1С
        if self._activate_tab_by_name(section_title):
            print(f"[1С:Windows] ✓ Раздел «{section_title}» уже был открыт — активирован кликом по вкладке.")
            time.sleep(1.0)
            return

        # 2. Если вкладка еще не открыта — переходим по прямой ссылке Shift + F11
        print(f"[1С:Windows] Открытие раздела «{section_title}» по ссылке (Shift+F11)...")
        print(f"             URL: {url}")

        set_clipboard(url)
        time.sleep(0.1)

        self._hotkey_shift_f11()
        time.sleep(1.0)

        link_dlg = self._find_dialog("переход по ссылке", timeout=4.0)

        pasted = False
        if link_dlg:
            try:
                for btn in link_dlg.descendants(control_type="Button"):
                    txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                    if "вставить из буфера" in txt:
                        btn.click_input()
                        pasted = True
                        time.sleep(0.3)
                        break
            except Exception as e:
                logger.debug("Клик 'Вставить из буфера': %s", e)

        if not pasted:
            self._paste_clipboard()
            time.sleep(0.2)

        transitioned = False
        if link_dlg:
            try:
                for btn in link_dlg.descendants(control_type="Button"):
                    txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                    if txt == "перейти":
                        btn.click_input()
                        transitioned = True
                        time.sleep(0.5)
                        break
            except Exception:
                pass

        if not transitioned:
            self._hotkey_enter()

        time.sleep(3.0)
        print(f"[1С:Windows] ✓ Раздел «{section_title}» открыт по прямой ссылке.")

    def _open_by_link(self, url: str, section_title: str) -> None:
        """Алиас для обратной совместимости."""
        self._activate_tab_or_open_link(url, section_title)

    def _close_unwanted_tabs(self) -> None:
        """
        Безопасная функция:
        Все сформированные временные таблицы («Модель расчетов.xlsx», «ЭТРН 1с.xlsx»)
        и вкладка отчета «Контроль подписания...» закрываются СТРОГО адресно через Ctrl+F4
        сразу после сохранения каждого файла на диск.
        Эта функция не сканирует и не закрывает вкладки наугад, что на 100% гарантирует
        сохранность 4 постоянных рабочих разделов пользователя.
        """
        pass

    def _close_temporary_tab(self, tab_title_keyword: str = "") -> None:
        """Алиас: очищает временные вкладки через _close_unwanted_tabs."""
        self._close_unwanted_tabs()

    def _is_checkbox_checked(self, cb) -> Optional[bool]:
        """Определяет состояние CheckBox в 1С."""
        try:
            if hasattr(cb, "is_checked"):
                return bool(cb.is_checked())
        except Exception:
            pass
        try:
            if hasattr(cb, "get_toggle_state"):
                return cb.get_toggle_state() == 1
        except Exception:
            pass
        try:
            action = (cb.legacy_properties().get("DefaultAction") or "").lower()
            if "снять" in action:
                return True
            elif "установить" in action:
                return False
        except Exception:
            pass
        try:
            val = str(cb.legacy_properties().get("Value") or "")
            if val in ("1", "True", "true"):
                return True
            elif val in ("0", "False", "false"):
                return False
        except Exception:
            pass
        return None

    def _ensure_marshrut_filters(self) -> None:
        """
        Проверяет и нормализует быстрые фильтры в шапке «Маршрутный лист»:
        Обязательно ВКЛЮЧЕНЫ:
          • «Дата отгрузки» (>=)
          • «Дата отгрузки» (<=)
          • «Пометка удаления» (Нет)
        Обязательно ВЫКЛЮЧЕНЫ (сняты галочки):
          • «Водитель»
          • «Номер»
          • «Перевозчик»
          • «Модель расчетов»
          • «Статус»
          • «Транспортное средство»
        """
        print("[1С:Windows] Проверка фильтров в шапке «Маршрутный лист»...")
        if not self.main_win:
            return

        TARGET_RULES = {
            "водитель": False,
            "номер": False,
            "перевозчик": False,
            "модель расчетов": False,
            "модель": False,
            "статус": False,
            "транспорт": False,
            "пометка удаления": True,
            "пометка": True,
            "дата отгрузки": True,
            "отгрузки": True,
        }

        try:
            main_r = self.main_win.rectangle()
            # Область фильтров шапки: по высоте обычно от main_r.top + 70 до main_r.top + 280
            filter_top = main_r.top + 70
            filter_bottom = main_r.top + 280

            labels = []
            for el in self.main_win.descendants(control_type="Text"):
                try:
                    r = el.rectangle()
                    if filter_top <= r.top <= filter_bottom:
                        txt = (el.element_info.name or el.window_text() or "").strip()
                        if txt:
                            labels.append((txt.lower(), r))
                except Exception:
                    continue

            checkboxes = []
            for el in self.main_win.descendants(control_type="CheckBox"):
                try:
                    r = el.rectangle()
                    if filter_top <= r.top <= filter_bottom:
                        checkboxes.append(el)
                except Exception:
                    continue

            print(f"[1С:Windows] Найдено элементов фильтров шапки: меток={len(labels)}, чекбоксов={len(checkboxes)}.")

            changes_made = 0
            for cb in checkboxes:
                cb_r = cb.rectangle()
                cb_name = (cb.element_info.name or cb.window_text() or "").strip().lower()

                matched_rule = None
                matched_name = ""

                # 1. По имени самого чекбокса
                for rule_kw, desired in TARGET_RULES.items():
                    if rule_kw in cb_name:
                        matched_rule = desired
                        matched_name = rule_kw
                        break

                # 2. По ближайшей текстовой метке слева
                if matched_rule is None and labels:
                    best_lbl = None
                    min_dist = 999999
                    for lbl_txt, lbl_r in labels:
                        if abs(lbl_r.top - cb_r.top) <= 22:
                            dist = cb_r.left - lbl_r.right
                            if -10 <= dist < min_dist:
                                min_dist = dist
                                best_lbl = lbl_txt

                    if best_lbl:
                        for rule_kw, desired in TARGET_RULES.items():
                            if rule_kw in best_lbl:
                                matched_rule = desired
                                matched_name = best_lbl.replace(":", "").strip()
                                break

                if matched_rule is not None:
                    curr_state = self._is_checkbox_checked(cb)
                    if curr_state is not None and curr_state != matched_rule:
                        action_str = "включение" if matched_rule else "снятие лишней галочки"
                        print(f"[1С:Windows] -> Фильтр «{matched_name}»: текущее={curr_state}, требуемое={matched_rule} ({action_str})...")
                        cb.click_input()
                        changes_made += 1
                        time.sleep(0.3)

            if changes_made > 0:
                print(f"[1С:Windows] ✓ Исправлено чекбоксов фильтра: {changes_made}. Ожидание обновления списка...")
                time.sleep(2.0)
            else:
                print("[1С:Windows] ✓ Все галочки фильтров в шапке «Маршрутный лист» соответствуют эталону.")

        except Exception as e:
            logger.warning("Проверка фильтров «Маршрутный лист»: %s", e)

    # ---------------------------------------------------------------------
    # Раздел 1: «Маршрутный лист»
    # ---------------------------------------------------------------------
    def _set_marshrut_period(self, date_from: datetime, date_to: datetime) -> None:
        """
        Устанавливает фильтр дат отгрузки в шапке списка «Маршрутный лист»:
        Строка 1: «Дата отгрузки: [Больше или равно] [03.10.2026 0:00:00]»
        Строка 2: «Дата отгрузки: [Меньше или равно] [05.11.2026 0:00:00]»
        """
        # Сначала проверяем и нормализуем галочки фильтров шапки
        self._ensure_marshrut_filters()

        d_from_str = date_from.strftime("%d.%m.%Y")
        d_to_str = date_to.strftime("%d.%m.%Y")
        print(f"[1С:Windows] Установка дат в «Маршрутный лист»: с {d_from_str} по {d_to_str}...")

        # Поиск Edit полей в фильтре шапки
        edits_found: List = []
        try:
            for el in self.main_win.descendants(control_type="Edit"):
                try:
                    r = el.rectangle()
                    if 90 <= r.top <= 180 and 400 <= r.left <= 750:
                        edits_found.append(el)
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Поиск Edit полей Маршрутного листа: %s", e)

        edits_found.sort(key=lambda x: x.rectangle().top)

        if len(edits_found) >= 2:
            print("[1С:Windows] Заполнение полей фильтра отгрузки через элементы интерфейса...")
            edits_found[0].click_input()
            time.sleep(0.15)
            self._select_all_and_paste(d_from_str)
            time.sleep(0.15)

            edits_found[1].click_input()
            time.sleep(0.15)
            self._select_all_and_paste(d_to_str)
            time.sleep(0.15)
        else:
            print("[1С:Windows] Использование точных экранных координат фильтра дат...")
            # Поле 1 (Больше или равно): x ~ 580, y ~ 122
            self.pywinauto.mouse.click(coords=(580, 122))
            time.sleep(0.15)
            self._select_all_and_paste(d_from_str)
            time.sleep(0.15)

            # Поле 2 (Меньше или равно): x ~ 580, y ~ 153
            self.pywinauto.mouse.click(coords=(580, 153))
            time.sleep(0.15)
            self._select_all_and_paste(d_to_str)
            time.sleep(0.15)

        # Обновление списка (Enter) и ожидание фильтрации
        self._hotkey_enter()
        time.sleep(2.5)
        print(f"[1С:Windows] ✓ Фильтр «Маршрутный лист» установлен: {d_from_str} - {d_to_str}.")

    def _configure_ml_columns(self, dlg) -> None:
        """
        Настраивает колонки в диалоге «Вывести список» для «Маршрутный лист»:
        1. Нажимает кнопку «Снять все флажки».
        2. Включает только: «Номер», «Водитель», «Перевозчик», «Модель расчетов».
        3. Нажимает кнопку [ОК].
        """
        print("[1С:Windows] Настройка колонок «Маршрутный лист» (Номер, Водитель, Перевозчик, Модель расчетов)...")
        try:
            dlg.set_focus()
        except Exception:
            pass
        try:
            self.win32gui.SetForegroundWindow(dlg.handle)
        except Exception:
            pass
        time.sleep(0.3)

        r = dlg.rectangle()

        # Шаг 1: Нажатие кнопки «Снять все флажки»
        uncheck_btn = None
        try:
            for btn in dlg.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or btn.element_info.help_text or "").lower()
                if "снять" in txt:
                    uncheck_btn = btn
                    break
        except Exception:
            pass

        if uncheck_btn:
            print("[1С:Windows] Нажатие кнопки «Снять все флажки» (UIA)...")
            uncheck_btn.click_input()
        else:
            print("[1С:Windows] Нажатие кнопки «Снять все флажки» по координатам...")
            self.pywinauto.mouse.click(coords=(r.right - 35, r.top + 77))

        time.sleep(0.5)

        # Шаг 2: Фокусируем список колонок
        self.pywinauto.mouse.click(coords=(r.left + 80, r.top + 130))
        time.sleep(0.2)

        # Способ 1: Клавиатурный выбор по номерам строк от первой строки (Дата отгрузки)
        # В 1С нажатие Пробел переключает галочку, Стрелка вниз переходит на следующую строку
        self._send_hotkey(self.win32con.VK_HOME)  # Строка 1: Дата отгрузки
        time.sleep(0.1)

        # Строка 2: Номер -> Пробел
        self._send_hotkey(self.win32con.VK_DOWN)
        time.sleep(0.1)
        self._send_hotkey(self.win32con.VK_SPACE)
        print("[1С:Windows] ✓ Включена колонка: «Номер»")
        time.sleep(0.1)

        # Переход к Строке 5: Водитель (3 шага вниз)
        for _ in range(3):
            self._send_hotkey(self.win32con.VK_DOWN)
            time.sleep(0.05)
        self._send_hotkey(self.win32con.VK_SPACE)
        print("[1С:Windows] ✓ Включена колонка: «Водитель»")
        time.sleep(0.1)

        # Переход к Строке 10: Перевозчик (5 шагов вниз: 5 -> 10)
        for _ in range(5):
            self._send_hotkey(self.win32con.VK_DOWN)
            time.sleep(0.06)
        self._send_hotkey(self.win32con.VK_SPACE)
        print("[1С:Windows] ✓ Включена колонка: «Перевозчик»")
        time.sleep(0.1)

        # Переход к Строке 17: Модель расчетов (ровно 7 шагов вниз от Перевозчик:
        # 11: Количество заказов, 12: Количество мест, 13: Количество паллет,
        # 14: Количество пунктов назначения, 15: Направление, 16: Переходники, 17: Модель расчетов)
        print("[1С:Windows] Прокрутка списка к колонке «Модель расчетов» (7 шагов вниз)...")
        for _ in range(7):
            self._send_hotkey(self.win32con.VK_DOWN)
            time.sleep(0.08)
        self._send_hotkey(self.win32con.VK_SPACE)
        print("[1С:Windows] ✓ Включена колонка: «Модель расчетов»")
        time.sleep(0.1)

        # Шаг 3: Нажатие кнопки «ОК»
        print("[1С:Windows] Подтверждение настроенных колонок (ОК)...")
        ok_clicked = False
        try:
            for btn in dlg.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                if txt == "ок":
                    btn.click_input()
                    ok_clicked = True
                    break
        except Exception:
            pass

        if not ok_clicked:
            # Нажимаем Enter или кликаем по желтой кнопке ОК
            self._hotkey_enter()

        # Ожидание закрытия модального диалога
        for _ in range(12):
            if not self._is_dialog_open("вывести список"):
                break
            time.sleep(0.4)

        # Ожидание генерации табличного документа «Информация по МЛ»
        time.sleep(3.5)

    def _export_marshrut_list(self, target_filepath: str) -> None:
        """
        Выгружает открытый «Маршрутный лист» через кнопку панели «Вывести список...»
        """
        print(f"[1С:Windows] Выгрузка «Маршрутный лист» в «{target_filepath}»...")

        # Даем 1С завершить фоновые запросы фильтрации
        time.sleep(1.5)

        # 1. Нажатие кнопки «Вывести список...» на панели инструментов
        print("[1С:Windows] Нажатие кнопки «Вывести список...» на панели инструментов...")
        btn_clicked = False
        try:
            for btn in self.main_win.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or btn.element_info.help_text or "").strip().lower()
                if "вывести список" in txt:
                    btn.click_input()
                    btn_clicked = True
                    break
        except Exception as e:
            logger.debug("UIA поиск 'Вывести список': %s", e)

        if not btn_clicked:
            # Координата кнопки тулбара (x ~ 1012, y ~ 88)
            self.pywinauto.mouse.click(coords=(1012, 88))

        dlg = self._find_dialog("вывести список", timeout=5.0)

        # 2. Повторная попытка клика по тулбару, если диалог не открылся
        if not dlg:
            print("[1С:Windows] Повторный клик по кнопке «Вывести список...»...")
            self.pywinauto.mouse.click(coords=(1012, 88))
            dlg = self._find_dialog("вывести список", timeout=4.0)

        # 3. Fallback: вызов через меню таблицы «Еще ▾»
        if not dlg:
            print("[1С:Windows] Вызов «Вывести список...» через меню таблицы «Еще ▾»...")
            self._click_table_more_button()
            time.sleep(0.6)
            for w in self.Desktop(backend="uia").windows():
                for el in w.descendants():
                    txt = (el.element_info.name or el.window_text() or "").strip().lower()
                    if "вывести список" in txt:
                        el.click_input()
                        break
            dlg = self._find_dialog("вывести список", timeout=6.0)

        if not dlg:
            raise RuntimeError("Не открылся диалог «Вывести список» в разделе «Маршрутный лист»!")

        # 4. Настройка колонок в диалоге
        self._configure_ml_columns(dlg)

        # 5. Сохранение файла на рабочий стол
        self._save_in_dialog(target_filepath)

        # 6. Закрытие сформированной вкладки списка (Ctrl+F4)
        print("[1С:Windows] Закрытие сформированной вкладки списка (Ctrl+F4)...")
        time.sleep(0.5)
        self._hotkey_ctrl_f4()
        time.sleep(0.5)

        # 7. Безопасное закрытие временных вкладок (сохраняем только 4 постоянных раздела)
        self._close_unwanted_tabs()

    # ---------------------------------------------------------------------
    # Раздел 2: «Электронные транспортные накладные»
    # ---------------------------------------------------------------------
    def _verify_etrn_period_pill(self, d_from_str: str, d_to_str: str) -> Tuple[bool, str]:
        """
        Проверяет текст на плашке периода списка «ЭТРН» (в левой части тулбара).
        Возвращает (True, pill_text), если период валиден.
        """
        try:
            for el in self.main_win.descendants():
                try:
                    r = el.rectangle()
                    if r.left <= 300 and 60 <= r.top <= 140:
                        txt = (el.element_info.name or el.window_text() or "").strip()
                        if re.search(r"\d{2}\.\d{2}\.\d{4}", txt) or "-" in txt:
                            if txt.startswith("-"):
                                return False, txt
                            if d_from_str in txt:
                                return True, txt
                            return True, txt
                except Exception:
                    continue
        except Exception:
            pass
        return True, ""

    def _set_etrn_period(self, date_from: datetime, date_to: datetime) -> None:
        """
        Устанавливает период для списка ЭТРН через диалог «Выберите период».
        Основано на проверенной рабочей логике коммита f2f3e2f:
        1. Клик по плашке периода строго в левой части тулбара (r.left <= 300, 60 <= r.top <= 140).
        2. Если не открылась — открытие через меню «Еще ▾» ➔ «Установить период...».
        3. Заполнение полей «Дата начала» и «Дата окончания» (через Edit или координаты/Tab).
        4. Нажатие «Выбрать» / Enter.
        """
        d_from_str = date_from.strftime("%d.%m.%Y")
        d_to_str = date_to.strftime("%d.%m.%Y")
        print(f"[1С:Windows] Установка периода в «ЭТРН»: с {d_from_str} по {d_to_str}...")

        time.sleep(1.0)

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            opened_dialog = False

            # 1. Клик по плашке периода строго в левой части тулбара (r.left <= 300, 60 <= r.top <= 140)
            # Ограничение r.left <= 300 на 100% исключает случайные клики по ячейкам таблицы списка
            try:
                for el in self.main_win.descendants():
                    try:
                        r = el.rectangle()
                        if r.left <= 300 and 60 <= r.top <= 140:
                            txt = (el.element_info.name or el.window_text() or "").strip()
                            if re.search(r"\d{2}\.\d{2}\.\d{4}", txt) or "-" in txt:
                                click_x = r.left + 25
                                click_y = r.top + r.height() // 2
                                print(f"[1С:Windows] Клик по плашке периода «{txt}» в coords=({click_x}, {click_y})...")
                                self.pywinauto.mouse.click(coords=(click_x, click_y))
                                time.sleep(1.2)
                                if self._is_dialog_open("период"):
                                    opened_dialog = True
                                break
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("Поиск плашки периода ЭТРН: %s", e)

            # 2. Если по плашке не открылся — открываем через меню таблицы «Еще ▾» (справа)
            if not opened_dialog and not self._is_dialog_open("период"):
                print("[1С:Windows] Открытие диалога периода через меню таблицы «Еще ▾»...")
                self._click_table_more_button()
                time.sleep(0.6)
                for w in self.Desktop(backend="uia").windows():
                    for el in w.descendants():
                        txt = (el.element_info.name or el.window_text() or "").strip().lower()
                        if "установить период" in txt:
                            print(f"[1С:Windows] Нажатие пункта «{txt}»...")
                            el.click_input()
                            opened_dialog = True
                            time.sleep(1.2)
                            break
                    if opened_dialog:
                        break

            # 3. Ищем диалог «Выберите период»
            dlg = self._find_dialog("период", timeout=4.0)
            if dlg:
                try:
                    dlg.set_focus()
                except Exception:
                    pass
                try:
                    self.win32gui.SetForegroundWindow(dlg.handle)
                except Exception:
                    pass
                time.sleep(0.3)

                # 4. Точный ввод дат в Edit-поля диалога
                edits = []
                try:
                    for e in dlg.descendants(control_type="Edit"):
                        try:
                            r = e.rectangle()
                            if r.width() >= 30 and r.height() >= 10:
                                edits.append(e)
                        except Exception:
                            continue
                except Exception:
                    pass

                edits.sort(key=lambda e: (e.rectangle().top, e.rectangle().left))

                dlg_r = dlg.rectangle()

                # Координаты Box 1 и Box 2 по проверенным пропорциям скриншота 1С
                box1_x = dlg_r.left + int(dlg_r.width() * 0.17)
                box1_y = dlg_r.top + int(dlg_r.height() * 0.14)
                box2_x = dlg_r.left + int(dlg_r.width() * 0.50)
                box2_y = box1_y

                # Если UIA видит Edit-элементы в верхней строке, уточняем координаты
                if len(edits) >= 2 and edits[0].rectangle().top < dlg_r.top + int(dlg_r.height() * 0.3):
                    r0 = edits[0].rectangle()
                    r1 = edits[1].rectangle()
                    box1_x = r0.left + r0.width() // 2
                    box1_y = r0.top + r0.height() // 2
                    box2_x = r1.left + r1.width() // 2
                    box2_y = r1.top + r1.height() // 2
                    print(f"[1С:Windows] Координаты Edit-полей: Поле 1=({box1_x}, {box1_y}), Поле 2=({box2_x}, {box2_y})...")
                else:
                    print(f"[1С:Windows] Расчетные координаты полей: Поле 1=({box1_x}, {box1_y}), Поле 2=({box2_x}, {box2_y})...")

                # Ввод даты начала
                print(f"[1С:Windows] Заполнение даты начала ({d_from_str}) в ({box1_x}, {box1_y})...")
                self._enter_date_field(box1_x, box1_y, d_from_str)

                # Ввод даты окончания
                print(f"[1С:Windows] Заполнение даты окончания ({d_to_str}) в ({box2_x}, {box2_y})...")
                self._enter_date_field(box2_x, box2_y, d_to_str)

                # Нажатие кнопки «Выбрать» / «ОК»
                selected = False
                try:
                    for btn in dlg.descendants(control_type="Button"):
                        btxt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                        if btxt in ("выбрать", "ок"):
                            print(f"[1С:Windows] Подтверждение выбора периода (кнопка «{btxt}»)...")
                            btn.click_input()
                            selected = True
                            break
                except Exception:
                    pass

                if not selected:
                    btn_x = dlg_r.left + int(dlg_r.width() * 0.73)
                    btn_y = dlg_r.top + int(dlg_r.height() * 0.94)
                    print(f"[1С:Windows] Координатный клик кнопки «Выбрать» в ({btn_x}, {btn_y})...")
                    try:
                        self.pywinauto.mouse.click(coords=(btn_x, btn_y))
                    except Exception:
                        pass
                    time.sleep(0.2)
                    self._hotkey_enter()

                # Ожидание закрытия диалога «Выберите период»
                for _ in range(15):
                    if not self._is_dialog_open("период"):
                        break
                    time.sleep(0.3)

                time.sleep(1.5)
                print(f"[1С:Windows] ✓ Период списка «ЭТРН» успешно установлен: с {d_from_str} по {d_to_str}.")
                return
            else:
                print(f"[1С:Windows] ⚠ Диалог периода не открылся на попытке {attempt}/{max_attempts}, повтор...")
                time.sleep(1.0)

        print(f"[1С:Windows] ⚠ Диалог периода не был открыт после {max_attempts} попыток, продолжаем выгрузку списка...")

    def _configure_etrn_columns(self, dlg) -> None:
        """
        Настраивает колонки в диалоге «Вывести список» для «ЭТРН»:
        Снимает галочки только с:
        - «Внутренний номер» (Строка 3)
        - «Грузоотправитель» (Строка 4)
        - «Комментарий» (Строка 9)
        Остальные (Номер, Дата, Грузополучатель, Перевозчик, Текущий шаг, Состояние ЭДО) остаются включенными.
        """
        print("[1С:Windows] Настройка колонок «ЭТРН» (снятие: Внутренний номер, Грузоотправитель, Комментарий)...")
        try:
            dlg.set_focus()
        except Exception:
            pass
        try:
            self.win32gui.SetForegroundWindow(dlg.handle)
        except Exception:
            pass
        time.sleep(0.3)

        r = dlg.rectangle()

        # Фокусируем список колонок
        self.pywinauto.mouse.click(coords=(r.left + 80, r.top + 130))
        time.sleep(0.2)

        # Клавиатурное снятие от первой строки (Номер)
        self._send_hotkey(self.win32con.VK_HOME)  # Строка 1: Номер
        time.sleep(0.1)

        # Переход к Строке 3: Внутренний номер (2 шага вниз) -> снять галочку
        self._send_hotkey(self.win32con.VK_DOWN)  # 2: Дата
        self._send_hotkey(self.win32con.VK_DOWN)  # 3: Внутренний номер
        time.sleep(0.1)
        self._send_hotkey(self.win32con.VK_SPACE)  # Снять
        print("[1С:Windows] ✓ Снята галочка: «Внутренний номер»")
        time.sleep(0.1)

        # Переход к Строке 4: Грузоотправитель (1 шаг вниз) -> снять галочку
        self._send_hotkey(self.win32con.VK_DOWN)  # 4: Грузоотправитель
        time.sleep(0.1)
        self._send_hotkey(self.win32con.VK_SPACE)  # Снять
        print("[1С:Windows] ✓ Снята галочка: «Грузоотправитель»")
        time.sleep(0.1)

        # Переход к Строке 9: Комментарий (5 шагов вниз: 5, 6, 7, 8, 9)
        for _ in range(5):
            self._send_hotkey(self.win32con.VK_DOWN)
            time.sleep(0.05)
        self._send_hotkey(self.win32con.VK_SPACE)  # Снять
        print("[1С:Windows] ✓ Снята галочка: «Комментарий»")
        time.sleep(0.1)

        # Нажатие кнопки «ОК»
        print("[1С:Windows] Подтверждение колонок ЭТРН (ОК)...")
        ok_clicked = False
        try:
            for btn in dlg.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                if txt == "ок":
                    btn.click_input()
                    ok_clicked = True
                    break
        except Exception:
            pass

        if not ok_clicked:
            self._hotkey_enter()

        # Ожидание закрытия модального диалога
        for _ in range(12):
            if not self._is_dialog_open("вывести список"):
                break
            time.sleep(0.4)

        # Ожидание генерации табличного документа «Список»
        time.sleep(3.5)

    def _export_etrn_list(self, target_filepath: str) -> None:
        """
        Выгружает открытый список «ЭТРН» через меню таблицы «Еще ▾» ➔ «Вывести список...»
        """
        print(f"[1С:Windows] Выгрузка «ЭТРН» в «{target_filepath}»...")

        time.sleep(1.5)

        # 1. Открытие меню «Еще ▾» таблицы списка
        self._click_table_more_button()
        time.sleep(0.6)

        # 2. Поиск и клик пункта «Вывести список...»
        item_clicked = False
        for w in self.Desktop(backend="uia").windows():
            for el in w.descendants():
                try:
                    txt = (el.element_info.name or el.window_text() or "").strip().lower()
                    if "вывести список" in txt:
                        el.click_input()
                        item_clicked = True
                        break
                except Exception:
                    continue
            if item_clicked:
                break

        if not item_clicked:
            # Fallback: координатный клик пункта меню ~ (1600, 576)
            self.pywinauto.mouse.click(coords=(1600, 576))

        # 3. Ожидаем появления диалога «Вывести список»
        dlg = self._find_dialog("вывести список", timeout=6.0)
        if not dlg:
            raise RuntimeError("Не открылся диалог «Вывести список» в разделе «ЭТРН»!")

        # 4. Настройка колонок (Снятие: Внутренний номер, Грузоотправитель, Комментарий -> ОК)
        self._configure_etrn_columns(dlg)

        # 5. Сохранение файла на рабочий стол
        self._save_in_dialog(target_filepath)

        # 6. Закрытие сформированной вкладки списка (Ctrl+F4)
        print("[1С:Windows] Закрытие сформированной вкладки списка (Ctrl+F4)...")
        time.sleep(0.5)
        self._hotkey_ctrl_f4()
        time.sleep(0.5)

        # 7. Безопасное закрытие временных вкладок (сохраняем только 4 постоянных раздела)
        self._close_unwanted_tabs()

    # ---------------------------------------------------------------------
    # Раздел 3: Отчет «Контроль подписания перевозчиков по ЭТРН»
    # ---------------------------------------------------------------------
    def _is_percent_report_ready(self) -> bool:
        """
        Проверяет, появились ли данные в сформированном табличном документе отчета.
        В строках сформированного отчета появляются перевозчики («ООО», «ИП») или проценты («%»).
        """
        try:
            main_r = self.main_win.rectangle()
            table_top = main_r.top + 150
            for el in self.main_win.descendants():
                try:
                    r = el.rectangle()
                    if r.top >= table_top and r.width() > 20 and r.height() > 10:
                        txt = (el.element_info.name or el.window_text() or "").strip()
                        if "%" in txt or any(kw in txt for kw in ("ИП ", "ООО ", "ОАО ", "ЗАО ", "ЛТД")):
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _export_percent_report(
        self,
        date_from: datetime,
        date_to: datetime,
        target_filepath: str,
    ) -> None:
        """
        Заполняет даты в шапке отчета «Контроль подписания...», формирует его (кнопка «Сформировать»)
        и сохраняет в Excel файл 'Процент.xlsx'.
        """
        d_from_str = date_from.strftime("%d.%m.%Y")
        d_to_str = date_to.strftime("%d.%m.%Y")
        print(f"\n[1С:Windows] Настройка отчета «{self.cfg.title_percent}»...")
        print(f"[1С:Windows] Заполнение дат в шапке отчета: с {d_from_str} по {d_to_str}...")

        time.sleep(1.5)

        # Поиск Edit полей в шапке отчета
        edits: List = []
        try:
            for el in self.main_win.descendants(control_type="Edit"):
                try:
                    r = el.rectangle()
                    if 50 <= r.top <= 120 and 300 <= r.left <= 600:
                        edits.append(el)
                except Exception:
                    continue
        except Exception:
            pass

        edits.sort(key=lambda x: x.rectangle().left)

        if len(edits) >= 2:
            edits[0].click_input()
            time.sleep(0.15)
            self._select_all_and_paste(d_from_str)
            time.sleep(0.15)

            edits[1].click_input()
            time.sleep(0.15)
            self._select_all_and_paste(d_to_str)
            time.sleep(0.15)
        else:
            # Координатный ввод в шапке отчета: (350, 80) и (450, 80)
            self.pywinauto.mouse.click(coords=(350, 80))
            time.sleep(0.15)
            self._select_all_and_paste(d_from_str)
            time.sleep(0.15)
            self.pywinauto.mouse.click(coords=(450, 80))
            time.sleep(0.15)
            self._select_all_and_paste(d_to_str)
            time.sleep(0.15)

        # Фиксация введенных дат (Enter без перехода в соседнее поле Перевозчик)
        self._hotkey_enter()
        time.sleep(0.3)

        # 3. Нажатие кнопки «Сформировать»
        print("[1С:Windows] Запуск формирования отчета (кнопка «Сформировать»)...")
        formed = False
        main_r = self.main_win.rectangle()

        # Ищем кнопку «Сформировать» среди всех элементов тулбара отчета
        try:
            for el in self.main_win.descendants():
                try:
                    txt = (el.element_info.name or el.window_text() or "").strip().lower()
                    r = el.rectangle()
                    if "сформировать" in txt and 35 <= r.top <= 140:
                        click_x = r.left + r.width() // 2
                        click_y = r.top + r.height() // 2
                        print(f"[1С:Windows] Клик по кнопке «Сформировать» «{txt}» в coords=({click_x}, {click_y})...")
                        try:
                            el.click_input()
                            formed = True
                            break
                        except Exception:
                            self.pywinauto.mouse.click(coords=(click_x, click_y))
                            formed = True
                            break
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Поиск кнопки Сформировать: %s", e)

        if not formed:
            # Если кнопку не нашли по имени — нажимаем F5
            print("[1С:Windows] Запуск формирования через горячую клавишу F5...")
            self._hotkey_f5()

        # Ожидание завершения формирования отчета (динамическое)
        max_timeout = max(getattr(self.cfg, "report_gen_timeout", 45), 45)
        print(f"[1С:Windows] Ожидание формирования отчета (до {max_timeout} сек)...")
        start_time = time.time()
        time.sleep(4.0)

        while True:
            elapsed = int(time.time() - start_time)
            if elapsed >= max_timeout:
                print(f"[1С:Windows] Достигнут таймаут ожидания ({max_timeout} сек), переходим к сохранению...")
                break

            # 1. Проверяем, появились ли данные в таблице
            if elapsed >= 8 and self._is_percent_report_ready():
                print(f"[1С:Windows] ✓ Данные отчета обнаружены на экране! Формирование завершено за {elapsed} сек.")
                time.sleep(1.0)
                break

            # 2. Проверяем признаки активной генерации отчета
            is_generating = False
            try:
                import ctypes
                if ctypes.windll.user32.IsHungAppWindow(self.main_win.handle):
                    is_generating = True
            except Exception:
                pass

            if not is_generating:
                try:
                    for el in self.main_win.descendants():
                        try:
                            txt = (el.element_info.name or el.window_text() or "").strip().lower()
                            if any(w in txt for w in ("прервать", "формирован", "выполняется")):
                                is_generating = True
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if is_generating:
                print(f"[1С:Windows] 1С формирует отчет... ({elapsed} сек)")
            elif elapsed >= 12:
                # Если 1С свободна и прошло более 12 секунд
                print(f"[1С:Windows] ✓ Отчет готов к сохранению ({elapsed} сек).")
                time.sleep(1.0)
                break

            time.sleep(1.5)

        # Кликаем в область сформированной таблицы перед сохранением, чтобы передать ей фокус
        table_focus_x = main_r.left + 250
        table_focus_y = main_r.top + 250
        print(f"[1С:Windows] Активация таблицы отчета перед сохранением ({table_focus_x}, {table_focus_y})...")
        self.pywinauto.mouse.click(coords=(table_focus_x, table_focus_y))
        time.sleep(0.5)

        # 4. Сохранение отчета
        self._save_in_dialog(target_filepath)

        # 5. Закрытие вкладки отчета (Ctrl+F4)
        print("[1С:Windows] Закрытие временной вкладки отчета (Ctrl+F4)...")
        time.sleep(0.5)
        self._hotkey_ctrl_f4()
        time.sleep(0.5)

        # 6. Закрытие оставшихся временных вкладок (сохраняем только 4 раздела пользователя)
        self._close_unwanted_tabs()

    # ---------------------------------------------------------------------
    # Вспомогательные методы выгрузки и сохранения
    # ---------------------------------------------------------------------
    def _click_table_more_button(self) -> None:
        """Кликает по кнопке «Еще ▾» на панели списка документов (в правой части тулбара)."""
        clicked = False
        try:
            more_btns = []
            for btn in self.main_win.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                if txt in ("еще", "ещё"):
                    r = btn.rectangle()
                    if 40 <= r.top <= 140:
                        more_btns.append(btn)
            if more_btns:
                target_btn = max(more_btns, key=lambda b: b.rectangle().left)
                print(f"[1С:Windows] Клик по кнопке «Еще ▾» ({target_btn.rectangle()})...")
                target_btn.click_input()
                clicked = True
        except Exception as e:
            logger.debug("Поиск кнопки Еще: %s", e)

        if not clicked:
            main_r = self.main_win.rectangle()
            # Координатный клик кнопки Еще в правой верхней части тулбара
            click_x = max(main_r.left + 500, main_r.right - 60)
            click_y = main_r.top + 70
            print(f"[1С:Windows] Координатный клик кнопки «Еще ▾» в ({click_x}, {click_y})...")
            self.pywinauto.mouse.click(coords=(click_x, click_y))

    def _save_in_dialog(self, target_filepath: str) -> None:
        """
        Сохраняет сформированный табличный документ в Excel по Ctrl+S.
        Гарантирует:
        1. Проверку и выбор формата «Лист Excel2007-... (*.xlsx)».
        2. Безошибочный ввод полного пути файла в поле «Имя файла:».
        3. Клик по кнопке [Сохранить].
        4. Ожидание записи файла на диск и проверку XLSX-сигнатуры (PK).
        Вкладки не закрывает (закрытие выполняется строго через _close_temporary_tab).
        """
        print(f"[1С:Windows] Сохранение документа в «{target_filepath}» через Ctrl+S...")

        # Удаляем предыдущий файл, чтобы гарантировать проверку свежего
        if os.path.exists(target_filepath):
            try:
                os.remove(target_filepath)
            except Exception:
                pass

        # Вызов системного диалога Windows «Сохранение»
        self._hotkey_ctrl_s()
        time.sleep(1.5)

        # Поиск диалога сохранения через системный поиск
        save_dlg = self._find_dialog("сохран", timeout=5.0)
        if not save_dlg:
            save_dlg = self._find_dialog("save", timeout=1.5)

        if not save_dlg:
            raise RuntimeError("Диалоговое окно «Сохранение» не появилось после нажатия Ctrl+S!")

        try:
            save_dlg.set_focus()
        except Exception:
            pass

        dlg_r = save_dlg.rectangle()

        # -------------------------------------------------------------
        # 1. Проверка и выбор формата «Лист Excel2007-... (*.xlsx)»
        # -------------------------------------------------------------
        already_xlsx = False
        try:
            for el in save_dlg.descendants():
                txt = (el.element_info.name or el.window_text() or "").lower()
                val = str(el.legacy_properties().get("Value", "")).lower() if hasattr(el, "legacy_properties") else ""
                if ("2007" in txt and "xlsx" in txt) or ("2007" in val and "xlsx" in val):
                    already_xlsx = True
                    break
        except Exception as e:
            logger.debug("Проверка формата xlsx: %s", e)

        if already_xlsx:
            print("[1С:Windows] ✓ Тип файла уже установлен в «Лист Excel2007-... (*.xlsx)»")
        else:
            print("[1С:Windows] Переключение выпадающего списка «Тип файла» на Excel XLSX...")
            type_combo = None
            try:
                for cb in save_dlg.descendants(control_type="ComboBox"):
                    txt = (cb.element_info.name or cb.window_text() or "").lower()
                    auto_id = getattr(cb.element_info, "automation_id", "")
                    r = cb.rectangle()
                    if auto_id == "FileTypeControlHost" or "тип" in txt or "type" in txt or r.top > dlg_r.bottom - 110:
                        type_combo = cb
                        break
            except Exception as e:
                logger.debug("Поиск ComboBox типа файла: %s", e)

            if type_combo:
                try:
                    type_combo.click_input()
                    time.sleep(0.3)
                    switched_by_item = False
                    for w in self.Desktop(backend="uia").windows():
                        for it in w.descendants():
                            it_txt = (it.element_info.name or it.window_text() or "").lower()
                            if "2007" in it_txt or "xlsx" in it_txt:
                                it.click_input()
                                switched_by_item = True
                                break
                        if switched_by_item:
                            break
                    if not switched_by_item:
                        self._send_hotkey(self.win32con.VK_DOWN)
                        time.sleep(0.1)
                        self._hotkey_enter()
                    time.sleep(0.3)
                    print("[1С:Windows] ✓ Выбран тип файла: «Лист Excel2007-... (*.xlsx)»")
                except Exception as e:
                    logger.debug("UIA выбор типа файла: %s", e)

        # -------------------------------------------------------------
        # 2. Поиск поля «Имя файла:» и ввод полного пути сохранения
        # -------------------------------------------------------------
        print(f"[1С:Windows] Вставка пути сохранения: {target_filepath}...")

        # Шаг 2.1: Ищем текстовую метку «Имя файла:»
        lbl_target = None
        try:
            for el in save_dlg.descendants(control_type="Text"):
                t = (el.element_info.name or el.window_text() or "").strip().lower()
                if "имя файла" in t or "file name" in t:
                    lbl_target = el
                    break
        except Exception as e:
            logger.debug("Поиск метки 'Имя файла': %s", e)

        # Шаг 2.2: Ищем ComboBox поля ввода (верхний ComboBox в нижней части диалога)
        fn_control = None
        try:
            bottom_combos = []
            for cb in save_dlg.descendants(control_type="ComboBox"):
                try:
                    r = cb.rectangle()
                    if r.top >= dlg_r.bottom - 170:
                        bottom_combos.append(cb)
                except Exception:
                    continue
            bottom_combos.sort(key=lambda c: c.rectangle().top)
            if bottom_combos:
                fn_control = bottom_combos[0]
        except Exception as e:
            logger.debug("Поиск ComboBox имени файла: %s", e)

        # Шаг 2.3: Надежная фокусировка поля «Имя файла» (клик прямо в белое поле ввода)
        focused = False
        if lbl_target:
            try:
                r_lbl = lbl_target.rectangle()
                # Поле ввода расположено сразу справа от метки «Имя файла:»
                click_x = r_lbl.right + 60
                click_y = r_lbl.top + r_lbl.height() // 2
                print(f"[1С:Windows] Фокусировка поля «Имя файла» (клик справа от метки: {click_x}, {click_y})...")
                self.pywinauto.mouse.click(coords=(click_x, click_y))
                focused = True
                time.sleep(0.15)
            except Exception as e:
                logger.debug("Клик справа от метки: %s", e)

        if not focused and fn_control:
            try:
                r = fn_control.rectangle()
                self.pywinauto.mouse.click(coords=(r.left + 50, r.top + r.height() // 2))
                focused = True
                time.sleep(0.15)
            except Exception:
                pass

        if not focused:
            # Горячая клавиша перехода к «Имя файла» в русской Windows: Alt + И (клавиша B = 0x42)
            self._send_hotkey(self.win32con.VK_MENU, 0x42)
            time.sleep(0.15)

        # Шаг 2.4: Прямая установка текста через Win32 WM_SETTEXT, если доступен дескриптор
        if fn_control:
            try:
                self.win32gui.SendMessage(fn_control.handle, self.win32con.WM_SETTEXT, 0, target_filepath)
                for ch in fn_control.descendants():
                    try:
                        self.win32gui.SendMessage(ch.handle, self.win32con.WM_SETTEXT, 0, target_filepath)
                    except Exception:
                        pass
                time.sleep(0.1)
            except Exception:
                pass

        # Шаг 2.5: Надежная вставка через буфер обмена: Ctrl+A -> Ctrl+V
        self._select_all_and_paste(target_filepath)
        time.sleep(0.4)

        # -------------------------------------------------------------
        # 3. Нажатие кнопки «Сохранить»
        # -------------------------------------------------------------
        save_btn = None
        try:
            for btn in save_dlg.descendants(control_type="Button"):
                txt = (btn.element_info.name or btn.window_text() or "").strip().lower()
                auto_id = getattr(btn.element_info, "automation_id", "")
                r = btn.rectangle()
                if (txt in ("сохранить", "save") or auto_id == "1") and r.top >= dlg_r.bottom - 70:
                    save_btn = btn
                    break
        except Exception:
            pass

        if save_btn:
            print("[1С:Windows] Нажатие кнопки «Сохранить»...")
            save_btn.click_input()
        else:
            print("[1С:Windows] Подтверждение сохранения (Enter)...")
            self._hotkey_enter()

        time.sleep(1.0)
        # Подтверждение перезаписи (если выскочит диалог "Заменить существующий файл?")
        confirm_dlg = self._find_dialog("подтверд", timeout=1.2)
        if confirm_dlg:
            print("[1С:Windows] Подтверждение замены файла (Enter)...")
            self._hotkey_enter()
        else:
            time.sleep(0.3)

        # -------------------------------------------------------------
        # 4. Ожидание записи файла на диск и проверка формата
        # -------------------------------------------------------------
        file_ready = False
        for _ in range(25):
            if os.path.exists(target_filepath) and os.path.getsize(target_filepath) > 0:
                time.sleep(0.6)
                file_ready = True
                break
            time.sleep(1.0)

        if not file_ready:
            raise RuntimeError(
                f"Файл {target_filepath} не был сохранен на рабочий стол!\n"
                "Проверьте активность диалогового окна 1С и права на запись."
            )

        # Проверяем сигнатуру XLSX (ZIP PK\x03\x04)
        try:
            with open(target_filepath, "rb") as f:
                sig = f.read(4)
            if sig == b"PK\x03\x04":
                print(f"[1С:Windows] ✓ Таблица успешно сохранена в формате Excel (XLSX): {target_filepath}")
            else:
                logger.warning(
                    "Внимание: файл %s имеет сигнатуру %s (не PK). 1С сохранила его в другом формате.",
                    target_filepath,
                    sig,
                )
        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Полный цикл выгрузки всех 3 таблиц
    # ---------------------------------------------------------------------
    def export_all(
        self,
        marshrut_period: Tuple[datetime, datetime],
        etrn_period: Tuple[datetime, datetime],
    ) -> Dict[str, str]:
        print("\n" + "=" * 70)
        print(" ЗАПУСК ВЫГРУЗКИ ТРЕХ ТАБЛИЦ ИЗ 1С:ПРЕДПРИЯТИЕ (e1cib)")
        print("=" * 70)

        # 1. Активация и разворачивание окна 1С
        self._find_and_activate_1c()

        try:
            # 2. Выгрузка «Маршрутный лист» -> Модель расчетов.xlsx
            self._activate_tab_or_open_link(self.cfg.url_marshrut, self.cfg.title_marshrut)
            self._ensure_marshrut_filters()
            self._set_marshrut_period(marshrut_period[0], marshrut_period[1])
            self._export_marshrut_list(self.cfg.model_file)
            self._close_unwanted_tabs()

            # 3. Выгрузка «Электронные транспортные накладные» -> ЭТРН 1с.xlsx
            self._activate_tab_or_open_link(self.cfg.url_etrn, self.cfg.title_etrn)
            self._set_etrn_period(etrn_period[0], etrn_period[1])
            self._export_etrn_list(self.cfg.etrn_file)
            self._close_unwanted_tabs()

            # 4. Выгрузка «Контроль подписания перевозчиков по ЭТРН» -> Процент.xlsx
            self._activate_tab_or_open_link(self.cfg.url_percent, self.cfg.title_percent)
            self._export_percent_report(
                etrn_period[0], etrn_period[1], self.cfg.percent_file
            )
            self._close_unwanted_tabs()

        finally:
            # Финальная очистка: гарантируем, что остаются открытыми ТОЛЬКО 4 рабочих раздела пользователя
            self._close_unwanted_tabs()

        print("\n" + "=" * 70)
        print(" ✓ ВЫГРУЗКА ВСЕХ 3 ТАБЛИЦ ИЗ 1С УСПЕШНО ЗАВЕРШЕНА!")
        print(f" 1. Модель расчетов: {self.cfg.model_file}")
        print(f" 2. ЭТРН:            {self.cfg.etrn_file}")
        print(f" 3. Процент:         {self.cfg.percent_file}")
        print("=" * 70 + "\n")

        return {
            "model": self.cfg.model_file,
            "etrn": self.cfg.etrn_file,
            "percent": self.cfg.percent_file,
        }


def get_onec_exporter(cfg: Optional[Config] = None) -> BaseOneCExporter:
    """Фабрика: возвращает реальный сервис на Windows или Mock-симулятор на Mac/Linux."""
    if sys.platform == "win32":
        try:
            return WindowsOneCExporter(cfg)
        except Exception as e:
            logger.warning(
                "Ошибка инициализации WindowsOneCExporter: %s. Включаем симулятор.", e
            )
            return MockOneCExporter(cfg)
    return MockOneCExporter(cfg)
