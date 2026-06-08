@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   ASAB 대시보드 (브라우저가 자동으로 열립니다)
echo   안 열리면 직접 http://localhost:8501 접속
echo   이 창을 닫으면 대시보드가 종료됩니다.
echo ============================================================
start "" http://localhost:8501
py -m streamlit run dashboard.py --server.port 8501
pause
