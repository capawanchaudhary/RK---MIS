@echo off
setlocal
cd /d "%~dp0"
echo This copies the local app records to your Supabase project.
echo It does not delete or change the local database.
echo Keep the connection details private; they will not be shown while you type.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install --user "psycopg[binary]>=3.2,<4"
  if errorlevel 1 goto failed
  py -3 migrate_to_supabase.py
) else (
  python -m pip install --user "psycopg[binary]>=3.2,<4"
  if errorlevel 1 goto failed
  python migrate_to_supabase.py
)
if errorlevel 1 goto failed
echo.
echo Migration finished. Review the record counts above.
pause
exit /b 0
:failed
echo.
echo Migration did not finish. The local database was not changed.
pause
exit /b 1
