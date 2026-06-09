@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ASAB
py -m src.menu
