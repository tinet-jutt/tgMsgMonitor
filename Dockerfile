FROM python:3.10-slim

WORKDIR /app

# 安装必要的系统底层工具（用于编译 telethon/cryptg 等依赖）与时区数据
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 设置容器全局默认时区为 Asia/Shanghai (UTC+8)
ENV TZ=Asia/Shanghai

# 拷贝并安装 Python 第三方依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝程序核心代码
COPY app/ ./app/
COPY main.py .

# 构建参数与版本环境变量注入
ARG APP_COMMIT_SHA="dev"
ARG APP_VERSION="v1.2.0"
ENV APP_COMMIT_SHA=${APP_COMMIT_SHA}
ENV APP_VERSION=${APP_VERSION}

# 声明容器内部服务暴露的端口
EXPOSE 8010

# 启动应用程序
CMD ["python", "main.py"]

