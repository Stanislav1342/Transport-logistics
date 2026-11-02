#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматизированный генератор графика отгрузок (РЦ Черная Грязь)
и модуль персональной почтовой рассылки через Microsoft Outlook.

Ключевые принципы:
1. Вход: 'График_отгрузки_филиалов_неделя_2.xlsm' (или любой .xlsm в папке).
2. Выход: Excel с 7 листами по дням недели (28.09 Пн, 29.09 Вт, ...).
3. Ограничение емкости: строго 2 машины в час.
4. Конфиденциальность: каждому перевозчику уходит ТОЛЬКО его расписание!
5. Интеграция с Outlook: прямая отправка без SMTP и паролей (pywin32).
"""

import sys
import os
import re
import glob
import json
import argparse
from datetime import datetime, timedelta, time
from collections import Counter, defaultdict
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

RUSSIAN_MONTHS = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель', 5: 'Май', 6: 'Июнь',
    7: 'Июль', 8: 'Август', 9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
}

DAYS_MAPPING = [
    ('Пн', 'пн', 0),
    ('Вт', 'вт', 1),
    ('Ср', 'ср', 2),
    ('Чт', 'чт', 3),
    ('Пт', 'пт', 4),
    ('Сб', 'сб', 5),
    ('Вс', 'вс', 6)
]

# Эталонная последовательность направлений (согласно файлу Последовательность направлений.xlsx)
DEFAULT_MASTER_CITY_ORDER = [
    'Ярославль', 'Казань', 'Воронеж', 'Краснодар', 'Волгоград',
    'Самара', 'Екатеринбург', 'Новосибирск', 'Красноярск', 'Иркутск',
    'Хабаровск', 'Санкт-Петербург', 'Брянск'
]

MASTER_CITY_ORDER = {d: list(DEFAULT_MASTER_CITY_ORDER) for d, _, _ in DAYS_MAPPING}

def find_city_sequence_file(base_dir=None):
    """Ищет файл Последовательность направлений.xlsx в проекте, Загрузках или на Рабочем столе"""
    names = [
        'Последовательность направлений.xlsx',
        'Последовательность направлений .xlsx',
        'Последовательность направлений.xlsx',
        'Последовательность направлений .xlsx'
    ]
    search_dirs = []
    if base_dir:
        search_dirs.append(base_dir)
    user_home = os.path.expanduser('~')
    search_dirs.extend([
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(user_home, 'Downloads'),
        os.path.join(user_home, 'Загрузки'),
        os.path.join(user_home, 'Desktop'),
        os.path.join(user_home, 'Рабочий стол'),
        os.path.join(user_home, 'OneDrive', 'Desktop'),
        os.path.join(user_home, 'OneDrive', 'Рабочий стол')
    ])
    for d in search_dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None

def load_city_sequence(base_dir=None):
    """Загружает эталонную последовательность направлений из Последовательность направлений.xlsx"""
    filepath = find_city_sequence_file(base_dir)
    if not filepath:
        return list(DEFAULT_MASTER_CITY_ORDER)
    try:
        wb = openpyxl.load_workbook(filepath, data_only=True)
        ws = wb.active
        seq = []
        for r in range(1, ws.max_row + 1):
            val = ws.cell(r, 1).value
            if not val:
                continue
            s = str(val).strip()
            s = re.sub(r'(?i)^пульс\s+', '', s).strip()
            s_clean = s.upper().replace(' ', '')
            if s_clean in ('СПБ', 'САНКТ-ПЕТЕРБУРГ', 'САНКТПЕТЕРБУРГ'):
                s = 'Санкт-Петербург'
            if s and s not in seq:
                seq.append(s)
        wb.close()
        return seq if seq else list(DEFAULT_MASTER_CITY_ORDER)
    except Exception as e:
        print(f"Ошибка загрузки последовательности направлений из {filepath}: {e}")
        return list(DEFAULT_MASTER_CITY_ORDER)

def get_ordered_cities(cities, base_dir=None):
    """Сортирует список городов строго по эталонной последовательности направлений"""
    master_seq = load_city_sequence(base_dir)
    
    def norm_c(name):
        n = str(name).strip().lower()
        if 'санкт' in n or 'спб' in n:
            return 'санкт-петербург'
        return n
        
    master_map = {norm_c(c): idx for idx, c in enumerate(master_seq)}
    
    def sort_key(city_item):
        c_norm = norm_c(city_item)
        for m_name, idx in master_map.items():
            if m_name == c_norm or m_name in c_norm or c_norm in m_name:
                return (0, idx)
        return (1, str(city_item))
        
    return sorted(cities, key=sort_key)

START_HOURS = {
    'Пн': 9, 'Вт': 9, 'Ср': 9, 'Чт': 9, 'Пт': 9, 'Сб': 9,
    'Вс': 8.5
}

def get_trip_time(d_name, city=None, truck_num=1, trip_index_in_day=0):
    """
    Рассчитывает согласованное время погрузки строго по правилу: 2 машины в час.
    В воскресенье ('Вс') начало отгрузки строго в 08:30:00.
    Для остальных дней (Пн-Сб) отгрузка начинается строго в 09:00:00.
    
    Каждые 2 машины занимают одно часовое окно:
      Машины 1 и 2 (индексы 0 и 1) -> 08:30 (Вс) / 09:00 (Пн-Сб)
      Машины 3 и 4 (индексы 2 и 3) -> 09:30 (Вс) / 10:00 (Пн-Сб)
      Машины 5 и 6 (индексы 4 и 5) -> 10:30 (Вс) / 11:00 (Пн-Сб)
      Машины 7 и 8 (индексы 6 и 7) -> 11:30 (Вс) / 12:00 (Пн-Сб)
      и т.д.
    """
    slot_offset_hours = trip_index_in_day // 2
    if d_name == 'Вс':
        start_min = 8 * 60 + 30  # 08:30:00
    else:
        start_min = 9 * 60       # 09:00:00
        
    total_min = start_min + slot_offset_hours * 60
    h = (total_min // 60) % 24
    m = total_min % 60
    return f"{h:02d}:{m:02d}:00"

DELIVERY_TIMINGS = {
    'брянск': {'transit_hours': 8, 'tz_diff': 0},
    'волгоград': {'transit_hours': 36, 'tz_diff': 0},       # 1.5 суток
    'воронеж': {'transit_hours': 24, 'tz_diff': 0},         # 1 сутки
    'екатеринбург': {'transit_hours': 72, 'tz_diff': 2},    # 3 суток, +2ч
    'иркутск': {'transit_hours': 168, 'tz_diff': 5},        # 7 суток, +5ч
    'казань': {'transit_hours': 36, 'tz_diff': 0},          # 1.5 суток
    'краснодар': {'transit_hours': 48, 'tz_diff': 0},       # 2 суток
    'красноярск': {'transit_hours': 144, 'tz_diff': 4},     # 6 суток, +4ч
    'новосибирск': {'transit_hours': 120, 'tz_diff': 3},    # 5 суток, +3ч
    'самара': {'transit_hours': 48, 'tz_diff': 1},          # 2 суток, +1ч
    'санкт-петербург': {'transit_hours': 24, 'tz_diff': 0}, # 1 сутки
    'спб': {'transit_hours': 24, 'tz_diff': 0},
    'ярославль': {'transit_hours': 5, 'tz_diff': 0},        # 5 часов
    'хабаровск': {'transit_hours': 276, 'tz_diff': 8},      # 11.5 суток, +8ч
    # Резервные/дополнительные направления
    'ростов-на-дону': {'transit_hours': 48, 'tz_diff': 0},
    'ростов': {'transit_hours': 48, 'tz_diff': 0},
    'уфа': {'transit_hours': 48, 'tz_diff': 2},
}

def get_delivery_info(city):
    key = str(city).strip().lower()
    for k, v in DELIVERY_TIMINGS.items():
        if k in key or key in k:
            return v
    return {'transit_hours': 24, 'tz_diff': 0}

def calculate_delivery_datetime(dep_date, time_str, city):
    """
    Рассчитывает дату и время доставки на основе времени погрузки,
    количества дней/часов в пути и разницы во времени.
    """
    if isinstance(dep_date, datetime):
        base_dt = datetime(dep_date.year, dep_date.month, dep_date.day)
    elif hasattr(dep_date, 'year'):
        base_dt = datetime(dep_date.year, dep_date.month, dep_date.day)
    else:
        try:
            base_dt = datetime.strptime(str(dep_date).strip(), '%d.%m.%Y')
        except Exception:
            base_dt = datetime.now()
            
    parts = [int(p) for p in str(time_str).strip().split(':') if p.isdigit()]
    h = parts[0] if len(parts) > 0 else 9
    m = parts[1] if len(parts) > 1 else 0
    extra_days = h // 24
    h = h % 24
    m = m % 60
    dep_dt = (base_dt + timedelta(days=extra_days)).replace(hour=h, minute=m, second=0)
    
    info = get_delivery_info(city)
    total_hours = info['transit_hours'] + info['tz_diff']
    delivery_dt = dep_dt + timedelta(hours=total_hours)
    return delivery_dt


def find_tariffs_file(base_dir=None):
    """Ищет файл Тарифы.xlsx (или устаревший ТАРИФЫ РК ТАБЛИЦА.xlsx) в проекте, Загрузках или на Рабочем столе"""
    names = [
        'Тарифы.xlsx',
        'Тарифы .xlsx',
        'ТАРИФЫ.xlsx',
        'ТАРИФЫ РК ТАБЛИЦА.xlsx'
    ]
    search_dirs = []
    if base_dir:
        search_dirs.append(base_dir)
    user_home = os.path.expanduser('~')
    search_dirs.extend([
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(user_home, 'Downloads'),
        os.path.join(user_home, 'Загрузки'),
        os.path.join(user_home, 'Desktop'),
        os.path.join(user_home, 'Рабочий стол'),
        os.path.join(user_home, 'OneDrive', 'Desktop'),
        os.path.join(user_home, 'OneDrive', 'Рабочий стол'),
    ])
    for n in names:
        for d in search_dirs:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None

def load_tariffs_matrix(base_dir=None):
    """
    Загружает актуальную матрицу тарифов из Тарифы.xlsx (табличный формат Откуда-Куда-Перевозчик-Тариф)
    или устаревшего 'ТАРИФЫ РК ТАБЛИЦА.xlsx'.
    Возвращает словарь: {город: {перевозчик: тариф}}
    """
    tariff_file = find_tariffs_file(base_dir)
    tariffs = {}
    
    carriers_norm = {
        'СИЯНИЕТК': 'ТК Сияние',
        'Сияние': 'ТК Сияние',
        'АО "НАЦИОНАЛЬНЫЙ ПЕРЕВОЗЧИК"': 'АО Национальный',
        'Наткар': 'АО Национальный',
        'ООО «Буш-Автопром»': 'Буш-Авто',
        'Буш': 'Буш-Авто',
        'Буш-Авто': 'Буш-Авто',
        'НОРД ЛАЙН МСК': 'НОРДЛАЙН',
        'Норд Лайн': 'НОРДЛАЙН',
        'ООО "ЕМАНТРАНСАВТО"': 'ЕманТрансАвто',
        'ЕманТрансАвто': 'ЕманТрансАвто',
        'ОЛИМП': 'Олимп',
        'Олимп': 'Олимп',
        'ООО "Караван"': 'Караван',
        'Караван': 'Караван',
        'Виллайн': 'Виллайн',
        'ООО ТК "АЗИМУТ"': 'Азимут',
        'АЗИМУТ': 'Азимут',
        'Азимут': 'Азимут',
        'Веб Логистика': 'Веб-Логистика',
        'Веб-Логистика': 'Веб-Логистика',
        'Агро-Авто': 'Агро-Авто',
        'Примум': 'Примум',
        'Васино': 'Васино',
        'ТДМ*': 'ТДМ*',
        'Гринлайн': 'Гринлайн',
        '700 Дорог': '700 Дорог',
        'УТК': 'УТК',
        'ИП Коршунов': 'ИП Коршунов',
        'ИП Мельник': 'ИП Мельник',
        'ИП Гусманов': 'ИП Гусманов'
    }
    
    city_norm = {
        'Санкт- Петербург': 'Санкт-Петербург',
        'СПБ': 'Санкт-Петербург',
        'УФА': 'Уфа'
    }
    
    if tariff_file and os.path.exists(tariff_file):
        try:
            wb = openpyxl.load_workbook(tariff_file, data_only=True)
            ws = wb.active
            
            headers = [str(ws.cell(1, c).value or '').strip().lower() for c in range(1, min(ws.max_column + 1, 10))]
            is_tabular = any('куда' in h for h in headers) and any('перевозчик' in h for h in headers) and any('тариф' in h for h in headers)
            
            if is_tabular:
                col_kuda = None
                col_car = None
                col_tar = None
                for c in range(1, ws.max_column + 1):
                    h_val = str(ws.cell(1, c).value or '').strip().lower()
                    if 'куда' in h_val:
                        col_kuda = c
                    elif 'перевозчик' in h_val:
                        col_car = c
                    elif 'тариф' in h_val:
                        col_tar = c
                        
                for r in range(2, ws.max_row + 1):
                    c_raw = ws.cell(r, col_kuda).value
                    car_raw = ws.cell(r, col_car).value
                    val = ws.cell(r, col_tar).value
                    if not c_raw or not car_raw or val is None:
                        continue
                    c = str(c_raw).strip()
                    c = city_norm.get(c, c)
                    car = str(car_raw).strip()
                    car = carriers_norm.get(car, car)
                    try:
                        val_int = int(float(val))
                    except (ValueError, TypeError):
                        continue
                    if val_int <= 0:
                        continue
                    if c not in tariffs:
                        tariffs[c] = {}
                    tariffs[c][car] = val_int
            else:
                ws_matrix = wb['Актуальные '] if 'Актуальные ' in wb.sheetnames else wb.active
                for r in range(5, ws_matrix.max_row + 1):
                    c_raw = ws_matrix.cell(r, 1).value
                    if not c_raw or '-экспресс' in str(c_raw):
                        continue
                    c_clean = str(c_raw).strip()
                    c = city_norm.get(c_clean, c_clean)
                    if c not in tariffs:
                        tariffs[c] = {}
                    for col in range(2, ws_matrix.max_column + 1):
                        car_raw = ws_matrix.cell(3, col).value
                        val = ws_matrix.cell(r, col).value
                        if car_raw in carriers_norm and isinstance(val, (int, float)) and val > 0:
                            car = carriers_norm[car_raw]
                            if car not in tariffs[c] or val < tariffs[c][car]:
                                tariffs[c][car] = int(val)
            wb.close()
        except Exception as e:
            print(f"Ошибка загрузки тарифов из {tariff_file}: {e}")
            
    # Фиксированные тарифы частных перевозчиков и спецправил
    if 'Ярославль' in tariffs:
        tariffs['Ярославль'].setdefault('ИП Коршунов', 52000)
        tariffs['Ярославль'].setdefault('ИП Мельник', 52000)
        tariffs['Ярославль'].setdefault('ИП Гусманов', 47000)
    if 'Хабаровск' in tariffs:
        az_tar = tariffs['Хабаровск'].get('Азимут', 1050000)
        tariffs['Хабаровск'].setdefault('Авангард', az_tar)
            
    # Резервная встроенная сетка тарифов (актуализирована по Тарифы.xlsx)
    if not tariffs:
        tariffs = {
            'Брянск': {'Васино': 50000, 'НОРДЛАЙН': 51000, 'ЕманТрансАвто': 51500, 'ТК Сияние': 57000, 'Олимп': 61800, 'ТДМ*': 70000, 'Агро-Авто': 73000},
            'Волгоград': {'ЕманТрансАвто': 134000, 'Васино': 135000, 'ТК Сияние': 150000, 'АО Национальный': 155000, 'Агро-Авто': 157000, 'Олимп': 160800, 'Веб-Логистика': 190000},
            'Воронеж': {'Буш-Авто': 63700, 'ЕманТрансАвто': 65000, 'ТДМ*': 70000, 'ТК Сияние': 71500, 'Олимп': 78000, 'Агро-Авто': 85000, 'Васино': 100000},
            'Екатеринбург': {'НОРДЛАЙН': 219300, 'Васино': 230000, 'ТК Сияние': 275000, 'Азимут': 280000, 'ЕманТрансАвто': 280000, 'УТК': 280000, 'Веб-Логистика': 280000, 'Агро-Авто': 290000, 'АО Национальный': 293000, 'Олимп': 296400, 'ТДМ*': 300000},
            'Иркутск': {'АО Национальный': 610000, 'ТДМ*': 620000, 'Агро-Авто': 636000, 'Гринлайн': 650000, 'Васино': 650000, 'ЕманТрансАвто': 660500, 'Азимут': 720000, '700 Дорог': 720000, 'ТК Сияние': 784000, 'Олимп': 792600},
            'Казань': {'НОРДЛАЙН': 118320, 'Буш-Авто': 128600, 'ЕманТрансАвто': 133800, 'АО Национальный': 150000, 'ТДМ*': 150000, 'Васино': 150000, 'ТК Сияние': 150000, 'Агро-Авто': 157000, 'Олимп': 160560, 'Азимут': 185000},
            'Краснодар': {'ЕманТрансАвто': 175000, 'Буш-Авто': 195700, 'ТДМ*': 200000, 'Васино': 200000, 'ТК Сияние': 210000, 'Олимп': 216000, 'АО Национальный': 230000, 'Веб-Логистика': 250000, 'Агро-Авто': 287000, 'Азимут': 295000},
            'Красноярск': {'Виллайн': 530000, 'Васино': 540000, 'ЕманТрансАвто': 547000, 'ТДМ*': 550000, 'Агро-Авто': 550000, 'Гринлайн': 570000, '700 Дорог': 630000, 'ТК Сияние': 639000, 'Олимп': 656400, 'Азимут': 690000},
            'Новосибирск': {'НОРДЛАЙН': 336600, 'Васино': 420000, 'ЕманТрансАвто': 433000, 'Виллайн': 450000, 'Агро-Авто': 455000, 'Гринлайн': 470000, 'ТДМ*': 480000, 'Веб-Логистика': 490000, 'АО Национальный': 495000, 'УТК': 500000, 'Олимп': 519600, '700 Дорог': 520000, 'ТК Сияние': 520000, 'Азимут': 557000},
            'Самара': {'ЕманТрансАвто': 134200, 'Васино': 140000, 'Буш-Авто': 145400, 'ТДМ*': 150000, 'ТК Сияние': 151000, 'Олимп': 161040, 'Агро-Авто': 162000, 'АО Национальный': 166000, 'Азимут': 180000, 'УТК': 180000, 'Веб-Логистика': 180000},
            'Санкт-Петербург': {'НОРДЛАЙН': 86700, 'ЕманТрансАвто': 97000, 'ТК Сияние': 107000, 'ТДМ*': 110000, 'Буш-Авто': 115000, 'Олимп': 116400, 'Караван': 117500, 'Васино': 120000, 'Агро-Авто': 133000, 'АО Национальный': 135000},
            'Уфа': {'Васино': 170000, 'Буш-Авто': 179000, 'ТДМ*': 200000, 'Олимп': 200000, 'ТК Сияние': 217000, 'Азимут': 220000, 'АО Национальный': 225000, 'Агро-Авто': 225000, 'Веб-Логистика': 240000, 'УТК': 250000},
            'Хабаровск': {'ЕманТрансАвто': 972000, 'Васино': 980000, 'Агро-Авто': 997000, 'ТДМ*': 1000000, 'Веб-Логистика': 1000000, 'Азимут': 1050000, 'Авангард': 1050000, 'Гринлайн': 1050000, 'Виллайн': 1050000, 'ТК Сияние': 1150000, 'Олимп': 1166400, '700 Дорог': 1200000},
            'Ярославль': {'ИП Гусманов': 47000, 'ИП Коршунов': 52000, 'ИП Мельник': 52000, 'Буш-Авто': 53680, 'ЕманТрансАвто': 56500, 'Олимп': 56500, 'ТК Сияние': 57000, 'Васино': 60000, 'Агро-Авто': 77000}
        }
    return tariffs

def plan_weekly_carrier_assignments(plan_trips, tariffs):
    """
    Распределяет перевозчиков и тарифы по рейсам согласно официальным правилам
    из файла 'Исключения для алгоритма распределения транспорта.docx':

    Иерархия приоритетов:
    1. Направление -> 2. Количество машин -> 3. Номер машины -> 4. Фиксированный перевозчик
    -> 5. Доступность ТС -> 6. Исключение/отказ -> 7. Резервный перевозчик -> 8. Экономическая оптимизация.
    Фиксированные правила имеют наивысший приоритет над оптимизацией по тарифу.
    """
    assigned = {}
    carrier_counts = defaultdict(int)
    city_carrier_counts = defaultdict(lambda: defaultdict(int))

    def assign_trip(d_name, city, tr_i, carrier):
        key = (d_name, city, tr_i)
        tar = tariffs.get(city, {}).get(carrier, 0)
        if tar == 0 and carrier == 'Авангард':
            tar = tariffs.get(city, {}).get('Азимут', 1050000)
        assigned[key] = (carrier, tar)
        carrier_counts[carrier] += 1
        city_carrier_counts[city][carrier] += 1

    def pick_balanced(city, allowed):
        # Балансировка: приоритет перевозчику с наименьшим накопленным количеством рейсов за неделю
        sorted_cars = sorted(
            allowed,
            key=lambda c: (
                carrier_counts[c],
                city_carrier_counts[city][c],
                tariffs.get(city, {}).get(c, 9999999)
            )
        )
        return sorted_cars[0]

    def pick_cheapest(city, allowed=None, exclude=None):
        c_tar = tariffs.get(city, {})
        candidates = allowed if allowed else list(c_tar.keys())
        if exclude:
            candidates = [c for c in candidates if c not in exclude]
        if not candidates:
            return 'ТК Сияние'
        sorted_cars = sorted(
            candidates,
            key=lambda c: (
                c_tar.get(c, 9999999),
                carrier_counts[c]
            )
        )
        return sorted_cars[0]

    # Обрабатываем дни недели последовательно (Пн -> Вс)
    for d_name, d_short, offset in DAYS_MAPPING:
        d_plan = plan_trips.get(d_name, {})
        city_order = get_ordered_cities(d_plan.keys())

        for city in city_order:
            cnt = d_plan.get(city, 0)
            for tr_i in range(1, cnt + 1):

                # 2. ЯРОСЛАВЛЬ
                # 1-я машина: чередование по дням (Пн/Ср/Пт - Мельник, Вт/Чт - Коршунов)
                # 2-я и последующие - всегда ИП Гусманов
                if city == 'Ярославль':
                    if tr_i == 1:
                        carrier = 'ИП Мельник' if d_name in ['Пн', 'Ср', 'Пт', 'Вс'] else 'ИП Коршунов'
                    else:
                        carrier = 'ИП Гусманов'

                # 3. БРЯНСК
                # 1-я машина — Норд Лайн, 2-я машина — Сияние ТК, 3-я+ — по рентабельному тарифу
                elif city == 'Брянск':
                    if tr_i == 1:
                        carrier = 'НОРДЛАЙН'
                    elif tr_i == 2:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = pick_cheapest(city, exclude={'НОРДЛАЙН', 'ТК Сияние'})

                # 4. КАЗАНЬ
                # 1-я машина — Сияние ТК; 2-я+ между Норд Лайн, АО Национальный, ООО Агро-Авто
                elif city == 'Казань':
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = pick_balanced(city, ['НОРДЛАЙН', 'АО Национальный', 'Агро-Авто'])

                # 5. УФА
                # 1-я машина — Сияние ТК; 2-я+ — АО Национальный и/или ООО Агро-Авто
                elif city == 'Уфа':
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = pick_balanced(city, ['АО Национальный', 'Агро-Авто'])

                # 6. ВОРОНЕЖ
                # Направление Воронеж — ТОЛЬКО Сияние ТК. Другие перевозчики не назначаются.
                elif city == 'Воронеж':
                    carrier = 'ТК Сияние'

                # 7. РОСТОВ-НА-ДОНУ
                # Основной перевозчик — Сияние ТК. Резервные: АО Национальный -> Агро-Авто -> Буш-Авто
                elif city == 'Ростов-на-Дону':
                    carrier = 'ТК Сияние'

                # 8. КРАСНОДАР
                # 1-я: Сияние ТК, 2-я: АО Национальный, 3-я: ООО Буш-Автопром, 4-я: ООО Агро-Авто
                elif city == 'Краснодар':
                    kras_seq = ['ТК Сияние', 'АО Национальный', 'Буш-Авто', 'Агро-Авто']
                    if tr_i <= len(kras_seq):
                        carrier = kras_seq[tr_i - 1]
                    else:
                        carrier = kras_seq[(tr_i - 1) % len(kras_seq)]

                # 9. ВОЛГОГРАД
                # Направление Волгоград — ТОЛЬКО Сияние ТК.
                elif city == 'Волгоград':
                    carrier = 'ТК Сияние'

                # 10. САМАРА
                # 1-я машина — Сияние ТК, 2-я и последующие — преимущественно АО Национальный перевозчик
                elif city == 'Самара':
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = 'АО Национальный'

                # 11. ЕКАТЕРИНБУРГ
                # 1-я: Сияние ТК, 2-я: АО Национальный, 3-я и последующие — по рентабельному тарифу
                elif city == 'Екатеринбург':
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    elif tr_i == 2:
                        carrier = 'АО Национальный'
                    else:
                        carrier = pick_cheapest(city, exclude={'ТК Сияние', 'АО Национальный'})

                # 12. НОВОСИБИРСК
                # Допустимые: ЕманТрансАвто, Виллайн, ВЕБ ЛОГИСТИКА. Равномерный баланс за неделю.
                elif city == 'Новосибирск':
                    carrier = pick_balanced(city, ['ЕманТрансАвто', 'Виллайн', 'Веб-Логистика'])

                # 13. КРАСНОЯРСК
                # Допустимые: ЕманТрансАвто, Виллайн, ВЕБ ЛОГИСТИКА. Равномерный баланс за неделю.
                elif city == 'Красноярск':
                    carrier = pick_balanced(city, ['ЕманТрансАвто', 'Виллайн', 'Веб-Логистика'])

                # 14. ИРКУТСК
                # Допустимые: Азимут ТК, ЕманТрансАвто. Равномерный баланс за неделю.
                elif city == 'Иркутск':
                    carrier = pick_balanced(city, ['Азимут', 'ЕманТрансАвто'])

                # 15. ХАБАРОВСК
                # 1-я и 2-я машины — Азимут ТК; 3-я и последующие — Авангард
                elif city == 'Хабаровск':
                    if tr_i in (1, 2):
                        carrier = 'Азимут'
                    else:
                        carrier = 'Авангард'

                # 16. САНКТ-ПЕТЕРБУРГ
                # 1-я машина — Сияние ТК; 2-я и последующие — Норд Лайн / АО Национальный
                elif city == 'Санкт-Петербург':
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = pick_balanced(city, ['НОРДЛАЙН', 'АО Национальный'])

                # Прочие направления по умолчанию
                else:
                    if tr_i == 1:
                        carrier = 'ТК Сияние'
                    else:
                        carrier = pick_cheapest(city)

                assign_trip(d_name, city, tr_i, carrier)

    return assigned

def find_default_plan_file(base_dir):
    candidates = [
        os.path.join(base_dir, 'График_отгрузки_филиалов_неделя_2.xlsm'),
        *glob.glob(os.path.join(base_dir, '*График*отгрузки*.xlsm')),
        *glob.glob(os.path.join(base_dir, '*.xlsm'))
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def parse_plan_file(plan_path):
    wb = openpyxl.load_workbook(plan_path, data_only=True)
    sheet_name = wb.sheetnames[0]
    for s in wb.sheetnames:
        if '.' in s and '-' in s:
            sheet_name = s
            break
    ws = wb[sheet_name]
    
    header_val = str(ws['A1'].value or '')
    match = re.search(r'(\d{1,2}\.\d{2})\s*-\s*(\d{1,2}\.\d{2})', header_val)
    if match:
        start_d_str = match.group(1)
        cur_year = datetime.now().year
        try:
            start_date = datetime.strptime(f"{start_d_str}.{cur_year}", "%d.%m.%Y")
        except Exception:
            start_date = datetime(2026, 9, 28)
    else:
        start_date = datetime(2026, 9, 28)
        
    days_cols = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    plan_trips = defaultdict(lambda: defaultdict(int))
    
    for r in range(2, ws.max_row+1):
        city = ws.cell(r, 1).value
        if not city or not str(city).strip() or str(city).strip().lower() in ['итого', 'всего']:
            continue
        city_clean = str(city).strip()
        for col_idx in range(2, 9):
            d_name = days_cols[col_idx-2]
            cnt = ws.cell(r, col_idx).value or 0
            try:
                cnt_int = int(cnt)
            except Exception:
                cnt_int = 0
            if cnt_int > 0:
                plan_trips[d_name][city_clean] = cnt_int
                
    wb.close()
    return plan_trips, start_date

MANUAL_FILL_COLOR = 'FFF2CC'  # Мягкий желтый цвет для рейсов, добавленных сотрудником вручную


def is_cell_manual_fill(cell):
    """Проверяет, выделена ли ячейка цветом ручного добавления (или пользовательским маркером в Excel)"""
    if not cell or not cell.fill or not cell.fill.fill_type or cell.fill.fill_type == 'none':
        return False
    sc = cell.fill.start_color
    if not sc:
        return False
    if sc.type == 'rgb':
        rgb = str(sc.rgb or '').upper()
        if not rgb:
            return False
        hex6 = rgb[-6:] if len(rgb) >= 6 else rgb
        # Игнорируем прозрачный/белый и стандартный зеленый цвет 40 паллет (E2EFDA)
        if hex6 in ('000000', 'FFFFFF', 'E2EFDA'):
            return False
        return True
    elif sc.type in ('theme', 'indexed'):
        if sc.type == 'indexed' and sc.indexed in (64, 65):
            return False
        return True
    return False


def get_no_tariffs_copy_path(excel_path):
    """Возвращает путь для копии графика без колонки тарифов в той же папке"""
    if not excel_path:
        return None
    folder = os.path.dirname(os.path.abspath(excel_path))
    fname = os.path.basename(excel_path)
    base, ext = os.path.splitext(fname)
    base_clean = re.sub(r'\s*\(без тарифов\)\s*$', '', base, flags=re.IGNORECASE).strip()
    return os.path.join(folder, f"{base_clean} (без тарифов){ext}")


def create_schedule_copy_without_tariffs(excel_path):
    """
    Создает по тому же пути копию файла графика без колонки 'Тариф' (столбец J).
    Все форматирование, цветовая разметка новых рейсов (FFF2CC) и 40 паллет (E2EFDA)
    сохраняются для столбцов A..I, а сводная таблица сдвигается в K..M.
    """
    if not excel_path or not os.path.exists(excel_path):
        return None
    copy_path = get_no_tariffs_copy_path(excel_path)
    if not copy_path:
        return None

    try:
        wb = openpyxl.load_workbook(excel_path)
        font_header = Font(name='Arial', size=10, bold=True)
        font_regular = Font(name='Arial', size=10)
        border_thin = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )
        border_summary_header = Border(
            left=Side(style='thin', color='BFBFBF'),
            right=Side(style='thin', color='BFBFBF'),
            top=Side(style='thin', color='BFBFBF'),
            bottom=Side(style='thin', color='000000')
        )
        border_total = Border(
            top=Side(style='thin', color='000000'),
            bottom=Side(style='double', color='000000')
        )

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            h_a = str(ws['A1'].value or '').strip().lower()
            if 'месяц' not in h_a:
                continue

            # Определяем последнюю строку данных и список перевозчиков
            last_data_row = 1
            carriers_in_sheet = []
            for r in range(2, ws.max_row + 1):
                c_val = ws[f'E{r}'].value
                if c_val and str(c_val).strip():
                    last_data_row = r
                    car_val = ws[f'G{r}'].value
                    if car_val and str(car_val).strip():
                        carriers_in_sheet.append(str(car_val).strip())

            # Очищаем столбец J (Тариф) и старую область сводной таблицы K..P
            max_clear = max(ws.max_row + 10, 50)
            for r in range(1, max_clear + 1):
                for col_l in ['J', 'K', 'L', 'M', 'N', 'O', 'P']:
                    cell = ws[f'{col_l}{r}']
                    cell.value = None
                    cell.border = None
                    cell.fill = PatternFill(fill_type=None)
                    cell.number_format = 'General'

            # Настраиваем ширину столбцов: J — узкий разделитель, K..M — сводная таблица
            ws.column_dimensions['J'].width = 5
            ws.column_dimensions['K'].width = 22
            ws.column_dimensions['L'].width = 10
            ws.column_dimensions['M'].width = 10

            if last_data_row >= 2:
                ws.auto_filter.ref = f'A1:I{last_data_row}'

            # Перестраиваем сводную таблицу в столбцах K..M
            carrier_counts = Counter(carriers_in_sheet)
            if carrier_counts and last_data_row >= 2:
                ws['K3'] = 'Перевозчик'
                ws['K3'].font = font_header
                ws['K3'].border = border_summary_header
                ws['L3'] = 'Рейсы'
                ws['L3'].font = font_header
                ws['L3'].alignment = Alignment(horizontal='center')
                ws['L3'].border = border_summary_header
                ws['M3'] = '%'
                ws['M3'].font = font_header
                ws['M3'].alignment = Alignment(horizontal='center')
                ws['M3'].border = border_summary_header

                s_row = 4
                total_target_row = 4 + len(carrier_counts)
                for car, _ in carrier_counts.most_common():
                    ws[f'K{s_row}'] = car
                    ws[f'K{s_row}'].font = font_regular
                    ws[f'K{s_row}'].border = border_thin

                    ws[f'L{s_row}'] = f'=COUNTIF(G$2:G${last_data_row}, K{s_row})'
                    ws[f'L{s_row}'].font = font_regular
                    ws[f'L{s_row}'].alignment = Alignment(horizontal='center')
                    ws[f'L{s_row}'].border = border_thin

                    ws[f'M{s_row}'] = f'=L{s_row}/L{total_target_row}'
                    ws[f'M{s_row}'].font = font_regular
                    ws[f'M{s_row}'].number_format = '0.0%'
                    ws[f'M{s_row}'].alignment = Alignment(horizontal='right')
                    ws[f'M{s_row}'].border = border_thin
                    s_row += 1

                ws[f'K{s_row}'] = 'Всего'
                ws[f'K{s_row}'].font = font_header
                ws[f'K{s_row}'].border = border_total

                ws[f'L{s_row}'] = f'=SUM(L4:L{s_row-1})'
                ws[f'L{s_row}'].font = font_header
                ws[f'L{s_row}'].alignment = Alignment(horizontal='center')
                ws[f'L{s_row}'].border = border_total

                ws[f'M{s_row}'] = '100%'
                ws[f'M{s_row}'].font = font_header
                ws[f'M{s_row}'].alignment = Alignment(horizontal='right')
                ws[f'M{s_row}'].border = border_total

        wb.save(copy_path)
        wb.close()
        print(f"Копия графика без тарифов успешно сохранена: {copy_path}")
        return copy_path
    except Exception as e:
        print(f"Ошибка при создании копии без тарифов ({copy_path}): {e}")
        return None


def _get_baseline_store_path(excel_path):
    folder = os.path.dirname(os.path.abspath(excel_path))
    return os.path.join(folder, '.schedule_baselines.json')


def _save_schedule_baseline(excel_path, sheets_baseline):
    """Сохраняет эталонный состав рейсов при первичной генерации графика для точного определения ручных добавлений"""
    try:
        store_path = _get_baseline_store_path(excel_path)
        data = {}
        if os.path.exists(store_path):
            try:
                with open(store_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        key = os.path.basename(excel_path)
        data[key] = sheets_baseline
        with open(store_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Не удалось сохранить baseline для {excel_path}: {e}")


def _load_schedule_baseline(excel_path):
    """Загружает эталонный состав рейсов для файла графика"""
    try:
        store_path = _get_baseline_store_path(excel_path)
        if not os.path.exists(store_path):
            return None
        with open(store_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get(os.path.basename(excel_path))
    except Exception:
        return None


def parse_final_schedule_file(excel_path):
    """
    Считывает готовый график отгрузок (Недельные графики.xlsx), 
    в который сотрудник мог внести ручные правки в Excel 
    (добавил/удалил машины, изменил время, перевозчика или тариф).
    Поддерживает:
    - Чистый формат (начинается со столбца Месяц в A: A..I)
    - Прежний формат со смещением (C..K)
    - Старый легаси формат
    """
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    all_trips = []
    
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        h_a = str(ws['A1'].value or '').strip().lower()
        h_c = str(ws['C1'].value or '').strip().lower()
        h_e = str(ws['E1'].value or '').strip().lower()

        if 'месяц' in h_a:
            fmt = 'starts_a'
        elif 'месяц' in h_c or 'время' in h_e:
            fmt = 'starts_c'
        elif 'дата' in h_c:
            fmt = 'legacy'
        else:
            continue
            
        for row in range(2, ws.max_row + 1):
            if fmt == 'starts_a':
                month_val = ws[f'A{row}'].value or ''
                date_val = ws[f'B{row}'].value
                time_val = ws[f'C{row}'].value
                day_val = ws[f'D{row}'].value or ''
                city_val = ws[f'E{row}'].value
                truck_num = ws[f'F{row}'].value or 1
                carrier_val = ws[f'G{row}'].value
                pallets = ws[f'H{row}'].value or 33
                deliv_val = ws[f'I{row}'].value
                tariff_val = ws[f'J{row}'].value
                check_cell = ws[f'E{row}']
            elif fmt == 'starts_c':
                month_val = ws[f'C{row}'].value or ''
                date_val = ws[f'D{row}'].value
                time_val = ws[f'E{row}'].value
                day_val = ws[f'F{row}'].value or ''
                city_val = ws[f'G{row}'].value
                truck_num = ws[f'H{row}'].value or 1
                carrier_val = ws[f'I{row}'].value
                pallets = ws[f'J{row}'].value or 33
                deliv_val = ws[f'K{row}'].value
                tariff_val = ws[f'L{row}'].value
                check_cell = ws[f'G{row}']
            else:
                date_val = ws[f'C{row}'].value
                day_val = ws[f'D{row}'].value or ''
                month_val = ws[f'E{row}'].value or ''
                truck_num = ws[f'F{row}'].value or 1
                city_val = ws[f'G{row}'].value
                pallets = ws[f'H{row}'].value or 33
                carrier_val = ws[f'I{row}'].value
                tariff_val = ws[f'J{row}'].value
                time_val = ws[f'K{row}'].value
                deliv_val = None
                check_cell = ws[f'G{row}']
            
            if not city_val or str(city_val).strip() == '' or 'Итого' in str(city_val):
                continue
            if not date_val:
                continue
                
            tariff_num = 0
            if tariff_val is not None:
                try:
                    clean_t = str(tariff_val).replace(' ', '').replace('₽', '').replace('\xa0', '').replace(',', '.')
                    tariff_num = int(float(clean_t))
                except Exception:
                    tariff_num = 0
            
            if isinstance(date_val, datetime):
                dt_obj = date_val
                date_str = dt_obj.strftime('%d.%m.%Y')
            else:
                date_str = str(date_val).strip()
                try:
                    dt_obj = datetime.strptime(date_str, '%d.%m.%Y')
                except Exception:
                    dt_obj = None
                    
            if isinstance(time_val, (datetime, time)):
                time_str = time_val.strftime('%H:%M:%S')
            else:
                time_str = str(time_val or '09:00:00').strip()
                if len(time_str) == 5:
                    time_str += ':00'
                    
            city_str = str(city_val).strip()
            if deliv_val is not None and str(deliv_val).strip():
                if isinstance(deliv_val, datetime):
                    delivery_dt = deliv_val
                    delivery_str = delivery_dt.strftime('%d.%m.%Y %H:%M')
                else:
                    delivery_str = str(deliv_val).strip()
                    try:
                        delivery_dt = datetime.strptime(delivery_str, '%d.%m.%Y %H:%M')
                    except Exception:
                        delivery_dt = None
            else:
                delivery_dt = calculate_delivery_datetime(dt_obj or date_str, time_str, city_str)
                delivery_str = delivery_dt.strftime('%d.%m.%Y %H:%M')
                    
            all_trips.append({
                'date': date_str,
                'dt': dt_obj,
                'day': str(day_val).strip(),
                'month': str(month_val).strip(),
                'city': city_str,
                'truck_num': truck_num,
                'carrier': str(carrier_val or 'ТК Сияние').strip(),
                'pallets': pallets,
                'tariff': tariff_num,
                'time': time_str,
                'delivery_dt': delivery_dt,
                'delivery_str': delivery_str,
                'is_manual': is_cell_manual_fill(check_cell)
            })
            
    wb.close()
    return all_trips


def build_schedule_from_plan(plan_trips, start_date, output_path):
    wb_out = openpyxl.Workbook()
    wb_out.remove(wb_out.active)
    
    tariffs = load_tariffs_matrix(os.path.dirname(output_path))
    weekly_assignments = plan_weekly_carrier_assignments(plan_trips, tariffs)
    
    font_header = Font(name='Arial', size=10, bold=True)
    font_regular = Font(name='Arial', size=10)
    font_bold_center = Font(name='Times New Roman', size=11, bold=True)
    font_city = Font(name='Times New Roman', size=11, bold=True)
    font_carrier = Font(name='Times New Roman', size=11)

    border_thin = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )
    border_header = Border(
        left=Side(style='thin', color='A6A6A6'),
        right=Side(style='thin', color='A6A6A6'),
        top=Side(style='thin', color='A6A6A6'),
        bottom=Side(style='medium', color='000000')
    )
    border_summary_header = Border(
        left=Side(style='thin', color='BFBFBF'),
        right=Side(style='thin', color='BFBFBF'),
        top=Side(style='thin', color='BFBFBF'),
        bottom=Side(style='thin', color='000000')
    )
    border_total = Border(
        top=Side(style='thin', color='000000'),
        bottom=Side(style='double', color='000000')
    )
    fill_40p = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')

    # Чистая структура: график начинается сразу со столбца Месяц (A), без столбцов A и B
    col_widths = {
        'A': 13, 'B': 12, 'C': 15, 'D': 6, 'E': 22, 'F': 15,
        'G': 26, 'H': 14, 'I': 18, 'J': 16, 'K': 5, 'L': 22, 'M': 10, 'N': 10
    }

    all_scheduled_trips = []
    sheets_baseline = {}

    for d_name, d_short, offset in DAYS_MAPPING:
        cur_date = start_date + timedelta(days=offset)
        d_str = cur_date.strftime('%d.%m.%Y')
        d_month = RUSSIAN_MONTHS[cur_date.month]
        sheet_title = f"{cur_date.strftime('%d.%m')} {d_name}"
        
        ws = wb_out.create_sheet(title=sheet_title)
        ws.views.sheetView[0].showGridLines = True
        
        for col_l, w in col_widths.items():
            ws.column_dimensions[col_l].width = w
        ws.row_dimensions[1].height = 26.0

        headers = {
            'A': 'Месяц', 'B': 'Дата', 'C': 'Время погрузки', 'D': '',
            'E': 'Направление', 'F': 'Номер Машины', 'G': 'Перевозчик',
            'H': 'Кол-во паллет ', 'I': 'Дата доставки', 'J': 'Тариф'
        }
        for col_l, h_text in headers.items():
            cell = ws[f'{col_l}1']
            cell.value = h_text
            cell.font = font_header
            cell.border = border_header
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

        day_plan = plan_trips.get(d_name, {})
        city_order = get_ordered_cities(day_plan.keys(), os.path.dirname(output_path))
        
        ordered_trips = []
        city_truck_counters = Counter()
        for city in city_order:
            count = day_plan.get(city, 0)
            for _ in range(count):
                city_truck_counters[city] += 1
                tr_num = city_truck_counters[city]
                carrier, tariff = weekly_assignments.get((d_name, city, tr_num), ('ТК Сияние', 0))
                
                # Точное распределение паллет
                if city == 'Ярославль':
                    pal = 40 if (tr_num == 1 or (d_name == 'Вт' and tr_num == 2)) else 33
                elif city == 'Хабаровск':
                    pal = 40 if (d_name == 'Чт' and tr_num == 1) else 33
                else:
                    pal = 33
                    
                time_str = get_trip_time(d_name, city, tr_num, len(ordered_trips))
                deliv_dt = calculate_delivery_datetime(cur_date, time_str, city)
                deliv_str = deliv_dt.strftime('%d.%m.%Y %H:%M')
                
                tr_data = {
                    'date': d_str,
                    'dt': cur_date,
                    'day': d_short,
                    'month': d_month,
                    'city': city,
                    'truck_num': tr_num,
                    'carrier': carrier,
                    'pallets': pal,
                    'tariff': tariff,
                    'time': time_str,
                    'delivery_dt': deliv_dt,
                    'delivery_str': deliv_str,
                    'is_manual': False
                }
                ordered_trips.append(tr_data)
                all_scheduled_trips.append(tr_data)

        sheets_baseline[sheet_title] = [
            {'city': tr['city'], 'truck_num': tr['truck_num'], 'carrier': tr['carrier']}
            for tr in ordered_trips
        ]

        row_idx = 2
        for tr in ordered_trips:
            c_a = ws[f'A{row_idx}']
            c_a.value = d_month
            c_a.font = font_regular
            c_a.alignment = Alignment(horizontal='center', vertical='center')
            c_a.border = border_thin
            
            c_b = ws[f'B{row_idx}']
            c_b.value = d_str
            c_b.font = font_regular
            c_b.alignment = Alignment(horizontal='center', vertical='center')
            c_b.border = border_thin
            
            c_c = ws[f'C{row_idx}']
            c_c.value = tr['time']
            c_c.font = font_regular
            c_c.alignment = Alignment(horizontal='center', vertical='center')
            c_c.border = border_thin
            
            c_d = ws[f'D{row_idx}']
            c_d.value = d_short
            c_d.font = font_header
            c_d.alignment = Alignment(horizontal='center', vertical='center')
            c_d.border = border_thin
            
            c_e = ws[f'E{row_idx}']
            c_e.value = tr['city']
            c_e.font = font_city
            c_e.alignment = Alignment(horizontal='left', vertical='center')
            c_e.border = border_thin
            
            c_f = ws[f'F{row_idx}']
            c_f.value = tr['truck_num']
            c_f.font = font_bold_center
            c_f.alignment = Alignment(horizontal='center', vertical='center')
            c_f.border = border_thin
            
            c_g = ws[f'G{row_idx}']
            c_g.value = tr['carrier']
            c_g.font = font_carrier
            c_g.alignment = Alignment(horizontal='center', vertical='center')
            c_g.border = border_thin
            
            c_h = ws[f'H{row_idx}']
            c_h.value = tr['pallets']
            c_h.font = font_regular
            c_h.alignment = Alignment(horizontal='center', vertical='center')
            c_h.border = border_thin
            
            c_i = ws[f'I{row_idx}']
            c_i.value = tr['delivery_str']
            c_i.font = font_regular
            c_i.alignment = Alignment(horizontal='center', vertical='center')
            c_i.border = border_thin
            
            c_j = ws[f'J{row_idx}']
            c_j.value = tr.get('tariff', 0)
            c_j.font = font_regular
            c_j.number_format = '#,##0 ₽'
            c_j.alignment = Alignment(horizontal='right', vertical='center')
            c_j.border = border_thin
            
            if tr['pallets'] == 40:
                for col_l in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J']:
                    ws[f'{col_l}{row_idx}'].fill = fill_40p
            
            ws.row_dimensions[row_idx].height = 18.0
            row_idx += 1

        last_data_row = row_idx - 1
        if last_data_row >= 2:
            ws.auto_filter.ref = f'A1:J{last_data_row}'

        carrier_counts = Counter([t['carrier'] for t in ordered_trips if t.get('carrier')])
        if carrier_counts:
            ws['L3'] = 'Перевозчик'
            ws['L3'].font = font_header
            ws['L3'].border = border_summary_header
            ws['M3'] = 'Рейсы'
            ws['M3'].font = font_header
            ws['M3'].alignment = Alignment(horizontal='center')
            ws['M3'].border = border_summary_header
            ws['N3'] = '%'
            ws['N3'].font = font_header
            ws['N3'].alignment = Alignment(horizontal='center')
            ws['N3'].border = border_summary_header
            
            s_row = 4
            for car, cnt in carrier_counts.most_common():
                ws[f'L{s_row}'] = car
                ws[f'L{s_row}'].font = font_regular
                ws[f'L{s_row}'].border = border_thin
                
                ws[f'M{s_row}'] = f'=COUNTIF(G$2:G${last_data_row}, L{s_row})'
                ws[f'M{s_row}'].font = font_regular
                ws[f'M{s_row}'].alignment = Alignment(horizontal='center')
                ws[f'M{s_row}'].border = border_thin
                
                total_target_row = 4 + len(carrier_counts)
                ws[f'N{s_row}'] = f'=M{s_row}/M{total_target_row}'
                ws[f'N{s_row}'].font = font_regular
                ws[f'N{s_row}'].number_format = '0.0%'
                ws[f'N{s_row}'].alignment = Alignment(horizontal='right')
                ws[f'N{s_row}'].border = border_thin
                s_row += 1
                
            ws[f'L{s_row}'] = 'Всего'
            ws[f'L{s_row}'].font = font_header
            ws[f'L{s_row}'].border = border_total
            
            ws[f'M{s_row}'] = f'=SUM(M4:M{s_row-1})'
            ws[f'M{s_row}'].font = font_header
            ws[f'M{s_row}'].alignment = Alignment(horizontal='center')
            ws[f'M{s_row}'].border = border_total
            
            ws[f'N{s_row}'] = '100%'
            ws[f'N{s_row}'].font = font_header
            ws[f'N{s_row}'].alignment = Alignment(horizontal='right')
            ws[f'N{s_row}'].border = border_total

    wb_out.save(output_path)
    _save_schedule_baseline(output_path, sheets_baseline)
    create_schedule_copy_without_tariffs(output_path)
    print(f"Таблица успешно создана: {output_path}")
    return all_scheduled_trips


def refresh_schedule_file_summary(excel_path):
    """
    Полностью пересобирает листы графика отгрузок после ручных правок пользователя в Excel:
    1. Считывает все рейсы с каждого листа (включая добавленные/измененные вручную строки).
    2. Определяет новые рейсы, которые сотрудник добавил руками, и помечает их цветом (FFF2CC).
    3. Выстраивает направления строго по эталонной очередности из Последовательность направлений.xlsx.
    4. Присваивает корректные номера машин (1, 2, 3...) по каждому направлению в этот день.
    5. Заново рассчитывает время погрузки строго по правилу 2 машины в час (Вс с 08:30, остальные дни с 09:00).
    6. Заново рассчитывает дату и время доставки по обновленному времени отгрузки.
    7. Актуализирует паллеты (40 для Ярославля и Хабаровска по правилам с зеленой заливкой, 33 для остальных).
    8. Актуализирует тарифы по Тарифы.xlsx (если тариф не был задан или был 0, либо изменился перевозчик).
    9. Полностью очищает старую область данных и выводит чистую таблицу в формате A..J (начиная со столбца Месяц).
    10. Пересобирает сводные таблицы по перевозчикам в столбцах L..N и обновляет автофильтр.
    11. Сохраняет файл и автоматически создает/обновляет по тому же пути копию без колонки тарифов.
    """
    if not excel_path or not os.path.exists(excel_path):
        return False
        
    try:
        wb = openpyxl.load_workbook(excel_path)
    except Exception as e:
        print(f"Ошибка открытия файла для обновления: {e}")
        return False
        
    modified = False
    base_dir = os.path.dirname(excel_path)
    tariffs_cached = load_tariffs_matrix(base_dir)
    saved_baseline = _load_schedule_baseline(excel_path)
    inferred_baseline = {}
    
    font_header = Font(name='Arial', size=10, bold=True)
    font_regular = Font(name='Arial', size=10)
    font_bold_center = Font(name='Times New Roman', size=11, bold=True)
    font_city = Font(name='Times New Roman', size=11, bold=True)
    font_carrier = Font(name='Times New Roman', size=11)

    border_thin = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )
    border_header = Border(
        left=Side(style='thin', color='A6A6A6'),
        right=Side(style='thin', color='A6A6A6'),
        top=Side(style='thin', color='A6A6A6'),
        bottom=Side(style='medium', color='000000')
    )
    border_summary_header = Border(
        left=Side(style='thin', color='BFBFBF'),
        right=Side(style='thin', color='BFBFBF'),
        top=Side(style='thin', color='BFBFBF'),
        bottom=Side(style='thin', color='000000')
    )
    border_total = Border(
        top=Side(style='thin', color='000000'),
        bottom=Side(style='double', color='000000')
    )
    fill_40p = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')
    fill_manual = PatternFill(start_color=MANUAL_FILL_COLOR, end_color=MANUAL_FILL_COLOR, fill_type='solid')

    col_widths = {
        'A': 13, 'B': 12, 'C': 15, 'D': 6, 'E': 22, 'F': 15,
        'G': 26, 'H': 14, 'I': 18, 'J': 16, 'K': 5, 'L': 22, 'M': 10, 'N': 10
    }
    
    carriers_norm = {
        'СИЯНИЕТК': 'ТК Сияние',
        'Сияние': 'ТК Сияние',
        'АО "НАЦИОНАЛЬНЫЙ ПЕРЕВОЗЧИК"': 'АО Национальный',
        'Наткар': 'АО Национальный',
        'ООО «Буш-Автопром»': 'Буш-Авто',
        'Буш': 'Буш-Авто',
        'Буш-Авто': 'Буш-Авто',
        'НОРД ЛАЙН МСК': 'НОРДЛАЙН',
        'Норд Лайн': 'НОРДЛАЙН',
        'ООО "ЕМАНТРАНСАВТО"': 'ЕманТрансАвто',
        'ЕманТрансАвто': 'ЕманТрансАвто',
        'ОЛИМП': 'Олимп',
        'Олимп': 'Олимп',
        'ООО "Караван"': 'Караван',
        'Караван': 'Караван',
        'Виллайн': 'Виллайн',
        'ООО ТК "АЗИМУТ"': 'Азимут',
        'АЗИМУТ': 'Азимут',
        'Азимут': 'Азимут',
        'Веб Логистика': 'Веб-Логистика',
        'Веб-Логистика': 'Веб-Логистика',
        'Агро-Авто': 'Агро-Авто',
        'Примум': 'Примум',
        'Васино': 'Васино',
        'ТДМ*': 'ТДМ*',
        'Гринлайн': 'Гринлайн',
        '700 Дорог': '700 Дорог',
        'УТК': 'УТК',
        'ИП Коршунов': 'ИП Коршунов',
        'ИП Мельник': 'ИП Мельник',
        'ИП Гусманов': 'ИП Гусманов',
        'Авангард': 'Авангард'
    }
    
    city_norm = {
        'СПБ': 'Санкт-Петербург',
        'Санкт- Петербург': 'Санкт-Петербург',
        'Санкт-Петербург': 'Санкт-Петербург',
        'УФА': 'Уфа'
    }

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        h_a = str(ws['A1'].value or '').strip().lower()
        h_c = str(ws['C1'].value or '').strip().lower()
        h_e = str(ws['E1'].value or '').strip().lower()

        if 'месяц' in h_a:
            fmt = 'starts_a'
        elif 'месяц' in h_c or 'время' in h_e:
            fmt = 'starts_c'
        elif 'дата' in h_c:
            fmt = 'legacy'
        else:
            has_day = any(d[0] in sheet_name for d in DAYS_MAPPING)
            if has_day:
                fmt = 'starts_a'
            else:
                continue

        # 1. Определяем день недели (d_name) и дату (cur_date)
        d_name = None
        for d_cand, _, _ in DAYS_MAPPING:
            if d_cand in sheet_name:
                d_name = d_cand
                break
                
        cur_date = None
        for r in range(2, min(ws.max_row + 1, 50)):
            val = ws[f'B{r}'].value if fmt == 'starts_a' else (ws[f'D{r}'].value if fmt == 'starts_c' else ws[f'C{r}'].value)
            if isinstance(val, datetime):
                cur_date = datetime(val.year, val.month, val.day)
                break
            elif val:
                try:
                    cur_date = datetime.strptime(str(val).strip(), '%d.%m.%Y')
                    break
                except Exception:
                    pass
                    
        if not cur_date:
            m_d = re.search(r'(\d{1,2})\.(\d{2})', sheet_name)
            if m_d:
                d_n, m_n = int(m_d.group(1)), int(m_d.group(2))
                y_n = datetime.now().year if datetime.now().year >= 2026 else 2026
                cur_date = datetime(y_n, m_n, d_n)
            else:
                cur_date = datetime.now()

        if not d_name and cur_date:
            for d_cand, _, w_idx in DAYS_MAPPING:
                if w_idx == cur_date.weekday():
                    d_name = d_cand
                    break
        if not d_name:
            d_name = 'Пн'
            
        d_short = d_name.lower()
        d_str = cur_date.strftime('%d.%m.%Y')
        d_month = RUSSIAN_MONTHS[cur_date.month]

        # 2. Считываем все существующие строки рейсов и определяем добавленные вручную
        raw_trips = []
        seen_city_trucks = set()

        for r in range(2, ws.max_row + 1):
            if fmt == 'starts_a':
                cell_month = ws[f'A{r}']
                cell_date = ws[f'B{r}']
                cell_time = ws[f'C{r}']
                cell_day = ws[f'D{r}']
                cell_city = ws[f'E{r}']
                cell_truck = ws[f'F{r}']
                cell_car = ws[f'G{r}']
                cell_pal = ws[f'H{r}']
                cell_deliv = ws[f'I{r}']
                cell_tar = ws[f'J{r}']
                row_check_cells = [cell_month, cell_date, cell_time, cell_day, cell_city, cell_truck, cell_car, cell_pal, cell_deliv]
            elif fmt == 'starts_c':
                cell_month = ws[f'C{r}']
                cell_date = ws[f'D{r}']
                cell_time = ws[f'E{r}']
                cell_day = ws[f'F{r}']
                cell_city = ws[f'G{r}']
                cell_truck = ws[f'H{r}']
                cell_car = ws[f'I{r}']
                cell_pal = ws[f'J{r}']
                cell_deliv = ws[f'K{r}']
                cell_tar = ws[f'L{r}']
                row_check_cells = [cell_month, cell_date, cell_time, cell_day, cell_city, cell_truck, cell_car, cell_pal, cell_deliv]
            else:
                cell_date = ws[f'C{r}']
                cell_day = ws[f'D{r}']
                cell_month = ws[f'E{r}']
                cell_truck = ws[f'F{r}']
                cell_city = ws[f'G{r}']
                cell_pal = ws[f'H{r}']
                cell_car = ws[f'I{r}']
                cell_tar = ws[f'J{r}']
                cell_time = ws[f'K{r}']
                cell_deliv = None
                row_check_cells = [cell_date, cell_day, cell_month, cell_truck, cell_city, cell_pal, cell_car, cell_time]

            c_city = cell_city.value
            c_car = cell_car.value
            c_pal = cell_pal.value
            c_tar = cell_tar.value

            if not c_city or not str(c_city).strip():
                continue
            city_str = str(c_city).strip()
            if any(w in city_str.lower() for w in ['итого', 'всего', 'перевозчик', 'рейсы', 'тариф']):
                continue

            city_clean = city_norm.get(city_str, city_str)
            car_raw_str = str(c_car or '').strip()
            car_clean = carriers_norm.get(car_raw_str, car_raw_str) if car_raw_str else 'ТК Сияние'

            # Признак 1: строка уже была выделена цветом (при прошлом обновлении или самим сотрудником)
            already_colored = any(is_cell_manual_fill(c) for c in row_check_cells if c is not None)

            # Признак 2: отсутствуют автоматически генерируемые поля (Месяц, Дата, Время, День, № Машины, Паллеты, Доставка)
            auto_cells = [cell_month, cell_date, cell_time, cell_day, cell_truck, cell_pal]
            if cell_deliv is not None:
                auto_cells.append(cell_deliv)
            missing_auto_fields = any(c.value is None or str(c.value).strip() == '' for c in auto_cells) or not car_raw_str

            # Признак 3: строка добавлена вручную без фирменных границ или шрифта Times New Roman в ячейке направления
            no_border = (cell_city.border is None or cell_city.border.left is None or not cell_city.border.left.style)
            wrong_font = (cell_city.font is not None and cell_city.font.name not in ('Times New Roman', None))
            missing_script_style = no_border or wrong_font

            # Признак 4: строка скопирована из другого дня или другого направления
            copied_mismatch = False
            if not missing_auto_fields:
                # Проверка совпадения даты и дня недели с текущим листом
                if isinstance(cell_date.value, datetime):
                    r_date_s = cell_date.value.strftime('%d.%m.%Y')
                else:
                    r_date_s = str(cell_date.value or '').strip()
                r_day_s = str(cell_day.value or '').strip().lower()
                if (r_date_s and r_date_s != d_str) or (r_day_s and r_day_s != d_short):
                    copied_mismatch = True

                # Проверка дублирования (Направление, Номер машины) при копировании строки
                try:
                    orig_tr_num = int(float(str(cell_truck.value).strip()))
                except Exception:
                    orig_tr_num = None
                if orig_tr_num is not None:
                    if (city_clean, orig_tr_num) in seen_city_trucks:
                        copied_mismatch = True
                    seen_city_trucks.add((city_clean, orig_tr_num))

                # Проверка несоответствия даты доставки направлению (если скопировали чужую строку и поменяли город)
                if cell_deliv is not None and cell_deliv.value:
                    if isinstance(cell_time.value, (datetime, time)):
                        r_time_s = cell_time.value.strftime('%H:%M:%S')
                    else:
                        r_time_s = str(cell_time.value or '09:00:00').strip()
                        if len(r_time_s) == 5:
                            r_time_s += ':00'
                    if isinstance(cell_deliv.value, datetime):
                        r_deliv_s = cell_deliv.value.strftime('%d.%m.%Y %H:%M')
                    else:
                        r_deliv_s = str(cell_deliv.value).strip()
                    exp_deliv_s = calculate_delivery_datetime(cur_date, r_time_s, city_clean).strftime('%d.%m.%Y %H:%M')
                    if r_deliv_s != exp_deliv_s:
                        copied_mismatch = True

            is_manual_row = bool(already_colored or missing_auto_fields or missing_script_style or copied_mismatch)

            tariff_num = 0
            if c_tar is not None:
                try:
                    clean_t = str(c_tar).replace(' ', '').replace('₽', '').replace('\xa0', '').replace(',', '.')
                    tariff_num = int(float(clean_t))
                except Exception:
                    tariff_num = 0

            pallets_num = None
            if c_pal is not None:
                try:
                    p_val = int(float(str(c_pal).strip()))
                    if p_val in (33, 40):
                        pallets_num = p_val
                except Exception:
                    pass

            raw_trips.append({
                'orig_index': len(raw_trips),
                'city': city_clean,
                'carrier': car_clean,
                'tariff': tariff_num,
                'pallets': pallets_num,
                'is_manual': is_manual_row
            })

        if not raw_trips:
            continue

        # Сверка с сохраненным baseline (на случай полного копирования строки со всеми полями)
        if saved_baseline and sheet_name in saved_baseline:
            base_sheet_trips = saved_baseline[sheet_name]
            base_city_counts = Counter(item['city'] for item in base_sheet_trips)
            trips_by_city = defaultdict(list)
            for t in raw_trips:
                trips_by_city[t['city']].append(t)
            for c_name, c_list in trips_by_city.items():
                b_cnt = base_city_counts.get(c_name, 0)
                if len(c_list) > b_cnt:
                    extra_needed = len(c_list) - b_cnt
                    already_man = sum(1 for t in c_list if t.get('is_manual'))
                    to_mark = extra_needed - already_man
                    if to_mark > 0:
                        for t in reversed(c_list):
                            if not t.get('is_manual') and to_mark > 0:
                                t['is_manual'] = True
                                to_mark -= 1

        # 3. Сортируем направления строго по эталонной очередности
        ordered_cities = get_ordered_cities(list({t['city'] for t in raw_trips}), base_dir)
        city_to_rank = {c: i for i, c in enumerate(ordered_cities)}
        sorted_trips = sorted(raw_trips, key=lambda t: (city_to_rank.get(t['city'], 999), 1 if t.get('is_manual') else 0, t['orig_index']))

        # Если baseline для этого файла еще не существовал, фиксируем исходные (не ручные) рейсы
        inferred_baseline[sheet_name] = [
            {'city': t['city'], 'truck_num': i + 1, 'carrier': t['carrier']}
            for i, t in enumerate([x for x in sorted_trips if not x.get('is_manual')])
        ]

        # 4. Нумеруем машины (1, 2, 3...), актуализируем паллеты и тарифы
        city_truck_counters = Counter()
        for t in sorted_trips:
            c = t['city']
            city_truck_counters[c] += 1
            tr_num = city_truck_counters[c]
            t['truck_num'] = tr_num

            # Паллеты: Ярославль и Хабаровск по регламенту
            if c == 'Ярославль':
                t['pallets'] = 40 if (tr_num == 1 or (d_name == 'Вт' and tr_num == 2)) else 33
            elif c == 'Хабаровск':
                t['pallets'] = 40 if (d_name == 'Чт' and tr_num == 1) else 33
            else:
                if t['pallets'] not in (33, 40):
                    t['pallets'] = 33

            # Тариф: автозаполнение из Тарифы.xlsx
            calc_tar = tariffs_cached.get(c, {}).get(t['carrier'], 0)
            known_city_tariffs = set(tariffs_cached.get(c, {}).values())
            if t['tariff'] == 0 and calc_tar > 0:
                t['tariff'] = calc_tar
            elif calc_tar > 0 and t['tariff'] != calc_tar:
                # Если в ячейке остался тариф другого перевозчика по этому городу (сменили перевозчика)
                if t['tariff'] in known_city_tariffs:
                    t['tariff'] = calc_tar

        # 5. Пересчитываем время погрузки строго по правилу: 2 машины в час
        for trip_idx, t in enumerate(sorted_trips):
            time_str = get_trip_time(d_name, t['city'], t['truck_num'], trip_index_in_day=trip_idx)
            t['time'] = time_str
            deliv_dt = calculate_delivery_datetime(cur_date, time_str, t['city'])
            t['delivery_dt'] = deliv_dt
            t['delivery_str'] = deliv_dt.strftime('%d.%m.%Y %H:%M')

        # 6. Полностью очищаем старые данные на листе
        max_clear = max(ws.max_row + 15, len(sorted_trips) + 20, 50)
        for r in range(2, max_clear + 1):
            for col_l in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P']:
                cell = ws[f'{col_l}{r}']
                cell.value = None
                cell.border = None
                cell.fill = PatternFill(fill_type=None)

        # 7. Настраиваем заголовки чистого формата A..J
        ws.views.sheetView[0].showGridLines = True
        for col_l, w in col_widths.items():
            ws.column_dimensions[col_l].width = w
        ws.row_dimensions[1].height = 26.0

        headers = {
            'A': 'Месяц', 'B': 'Дата', 'C': 'Время погрузки', 'D': '',
            'E': 'Направление', 'F': 'Номер Машины', 'G': 'Перевозчик',
            'H': 'Кол-во паллет ', 'I': 'Дата доставки', 'J': 'Тариф'
        }
        for col_l, h_text in headers.items():
            cell = ws[f'{col_l}1']
            cell.value = h_text
            cell.font = font_header
            cell.border = border_header
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

        # 8. Записываем пересобранные строки
        row_idx = 2
        for tr in sorted_trips:
            c_a = ws[f'A{row_idx}']
            c_a.value = d_month
            c_a.font = font_regular
            c_a.alignment = Alignment(horizontal='center', vertical='center')
            c_a.border = border_thin

            c_b = ws[f'B{row_idx}']
            c_b.value = d_str
            c_b.font = font_regular
            c_b.alignment = Alignment(horizontal='center', vertical='center')
            c_b.border = border_thin

            c_c = ws[f'C{row_idx}']
            c_c.value = tr['time']
            c_c.font = font_regular
            c_c.alignment = Alignment(horizontal='center', vertical='center')
            c_c.border = border_thin

            c_d = ws[f'D{row_idx}']
            c_d.value = d_short
            c_d.font = font_header
            c_d.alignment = Alignment(horizontal='center', vertical='center')
            c_d.border = border_thin

            c_e = ws[f'E{row_idx}']
            c_e.value = tr['city']
            c_e.font = font_city
            c_e.alignment = Alignment(horizontal='left', vertical='center')
            c_e.border = border_thin

            c_f = ws[f'F{row_idx}']
            c_f.value = tr['truck_num']
            c_f.font = font_bold_center
            c_f.alignment = Alignment(horizontal='center', vertical='center')
            c_f.border = border_thin

            c_g = ws[f'G{row_idx}']
            c_g.value = tr['carrier']
            c_g.font = font_carrier
            c_g.alignment = Alignment(horizontal='center', vertical='center')
            c_g.border = border_thin

            c_h = ws[f'H{row_idx}']
            c_h.value = tr['pallets']
            c_h.font = font_regular
            c_h.alignment = Alignment(horizontal='center', vertical='center')
            c_h.border = border_thin

            c_i = ws[f'I{row_idx}']
            c_i.value = tr['delivery_str']
            c_i.font = font_regular
            c_i.alignment = Alignment(horizontal='center', vertical='center')
            c_i.border = border_thin

            c_j = ws[f'J{row_idx}']
            c_j.value = tr.get('tariff', 0)
            c_j.font = font_regular
            c_j.number_format = '#,##0 ₽'
            c_j.alignment = Alignment(horizontal='right', vertical='center')
            c_j.border = border_thin

            # Приоритет цветовой заливки:
            # 1) Новые рейсы, добавленные сотрудником вручную -> желтый (FFF2CC)
            # 2) Рейсы на 40 паллет -> светло-зеленый (E2EFDA)
            if tr.get('is_manual'):
                for col_l in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J']:
                    ws[f'{col_l}{row_idx}'].fill = fill_manual
            elif tr['pallets'] == 40:
                for col_l in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J']:
                    ws[f'{col_l}{row_idx}'].fill = fill_40p

            ws.row_dimensions[row_idx].height = 18.0
            row_idx += 1

        last_data_row = row_idx - 1
        if last_data_row >= 2:
            ws.auto_filter.ref = f'A1:J{last_data_row}'

        # 9. Формируем актуальную сводную таблицу по перевозчикам в L..N
        carrier_counts = Counter([t['carrier'] for t in sorted_trips if t.get('carrier')])
        if carrier_counts:
            ws['L3'] = 'Перевозчик'
            ws['L3'].font = font_header
            ws['L3'].border = border_summary_header
            ws['M3'] = 'Рейсы'
            ws['M3'].font = font_header
            ws['M3'].alignment = Alignment(horizontal='center')
            ws['M3'].border = border_summary_header
            ws['N3'] = '%'
            ws['N3'].font = font_header
            ws['N3'].alignment = Alignment(horizontal='center')
            ws['N3'].border = border_summary_header

            s_row = 4
            for car, cnt in carrier_counts.most_common():
                ws[f'L{s_row}'] = car
                ws[f'L{s_row}'].font = font_regular
                ws[f'L{s_row}'].border = border_thin

                ws[f'M{s_row}'] = f'=COUNTIF(G$2:G${last_data_row}, L{s_row})'
                ws[f'M{s_row}'].font = font_regular
                ws[f'M{s_row}'].alignment = Alignment(horizontal='center')
                ws[f'M{s_row}'].border = border_thin

                total_target_row = 4 + len(carrier_counts)
                ws[f'N{s_row}'] = f'=M{s_row}/M{total_target_row}'
                ws[f'N{s_row}'].font = font_regular
                ws[f'N{s_row}'].number_format = '0.0%'
                ws[f'N{s_row}'].alignment = Alignment(horizontal='right')
                ws[f'N{s_row}'].border = border_thin
                s_row += 1

            ws[f'L{s_row}'] = 'Всего'
            ws[f'L{s_row}'].font = font_header
            ws[f'L{s_row}'].border = border_total

            ws[f'M{s_row}'] = f'=SUM(M4:M{s_row-1})'
            ws[f'M{s_row}'].font = font_header
            ws[f'M{s_row}'].alignment = Alignment(horizontal='center')
            ws[f'M{s_row}'].border = border_total

            ws[f'N{s_row}'] = '100%'
            ws[f'N{s_row}'].font = font_header
            ws[f'N{s_row}'].alignment = Alignment(horizontal='right')
            ws[f'N{s_row}'].border = border_total

        modified = True

    if modified:
        try:
            wb.save(excel_path)
            if not saved_baseline and inferred_baseline:
                _save_schedule_baseline(excel_path, inferred_baseline)
            create_schedule_copy_without_tariffs(excel_path)
            print(f"График успешно пересобран и сохранен в: {excel_path}")
        except Exception as e:
            print(f"Ошибка сохранения файла {excel_path}: {e}")
            wb.close()
            return False

    wb.close()
    return True

def generate_carrier_html(carrier_name, trips, start_date_str, end_date_str):
    """Генерирует индивидуальное конфиденциальное HTML-письмо для конкретного перевозчика"""
    rows_html = ""
    for tr in trips:
        bg_color = "#e2efda" if tr.get('pallets') == 40 else "#ffffff"
        deliv_str = tr.get('delivery_str') or tr.get('deliv_date') or ''
        day_str = tr.get('day') or ''
        city_str = tr.get('city') or tr.get('route') or ''
        time_str = str(tr.get('time', ''))[:5]
        truck_str = str(tr.get('truck_num', ''))
        pal_str = str(tr.get('pallets', ''))
        rows_html += f"""
        <tr style="background-color: {bg_color}; text-align: center;">
            <td style="padding: 8px; border: 1px solid #d9d9d9;">{tr.get('date', '')}</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9; font-weight: bold;">{day_str}</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9; font-weight: bold; color: #1f4e79;">{time_str}</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9; text-align: left; font-weight: bold;">{city_str}</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9;">№ {truck_str}</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9;">{pal_str} пал.</td>
            <td style="padding: 8px; border: 1px solid #d9d9d9; font-weight: bold; color: #1f4e79;">{deliv_str}</td>
        </tr>
        """
        
    period_str = f"{start_date_str} – {end_date_str}" if start_date_str != end_date_str else start_date_str

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; font-size: 14px; color: #333333; line-height: 1.5; }}
            table {{ border-collapse: collapse; width: 100%; max-width: 720px; margin-top: 15px; margin-bottom: 20px; }}
            th {{ background-color: #2f5597; color: #ffffff; padding: 10px; border: 1px solid #2f5597; text-align: center; }}
            .notice {{ background-color: #fff2cc; border-left: 4px solid #d6b656; padding: 12px; margin: 15px 0; font-size: 13px; }}
            .footer {{ font-size: 12px; color: #7f7f7f; margin-top: 25px; border-top: 1px solid #e0e0e0; padding-top: 10px; }}
        </style>
    </head>
    <body>
        <p>Добрый день!</p>
        <p>Направляем согласованный график погрузки на период <b>{period_str}</b>.<br>
        Просим ознакомиться с графиком и обеспечить его соблюдение.</p>
        
        <p><b>Важные условия регламента:</b><br>
        • Погрузка на складе осуществляется строго из расчёта 2 машины в час.<br>
        • Просьба обеспечить своевременное прибытие ТС к назначенному тайм-слоту, без опозданий.<br>
        • В случае возникновения задержек в пути необходимо оперативно информировать ответственных лиц, чтобы своевременно скорректировать дальнейшее планирование.</p>
        
        <table>
            <thead>
                <tr>
                    <th>Дата погрузки</th>
                    <th>День</th>
                    <th>Время</th>
                    <th>Направление</th>
                    <th>№ ТС</th>
                    <th>Паллеты</th>
                    <th>Дата доставки</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
        
        <p>С уважением,<br>
        ФК «ПУЛЬС»</p>
        
        <div class="footer">
            Данное сообщение сформировано автоматически и предназначено исключительно для перевозчика {carrier_name}. Конфиденциально.
        </div>
    </body>
    </html>
    """
    return html

def send_emails_via_outlook(all_trips, target_email="n.rozhkov@puls.ru", draft_mode=False):
    """
    Отправляет индивидуальные письма через установленный Microsoft Outlook.
    Каждый перевозчик получает ТОЛЬКО свое расписание!
    В тестовом режиме все письма отправляются на адрес target_email.
    """
    try:
        import win32com.client as win32
    except ImportError:
        print("\n[ВНИМАНИЕ] Библиотека 'pywin32' не установлена.")
        print("Для отправки через Outlook выполните: pip install pywin32")
        print("\nПоказываем текстовый предпросмотр писем:\n")
        preview_emails_console(all_trips, target_email)
        return

    # Группируем рейсы по перевозчикам
    carrier_trips = defaultdict(list)
    for tr in all_trips:
        car = tr['carrier']
        carrier_trips[car].append(tr)

    dates = [tr['date'] for tr in all_trips]
    start_d = dates[0] if dates else ''
    end_d = dates[-1] if dates else ''

    print("\n" + "="*70)
    print(f"ПОДКЛЮЧЕНИЕ К OUTLOOK: Подготовка {len(carrier_trips)} писем (получатель: {target_email})")
    print("="*70)

    try:
        outlook = win32.Dispatch('outlook.application')
    except Exception as e:
        print(f"Ошибка подключения к приложению Outlook: {e}")
        print("Убедитесь, что Microsoft Outlook запущен на компьютере.")
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    contacts = {}
    for fname in ['contacts_schedules.json', 'carriers_contacts.json']:
        contacts_file = os.path.join(base_dir, fname)
        if os.path.exists(contacts_file):
            try:
                with open(contacts_file, 'r', encoding='utf-8') as f:
                    contacts = json.load(f)
                    break
            except Exception:
                pass

    sent_count = 0
    for carrier, c_trips in sorted(carrier_trips.items()):
        # Сортируем рейсы перевозчика по хронологии
        sorted_trips = sorted(c_trips, key=lambda x: (x.get('dt') or datetime.strptime(x['date'], '%d.%m.%Y'), x['time']))
        
        recipient = contacts.get(carrier, target_email)
        mail = outlook.CreateItem(0) # 0 = olMailItem
        mail.To = recipient
        mail.Subject = f"[{carrier}] График погрузки РЦ Черная Грязь ({start_d} - {end_d})"
        mail.HTMLBody = generate_carrier_html(carrier, sorted_trips, start_d, end_d)
        
        if draft_mode:
            mail.Save() # Сохранить в папку "Черновики" Outlook
            print(f"  [ЧЕРНОВИК СОХРАНЕН] -> {carrier:25s} ({len(sorted_trips)} рейсов)")
        else:
            mail.Send() # Отправить
            print(f"  [ОТПРАВЛЕНО В OUTLOOK] -> {carrier:25s} ({len(sorted_trips)} рейсов)")
            
        sent_count += 1

    mode_text = "сохранены в Черновиках" if draft_mode else "успешно отправлены"
    print("="*70)
    print(f"ИТОГО: {sent_count} индивидуальных писем {mode_text} через Outlook на адрес {target_email}!")
    print("Каждое письмо содержит ТОЛЬКО рейсы соответствующего перевозчика.")

def preview_emails_console(all_trips, target_email):
    carrier_trips = defaultdict(list)
    for tr in all_trips:
        carrier_trips[tr['carrier']].append(tr)

    dates = [tr['date'] for tr in all_trips]
    start_d = dates[0] if dates else ''
    end_d = dates[-1] if dates else ''

    for carrier, c_trips in sorted(carrier_trips.items()):
        print(f"\n[ТЕМА]: [{carrier}] График погрузки РЦ Черная Грязь ({start_d} - {end_d})")
        print(f"[КОМУ]: {target_email} (в боевом режиме: диспетчер {carrier})")
        print(f"Рейсы перевозчика ({len(c_trips)} шт.):")
        for tr in c_trips:
            deliv_info = f" -> Доставка: {tr.get('delivery_str')}" if tr.get('delivery_str') else ""
            print(f"  • {tr['date']} ({tr['day']}) в {tr['time'][:5]} -> {tr['city']:15s} (ТС #{tr['truck_num']}, {tr['pallets']} пал.){deliv_info}")


def get_schedules_dir():
    """
    Возвращает путь к папке 'Готовые недельные графики'.
    Приоритет: корпоративный сетевой/облачный диск L:
    L:\\ОТДЕЛЫ\\ТРАНСПОРТНЫЙ ОТДЕЛ\\Рожков\\РК недельные графики + заявки\\Готовые недельные графики
    Резерв: Рабочий стол / Готовые недельные графики
    """
    network_base = r"L:\ОТДЕЛЫ\ТРАНСПОРТНЫЙ ОТДЕЛ\Рожков\РК недельные графики + заявки\Готовые недельные графики"
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
            folder = os.path.join(d, "Готовые недельные графики")
            os.makedirs(folder, exist_ok=True)
            return folder

    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Готовые недельные графики")
    os.makedirs(fallback, exist_ok=True)
    return fallback

if __name__ == '__main__':
    base_dir = os.path.dirname(os.path.abspath(__file__))
    default_plan = find_default_plan_file(base_dir)
    schedules_dir = get_schedules_dir()
    default_out = os.path.join(schedules_dir, 'Недельные графики (готовый).xlsx')
    
    parser = argparse.ArgumentParser(description='Генератор графика отгрузок из плана .xlsm и отправка в Outlook')
    parser.add_argument('--plan', default=default_plan, help='Путь к файлу График_отгрузки_филиалов_неделя_2.xlsm')
    parser.add_argument('--out', default=default_out, help='Путь к результирующему файлу Excel')
    parser.add_argument('--send-outlook', action='store_true', help='Отправить индивидуальные письма через Outlook')
    parser.add_argument('--draft', action='store_true', help='Сохранить письма в черновики Outlook вместо отправки')
    parser.add_argument('--email', default='n.rozhkov@puls.ru', help='Тестовый адрес получателя писем')
    
    args = parser.parse_args()
    
    if not args.plan or not os.path.exists(args.plan):
        print(f"ОШИБКА: Файл плана отгрузок (*.xlsm) не найден в папке {base_dir}!")
        sys.exit(1)
        
    print(f"Обработка плана отгрузок: {args.plan}")
    plan_trips, start_date = parse_plan_file(args.plan)
    all_trips = build_schedule_from_plan(plan_trips, start_date, args.out)
    
    # Если указан флаг отправки
    if args.send_outlook or args.draft:
        send_emails_via_outlook(all_trips, target_email=args.email, draft_mode=args.draft)
    else:
        print("\n[ПОДСКАЗКА]:")
        print(f"1. Чтобы сохранить черновики в Outlook для проверки:")
        print(f"   python generate_schedule.py --draft")
        print(f"2. Чтобы сразу отправить письма на {args.email}:")
        print(f"   python generate_schedule.py --send-outlook")
