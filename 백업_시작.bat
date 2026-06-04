@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   구글드라이브 자동 백업 감시
echo   - 일봉이 10만 행(약 50종목) 늘 때마다 자동 백업
echo   - 저장 위치: G:\내 드라이브\ASAB_backup
echo   - 이 창을 닫으면 자동 백업이 멈춥니다 (수집과 별개)
echo ============================================================
py -m src.backup --dest "G:\내 드라이브\ASAB_backup" --watch --threshold 100000 --interval 300
pause
