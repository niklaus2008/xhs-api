# 依然使用 bullseye 版本，非常稳定
FROM python:3.9-slim-bullseye

# 设置工作目录
WORKDIR /app

# 🔴 修改点：将 ustc.edu.cn 换成了 mirrors.aliyun.com (阿里云)，通常更稳定
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list && \
    sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y chromium chromium-driver && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 复制代码
COPY main.py .

# 4. 暴露端口
EXPOSE 8000

# 5. 启动服务
CMD ["python", "main.py"]
