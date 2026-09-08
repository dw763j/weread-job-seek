#!/usr/bin/env bash
# 后台编排：冷却 -> 补跑增量提取 -> 启动网页服务
set -uo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
log_dir="$script_dir/.orchestrator-logs"
mkdir -p "$log_dir"
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

cooldown_seconds="${WR_COOLDOWN_SECONDS:-900}"   # 默认 15 分钟
host="${WR_SERVE_HOST:-127.0.0.1}"
port="${WR_SERVE_PORT:-8888}"

extract_log="$log_dir/extract.log"
serve_log="$log_dir/serve.log"
state_file="$log_dir/state.txt"

echo "[$(stamp)] 编排启动：冷却 $cooldown_seconds 秒后补跑增量，再启动 $host:$port" | tee -a "$extract_log"
echo "cooldown_pending" > "$state_file"

sleep "$cooldown_seconds"

echo "[$(stamp)] 冷却完成，开始增量提取" | tee -a "$extract_log"
echo "extract_running" > "$state_file"

cd "$script_dir"
node weread_extract.mjs >> "$extract_log" 2>&1
extract_status=$?
echo "[$(stamp)] 增量提取结束，退出码 $extract_status" | tee -a "$extract_log"
echo "extract_done status=$extract_status" > "$state_file"

# 网页服务优先走 Docker 容器（随系统重启自动拉起）；检测到 web 容器在运行就不再重复启动本机进程。
compose_file="$script_dir/docker-compose.yaml"
if command -v docker >/dev/null 2>&1 &&
   [ -f "$compose_file" ] &&
   [ -n "$(docker compose -f "$compose_file" ps --status running -q web 2>/dev/null)" ]; then
  echo "[$(stamp)] 检测到 Docker 的 web 容器已在运行，跳过本机网页服务启动。" | tee -a "$serve_log"
  echo "serve_running(docker)" > "$state_file"
else
  echo "[$(stamp)] 未检测到 Docker web 容器，改用本机进程启动 $host:$port（日志 $serve_log）" | tee -a "$serve_log"
  echo "serve_running" >> "$state_file"
  uv run python web/server.py serve --host "$host" --port "$port" >> "$serve_log" 2>&1 &
  serve_pid=$!
  echo "$serve_pid" > "$log_dir/serve.pid"
  echo "[$(stamp)] 网页服务 PID=$serve_pid 已在后台监听 http://$host:$port" | tee -a "$serve_log"
fi
