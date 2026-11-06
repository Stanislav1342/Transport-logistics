# 🚀 Инструкция по интеграции модуля ЭТРН в веб-сервер «Транспортная логистика»

Данный документ описывает, как подключить отчеты по **ЭТРН** в корпоративный веб-портал на базе **FastAPI** ([`server.py`](file:///Users/stanislavhvostov/Documents/Транспортная%20логистика/server.py)).

---

## 📦 Архитектура решения

Модуль спроектирован так, чтобы встраиваться в существующую структуру портала без переделки кода:

```
[templates/index.html]                [server.py (FastAPI)]
  ┌───────────────────────┐            ┌──────────────────────────────────┐
  │ Вкладка «ЭТРН»:       │            │ app.include_router(etrn_router)  │
  │  • Выбор дат          ├───────────►│   POST /api/etrn/generate-task   │
  │  • Drag-and-Drop      │            │   POST /api/etrn/process-files   │
  │  • Прогресс-бар       │◄───────────┤   GET  /api/etrn/task-status     │
  │  • Кнопка «Скачать»   │            │   GET  /api/etrn/download        │
  └───────────────────────┘            └─────────────────┬────────────────┘
                                                         │
                                               [etrn_service.py]
                                                         │
                                        ┌────────────────┴────────────────┐
                                        ▼                                 ▼
                               [1C Фоновый экспорт]             [etrn_optimizer.py]
                               (.epf / headless batch)          (в памяти: pandas + openpyxl)
```

В веб-интерфейсе пользователю доступны **два режима**:
1. **Автоматический сбор из 1С**: выбирается дата/период ➔ сервер в фоне запускает 1С ➔ собирает и отдает готовый файл.
2. **Мгновенная сборка из 3 файлов (Drag-and-Drop)**: если файлы уже есть на диске, пользователь просто перетаскивает их в браузер и за 1 секунду получает готовый отчет без обращения к 1С.

---

## 🛠 Шаг 1. Подключение роутера в `server.py`

В файл `server.py` проекта **«Транспортная логистика»** достаточно добавить всего 2 строки:

```python
# 1. Импорт в начале server.py
from api_router import router as etrn_router

# 2. Подключение к приложению FastAPI (рядом с другими роутерами)
app.include_router(etrn_router)
```

Всё! Сервер сразу получит все эндпоинты:
* `POST /api/etrn/process-files` — моментальная сборка из загруженных файлов.
* `POST /api/etrn/generate-task` — запуск фоновой задачи по диапазону дат.
* `GET /api/etrn/task-status/{task_id}` — проверка статуса и прогресса (0–100%).
* `GET /api/etrn/download/{task_id}` — отдача готового Excel-файла.

---

## 🎨 Шаг 2. Добавление вкладки в `templates/index.html`

### 2.1. Добавление кнопки в боковое меню (навигацию)
В блок навигации (рядом с `nav-opt` и `nav-thermo`):

```html
<button class="nav-item" id="nav-etrn" onclick="openSection('etrn')">
  <span class="nav-icon">📑</span>
  <span class="nav-label">Отчет ЭТРН</span>
</button>
```

### 2.2. Добавление содержимого раздела
В основной контейнер контента (рядом с секциями `section-opt`, `section-thermo`):

```html
<!-- СЕКЦИЯ: ЭТРН -->
<div id="section-etrn" class="section-content" style="display: none;">
  <div class="card">
    <div class="card-header">
      <h2>📑 Сводный отчет по подписанию ЭТРН</h2>
      <p class="subtitle">Автоматическое сведение реестров, титулов и расчет среднего времени подписания</p>
    </div>

    <div class="card-body">
      <!-- Вкладки режимов -->
      <div class="mode-tabs">
        <button class="btn btn-tab active" id="tab-etrn-auto" onclick="switchEtrnMode('auto')">🚀 Автосбор из 1С</button>
        <button class="btn btn-tab" id="tab-etrn-manual" onclick="switchEtrnMode('manual')">📁 Загрузка 3 файлов</button>
      </div>

      <!-- РЕЖИМ 1: Автосбор из 1С -->
      <div id="etrn-mode-auto" class="etrn-pane">
        <div class="form-row">
          <div class="form-group">
            <label for="etrn-date-from">Дата начала:</label>
            <input type="date" id="etrn-date-from" class="form-control">
          </div>
          <div class="form-group">
            <label for="etrn-date-to">Дата окончания:</label>
            <input type="date" id="etrn-date-to" class="form-control">
          </div>
        </div>
        <button class="btn btn-primary" onclick="startEtrnAutoTask()">Сформировать отчет из 1С</button>
      </div>

      <!-- РЕЖИМ 2: Загрузка файлов вручную -->
      <div id="etrn-mode-manual" class="etrn-pane" style="display: none;">
        <form id="etrn-upload-form" onsubmit="submitEtrnFiles(event)">
          <div class="form-group">
            <label>1. Файл «ЭТРН 1с.xlsx»:</label>
            <input type="file" name="etrn_file" accept=".xlsx,.csv" required class="form-control">
          </div>
          <div class="form-group">
            <label>2. Файл «Модель расчетов.xlsx»:</label>
            <input type="file" name="model_file" accept=".xlsx" required class="form-control">
          </div>
          <div class="form-group">
            <label>3. Файл «Процент.xlsx»:</label>
            <input type="file" name="percent_file" accept=".xlsx" required class="form-control">
          </div>
          <button type="submit" class="btn btn-success">Собрать и скачать отчет</button>
        </form>
      </div>

      <!-- Индикатор прогресса -->
      <div id="etrn-progress-box" style="display: none; margin-top: 20px;">
        <div class="progress-bar-container">
          <div id="etrn-progress-bar" class="progress-bar" style="width: 0%;"></div>
        </div>
        <p id="etrn-progress-text" class="progress-text">Инициализация...</p>
      </div>

      <!-- Результат / кнопка скачивания -->
      <div id="etrn-result-box" style="display: none; margin-top: 20px;" class="alert alert-success">
        <p>✓ Отчет успешно сформирован!</p>
        <a id="etrn-download-link" href="#" class="btn btn-primary" download>📥 Скачать готовый отчет</a>
      </div>
    </div>
  </div>
</div>
```

### 2.3. Добавление JavaScript-логики
В блок `<script>`:

```javascript
// Переключение режимов (Авто / Ручной)
function switchEtrnMode(mode) {
  document.getElementById('etrn-mode-auto').style.display = mode === 'auto' ? 'block' : 'none';
  document.getElementById('etrn-mode-manual').style.display = mode === 'manual' ? 'block' : 'none';
  document.getElementById('tab-etrn-auto').classList.toggle('active', mode === 'auto');
  document.getElementById('tab-etrn-manual').classList.toggle('active', mode === 'manual');
}

// Запуск фоновой выгрузки из 1С
async function startEtrnAutoTask() {
  const dFromRaw = document.getElementById('etrn-date-from').value;
  const dToRaw = document.getElementById('etrn-date-to').value;
  if (!dFromRaw) { alert('Укажите дату!'); return; }

  const formatDate = (iso) => {
    const [y, m, d] = iso.split('-');
    return `${d}.${m}.${y}`;
  };

  const payload = {
    date_from: formatDate(dFromRaw),
    date_to: dToRaw ? formatDate(dToRaw) : formatDate(dFromRaw)
  };

  document.getElementById('etrn-progress-box').style.display = 'block';
  document.getElementById('etrn-result-box').style.display = 'none';

  try {
    const res = await fetch('/api/etrn/generate-task', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Ошибка запуска');
    
    pollEtrnTask(data.task_id);
  } catch (err) {
    alert('Ошибка: ' + err.message);
    document.getElementById('etrn-progress-box').style.display = 'none';
  }
}

// Опрос статуса фоновой задачи
function pollEtrnTask(taskId) {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/etrn/task-status/${taskId}`);
      const task = await res.json();
      
      const pBar = document.getElementById('etrn-progress-bar');
      const pText = document.getElementById('etrn-progress-text');
      pBar.style.width = (task.progress || 0) + '%';
      pText.innerText = task.message || 'Обработка...';

      if (task.status === 'completed') {
        clearInterval(timer);
        document.getElementById('etrn-progress-box').style.display = 'none';
        const resBox = document.getElementById('etrn-result-box');
        resBox.style.display = 'block';
        document.getElementById('etrn-download-link').href = `/api/etrn/download/${taskId}`;
      } else if (task.status === 'failed') {
        clearInterval(timer);
        alert('Сбой: ' + (task.error || task.message));
        document.getElementById('etrn-progress-box').style.display = 'none';
      }
    } catch (e) {
      clearInterval(timer);
    }
  }, 1500);
}

// Мгновенная отправка 3 файлов (Drag-and-Drop)
async function submitEtrnFiles(e) {
  e.preventDefault();
  const formData = new FormData(document.getElementById('etrn-upload-form'));

  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  btn.innerText = 'Обработка...';

  try {
    const res = await fetch('/api/etrn/process-files', {
      method: 'POST',
      body: formData
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Ошибка обработки');
    }

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'ЭТРН_Сводный_Отчет.xlsx';
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (err) {
    alert('Ошибка: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.innerText = 'Собрать и скачать отчет';
  }
}
```

---

## ⚡ Шаг 3. Настройка тихого экспорта 1С (`export_etrn.epf`)

В репозитории подготовлен исходный код 1С: [`export_etrn.bsl`](file:///Users/stanislavhvostov/Documents/Этрн/export_etrn.bsl).

### Как создать файл `.epf` в Конфигураторе 1С (за 2 минуты):
1. Откройте **Конфигуратор 1С**.
2. В верхнем меню выберите: **Файл ➔ Новый ➔ Внешняя обработка**.
3. В поле *«Имя»* укажите: `ВыгрузкаЭТРН`.
4. Нажмите кнопку **«Добавить форму»** ➔ Форма обработки ➔ Готово.
5. Откройте вкладку **«Модуль формы»** и вставьте весь текст из файла [`export_etrn.bsl`](file:///Users/stanislavhvostov/Documents/Этрн/export_etrn.bsl).
6. Сохраните файл как `C:\1C_Scripts\export_etrn.epf` (или в папку проекта).

Теперь 1С умеет запускаться из веб-сервера абсолютно тихо в фоне без открытия графических окон:
```cmd
1cv8c.exe ENTERPRISE /IBConnectionString "..." /Execute "export_etrn.epf" /C "03.11.2026;05.11.2026;C:\Users\s.khvostov\Desktop"
```

---

## ✅ Итог

Проект полностью модульный:
* Любая часть может работать как **автономный скрипт на ПК** через `run.py`.
* Любая часть готова к **мгновенному вызову внутри FastAPI**.
* Отчет формируется потокобезопасно, в памяти, с чистыми стилями и поддержкой любых периодов.
