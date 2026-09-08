# 招聘链接网页服务镜像
# 代码通过 docker-compose.yaml 的 bind 挂载进容器，改代码无需重建镜像。
# 汇总生成.py 不在容器里运行（由宿主机 uv 执行），因此镜像不需要 certifi。
# zxing-cpp / pillow 供 web/screen_update.py 解码海报二维码使用（AI 筛选任务
# 由网页服务在容器内以子进程触发）；两者均为纯 wheel，无需编译，
# 调整版本后需 docker compose up -d --build 重建镜像。
FROM python:3.12-slim

RUN pip install --no-cache-dir "zxing-cpp>=2.2" "pillow>=10.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 统一以宿主机用户(1000:1000) 运行，避免容器写出的文件归 root、造成宿主机权限错乱。
USER 1000:1000

# server.py 由 compose 挂载进 /app/web/。
CMD ["python", "web/server.py", "serve", "--host", "0.0.0.0", "--port", "8888"]
