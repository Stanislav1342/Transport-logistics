import io
import os
import tempfile
from datetime import datetime, timedelta
from typing import BinaryIO, Optional, Union

import openpyxl
import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def _read_excel_or_csv(source: Union[str, os.PathLike, BinaryIO, bytes], is_percent: bool = False) -> pd.DataFrame:
    """Универсально читает DataFrame из пути к файлу, байтов или файлового потока."""
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    elif hasattr(source, "read") and hasattr(source, "seek"):
        source.seek(0)
    elif isinstance(source, (str, os.PathLike)):
        if not os.path.exists(source):
            raise FileNotFoundError(f"Файл не найден по пути: {source}")

    if is_percent:
        return pd.read_excel(source, header=None, engine='openpyxl')

    if isinstance(source, (str, os.PathLike)) and str(source).lower().endswith('.csv'):
        return pd.read_csv(source)

    try:
        return pd.read_excel(source, engine='openpyxl')
    except Exception as e:
        if isinstance(source, (str, os.PathLike)):
            try:
                with open(source, 'rb') as f:
                    sig = f.read(8)
                if not sig.startswith(b'PK'):
                    raise ValueError(
                        f"Файл '{source}' сохранен 1С не как XLSX (сигнатура: {sig}). "
                        "Возможно, 1С сохранила его в формате MXL. Перезапустите выгрузку."
                    ) from e
            except Exception:
                pass
        raise e

def determine_title(row):
    """Определение номера титула на основе текущего шага и состояния ЭДО."""
    step = str(row.get('Текущий шаг', '')).strip()
    status = str(row.get('Состояние ЭДО', '')).strip()

    if status in ['Не начат', 'Требуется исправление']:
        return ''

    if step == 'Оформление' and status == 'Ожидается извещение о получении':
        return 2
    elif (step == 'Погрузка' and status == 'Ожидается подтверждение') or \
         (step == 'Оформление' and status == 'Ожидается подтверждение') or \
         (step == 'Приемка' and status == 'Требуется подписание'):
        return 3
    elif (step == 'Приемка' and status == 'Ожидается извещение о получении') or \
         (step == 'Приемка' and status == 'Ожидается подтверждение'):
        return 4
    
    return ''

def style_worksheet(ws):
    """Функция для наведения красоты на любом переданном листе Excel."""
    font_header = Font(name='Calibri', size=11, bold=True)
    font_body = Font(name='Calibri', size=11)
    fill_header = PatternFill(start_color='E0E0E0', end_color='E0E0E0', fill_type='solid') 
    thin_border = Side(border_style="thin", color="808080")
    border_all = Border(left=thin_border, right=thin_border, top=thin_border, bottom=thin_border)

    align_center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    align_left = Alignment(horizontal='left', vertical='center')

    # Форматируем шапку
    ws.row_dimensions[1].height = 25
    for col_num in range(1, ws.max_column + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_all

    # Форматируем тело таблицы
    for row_num in range(2, ws.max_row + 1):
        ws.row_dimensions[row_num].height = 18
        for col_num in range(1, ws.max_column + 1):
            cell = ws.cell(row=row_num, column=col_num)
            cell.font = font_body
            cell.border = border_all
            
            col_name = ws.cell(row=1, column=col_num).value
            # Центрируем цифры и даты, а тексты (ФИО, статусы) прижимаем влево
            if col_name in ['Номер', 'Дата', 'Не подписанный титул №', 
                            'Водитель получении', 'Клиент', 'Водитель сдаче', 'Требуется исправление']:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

    # Автоподбор ширины
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max(max_len + 7, 16)

    # Автофильтр
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"


def process_and_style_etrn(
    input_filepath: Union[str, os.PathLike, BinaryIO, bytes],
    model_filepath: Union[str, os.PathLike, BinaryIO, bytes],
    percent_filepath: Union[str, os.PathLike, BinaryIO, bytes],
    output_filepath: Optional[Union[str, os.PathLike]] = None,
) -> bytes:
    """
    Обрабатывает исходные таблицы ЭТРН, моделей расчетов и процента,
    формирует стилизованный Excel-файл с двумя листами: «ЭТРН» и «Отчет».
    
    Поддерживает:
    - Пути к файлам на диске (str, Path)
    - Файловые потоки в памяти (io.BytesIO, UploadFile.file)
    - Байтовые данные (bytes)
    
    Возвращает:
    - bytes: Содержимое готового XLSX-документа.
    - При указании output_filepath также сохраняет файл на диск.
    """
    source_name = str(input_filepath) if isinstance(input_filepath, (str, os.PathLike)) else "память"
    print(f"\n[ЭТРН-Оптимизатор] Чтение исходных данных ЭТРН ({source_name})...")
    df = _read_excel_or_csv(input_filepath)

    model_name = str(model_filepath) if isinstance(model_filepath, (str, os.PathLike)) else "память"
    print(f"[ЭТРН-Оптимизатор] Чтение файла моделей расчетов ({model_name})...")
    model_df = _read_excel_or_csv(model_filepath)

    percent_name = str(percent_filepath) if isinstance(percent_filepath, (str, os.PathLike)) else "память"
    print(f"[ЭТРН-Оптимизатор] Чтение файла отчета подписания ({percent_name})...")
    percent_df = _read_excel_or_csv(percent_filepath, is_percent=True)

    # --- ЭТАП 1: ОБРАБОТКА ДАННЫХ (pandas) ---
    df = df[df['Состояние ЭДО'].astype(str).str.strip() != 'Завершен'].copy()

    df['Перевозчик'] = df['Перевозчик'].astype(str).str.strip()
    model_df['Перевозчик'] = model_df['Перевозчик'].astype(str).str.strip()
    model_df['Номер'] = model_df['Номер'].astype(str).str.strip()

    df['Короткий номер'] = (
        df['Номер']
        .fillna('')
        .astype(str)
        .str.split('.')
        .str[0]
        .replace('nan', '')
        .str.strip()
    )

    model_df = model_df[['Номер', 'Водитель', 'Перевозчик', 'Модель расчетов']].drop_duplicates(subset=['Номер', 'Перевозчик'])

    df = df.merge(
        model_df, 
        left_on=['Короткий номер', 'Перевозчик'], 
        right_on=['Номер', 'Перевозчик'], 
        how='left'
    )

    df['Не подписанный титул №'] = df.apply(determine_title, axis=1)
    df['Запрос в ТП'] = ''

    df['Водитель'] = df['Водитель'].fillna('')
    df['Модель расчетов'] = df['Модель расчетов'].fillna('')

    # --- ЭТАП 2: ФОРМИРОВАНИЕ ОТЧЕТА ---
    print("[ЭТРН-Оптимизатор] Формирование сводного отчета по водителям...")
    report_data = []
    
    # Группируем данные по связке Перевозчик + Водитель + Модель расчетов
    for (date, carrier, driver, model), group in df.groupby(['Дата', 'Перевозчик', 'Водитель', 'Модель расчетов']):
        count_2 = (group['Не подписанный титул №'] == 2).sum()
        count_3 = (group['Не подписанный титул №'] == 3).sum()
        count_4 = (group['Не подписанный титул №'] == 4).sum()
        count_empty = (group['Не подписанный титул №'] == '').sum()
        
        report_data.append({
            'Дата': date,
            'Перевозчик': carrier,
            'Водитель': driver,
            'Модель расчетов': model,
            'Титул №2': count_2,
            'Титул №3': count_3,
            'Титул №4': count_4,
            'Требуется исправление/Ошибка': count_empty
        })
        
    report_df = pd.DataFrame(report_data)

    # =================================================================
    # --- ЭТАП 2.1: ДОБАВЛЕНИЕ СРЕДНЕГО ВРЕМЕНИ ИЗ ФАЙЛА ПРОЦЕНТ ---
    # =================================================================
    print("[ЭТРН-Оптимизатор] Расчет среднего времени подписания перевозчиков...")
    time_col_idx = 2 
    for r in range(min(15, percent_df.shape[0])):
        for c in range(percent_df.shape[1]):
            cell_val = str(percent_df.iloc[r, c]).strip().lower().replace('\n', ' ')
            if 'среднее время (мин)' in cell_val:
                time_col_idx = c
                break
        else:
            continue
        break
        
    valid_carriers = set(report_df['Перевозчик'].unique())
    carrier_times = {}
    
    for r in range(percent_df.shape[0]):
        val_a = str(percent_df.iloc[r, 0]).strip()
        if val_a in valid_carriers:
            time_val = percent_df.iloc[r, time_col_idx]
            if pd.isna(time_val):
                time_val = ''
            carrier_times[val_a] = time_val
            
    report_df['Среднее время (мин)'] = report_df['Перевозчик'].map(carrier_times).fillna('')

    # --- ЭТАП 3: РАССТАНОВКА СТОЛБЦОВ В ОСНОВНОМ ЛИСТЕ ---
    cols = list(df.columns)
    if 'Короткий номер' in cols: cols.remove('Короткий номер')
    if 'Номер_y' in cols: cols.remove('Номер_y')
    if 'Водитель' in cols: cols.remove('Водитель')
    if 'Модель расчетов' in cols: cols.remove('Модель расчетов')
    if 'Номер_x' in cols:
        cols[cols.index('Номер_x')] = 'Номер'
        df.rename(columns={'Номер_x': 'Номер'}, inplace=True)

    insert_idx = cols.index('Перевозчик') + 1
    cols.insert(insert_idx, 'Водитель')
    cols.insert(insert_idx + 1, 'Модель расчетов')
    df = df[cols]

    # --- ЭТАП 4: СОХРАНЕНИЕ 2 ЛИСТОВ И КОСМЕТИКА (ПОТОКОБЕЗОПАСНО) ---
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(temp_fd)

    try:
        with pd.ExcelWriter(temp_path, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='ЭТРН', index=False)
            report_df.to_excel(writer, sheet_name='Отчет', index=False)
        
        print("[ЭТРН-Оптимизатор] Стилизация документа (автоширина, автофильтр, шрифты)...")
        wb = openpyxl.load_workbook(temp_path)
        for sheet_name in wb.sheetnames:
            style_worksheet(wb[sheet_name])

        out_stream = io.BytesIO()
        wb.save(out_stream)
        result_bytes = out_stream.getvalue()

        if output_filepath:
            out_str = str(output_filepath)
            os.makedirs(os.path.dirname(os.path.abspath(out_str)), exist_ok=True)
            with open(out_str, "wb") as f:
                f.write(result_bytes)
            print(f"[ЭТРН-Оптимизатор] ✓ Итоговый отчет сохранен: {out_str}")

        return result_bytes

    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def get_date_from_user():
    """Спрашивает у пользователя, какую дату использовать в названии файла."""
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    
    today_str = today.strftime("%d.%m.%Y")
    yesterday_str = yesterday.strftime("%d.%m.%Y")
    
    print("-" * 40)
    print("КАКУЮ ДАТУ УКАЗАТЬ В НАЗВАНИИ ФАЙЛА?")
    print(f"1 - Вчерашняя ({yesterday_str}) [по умолчанию]")
    print(f"2 - Сегодняшняя ({today_str})")
    print("3 - Ввести дату вручную")
    print("-" * 40)
    
    choice = input("Ваш выбор (1, 2, 3 или Enter): ").strip()
    
    if choice == '2':
        return today_str
    elif choice == '3':
        manual_date = input("Введите нужную дату (например, 01.01.2026): ").strip()
        return manual_date if manual_date else yesterday_str
    else:
        return yesterday_str


if __name__ == "__main__":
    desktop_path = os.path.join(os.path.expanduser('~'), 'Desktop')
    
    INPUT_FILE = os.path.join(desktop_path, 'ЭТРН 1с.xlsx')  
    MODEL_FILE = os.path.join(desktop_path, 'Модель расчетов.xlsx')  
    PERCENT_FILE = os.path.join(desktop_path, 'Процент.xlsx')
    
    output_dir = os.path.join(desktop_path, 'ЭТРН')
    os.makedirs(output_dir, exist_ok=True)
    
    selected_date = get_date_from_user()
    OUTPUT_FILE = os.path.join(output_dir, f'ЭТРН_{selected_date}.xlsx') 
    
    process_and_style_etrn(INPUT_FILE, MODEL_FILE, PERCENT_FILE, OUTPUT_FILE)