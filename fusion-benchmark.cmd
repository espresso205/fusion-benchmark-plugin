@echo off
setlocal
set "FUSION_BENCHMARK_ROOT=%~dp0"
if not exist "%FUSION_BENCHMARK_ROOT%runtime\python\python.exe" (
  echo Bundled Python is missing. Extract the complete portable release before running this launcher. 1>&2
  exit /b 3
)
"%FUSION_BENCHMARK_ROOT%runtime\python\python.exe" -B "%FUSION_BENCHMARK_ROOT%fusion-benchmark-bootstrap.py" %*
exit /b %errorlevel%
