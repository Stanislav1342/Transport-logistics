@echo off
chcp 65001 > nul
title Температурные графики Advantum — Сервер

echo ======================================================================
echo       Запуск веб-сервера: Температурные графики Advantum (v42)
echo ======================================================================
echo.

:: Проверка наличия Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден в системе!
    echo Установите Python с официального сайта https://www.python.org/
    echo Обязательно отметьте галочку "Add Python to PATH" при установке.
    pause
    exit /b 1
)

:: Проверка виртуального окружения
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXEC=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXEC=python"
)

:: Проверка и установка зависимостей только при первом запуске
echo [1/2] Проверка зависимостей...
%PYTHON_EXEC% -c "import fastapi, uvicorn, selenium, pydantic" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ИНФО] Не все пакеты установлены. Выполняется установка requirements.txt...
    %PYTHON_EXEC% -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ПРЕДУПРЕЖДЕНИЕ] Не удалось автоматически установить зависимости.
    )
) else (
    echo [OK] Все необходимые библиотеки уже установлены.
)

:: Запуск веб-сервера
echo [2/2] Запуск сервера...
echo.
%PYTHON_EXEC% server.py

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Сервер завершил работу с кодом ошибки %errorlevel%.
    pause
)
