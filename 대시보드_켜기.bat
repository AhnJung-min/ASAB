@echo off
chcp 65001 >nul
cd /d "C:\Users\dkswj\Desktop\ASAB"
echo 대시보드를 켭니다. 잠시 후 브라우저에서 http://localhost:8501 로 접속하세요.
echo (이 창을 닫으면 대시보드가 종료됩니다. 데이터 수집과는 무관합니다.)
py -m streamlit run dashboard.py
pause
