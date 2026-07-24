@echo off
chcp 65001 > nul
cd /d %~dp0
python collect.py >> collect_log.txt 2>&1
