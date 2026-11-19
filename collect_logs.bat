@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Собираю логи бота в архив на Рабочий стол...
".venv\Scripts\python.exe" -m bot.logbundle
if errorlevel 1 echo Не получилось собрать архив. Пришлите папку data\logs целиком.
pause
