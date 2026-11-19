@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: ставлю зависимости...
  python -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)
echo Бот запускается (сам перезапустится, если упадёт). Закройте это окно, чтобы остановить.
".venv\Scripts\python.exe" -m bot.supervisor
pause
exit /b 0
:err
echo Ошибка установки. Нужен Python 3.11+ (python.org), галочка "Add to PATH".
pause
