@echo off
rem ====================================================
rem  MiDescargador - inicio
rem  Arranca el servidor y abre el panel SOLO cuando ya
rem  responde (evita el "no se puede acceder al sitio").
rem  Si el servidor ya esta corriendo, solo abre el panel.
rem  Para detenerlo: cierra la ventana "MiDescargador"
rem  (o pulsa Ctrl+C dentro de ella).
rem ====================================================
cd /d "%~dp0"

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

rem ¿Ya hay un servidor escuchando? Entonces solo abrir el panel.
powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 17890 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto listo

echo Iniciando MiDescargador en http://127.0.0.1:17890
start "MiDescargador" /min "%PY%" servidor.py

echo Esperando a que el servidor responda...
set /a intentos=0
:esperar
set /a intentos+=1
powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 17890 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto listo
if %intentos% lss 40 goto esperar
echo.
echo El servidor no respondio a tiempo. Revisa la ventana "MiDescargador":
echo puede que el puerto 17890 este ocupado o que haya un error visible.
pause
exit /b 1

:listo
echo Abriendo el panel en el navegador...
start "" http://127.0.0.1:17890
ping -n 3 127.0.0.1 >nul
exit /b 0
