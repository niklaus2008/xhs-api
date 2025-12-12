#!/bin/bash

# 小红书 API 服务重启脚本
# 用途：停止旧容器、重新构建镜像、启动新容器（带持久化 Profile）

set -e  # 遇到错误立即退出

echo "🛑 停止并删除旧容器..."
docker rm -f xhs-service 2>/dev/null || echo "   (旧容器不存在，跳过)"

echo "🔨 重新构建镜像..."
docker build -t xhs-scraper .

echo "🚀 启动新容器（带持久化 Profile）..."
docker run -d --name xhs-service -p 8000:8000 \
  -e XHS_USER_DATA_PATH=/data/chrome \
  -v /Users/liuqiang/code/n8n/xhs-api/chrome-data:/data/chrome \
  xhs-scraper

echo "✅ 服务已启动！"
echo ""
echo "📋 查看日志: docker logs -f xhs-service"
echo "🛑 停止服务: docker stop xhs-service"
echo "🗑️  删除容器: docker rm -f xhs-service"

