@echo off
cd /d "%~dp0"
title B站弹幕点歌机 启动器
echo ============================================================
echo   B站弹幕点歌机  v1.1  启动器
echo ------------------------------------------------------------
echo   作用：解除 Windows 对这个 exe 的「网络锁定」，然后启动程序。
echo   被 SmartScreen 拦住时，也可以手动点「更多信息」再点「仍要运行」。
echo ============================================================
echo.
if not exist "%~dp0bili-jukebox.exe" (
    echo [错误] 没找到 bili-jukebox.exe，请把本文件和 exe 放在同一个文件夹。
    pause
    exit /b 1
)
echo 正在解除锁定 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Unblock-File -LiteralPath '%~dp0bili-jukebox.exe' -ErrorAction Stop; Write-Host '  OK：已解除锁定' } catch { Write-Host '  跳过：解除失败，若仍被拦请手动点 更多信息 再点 仍要运行' }"
echo.
echo 正在启动（这个窗口就是程序本体，关掉它程序就停了）...
echo.
"%~dp0bili-jukebox.exe"
echo.
echo 程序已退出。如果启动失败，请查看 exe 同目录 cache\logs\jukebox.log
pause
