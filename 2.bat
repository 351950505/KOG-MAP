@echo off
chcp 65001 >nul
title 从 0 开始全量上传 KOG-MAP
echo ========================================================
echo 正在执行从 0 开始全新初始化与上传...
echo 工作目录: %~dp0
echo 目标仓库: https://github.com/351950505/KOG-MAP.git
echo ========================================================
cd /d "%~dp0"

:: 1. 彻底粉碎旧的 .git 本地历史记录，彻底从 0 开始
if exist .git (
    echo [1/6] 正在物理删除旧的 .git 缓存...
    attrib -h -s -r .git\*.* /s /d >nul 2>&1
    rd /s /q .git
)

:: 2. 配置提交者身份，防止报 Please tell me who you are
echo [2/6] 配置 Git 身份与抗断连网络参数...
git config --global user.name "351950505"
git config --global user.email "351950505@users.noreply.github.com"

:: 3. 优化网络通道，防止大文件传输连接被重置 (Connection was reset)
git config --global http.postBuffer 524288000
git config --global http.sslBackend schannel
git config --global http.version HTTP/1.1
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999

:: 4. 初始化全新 main 分支并绑定远程仓库
echo [3/6] 初始化全新本地版本库...
git init -b main
git remote add origin https://github.com/351950505/KOG-MAP.git

:: 5. 全量扫描当前目录的所有地图与配置文件
echo [4/6] 正在添加当前目录的所有文件 (文件较多，请稍候 10~20 秒)...
git add .

:: 6. 生成全新初始提交
echo [5/6] 正在生成全新初始提交记录...
git commit -m "Initial release: Pure short-named KoG maps and ddnet-style votes"

:: 7. 强推到 GitHub，彻底抹杀远程旧文件
echo.
echo [6/6] 正在强制推送到 GitHub (如果弹出浏览器，请点击绿色 Authorize 授权)...
git push -u origin main --force

echo.
echo ========================================================
echo 恭喜！全量从 0 上传完毕！请刷新查看：
echo https://github.com/351950505/KOG-MAP
echo ========================================================
pause