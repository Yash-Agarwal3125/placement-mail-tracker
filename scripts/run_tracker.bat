@echo off

cd /d "C:\Users\Yash\Documents\Codex\2026-05-26\build-a-production-ready-python-project\placement-mail-tracker"

echo ================================================== >> logs\scheduler.log
echo STARTED %date% %time% >> logs\scheduler.log

call .venv\Scripts\activate.bat

python main.py >> logs\scheduler.log 2>&1

echo FINISHED %date% %time% >> logs\scheduler.log

exit