@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   ASAB 패키지 설치 (최초 1회만)
echo   requests, streamlit, lightgbm 등 필요한 패키지를 설치합니다.
echo   이 작업을 먼저 해야 다른 bat 파일들이 동작합니다.
echo ============================================================
echo.
py -m pip install -r requirements.txt
echo.
echo [설치 완료] 이제 수급_백필.bat, 일봉_업데이트.bat 등을 쓸 수 있습니다.
pause
