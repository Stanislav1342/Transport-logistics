#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
outlook_bridge.py — Легковесный локальный мост к Microsoft Outlook (для ПК сотрудников).

Запускается на компьютере сотрудника (можно прямо с сетевого диска L:) и принимает
готовые сформированные письма и файлы заявок (.docx) от центрального сервера портала
«ПУЛЬС — Транспортная логистика», чтобы:
- Создавать черновики в папке «Черновики» личного Outlook сотрудника (mode='draft')
- Или сразу отправлять письма с личной почты сотрудника (mode='send')

Скрипт не содержит бизнес-логики и НЕ требует обновлений при изменении портала.
"""

import base64
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765


class OutlookBridgeHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self, status_code: int = 200) -> None:
        self.send_response(status_code)
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self._send_cors_headers(200)

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._send_cors_headers(200)
            self.wfile.write(json.dumps({"status": "ok", "bridge": "active"}).encode("utf-8"))
        else:
            self._send_cors_headers(404)
            self.wfile.write(json.dumps({"status": "not_found"}).encode("utf-8"))

    def do_POST(self) -> None:
        if self.path != "/send":
            self._send_cors_headers(404)
            self.wfile.write(json.dumps({"status": "error", "message": "Неизвестный путь"}).encode("utf-8"))
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw_body)

            mode = payload.get("mode", "draft")
            emails = payload.get("emails", [])
            draft_mode = (mode == "draft")

            import pythoncom
            import win32com.client as win32

            pythoncom.CoInitialize()
            outlook = win32.Dispatch("outlook.application")

            sent_count = 0
            temp_files_to_clean = []

            for item in emails:
                mail = outlook.CreateItem(0)
                mail.To = item.get("to", "")
                mail.Subject = item.get("subject", "")
                mail.HTMLBody = item.get("html_body", "")

                for att in item.get("attachments", []):
                    net_path = att.get("network_path", "")
                    b64_data = att.get("content_b64", "")
                    fname = os.path.basename(att.get("filename", "attachment.docx"))

                    if net_path and os.path.exists(net_path):
                        mail.Attachments.Add(os.path.abspath(net_path))
                    elif b64_data:
                        tmp_dir = tempfile.mkdtemp(prefix="puls_att_")
                        tmp_path = os.path.join(tmp_dir, fname)
                        with open(tmp_path, "wb") as f:
                            f.write(base64.b64decode(b64_data))
                        mail.Attachments.Add(os.path.abspath(tmp_path))
                        temp_files_to_clean.append(tmp_path)

                if draft_mode:
                    mail.Save()
                else:
                    mail.Send()
                sent_count += 1

            action_text = "сохранено в Черновиках вашего Outlook" if draft_mode else "отправлено с вашей почты Outlook"
            self._send_cors_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "status": "success",
                        "count": sent_count,
                        "message": f"Успешно {action_text}: {sent_count} шт.",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except ImportError:
            self._send_cors_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "status": "error",
                        "message": "На этом компьютере не установлен pywin32 или Microsoft Outlook.",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except Exception as e:
            self._send_cors_headers(200)
            self.wfile.write(
                json.dumps(
                    {"status": "error", "message": f"Ошибка локального Outlook: {e}"},
                    ensure_ascii=False,
                ).encode("utf-8")
            )

    def log_message(self, format: str, *args) -> None:
        # Отключаем лишний шум в консоли
        pass


if __name__ == "__main__":
    print("=" * 64)
    print("  ✉️  МОСТ OUTLOOK (ПУЛЬС — ТРАНСПОРТНАЯ ЛОГИСТИКА) ЗАПУЩЕН")
    print(f"  • Слушает локальный порт: http://127.0.0.1:{PORT}")
    print("  • Письма и черновики будут создаваться в вашем личном Outlook.")
    print("=" * 64)
    server = HTTPServer(("127.0.0.1", PORT), OutlookBridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка моста Outlook.")
