@echo off
rem ============================================================================
rem  Ejecutar las pruebas del filtro de selección (temporada + servidor).
rem    - backend : python -m unittest discover -s tests
rem    - frontend: node test_frontend/seleccion.test.js (jsdom)
rem  Si alguna falla, termina con código de error != 0.
rem ============================================================================
setlocal
chcp 65001 >nul

echo.
echo === Pruebas del filtro (backend) ===
python -m unittest discover -s tests -p "test*.py" -v
if errorlevel 1 (
    echo.
    echo [ERROR] Fallaron pruebas del backend.
    exit /b 1
)

echo.
echo === Pruebas del filtro (frontend) ===
cd test_frontend
if not exist node_modules\jsdom\package.json (
    echo Instalando jsdom (solo la primera vez)...
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        echo [ERROR] No se pudo instalar jsdom en test_frontend.
        exit /b 1
    )
)
node seleccion.test.js
if errorlevel 1 (
    echo.
    echo [ERROR] Fallaron pruebas del frontend.
    exit /b 1
)

cd ..
echo.
echo Todas las pruebas pasaron.
exit /b 0