@echo off
chcp 65001 >nul 2>&1
title PROTEC Suite
cd /d "%~dp0"

echo ============================================
echo  PROTEC Suite - Demarrage
echo ============================================
echo.

REM Verifie Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERREUR : Python n'est pas installe.
    echo Telecharge Python 3.10+ sur https://www.python.org/
    pause & exit /b 1
)

REM Cree le venv si absent
if not exist .venv\Scripts\python.exe (
    echo Creation de l'environnement virtuel...
    python -m venv .venv
    if errorlevel 1 ( echo ERREUR venv & pause & exit /b 1 )
)

REM Installe les dependances si necessaire
if not exist .venv\Lib\site-packages\flask (
    echo Installation des dependances...
    .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 ( echo ERREUR installation & pause & exit /b 1 )
)

echo.
echo  Demarrage sur http://localhost:5000
echo  Fermez cette fenetre pour arreter.
echo.

start "" "http://localhost:5000"
.venv\Scripts\python.exe app.py

if errorlevel 1 (
    echo.
    echo ERREUR - Voir message ci-dessus.
    pause
)
