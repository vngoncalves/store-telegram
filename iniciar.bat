@echo off
rem Telegram Store - prepara o ambiente (na primeira vez) e inicia o bot.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual em .venv ...
    python -m venv .venv || goto :erro
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :erro

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Arquivo .env criado a partir do .env.example.
    echo Preencha o .env, salve e execute iniciar.bat novamente.
    notepad ".env"
    goto :fim
)

".venv\Scripts\python.exe" store_bot.py
goto :fim

:erro
echo.
echo Falha ao preparar o ambiente. Confira se o Python 3.10 ou superior esta instalado
echo e marcado no PATH (python --version).

:fim
pause
