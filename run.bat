@echo off
set "ACC_PRODUCT_CONFIG_V3="
cd /d "%~dp0"
echo 群像谱 - 微信聊天记录分析 启动中...
echo 浏览器打开 http://127.0.0.1:8800
if exist "C:\Users\Dtou\.workbuddy\binaries\python\envs\wxinsight\Scripts\python.exe" (
  "C:\Users\Dtou\.workbuddy\binaries\python\envs\wxinsight\Scripts\python.exe" -m uvicorn wxinsight.web.app:app --host 127.0.0.1 --port 8800
) else (
  python -m uvicorn wxinsight.web.app:app --host 127.0.0.1 --port 8800
)
pause
