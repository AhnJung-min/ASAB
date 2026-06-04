@echo off
chcp 65001 >nul
cd /d "C:\Users\dkswj\Desktop\ASAB"
echo ============================================================
echo   데이터 수집 이어하기
echo   - 이미 받은 종목은 자동으로 건너뜁니다 (이어받기)
echo   - 시총조사(완료분 skip) -^> 대형주 우선 10년 백필
echo   - 진행 로그가 아래에 흐릅니다. 끝나면 창을 닫으세요.
echo ============================================================
echo.
py -m src.universe --liquidity
py -m src.collect --source master --months 120 --skip-existing
echo.
echo [완료 또는 중단됨] 다시 이어하려면 이 파일을 또 더블클릭하세요.
pause
