@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 弹幕表情压制工具

set "TOOL_DIR=%~dp0"
set "CONFIG=%TOOL_DIR%config.json"

REM ---------- 查找可用的 Python（必须带 Pillow）----------
set "PY="
call :TRY_PY "%TOOL_DIR%python\python.exe"
if not defined PY call :TRY_PY "%TOOL_DIR%tools\python\python.exe"
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY call :TRY_PY "%%p"
    )
)
if not defined PY (
    echo =======================================================
    echo [错误] 没有找到可用的 Python（需要 Pillow）。
    echo.
    echo 两种解决办法，任选其一：
    echo   1^) 安装 Python 3.9+，安装时勾选 "Add python.exe to PATH"，
    echo      然后执行:  python -m pip install pillow
    echo   2^) 把便携版 Python 放到:  %TOOL_DIR%python\
    echo =======================================================
    pause
    exit /b 1
)

if not exist "%CONFIG%" (
    echo [错误] 脚本目录下找不到 config.json 配置文件。
    pause
    exit /b 1
)

REM ---------- 无参数：显示菜单 ----------
if "%~1"=="" goto :MENU

REM ---------- 拖拽进来：直接压制 ----------
call :RUN_BURN "%~1"
goto :END

:MENU
cls
echo =======================================================
echo            弹幕表情压制工具
echo =======================================================
echo  把【视频文件】直接拖到本脚本图标上即可一键出片。
echo  需要视频同目录下存在同名 .xml 弹幕文件。
echo -------------------------------------------------------
echo  [1] 更新本直播间表情包（需在 config.json 填 cookie）
echo  [2] 只生成 ASS 字幕（不压制视频）
echo  [3] 打开表情库目录
echo  [4] 编辑配置文件 config.json
echo  [5] 重新扫描表情库（把新丢进去的图片登记进来）
echo  [6] 切换编码器（显卡加速 / CPU）
echo  [0] 退出
echo =======================================================
set "CHOICE="
set /p "CHOICE=请输入编号并回车: "

if "%CHOICE%"=="1" (
    "%PY%" "%TOOL_DIR%run_from_config.py" --mode fetch --config "%CONFIG%"
    goto :PAUSE_MENU
)
if "%CHOICE%"=="2" (
    echo.
    echo 请把 .xml 弹幕文件拖到本脚本上，或在此输入完整路径：
    set "XMLPATH="
    set /p "XMLPATH=XML 路径: "
    if defined XMLPATH (
        "%PY%" "%TOOL_DIR%run_from_config.py" "!XMLPATH!" --mode ass --config "%CONFIG%"
    )
    goto :PAUSE_MENU
)
if "%CHOICE%"=="3" (
    if not exist "%TOOL_DIR%emotes" mkdir "%TOOL_DIR%emotes"
    start "" "%TOOL_DIR%emotes"
    goto :PAUSE_MENU
)
if "%CHOICE%"=="4" (
    start "" notepad "%CONFIG%"
    goto :PAUSE_MENU
)
if "%CHOICE%"=="5" (
    "%PY%" "%TOOL_DIR%run_from_config.py" --mode scan --config "%CONFIG%"
    goto :PAUSE_MENU
)
if "%CHOICE%"=="6" (
    "%PY%" "%TOOL_DIR%run_from_config.py" --mode encoder --config "%CONFIG%"
    goto :PAUSE_MENU
)
goto :END

:PAUSE_MENU
echo.
pause
goto :MENU

:RUN_BURN
set "TARGET=%~1"
echo =======================================================
echo 处理文件: %TARGET%
echo =======================================================
"%PY%" "%TOOL_DIR%run_from_config.py" "%TARGET%" --mode burn --config "%CONFIG%"
echo.
pause
exit /b 0

:TRY_PY
if defined PY exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import PIL" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0

:END
endlocal
