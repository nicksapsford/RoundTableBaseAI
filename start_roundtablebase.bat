@echo off
title RoundTableBase A.I. - Port 5036
cd /d C:\Users\abc\Desktop\AlbionBase\RoundTableBaseAI
start /min "RoundTableBase A.I." cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_roundtablebase.py
timeout /t 5 /nobreak >nul
start http://localhost:5036
