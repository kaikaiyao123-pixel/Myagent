@echo off
REM ============================================================================
REM AgentChat 公网隧道启动脚本（Windows 双击运行，内置自动重连）
REM
REM 用法：
REM   双击运行                -> serveo，自动重连
REM   start_tunnel.bat pinggy -> 换用 pinggy.io
REM   set SUBDOMAIN=agentchat 后再运行 -> serveo 固定子域名（需先登记 SSH 公钥）
REM
REM 重要：免费隧道的地址是短时的（serveo 约 10~30 分钟、pinggy 60 分钟会过期），
REM       因此本脚本会在断开后自动重建隧道。每次重建地址可能变化，
REM       当前地址会打印在窗口中，并写入同目录的 tunnel_url.txt。
REM ============================================================================
setlocal enabledelayedexpansion

if "%PORT%"=="" set PORT=8090
set SERVICE=%1
if "%SERVICE%"=="" set SERVICE=serveo

set URLFILE=%~dp0tunnel_url.txt

echo ==============================================
echo  AgentChat 公网隧道
echo  本地目标: http://localhost:%PORT%
echo  隧道服务: %SERVICE%
echo  地址写入: %URLFILE%
echo  按 Ctrl+C 停止
echo ==============================================
echo.

where ssh >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 ssh 命令。
  echo        请在「设置 - 应用 - 可选功能」中安装「OpenSSH 客户端」。
  pause
  exit /b 1
)

REM --noproxy "*" 防止企业/沙箱代理拦截对 localhost 的探测而误报"无响应"
curl -s -o nul --noproxy "*" --max-time 5 http://127.0.0.1:%PORT% >nul 2>nul
if errorlevel 1 (
  echo [警告] http://127.0.0.1:%PORT% 无响应。
  echo        请先启动容器: cd docker ^&^& docker compose up -d
  echo.
)

if /i "%SERVICE%"=="serveo" (
  echo 提示: serveo 免费版首次访问会显示浏览器提示页，点击继续即可。
  echo.
)
if /i "%SERVICE%"=="pinggy" (
  echo 提示: pinggy 免费隧道 60 分钟后过期（本脚本会自动重连）。
  echo.
)

set ROUND=0

:loop
set /a ROUND+=1
echo [%TIME:~0,8%] 第 !ROUND! 次建立隧道...

if /i "%SERVICE%"=="serveo" (
  if not "%SUBDOMAIN%"=="" (
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R %SUBDOMAIN%:80:localhost:%PORT% serveo.net
  ) else (
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 80:localhost:%PORT% serveo.net
  )
) else if /i "%SERVICE%"=="localhostrun" (
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 80:localhost:%PORT% nokey@localhost.run
) else if /i "%SERVICE%"=="pinggy" (
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p 443 -R0:localhost:%PORT% a.pinggy.io
) else (
  echo 未知的隧道服务: %SERVICE%
  echo 可选: serveo ^| localhostrun ^| pinggy
  pause
  exit /b 1
)

echo.
echo [%TIME:~0,8%] 隧道已断开（免费版转发有时长限制）。5 秒后自动重连...
echo.
timeout /t 5 /nobreak >nul
goto loop
