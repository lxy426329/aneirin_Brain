# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build: docker build -t ombre-brain .
# Run:   docker run -e OMBRE_API_KEY=your-key -p 8000:8000 ombre-brain
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# No system dependencies needed — no local model loading.
# 不需要系统依赖——不加载本地模型。

# Install Python dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files / 复制项目文件
COPY *.py .
COPY *.html .
COPY *.js .
COPY config.example.yaml ./config.yaml

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/aneirin_Brain/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/aneirin_Brain/buckets

EXPOSE 8000

CMD ["python", "server.py"]
