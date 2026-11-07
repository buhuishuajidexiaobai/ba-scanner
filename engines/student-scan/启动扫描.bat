@echo off
chcp 65001 >nul
title BA 学生练度扫描器
echo ============================================
echo   BA 学生练度扫描器 (Blue Archive Student Scan)
echo   请先把游戏停在「学生详情页(基本情報)」,
echo   光标放在第一个学生上, 然后回到本窗口按任意键
echo ============================================
pause >nul
rem 便携版优先使用内嵌 runtime, 源码运行版回退系统 Python
if exist "%~dp0runtime\python.exe" (
    "%~dp0runtime\python.exe" -I "%~dp0ba_student_scan.py" %*
) else (
    python -I "%~dp0ba_student_scan.py" %*
)
echo.
echo 扫描结束, 结果见 输出\ 目录
pause
