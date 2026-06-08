@echo off
chcp 65001 >/dev/null
cd /d "%~dp0"
title ASAB 통합 실행
py -m src.menu
