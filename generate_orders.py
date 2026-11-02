#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_orders.py — Модуль формирования транспортных заявок для перевозчиков.

Логика:
1. Загрузка исходного реестра со столбцами:
   Адрес ссылка, Адрес, Количество паллет, Режим термоперевозки, Дата отгрузки, Перевозчик.
2. Формирование раздельных заявок для каждого клиента (Адрес ссылка) у каждого перевозчика.
   Если у одного перевозчика несколько строк с одинаковым 'Адрес ссылка', количество паллет суммируется.
3. Расчет количества требуемых транспортных средств:
   - до 33 паллет = 1 машина;
   - от 34 до 66 = 2 машины;
   - от 67 до 99 = 3 машины и т.д. (каждые 33 паллета сверху добавляют машину).
4. Формирование строки Маршрут: <город/деревня погрузки> – д. Черная Грязь.
5. Заполнение Word-шаблона 'Шаблон.docx' для каждого заказа:
   - Кому: Перевозчик
   - Дата: текущая дата (например, 30 сентября 2026)
   - Маршрут: <город/деревня> – д. Черная Грязь
   - Тип транспортного средства: <N> х <кол-во> паллет, РЕФ <режим>
   - Адрес погрузки: Адрес ссылка, Адрес
   - Дата и время погрузки: Дата отгрузки к 9:00
   - Дата и время выгрузки: Дата отгрузки, по прибытию
6. Сохранение каждого файла в папку Заявки:
   Основной путь: L:\\ОТДЕЛЫ\\ТРАНСПОРТНЫЙ ОТДЕЛ\\Рожков\\РК недельные графики + заявки\\Заявки
   Формат имени: <Перевозчик> (<Адрес ссылка>) <ДД.ММ>.docx
7. Подготовка и отправка писем с вложенными заявками через Outlook.
"""

import os
import sys
import re
import math
import datetime
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RUSSIAN_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

def get_orders_dir():
    """
    Возвращает путь к целевой папке 'Заявки'.
    Приоритет: сетевой/облачный диск L:
    L:\\ОТДЕЛЫ\\ТРАНСПОРТНЫЙ ОТДЕЛ\\Рожков\\РК недельные графики + заявки\\Заявки
    Резерв: Рабочий стол / Заявки или локальная папка.
    """
    network_base = r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки\Заявки"
    try:
        drive = os.path.splitdrive(network_base)[0]
        if drive and os.path.exists(drive + "\\"):
            os.makedirs(network_base, exist_ok=True)
            return network_base
        elif os.path.exists(network_base):
            return network_base
    except Exception:
        pass

    user_home = os.path.expanduser("~")
    desktop_candidates = [
        os.path.join(user_home, "OneDrive", "Рабочий стол"),
        os.path.join(user_home, "OneDrive", "Desktop"),
        os.path.join(user_home, "Рабочий стол"),
        os.path.join(user_home, "Desktop"),
    ]
    for d in desktop_candidates:
        if os.path.exists(d):
            folder = os.path.join(d, "Заявки")
            os.makedirs(folder, exist_ok=True)
            return folder

    fallback = os.path.join(BASE_DIR, "Заявки")
    os.makedirs(fallback, exist_ok=True)
    return fallback

def sanitize_filename(name: str) -> str:
    """Очищает строку от запрещенных символов в именах файлов ОС Windows и Mac"""
    for ch in ['"', '<', '>', ':', '/', '\\', '|', '?', '*']:
        name = name.replace(ch, '')
    return re.sub(r'\s+', ' ', name).strip()

def format_pallets(val) -> str:
    """Форматирует число паллет (20.0 -> '20', 20.8 -> '20.8')"""
    try:
        f = float(val)
        if f.is_integer():
            return str(int(f))
        return f"{f:g}"
    except (ValueError, TypeError):
        return str(val) if val is not None else "0"

def get_russian_date_str(dt: datetime.date = None) -> str:
    """Возвращает дату в формате '30 сентября 2026'"""
    if dt is None:
        dt = datetime.date.today()
    month_name = RUSSIAN_MONTHS[dt.month - 1]
    return f"{dt.day} {month_name} {dt.year}"

def clean_client_name(name: str) -> str:
    """Очищает наименование клиента от кавычек и форм собственности для надежного поиска в таблице"""
    if not name:
        return ""
    s = str(name).lower()
    for ch in ['"', "'", '«', '»', '(', ')', ',', '.', '-', '+']:
        s = s.replace(ch, ' ')
    words = [w.strip() for w in s.split() if w.strip()]
    stop_words = {'ооо', 'ао', 'зао', 'пао', 'ип', 'тк', 'оао'}
    filtered = [w for w in words if w not in stop_words]
    return ' '.join(filtered)

def find_client_address(client_name: str, client_address_map: dict) -> str:
    """
    Ищет адрес клиента в словаре адресов:
    1. Точное совпадение
    2. Регистронезависимое совпадение
    3. Очищенное сравнение (без кавычек и ООО/ИП)
    4. Вхождение подстроки
    """
    if not client_name or not client_address_map:
        return ""
    raw_target = str(client_name).strip()
    clean_target = clean_client_name(raw_target)
    
    # 1 & 2. Точное и регистронезависимое совпадение
    for k, addr in client_address_map.items():
        if k.strip().lower() == raw_target.lower() and addr:
            return addr.strip()
            
    # 3. Нормализованное совпадение
    if clean_target:
        for k, addr in client_address_map.items():
            if clean_client_name(k) == clean_target and addr:
                return addr.strip()
                
    # 4. Вхождение ключевого названия
    if clean_target and len(clean_target) >= 4:
        for k, addr in client_address_map.items():
            ck = clean_client_name(k)
            if (clean_target in ck or ck in clean_target) and addr:
                return addr.strip()
                
    return ""

def extract_settlement(addr: str) -> str:
    """
    Извлекает название города / деревни / села / пгт из полного адреса для строки 'Маршрут:'.
    Примеры:
    - 'Московская обл, Пушкинский р-н, Тарасовка с, ...' -> 'с. Тарасовка'
    - '141280, Московская область, г.о. Пушкинский, ...' -> 'г.о. Пушкинский'
    - '... деревня Давыдовское, ...' -> 'д. Давыдовское'
    - '143581, ... д Лешково, стр. 244' -> 'д. Лешково'
    - '141400 Московская область, г. Химки, ...' -> 'г. Химки'
    - '143080, ..., пгт. Лесной Городок, ...' -> 'пгт. Лесной Городок'
    """
    if not addr:
        return ""

    # 1. Городской округ: 'г.о. Пушкинский', 'г.о. Истра', 'г.о. Домодедово'
    m = re.search(r'\bг\.?о\.?\s+([А-Яа-яЁё\-]+)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        return f"г.о. {val}"

    # 2. Город: 'г. Химки', 'г Пушкино', 'г. Домодедово', 'Санкт-Петербург г'
    m = re.search(r'(?:^|[\s,])г\.(?!о\b)\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if not val.lower().startswith('о '):
            return f"г. {val}"

    m = re.search(r'(?:^|[\s,])г\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ('о', 'о.'):
            return f"г. {val}"

    m = re.search(r'\b([А-Яа-яЁё\-]+)\s+г\b', addr, re.IGNORECASE)
    if m:
        return f"г. {m.group(1).strip()}"

    # 3. Село: 'село Лешково', 'Тарасовка с', 'с. Тарасовка'
    m = re.search(r'\bсело\s+([А-Яа-яЁё\-]+)', addr, re.IGNORECASE)
    if m:
        return f"с. {m.group(1).strip()}"
    m = re.search(r'\b([А-Яа-яЁё\-]+)\s+с(?:,|\b)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ('московская', 'обл', 'область', 'р-н', 'район'):
            return f"с. {val}"
    m = re.search(r'(?:^|[\s,])с\.\s*([А-Яа-яЁё\-]+)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ('московская', 'обл', 'область', 'р-н', 'район'):
            return f"с. {val}"

    # 4. Деревня: 'деревня Давыдовское', 'д Новоселки', 'д. Лешково'
    m = re.search(r'\bдеревня\s+([А-Яа-яЁё\-]+)', addr, re.IGNORECASE)
    if m:
        return f"д. {m.group(1).strip()}"

    house_words = {'стр', 'строение', 'корп', 'корпус', 'вл', 'влд', 'владение', 'к', 'уч', 'участок', 'поз', 'пом', 'лит', 'литер'}
    m = re.search(r'(?:^|[\s,])д\.?\s+([А-Яа-яЁё\-]{3,})(?!\s*\d)', addr, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in house_words:
            return f"д. {val}"

    # 5. пгт / поселок
    m = re.search(r'\bпгт\.?\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)', addr, re.IGNORECASE)
    if m:
        return f"пгт. {m.group(1).strip()}"
    m = re.search(r'\bпос(?:елок|\.)?\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)', addr, re.IGNORECASE)
    if m:
        return f"пос. {m.group(1).strip()}"

    parts = [p.strip() for p in addr.split(',') if p.strip()]
    for p in parts:
        if not re.match(r'^\d+$', p) and 'область' not in p.lower() and 'обл' not in p.lower():
            return p
    return addr

def parse_orders_excel(input_path: str):
    """
    Считывает строки из файла отгрузок и формирует список строк с поддержкой формул Excel и карты адресов.
    Возвращает: (raw_items, client_address_map)
    """
    wb_data = openpyxl.load_workbook(input_path, data_only=True)
    wb_form = openpyxl.load_workbook(input_path, data_only=False)
    ws_data = wb_data.active
    ws_form = wb_form.active

    header = [str(ws_data.cell(1, c).value or '').strip() for c in range(1, ws_data.max_column + 1)]
    def find_idx(candidates, exclude_idx=-1):
        for cand in candidates:
            for i, h in enumerate(header):
                if i != exclude_idx and cand.lower() in h.lower():
                    return i
        return -1

    idx_ref = find_idx(["адрес ссылка", "ссылка", "клиент"])
    idx_addr = find_idx(["адрес"], exclude_idx=idx_ref)
    idx_pal = find_idx(["паллет", "кол-во", "количество"])
    idx_temp = find_idx(["режим", "термо"])
    idx_date = find_idx(["дата отгрузки", "дата"])
    idx_carrier = find_idx(["перевозчик", "подрядчик", "компания"])
    if idx_carrier == -1 and len(header) >= 6:
        idx_carrier = 5

    raw_items = []
    client_address_map = {}

    for r in range(2, ws_data.max_row + 1):
        carrier_val = str(ws_data.cell(r, idx_carrier + 1).value or '').strip() if idx_carrier >= 0 else ''
        if not carrier_val:
            carrier_val = "Неизвестный перевозчик"

        pal_cell = ws_data.cell(r, idx_pal + 1).value if idx_pal >= 0 else None
        try:
            pal_val = float(pal_cell) if pal_cell is not None else 0.0
        except (ValueError, TypeError):
            pal_val = 0.0

        date_val = ws_data.cell(r, idx_date + 1).value if idx_date >= 0 else None
        if isinstance(date_val, (datetime.date, datetime.datetime)):
            date_str = date_val.strftime("%d.%m.%Y")
        elif date_val:
            date_str = str(date_val).strip()
        else:
            date_str = datetime.date.today().strftime("%d.%m.%Y")

        c_form = str(ws_form.cell(r, idx_ref + 1).value or '').strip() if idx_ref >= 0 else ''
        c_data = str(ws_data.cell(r, idx_ref + 1).value or '').strip() if idx_ref >= 0 else ''
        addr_str = str(ws_data.cell(r, idx_addr + 1).value or '').strip() if idx_addr >= 0 else ''
        temp_str = str(ws_data.cell(r, idx_temp + 1).value or '').strip() if idx_temp >= 0 else '+15+25'
        if not temp_str:
            temp_str = '+15+25'

        if not carrier_val and not addr_str and pal_val == 0 and not c_data and not c_form:
            continue
        if carrier_val == "Неизвестный перевозчик" and not addr_str and pal_val == 0 and not c_data:
            continue

        raw_cell = c_form if c_form else c_data
        is_formula = raw_cell.startswith('=')

        raw_no_quotes = re.sub(r'"[^"]*"', '', raw_cell)
        cell_refs = [int(m) for m in re.findall(r'\b[A-Za-z]+\$?(\d+)\b', raw_no_quotes)]
        if not cell_refs and '+' in raw_cell:
            cell_refs = [int(m) for m in re.findall(r'[+]\s*(?:[A-Za-z]+\$?)?(\d+)\b', raw_no_quotes)]
        donor_rows = [row_idx for row_idx in cell_refs if row_idx != r]

        base_client = ""
        donor_client_names = []

        if is_formula:
            str_literals = re.findall(r'"([^"]+)"', raw_cell)
            clean_literals = [s.replace('+', '').strip() for s in str_literals if s.replace('+', '').strip()]
            if clean_literals:
                base_client = clean_literals[0]
            elif cell_refs:
                first_ref = cell_refs[0]
                val = ws_data.cell(first_ref, idx_ref + 1).value if ws_data else None
                base_client = str(val or '').strip()
            elif c_data:
                base_client = c_data.split('+')[0].strip()
            else:
                base_client = raw_cell.strip('=" ')
        else:
            if '+' in raw_cell:
                parts = [p.strip() for p in raw_cell.split('+') if p.strip()]
                base_client = parts[0]
                if not donor_rows and len(parts) > 1:
                    donor_client_names = parts[1:]
            else:
                base_client = raw_cell

        if not base_client and c_data:
            base_client = c_data

        if base_client and addr_str and '+' not in base_client:
            if base_client not in client_address_map:
                client_address_map[base_client] = addr_str

        raw_items.append({
            "row_num": r,
            "carrier": carrier_val,
            "raw_cell": raw_cell,
            "addr_ref": base_client,
            "base_client": base_client,
            "donor_row_nums": donor_rows,
            "donor_client_names": donor_client_names,
            "addr": addr_str,
            "pallets": pal_val,
            "temp": temp_str,
            "date": date_str,
            "is_donor": False
        })

    wb_data.close()
    wb_form.close()
    return raw_items, client_address_map

def aggregate_orders(raw_items: list, client_address_map: dict = None) -> list:
    """
    Группирует данные по паре (Перевозчик, Базовый клиент).
    Для каждой пары суммирует паллеты.
    Поддерживает склеивание заказов (перемещение):
    - если в строке указана формула со ссылками на строки (A28, A27) или клиенты через +,
      заказ-донор ПОЛНОСТЬЮ перемещается в целевой заказ (в отдельную заявку не идет);
    - подтягиваются адреса и паллеты всех склеенных строк;
    - формируется составной адрес погрузки с пунктами 1), 2), 3);
    - формируется составной маршрут через все точки;
    - паллеты суммируются;
    - количество машин определяется как ceil(pallets / 33.0).
    """
    if client_address_map is None:
        client_address_map = {}

    rows_by_id = {it["row_num"]: it for it in raw_items}

    # 1. Если указаны текстовые имена доноров (без номеров строк), находим соответствующие строки
    for it in raw_items:
        if not it["donor_row_nums"] and it.get("donor_client_names"):
            for p_name in it["donor_client_names"]:
                clean_p = clean_client_name(p_name)
                for cand_row, cand_it in rows_by_id.items():
                    if cand_row != it["row_num"] and not cand_it.get("is_donor"):
                        if clean_client_name(cand_it["base_client"]) == clean_p:
                            it["donor_row_nums"].append(cand_row)
                            break

    # 2. Помечаем все строки-доноры: они полностью перемещены и исключаются из отдельных заявок
    for it in raw_items:
        for d_num in it.get("donor_row_nums", []):
            if d_num in rows_by_id:
                rows_by_id[d_num]["is_donor"] = True

    # 3. Группируем по (Перевозчик, Базовый клиент)
    groups = defaultdict(lambda: {
        "carrier": "",
        "base_client": "",
        "rows": [],
        "donors": [],
        "pallets": 0.0,
        "temps": set(),
        "dates": set()
    })

    for it in raw_items:
        if it.get("is_donor"):
            # Заказ перемещен в другой заказ — отдельная заявка не формируется!
            continue

        key = (it["carrier"], it["base_client"])
        g = groups[key]
        g["carrier"] = it["carrier"]
        g["base_client"] = it["base_client"]
        g["rows"].append(it)
        g["pallets"] += it["pallets"]
        if it["temp"]:
            g["temps"].add(it["temp"])
        if it["date"]:
            g["dates"].add(it["date"])

        for d_num in it.get("donor_row_nums", []):
            if d_num in rows_by_id:
                d_row = rows_by_id[d_num]
                g["donors"].append(d_row)
                g["pallets"] += d_row["pallets"]
                if d_row["temp"]:
                    g["temps"].add(d_row["temp"])
                if d_row["date"]:
                    g["dates"].add(d_row["date"])

    aggregated = []
    for (carrier, b_client), g in groups.items():
        pallets = g["pallets"]
        trucks = max(1, math.ceil(pallets / 33.0)) if pallets > 0 else 1
        temp_val = ", ".join(sorted(g["temps"])) if g["temps"] else "+15+25"
        date_val = sorted(list(g["dates"]))[0] if g["dates"] else datetime.date.today().strftime("%d.%m.%Y")

        # Приоритетный адрес для основного клиента: из строки с формулой/донорами, либо из последней строки
        main_addr = ""
        for rw in g["rows"]:
            if rw.get("donor_row_nums") and rw["addr"]:
                main_addr = rw["addr"]
                break
        if not main_addr:
            for rw in reversed(g["rows"]):
                if rw["addr"]:
                    main_addr = rw["addr"]
                    break
        if not main_addr:
            main_addr = find_client_address(b_client, client_address_map)

        client_items = [{
            "client": b_client,
            "addr": main_addr,
            "pallets": sum(rw["pallets"] for rw in g["rows"])
        }]

        # Добавляем всех доноров
        for d_row in g["donors"]:
            d_client = d_row["base_client"]
            d_addr = d_row["addr"] or find_client_address(d_client, client_address_map)
            client_items.append({
                "client": d_client,
                "addr": d_addr,
                "pallets": d_row["pallets"]
            })

        is_multi_client = len(client_items) > 1

        if is_multi_client:
            combined_ref = " + ".join(ci["client"] for ci in client_items)
            load_addr_lines = []
            for idx, ci in enumerate(client_items):
                c_name = ci["client"]
                c_addr = ci["addr"]
                clean_c = clean_client_name(c_name)
                clean_a = clean_client_name(c_addr[:len(c_name)+15])
                if clean_c and clean_c in clean_a:
                    line_text = c_addr
                else:
                    line_text = f"{c_name}, {c_addr}" if c_addr else c_name
                load_addr_lines.append(f"{idx+1}) {line_text}")

            display_load_addr = "\n  " + "\n  ".join(load_addr_lines)

            settlements = []
            for ci in client_items:
                s = extract_settlement(ci["addr"])
                if s and s not in settlements:
                    settlements.append(s)
            route_from = " – ".join(settlements) if settlements else "г. Москва"
            route_val = f"{route_from} – д. Черная Грязь"
        else:
            combined_ref = b_client
            display_load_addr = f"{b_client}, {main_addr}" if main_addr else b_client
            s = extract_settlement(main_addr)
            route_from = s if s else "г. Москва"
            route_val = f"{route_from} – д. Черная Грязь"

        aggregated.append({
            "carrier": carrier,
            "addr_ref": combined_ref,
            "base_client": b_client,
            "addr": main_addr,
            "is_multi_client": is_multi_client,
            "client_items": client_items,
            "display_load_addr": display_load_addr,
            "route_value": route_val,
            "pallets": pallets,
            "pallets_str": format_pallets(pallets),
            "temp": temp_val,
            "date": date_val,
            "all_dates": sorted(list(g["dates"])),
            "trucks": trucks
        })

    # Сортировка по перевозчику, затем по клиенту
    aggregated.sort(key=lambda x: (x["carrier"], x["addr_ref"]))
    return aggregated

def save_aggregated_excel(aggregated_orders: list, output_path: str):
    """
    Формирует и сохраняет сводный Excel-файл со всеми заказами.
    Колонки: Адрес ссылка, Адрес, Количество паллет, Режим термоперевозки, Дата отгрузки, Перевозчик
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка заявок"

    headers = [
        "Адрес ссылка",
        "Адрес",
        "Количество паллет",
        "Режим термоперевозки",
        "Дата отгрузки",
        "Перевозчик"
    ]
    ws.append(headers)

    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    align_right = Alignment(horizontal="right", vertical="center")
    
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center
        cell.border = thin_border
    ws.row_dimensions[1].height = 28

    current_row = 2
    for it in aggregated_orders:
        addr_for_excel = it["addr"]
        if it.get("is_multi_client") and it.get("client_items"):
            addr_for_excel = "\n".join(f"{idx+1}) {ci['client']}: {ci['addr']}" for idx, ci in enumerate(it["client_items"]))

        ws.append([
            it["addr_ref"],
            addr_for_excel,
            it["pallets"],
            it["temp"],
            it["date"],
            it["carrier"]
        ])
        
        row_cells = ws[current_row]
        for idx, c in enumerate(row_cells):
            c.border = thin_border
            c.font = Font(name="Arial", size=10)
            if idx in (0, 1, 5):
                c.alignment = align_left
            elif idx in (2,):
                c.alignment = align_right
                c.number_format = '#,##0.#'
            else:
                c.alignment = align_center
        ws.row_dimensions[current_row].height = 22
        current_row += 1

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 50)

    wb.save(output_path)

def fill_order_docx(
    template_path: str,
    carrier: str,
    addr_ref: str,
    addr: str,
    pallets: float,
    trucks: int,
    temp: str,
    date_ship: str,
    output_docx_path: str,
    today_date_str: str = None,
    load_address_value: str = None,
    route_value: str = None
):
    """
    Заполняет Шаблон.docx для конкретного заказа:
    - Кому: Перевозчик
    - Дата: сегодняшняя дата
    - Маршрут: <город/деревня> – д. Черная Грязь (или список точек)
    - Тип транспортного средства: <N> х <кол-во> паллет, РЕФ <режим>
    - Адрес погрузки: Адрес ссылка, Адрес (или нумерованный список при нескольких клиентах)
    - Дата и время погрузки: Дата отгрузки к 9:00
    - Дата и время выгрузки: Дата отгрузки, по прибытию
    """
    if today_date_str is None:
        today_date_str = get_russian_date_str()

    doc = docx.Document(template_path)
    type_ts_value = f"{trucks} х 33 паллет, РЕФ {temp}"

    if not route_value:
        settlement = extract_settlement(addr)
        route_from = settlement if settlement else "г. Москва"
        route_value = f"{route_from} – д. Черная Грязь"

    if not load_address_value:
        load_address_value = f"{addr_ref}, {addr}" if addr else addr_ref

    load_datetime_value = f"{date_ship} к 9:00"
    unload_datetime_value = f"{date_ship}, по прибытию"

    def set_field(paragraph, label_bold: str, val_text: str, prefix_spaces: str = "", align=None):
        paragraph.text = ""
        if prefix_spaces:
            r_pre = paragraph.add_run(prefix_spaces)
            r_pre.bold = True
            r_pre.font.name = "Arial"
        r_lbl = paragraph.add_run(label_bold)
        r_lbl.bold = True
        r_lbl.font.name = "Arial"
        r_val = paragraph.add_run(val_text)
        r_val.bold = False
        r_val.font.name = "Arial"
        if align is not None:
            paragraph.alignment = align

    for p in doc.paragraphs:
        txt = p.text.strip()
        if "Дата:" in txt and any(y in txt for y in ["августа", "202", "2025", "2026"]):
            # Шапка Дата
            set_field(p, "Дата: ", today_date_str, prefix_spaces="                                                                                                                ")
        elif txt.startswith("Кому:"):
            # Шапка Кому
            carrier_clean = str(carrier or "").strip()
            carrier_norm = carrier_clean.lower().replace("ё", "е")
            if "дергачев" in carrier_norm:
                set_field(p, "Кому:", "_____________", align=WD_ALIGN_PARAGRAPH.RIGHT)
            else:
                set_field(p, "Кому: ", carrier_clean, align=WD_ALIGN_PARAGRAPH.RIGHT)
        elif txt.startswith("Маршрут:"):
            # Маршрут
            set_field(p, "Маршрут: ", route_value)
        elif txt.startswith("Тип транспортного средства:"):
            # Тип транспортного средства
            set_field(p, "Тип транспортного средства: ", type_ts_value)
        elif txt.startswith("Адрес погрузки:"):
            # Адрес погрузки
            if load_address_value.startswith("\n"):
                set_field(p, "Адрес погрузки:", load_address_value)
            else:
                set_field(p, "Адрес погрузки: ", load_address_value)
        elif txt.startswith("Дата и время погрузки:"):
            # Дата и время погрузки
            set_field(p, "Дата и время погрузки: ", load_datetime_value)
        elif txt.startswith("Дата и время выгрузки:"):
            # Дата и время выгрузки
            set_field(p, "Дата и время выгрузки: ", unload_datetime_value)

    doc.save(output_docx_path)

def process_daily_orders(
    input_excel_path: str,
    template_docx_path: str = None,
    output_dir: str = None
) -> dict:
    """
    Основная точка входа для генерации заявок:
    - Считывает Excel
    - Группирует по (Перевозчик, Адрес ссылка)
    - Создает отдельный документ Word для каждого клиента каждого перевозчика
    - Создает сводный Excel-файл
    - Сохраняет все файлы в output_dir
    """
    if template_docx_path is None:
        template_docx_path = os.path.join(BASE_DIR, "Шаблон.docx")
    if output_dir is None:
        output_dir = get_orders_dir()

    os.makedirs(output_dir, exist_ok=True)

    # 1. Чтение файла и сбор карты адресов клиентов
    raw_items, client_address_map = parse_orders_excel(input_excel_path)
    if not raw_items:
        raise ValueError("В загруженном файле нет данных для формирования заявок")

    # 2. Агрегация по (Перевозчик, Адрес ссылка) с поддержкой составных рейсов (+)
    aggregated_orders = aggregate_orders(raw_items, client_address_map)

    # Определение даты отгрузки (ДД.ММ)
    all_dates = set()
    for it in aggregated_orders:
        for d in it.get("all_dates", []):
            all_dates.add(d)

    date_tag = ""
    primary_date = ""
    if all_dates:
        primary_date = sorted(list(all_dates))[0]
        parts = primary_date.split(".")
        if len(parts) >= 2:
            date_tag = f"{parts[0]}.{parts[1]}"
    if not date_tag:
        date_tag = datetime.date.today().strftime("%d.%m")
    if not primary_date:
        primary_date = datetime.date.today().strftime("%d.%m.%Y")

    # 3. Сохранение сводной таблицы Excel
    summary_excel_name = f"Сводный реестр заявок ({date_tag}).xlsx"
    summary_excel_path = os.path.join(output_dir, summary_excel_name)
    save_aggregated_excel(aggregated_orders, summary_excel_path)

    # 4. Заполнение Word заявок
    generated_docs = []
    total_pallets_all = 0.0
    today_str = get_russian_date_str()

    carrier_orders_map = defaultdict(list)

    for it in aggregated_orders:
        carrier = it["carrier"]
        addr_ref = it["addr_ref"]
        addr = it["addr"]
        pallets = it["pallets"]
        trucks = it["trucks"]
        temp = it["temp"]
        date_ship = it["date"]
        route_str = it.get("route_value") or (f"{extract_settlement(addr)} – д. Черная Грязь" if addr else "д. Черная Грязь")

        total_pallets_all += pallets

        # Имя файла: Перевозчик (Адрес ссылка) ДД.ММ.docx
        clean_carrier = sanitize_filename(carrier)
        clean_ref = sanitize_filename(addr_ref)
        if len(clean_ref) > 80:
            clean_ref = clean_ref[:80].strip()
        if clean_ref:
            docx_filename = f"{clean_carrier} ({clean_ref}) {date_tag}.docx"
        else:
            docx_filename = f"{clean_carrier} {date_tag}.docx"
            
        docx_path = os.path.join(output_dir, docx_filename)

        fill_order_docx(
            template_path=template_docx_path,
            carrier=carrier,
            addr_ref=addr_ref,
            addr=addr,
            pallets=pallets,
            trucks=trucks,
            temp=temp,
            date_ship=date_ship,
            output_docx_path=docx_path,
            today_date_str=today_str,
            load_address_value=it.get("display_load_addr"),
            route_value=route_str
        )

        doc_info = {
            "carrier": carrier,
            "addr_ref": addr_ref,
            "addr": addr,
            "display_load_addr": it.get("display_load_addr"),
            "filename": docx_filename,
            "path": docx_path,
            "trucks": trucks,
            "route": route_str,
            "pallets": pallets,
            "pallets_str": format_pallets(pallets),
            "temp": temp,
            "date": date_ship,
            "is_multi_client": it.get("is_multi_client", False)
        }
        generated_docs.append(doc_info)
        carrier_orders_map[carrier].append(doc_info)

    # Список уникальных перевозчиков со статистикой для интерфейса
    carriers_summary = []
    for c_name, c_orders in sorted(carrier_orders_map.items()):
        c_pallets = sum(o["pallets"] for o in c_orders)
        carriers_summary.append({
            "name": c_name,
            "orders_count": len(c_orders),
            "total_pallets": c_pallets,
            "pallets_str": format_pallets(c_pallets),
            "files": [o["filename"] for o in c_orders]
        })

    return {
        "status": "success",
        "output_dir": output_dir,
        "date_tag": date_tag,
        "primary_date": primary_date,
        "total_carriers": len(carriers_summary),
        "total_orders": len(generated_docs),
        "total_pallets": format_pallets(total_pallets_all),
        "summary_excel": summary_excel_name,
        "carriers": carriers_summary,
        "orders": generated_docs
    }

def generate_order_email_html(carrier: str, orders: list, date_str: str) -> str:
    """
    Генерирует HTML-письмо для отправки заявок конкретному перевозчику.
    """
    rows_html = ""
    for o in orders:
        rows_html += f"""
        <tr>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px; font-weight: 700; color: #1e3a8a;">{o.get('addr_ref', '')}</td>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: center; font-weight: 700; color: #174ea6;">{o.get('trucks', 1)}</td>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: right; font-weight: 700;">{o.get('pallets_str', '')}</td>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: center;">{o.get('temp', '+15+25')}</td>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px;">{o.get('route', '')}</td>
            <td style="border: 1px solid #cbd5e1; padding: 9px 12px; font-size: 11px; color: #475569;">{o.get('addr', '')}</td>
        </tr>
        """

    html = f"""
    <div style="font-family: Arial, sans-serif; font-size: 14px; color: #1e293b; line-height: 1.5;">
        <p>Добрый день!</p>
        <p>Заявки на перевозку во вложении.</p>
        
        <table style="border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 13px;">
            <tr style="background: #f1f5f9; color: #334155;">
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: left;">Клиент / Точка</th>
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: center;">Машин</th>
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: right;">Паллеты</th>
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: center;">Режим</th>
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: left;">Маршрут</th>
                <th style="border: 1px solid #cbd5e1; padding: 9px 12px; text-align: left;">Адрес погрузки</th>
            </tr>
            {rows_html}
        </table>
        
        <p><b>Дата и время погрузки:</b> {date_str} к 9:00<br>
        <b>Дата и время выгрузки:</b> {date_str}, по прибытию<br>
        <b>Адрес выгрузки:</b> ООО «ФК ПУЛЬС», Московская обл, Солнечногорский р-н, Черная Грязь д, Сходненская ул, д. стр.1, «НЛП»</p>
        
        <br>
        <p style="color: #64748b; font-size: 12px; border-top: 1px solid #e2e8f0; padding-top: 12px;">
            С уважением,<br>
            <b>Транспортный отдел ООО «ФК ПУЛЬС»</b><br>
            Тел. 8 (495) 665-76-20 (доб. 1231)<br>
            E-mail: transport_opt@puls.ru
        </p>
    </div>
    """
    return html

if __name__ == "__main__":
    test_in = os.path.join(BASE_DIR, "Тест_Заявки_Скриншот.xlsx")
    if not os.path.exists(test_in):
        test_in = os.path.join(BASE_DIR, "Книга4.xlsx")
    if os.path.exists(test_in):
        print(f"Тестовый запуск на {test_in}...")
        res = process_daily_orders(test_in)
        print(f"Готово! Перевозчиков: {res['total_carriers']}, Всего заявок: {res['total_orders']}")
        print(f"Всего паллет: {res['total_pallets']}")
        print(f"Папка сохранения: {res['output_dir']}")
        print(f"Сводка Excel: {res['summary_excel']}")
        for ord_info in res["orders"]:
            print(f"  • {ord_info['filename']} | Машин: {ord_info['trucks']} | {ord_info['pallets_str']} пал. | Маршрут: {ord_info['route']}")
