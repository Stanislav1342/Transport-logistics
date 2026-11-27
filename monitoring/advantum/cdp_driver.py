"""
cdp_driver.py — Прямой драйвер браузера через Chrome DevTools Protocol (CDP).

Позволяет управлять Яндекс.Браузером / Google Chrome напрямую из Python
через встроенный протокол отладки (--remote-debugging-port) и WebSocket,
ПОЛНОСТЬЮ без использования chromedriver.exe.

Это решает проблему блокировки сторонних исполняемых файлов (WinError 1260)
корпоративными групповыми политиками Windows (AppLocker / SRP).
"""

import base64
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)


# ─── Минималистичный WebSocket-клиент (RFC 6455) на стандартной библиотеке ────


class _CDPWebSocket:
    """
    Чистый Python WebSocket-клиент поверх стандартного модуля socket.
    Не требует установки сторонних библиотек (websocket-client, websockets и т.д.).
    """

    def __init__(self, ws_url: str, timeout: float = 30.0):
        parsed = urllib.parse.urlparse(ws_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query

        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._rx_buf = bytearray()
        self._msg_id = 0
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode("ascii"))

        header_buf = bytearray()
        while b"\r\n\r\n" not in header_buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("Не удалось выполнить WebSocket handshake с браузером")
            header_buf.extend(chunk)

        sep_idx = header_buf.index(b"\r\n\r\n") + 4
        headers_text = header_buf[:sep_idx].decode("latin1", errors="replace")
        if "101" not in headers_text.splitlines()[0]:
            raise ConnectionError(f"Ошибка WebSocket handshake: {headers_text.splitlines()[0]}")

        # Сохраняем остаток байтов после заголовка в буфер чтения
        if len(header_buf) > sep_idx:
            self._rx_buf.extend(header_buf[sep_idx:])

    def _read_exact(self, n: int) -> bytes:
        while len(self._rx_buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Соединение с браузером (CDP WebSocket) закрыто")
            self._rx_buf.extend(chunk)
        out = bytes(self._rx_buf[:n])
        del self._rx_buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        fin_and_opcode = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask_key = os.urandom(4)

        if length < 126:
            header = struct.pack("!BB", fin_and_opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", fin_and_opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", fin_and_opcode, 0x80 | 127, length)

        masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(header + mask_key + masked)

    def _recv_message(self) -> str:
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = (b0 & 0x80) != 0
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]

            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                raise ConnectionError("Браузер закрыл WebSocket-соединение")
            elif opcode == 0x9:  # Ping -> Pong
                self._send_frame(0xA, payload)
                continue
            elif opcode == 0xA:  # Pong
                continue
            elif opcode in (0x1, 0x2, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._msg_id += 1
        req_id = self._msg_id
        message = json.dumps({"id": req_id, "method": method, "params": params or {}})
        self._send_frame(0x1, message.encode("utf-8"))

        while True:
            raw = self._recv_message()
            data = json.loads(raw)
            if data.get("id") == req_id:
                if "error" in data:
                    err = data["error"]
                    raise WebDriverException(
                        f"CDP ошибка ({method}): {err.get('message', err)}"
                    )
                return data.get("result", {})

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


# ─── Элемент DOM (аналог WebElement) ──────────────────────────────────────────


class CDPElement:
    """Представляет DOM-элемент в браузере, управляемом через CDP."""

    def __init__(self, driver: "DirectCDPDriver", object_id: str):
        self._driver = driver
        self.object_id = object_id

    def _call_js(
        self,
        fn_decl: str,
        args: list[Any] | None = None,
        return_by_value: bool = True,
    ) -> Any:
        cdp_args = []
        for a in args or []:
            if isinstance(a, CDPElement):
                cdp_args.append({"objectId": a.object_id})
            else:
                cdp_args.append({"value": a})

        try:
            res = self._driver.execute_cdp_cmd(
                "Runtime.callFunctionOn",
                {
                    "objectId": self.object_id,
                    "functionDeclaration": fn_decl,
                    "arguments": cdp_args,
                    "returnByValue": return_by_value,
                    "awaitPromise": True,
                },
            )
        except WebDriverException as e:
            msg = str(e)
            if "Could not find object" in msg or "Cannot find context" in msg:
                raise StaleElementReferenceException(
                    "Элемент больше не существует в DOM (stale objectId)"
                ) from e
            raise

        if "exceptionDetails" in res:
            exc = res["exceptionDetails"]
            text = exc.get("exception", {}).get("description") or exc.get("text", "JS Error")
            if "StaleElement" in text:
                raise StaleElementReferenceException(text)
            raise WebDriverException(f"Ошибка JS на элементе: {text}")

        return res.get("result", {})

    def _ensure_connected(self) -> None:
        res = self._call_js(
            "function() { return Boolean(this && this.isConnected); }"
        )
        if not res.get("value", False):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")

    def is_displayed(self) -> bool:
        res = self._call_js(
            """
            function() {
                if (!this || !this.isConnected) return { stale: true };
                const style = window.getComputedStyle(this);
                if (!style || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || parseFloat(style.opacity) === 0) {
                    return { displayed: false };
                }
                const rect = this.getBoundingClientRect();
                return { displayed: (rect.width > 0 && rect.height > 0) };
            }
            """
        ).get("value", {})
        if res.get("stale"):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")
        return bool(res.get("displayed", False))

    def is_enabled(self) -> bool:
        res = self._call_js(
            """
            function() {
                if (!this || !this.isConnected) return { stale: true };
                return { enabled: !Boolean(this.disabled) };
            }
            """
        ).get("value", {})
        if res.get("stale"):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")
        return bool(res.get("enabled", True))

    @property
    def text(self) -> str:
        res = self._call_js(
            """
            function() {
                if (!this || !this.isConnected) return { stale: true };
                return { text: (this.innerText !== undefined ? this.innerText : this.textContent) || '' };
            }
            """
        ).get("value", {})
        if res.get("stale"):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")
        return str(res.get("text", "")).strip()

    def get_attribute(self, name: str) -> str | None:
        res = self._call_js(
            """
            function(attrName) {
                if (!this || !this.isConnected) return { stale: true };
                if (attrName === 'value' && 'value' in this) {
                    return { val: this.value !== null && this.value !== undefined ? String(this.value) : '' };
                }
                const attr = this.getAttribute(attrName);
                if (attr !== null) return { val: String(attr) };
                if (attrName in this && typeof this[attrName] !== 'function' && typeof this[attrName] !== 'object') {
                    return { val: String(this[attrName]) };
                }
                return { val: null };
            }
            """,
            [name],
        ).get("value", {})
        if res.get("stale"):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")
        return res.get("val")

    def click(self) -> None:
        info = self._call_js(
            """
            function() {
                if (!this || !this.isConnected) return { stale: true };
                this.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                const rect = this.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) {
                    return { interactable: false };
                }
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const topEl = document.elementFromPoint(x, y);
                const intercepted = Boolean(topEl && topEl !== this && !this.contains(topEl) && !topEl.contains(this));
                if (!intercepted && typeof this.focus === 'function') {
                    this.focus();
                }
                return {
                    interactable: true,
                    intercepted: intercepted,
                    x: x,
                    y: y
                };
            }
            """
        ).get("value", {})

        if info.get("stale"):
            raise StaleElementReferenceException("Элемент отсоединен от DOM")
        if not info.get("interactable"):
            raise ElementNotInteractableException("Элемент имеет нулевой размер и не интерактивен")
        if info.get("intercepted"):
            raise ElementClickInterceptedException("Клик по элементу перехвачен другим слоем (overlay)")

        x = float(info["x"])
        y = float(info["y"])
        self._driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "left"},
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    def clear(self) -> None:
        self._ensure_connected()
        self._call_js(
            """
            function() {
                if (typeof this.focus === 'function') this.focus();
                const proto = Object.getPrototypeOf(this);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
                             Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (this._valueTracker) {
                    this._valueTracker.setValue('__cdp_force_clear__');
                }
                if (desc && desc.set) {
                    desc.set.call(this, '');
                } else {
                    this.value = '';
                }
                this.dispatchEvent(new Event('input', { bubbles: true }));
                this.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """
        )

    def send_keys(self, *values: Any) -> None:
        self._ensure_connected()
        self._call_js("function() { if (typeof this.focus === 'function') this.focus(); }")

        combined = "".join(str(v) for v in values)

        # Обработка комбинации Ctrl+A / Cmd+A (выделить всё)
        if (Keys.CONTROL + "a") in combined or (Keys.COMMAND + "a") in combined or (Keys.META + "a") in combined:
            self._call_js(
                """
                function() {
                    if (typeof this.select === 'function') {
                        this.select();
                    } else if (typeof this.setSelectionRange === 'function' && this.value) {
                        this.setSelectionRange(0, this.value.length);
                    }
                }
                """
            )
            combined = (
                combined.replace(Keys.CONTROL + "a", "")
                .replace(Keys.COMMAND + "a", "")
                .replace(Keys.META + "a", "")
            )

        text_buffer: list[str] = []

        def _flush_text() -> None:
            if not text_buffer:
                return
            chunk = "".join(text_buffer)
            text_buffer.clear()
            # Вводим текст через нативный сеттер React + события input/change
            self._call_js(
                """
                function(textToInsert) {
                    if (typeof this.focus === 'function') this.focus();
                    const proto = Object.getPrototypeOf(this);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
                                 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                    const curVal = this.value || '';
                    let nextVal = curVal + textToInsert;
                    if (typeof this.selectionStart === 'number' && typeof this.selectionEnd === 'number' && this.selectionStart !== this.selectionEnd) {
                        nextVal = curVal.slice(0, this.selectionStart) + textToInsert + curVal.slice(this.selectionEnd);
                    }
                    if (this._valueTracker) {
                        this._valueTracker.setValue(curVal === nextVal ? '__cdp_prev__' : curVal);
                    }
                    if (desc && desc.set) {
                        desc.set.call(this, nextVal);
                    } else {
                        this.value = nextVal;
                    }
                    this.dispatchEvent(new InputEvent('input', { bubbles: true, data: textToInsert, inputType: 'insertText' }));
                    this.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                [chunk],
            )

        for ch in combined:
            if ch in (Keys.ENTER, Keys.RETURN):
                _flush_text()
                self._driver._dispatch_special_key("Enter", "Enter", 13, text="\r")
            elif ch == Keys.BACKSPACE:
                _flush_text()
                self._call_js(
                    """
                    function() {
                        if (!('value' in this)) return;
                        const cur = this.value || '';
                        let next = cur.slice(0, -1);
                        if (typeof this.selectionStart === 'number' && typeof this.selectionEnd === 'number' && this.selectionStart !== this.selectionEnd) {
                            next = cur.slice(0, this.selectionStart) + cur.slice(this.selectionEnd);
                        }
                        const proto = Object.getPrototypeOf(this);
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
                                     Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                        if (this._valueTracker) {
                            this._valueTracker.setValue(cur === next ? '__cdp_prev__' : cur);
                        }
                        if (desc && desc.set) {
                            desc.set.call(this, next);
                        } else {
                            this.value = next;
                        }
                        this.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
                        this.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """
                )
                self._driver._dispatch_special_key("Backspace", "Backspace", 8)
            elif ch == Keys.ESCAPE:
                _flush_text()
                self._driver._dispatch_special_key("Escape", "Escape", 27)
            elif ch == Keys.TAB:
                _flush_text()
                self._driver._dispatch_special_key("Tab", "Tab", 9)
            elif "\ue000" <= ch <= "\ue0ff":
                # Игнорируем одиночные модификаторы
                _flush_text()
            else:
                text_buffer.append(ch)

        _flush_text()

    def find_element(self, by: str = By.ID, value: str = "") -> "CDPElement":
        els = self.find_elements(by, value)
        if not els:
            raise NoSuchElementException(f"Элемент не найден внутри родителя: {by}={value}")
        return els[0]

    def find_elements(self, by: str = By.ID, value: str = "") -> list["CDPElement"]:
        return self._driver._find_elements_internal(by, value, root_object_id=self.object_id)


# ─── Переключение окон и фреймов (switch_to) ──────────────────────────────────


class _CDPSwitchTo:
    def __init__(self, driver: "DirectCDPDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        target_id = handle.replace("CDwindow-", "")
        self._driver._attach_to_target(target_id)

    def frame(self, frame_reference: Any) -> None:
        if isinstance(frame_reference, CDPElement):
            self._driver._current_frame_object_id = frame_reference.object_id
        else:
            raise WebDriverException("Поддерживается переключение только по элементу iframe")

    def parent_frame(self) -> None:
        self._driver._current_frame_object_id = None

    def default_content(self) -> None:
        self._driver._current_frame_object_id = None


# ─── Основной класс драйвера (DirectCDPDriver) ────────────────────────────────


class DirectCDPDriver:
    """
    Полноценная замена selenium.webdriver.Chrome, работающая напрямую
    с Яндекс.Браузером / Chrome через порт отладки CDP без chromedriver.exe.
    """

    def __init__(
        self,
        browser_path: str,
        headless: bool = True,
        disable_js: bool = False,
        prefs: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
    ):
        self.browser_path = browser_path
        self.headless = headless
        self.disable_js = disable_js
        self._user_data_dir = tempfile.mkdtemp(prefix="advantum_cdp_profile_")
        self._port = self._get_free_port()
        self._proc: subprocess.Popen | None = None
        self._ws: _CDPWebSocket | None = None
        self._current_target_id: str = ""
        self._current_frame_object_id: str | None = None
        self.switch_to = _CDPSwitchTo(self)

        # Записываем настройки профиля до старта браузера
        self._prepare_profile_preferences(disable_js=disable_js, prefs=prefs)

        # Запускаем процесс браузера
        self._start_browser_process(extra_args=extra_args)

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _prepare_profile_preferences(
        self, disable_js: bool, prefs: dict[str, Any] | None
    ) -> None:
        default_dir = os.path.join(self._user_data_dir, "Default")
        os.makedirs(default_dir, exist_ok=True)
        pref_data: dict[str, Any] = {}

        if prefs:
            for k, v in prefs.items():
                parts = k.split(".")
                cur = pref_data
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = v

        if disable_js:
            profile_sec = pref_data.setdefault("profile", {})
            managed = profile_sec.setdefault("managed_default_content_settings", {})
            managed["javascript"] = 2
            default_vals = profile_sec.setdefault("default_content_setting_values", {})
            default_vals["javascript"] = 2

        pref_file = os.path.join(default_dir, "Preferences")
        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(pref_data, f, ensure_ascii=False)

    def _start_browser_process(self, extra_args: list[str] | None = None) -> None:
        cmd = [
            self.browser_path,
            f"--remote-debugging-port={self._port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--kiosk-printing",
        ]
        if self.headless:
            cmd.extend([
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
            ])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append("about:blank")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        # Ждем запуска HTTP-эндпоинта отладки CDP
        deadline = time.time() + 20.0
        targets = []
        while time.time() < deadline:
            try:
                targets = self._fetch_json_list()
                page_targets = [
                    t for t in targets
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
                if page_targets:
                    self._attach_to_target(page_targets[0]["id"])
                    return
            except Exception:
                pass
            time.sleep(0.2)

        self.quit()
        raise WebDriverException(
            f"Не удалось подключиться к браузеру по протоколу CDP (порт {self._port})"
        )

    def _fetch_json_list(self) -> list[dict[str, Any]]:
        url = f"http://127.0.0.1:{self._port}/json/list"
        req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{self._port}"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _attach_to_target(self, target_id: str) -> None:
        targets = self._fetch_json_list()
        target_info = next((t for t in targets if t.get("id") == target_id), None)
        if not target_info or not target_info.get("webSocketDebuggerUrl"):
            raise WebDriverException(f"Вкладка CDP {target_id} не найдена")

        if self._ws:
            self._ws.close()

        self._ws = _CDPWebSocket(target_info["webSocketDebuggerUrl"])
        self._current_target_id = target_id
        self._current_frame_object_id = None
        self.execute_cdp_cmd("Page.enable", {})
        self.execute_cdp_cmd("Runtime.enable", {})
        self.execute_cdp_cmd("DOM.enable", {})
        if self.disable_js:
            try:
                self.execute_cdp_cmd(
                    "Emulation.setScriptExecutionDisabled", {"value": True}
                )
            except Exception:
                pass

    def execute_cdp_cmd(self, cmd: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._ws:
            raise WebDriverException("CDP-сессия не активна")
        return self._ws.call(cmd, params or {})

    # ─── Поддержка ActionChains (W3C Actions) ─────────────────────────────────

    def execute(self, driver_command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Поддержка вызовов ActionChains(driver).send_keys(...).perform()."""
        if driver_command == "actions" and params:
            for action_seq in params.get("actions", []):
                if action_seq.get("type") == "key":
                    for item in action_seq.get("actions", []):
                        if item.get("type") == "keyDown":
                            val = item.get("value", "")
                            if val == Keys.ESCAPE:
                                self._dispatch_special_key("Escape", "Escape", 27)
                            elif val in (Keys.ENTER, Keys.RETURN):
                                self._dispatch_special_key("Enter", "Enter", 13, text="\r")
                            elif val == Keys.BACKSPACE:
                                self._dispatch_special_key("Backspace", "Backspace", 8)
            return {"value": None}
        return {"value": None}

    def _dispatch_special_key(
        self, key: str, code: str, key_code: int, text: str = ""
    ) -> None:
        down_params: dict[str, Any] = {
            "type": "rawKeyDown" if not text else "keyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code,
        }
        if text:
            down_params["text"] = text
            down_params["unmodifiedText"] = text
        self.execute_cdp_cmd("Input.dispatchKeyEvent", down_params)
        self.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
            },
        )

    # ─── Навигация и свойства страницы ────────────────────────────────────────

    def get(self, url: str) -> None:
        self._current_frame_object_id = None
        self.execute_cdp_cmd("Page.navigate", {"url": url})
        if self.disable_js:
            time.sleep(0.4)
            return
        # Ждем завершения загрузки документа (document.readyState === 'complete')
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                state = self.execute_script("return document.readyState;")
                if state in ("complete", "interactive"):
                    break
            except Exception:
                pass
            time.sleep(0.15)

    @property
    def current_url(self) -> str:
        try:
            return str(self.execute_script("return window.location.href;") or "")
        except Exception:
            return ""

    @property
    def page_source(self) -> str:
        try:
            return str(
                self.execute_script(
                    "return document.documentElement ? document.documentElement.outerHTML : '';"
                )
                or ""
            )
        except Exception:
            return ""

    @property
    def window_handles(self) -> list[str]:
        try:
            targets = self.execute_cdp_cmd("Target.getTargets", {})
            return [
                f"CDwindow-{t['targetId']}"
                for t in targets.get("targetInfos", [])
                if t.get("type") == "page"
            ]
        except Exception:
            return [f"CDwindow-{self._current_target_id}"]

    @property
    def current_window_handle(self) -> str:
        return f"CDwindow-{self._current_target_id}"

    # ─── Выполнение JavaScript и поиск элементов ──────────────────────────────

    def _get_window_object_id(self) -> str:
        if self._current_frame_object_id:
            try:
                res = self.execute_cdp_cmd(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": self._current_frame_object_id,
                        "functionDeclaration": "function() { return this.contentWindow || window; }",
                        "returnByValue": False,
                    },
                )
                oid = res.get("result", {}).get("objectId")
                if oid:
                    return oid
            except Exception:
                self._current_frame_object_id = None

        res = self.execute_cdp_cmd(
            "Runtime.evaluate",
            {"expression": "window", "returnByValue": False},
        )
        return res["result"]["objectId"]

    def execute_script(self, script: str, *args: Any) -> Any:
        win_obj_id = self._get_window_object_id()
        cdp_args = []
        for a in args:
            if isinstance(a, CDPElement):
                cdp_args.append({"objectId": a.object_id})
            else:
                cdp_args.append({"value": a})

        wrapped_fn = f"""
        function() {{
            const __userFn = function() {{
                {script}
            }};
            const __res = __userFn.apply(window, arguments);
            if (__res instanceof Node || __res instanceof Window) {{
                return null;
            }}
            return __res;
        }}
        """

        res = self.execute_cdp_cmd(
            "Runtime.callFunctionOn",
            {
                "objectId": win_obj_id,
                "functionDeclaration": wrapped_fn,
                "arguments": cdp_args,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"]
            text = exc.get("exception", {}).get("description") or exc.get("text", "JS Error")
            raise WebDriverException(f"Ошибка execute_script: {text}")

        return res.get("result", {}).get("value")

    def _find_elements_internal(
        self,
        by: str,
        value: str,
        root_object_id: str | None = None,
    ) -> list[CDPElement]:
        target_obj_id = root_object_id or self._get_window_object_id()

        finder_fn = """
        function(by, selector) {
            let root = document;
            if (this && this.nodeType === 1) {
                if (this.tagName === 'IFRAME' && this.contentDocument) {
                    root = this.contentDocument;
                } else {
                    root = this;
                }
            } else if (this && this.document) {
                root = this.document;
            }
            const doc = root.ownerDocument || root;
            let nodes = [];
            try {
                if (by === 'xpath') {
                    const snap = doc.evaluate(
                        selector,
                        root,
                        null,
                        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                        null
                    );
                    for (let i = 0; i < snap.snapshotLength; i++) {
                        nodes.push(snap.snapshotItem(i));
                    }
                } else if (by === 'css selector') {
                    nodes = Array.from(root.querySelectorAll(selector));
                } else if (by === 'tag name') {
                    nodes = Array.from(root.getElementsByTagName(selector));
                } else if (by === 'id') {
                    const el = doc.getElementById(selector);
                    if (el && (root === doc || root.contains(el))) nodes = [el];
                } else if (by === 'name') {
                    nodes = Array.from(root.querySelectorAll('[name=' + JSON.stringify(selector) + ']'));
                } else if (by === 'class name') {
                    nodes = Array.from(root.getElementsByClassName(selector));
                }
            } catch (e) {
                return [];
            }
            return nodes;
        }
        """

        res = self.execute_cdp_cmd(
            "Runtime.callFunctionOn",
            {
                "objectId": target_obj_id,
                "functionDeclaration": finder_fn,
                "arguments": [{"value": by}, {"value": value}],
                "returnByValue": False,
            },
        )
        arr_obj_id = res.get("result", {}).get("objectId")
        if not arr_obj_id:
            return []

        props = self.execute_cdp_cmd(
            "Runtime.getProperties",
            {"objectId": arr_obj_id, "ownProperties": True},
        )
        elements: list[CDPElement] = []
        for p in props.get("result", []):
            name = p.get("name", "")
            if name.isdigit():
                val = p.get("value", {})
                oid = val.get("objectId")
                if oid:
                    elements.append(CDPElement(self, oid))
        return elements

    def find_element(self, by: str = By.ID, value: str = "") -> CDPElement:
        els = self._find_elements_internal(by, value)
        if not els:
            raise NoSuchElementException(f"Элемент не найден: {by}={value}")
        return els[0]

    def find_elements(self, by: str = By.ID, value: str = "") -> list[CDPElement]:
        return self._find_elements_internal(by, value)

    # ─── Завершение работы ────────────────────────────────────────────────────

    def close(self) -> None:
        if self._current_target_id:
            try:
                self.execute_cdp_cmd(
                    "Target.closeTarget", {"targetId": self._current_target_id}
                )
            except Exception:
                pass

    def quit(self) -> None:
        if self._ws:
            try:
                self.execute_cdp_cmd("Browser.close", {})
            except Exception:
                pass
            self._ws.close()
            self._ws = None

        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        if self._user_data_dir and os.path.exists(self._user_data_dir):
            shutil.rmtree(self._user_data_dir, ignore_errors=True)
