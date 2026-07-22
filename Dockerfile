# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build: docker build -t ombre-brain .
# Run:   docker run -e OMBRE_API_KEY=your-key -p 8000:8000 ombre-brain
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for sentence-transformers
# 安装 sentence-transformers 需要的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Rerank model during build phase
# 在构建阶段预下载 Rerank 模型（避免运行时动态下载 400MB 文件）
# 设置 HuggingFace 缓存目录
ENV TRANSFORMERS_CACHE=/app/hf_cache
RUN mkdir -p $TRANSFORMERS_CACHE && \
    python -c "from sentence_transformers import CrossEncoder; model = CrossEncoder('BAAI/bge-reranker-base')"

# Copy project files / 复制项目文件
COPY *.py .
COPY dashboard.html .
COPY dashboard.js .
COPY config.example.yaml ./config.yaml

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/aneirin_Brain/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/aneirin_Brain/buckets
ENV TRANSFORMERS_CACHE=/app/hf_cache

EXPOSE 8000

CMD ["python", "server.py"]
