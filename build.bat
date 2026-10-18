@echo off
REM 一键打包为单文件 exe（dist\bili-jukebox.exe）
REM 前提：pip install pyinstaller 且当前在虚拟环境/项目根目录

cd /d %~dp0
python -m PyInstaller build.spec --clean --noconfirm
if %errorlevel%==0 (
    echo.
    echo ========== 构建完成 ==========
    echo 产物：dist\bili-jukebox.exe
    echo 把它（单独一个 exe）发给别人即可，无需安装 Python。
    echo 首次运行会在 exe 同目录生成 cache\ 文件夹。
) else (
    echo 构建失败，请查看上方输出。
)
pause
