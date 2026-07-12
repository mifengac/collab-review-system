# 材料协同办理系统
# 构建时优先使用国内 Debian/PyPI 源，失败则回退官方源
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# APT：优先阿里云，失败则保持官方源
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources || true; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list || true; \
    fi; \
    apt-get update \
      || ( \
        echo "国内 apt 源失败，回退官方源"; \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
          sed -i 's|mirrors.aliyun.com|deb.debian.org|g' /etc/apt/sources.list.d/debian.sources || true; \
        fi; \
        if [ -f /etc/apt/sources.list ]; then \
          sed -i 's|mirrors.aliyun.com|deb.debian.org|g' /etc/apt/sources.list || true; \
        fi; \
        apt-get update; \
      ); \
    apt-get install -y --no-install-recommends curl; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Pip：优先清华，其次阿里云，最后官方
RUN set -eux; \
    pip install -r requirements.txt \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      --trusted-host pypi.tuna.tsinghua.edu.cn \
    || pip install -r requirements.txt \
      -i https://mirrors.aliyun.com/pypi/simple/ \
      --trusted-host mirrors.aliyun.com \
    || pip install -r requirements.txt

COPY app ./app
COPY frontend ./frontend

# 命名卷挂载后属主多为 root，保持 root 运行以保证写库/上传（内网单机部署）
RUN mkdir -p /app/data /app/uploads

EXPOSE 5009

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5009/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5009"]
