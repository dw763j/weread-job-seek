#!/usr/bin/env bash
# 在 macOS 或 Linux 启动使用独立 Profile 的微信读书 Chrome。
# 自动探测多种 Chrome 安装方式：系统包、Flatpak、macOS 应用，
# 也支持 WR_CHROME_BIN 显式指定（可执行文件、wrapper 脚本或含空格的命令串）。
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
profile_dir="${WR_BROWSER_PROFILE_DIR:-$script_dir/.browser-profile/weread}"
cdp_port="${WR_CDP_PORT:-9223}"

# CHROME_CMD 为数组，存放启动 Chrome 的完整命令。
# 既支持单一可执行路径，也支持 "flatpak run com.google.Chrome" 这类多段命令。
CHROME_CMD=()
CHROME_KIND=""

find_chrome() {
  # 1) WR_CHROME_BIN 显式指定：可执行文件、wrapper 脚本、或含空格的命令串
  if [[ -n "${WR_CHROME_BIN:-}" ]]; then
    if [[ -x "$WR_CHROME_BIN" ]]; then
      CHROME_CMD=("$WR_CHROME_BIN")
      CHROME_KIND="env"
      return
    fi
    # 含空格的情况按 shell 词法拆分（如 "flatpak run com.google.Chrome"）
    # shellcheck disable=SC2206
    CHROME_CMD=($WR_CHROME_BIN)
    CHROME_KIND="${CHROME_CMD[0]:-env}"
    [[ "$CHROME_KIND" == "flatpak" ]] && CHROME_KIND="flatpak-env"
    return
  fi

  # 2) PATH 中的原生 Chrome / Chromium
  local candidate
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CHROME_CMD=("$candidate")
      CHROME_KIND="native"
      return
    fi
  done

  # 3) Flatpak 安装的 Chrome / Chromium
  if command -v flatpak >/dev/null 2>&1; then
    local fp_app
    for fp_app in com.google.Chrome org.chromium.Chromium; do
      if flatpak info "$fp_app" >/dev/null 2>&1; then
        CHROME_CMD=(flatpak run "$fp_app")
        CHROME_KIND="flatpak"
        return
      fi
    done
  fi

  # 4) macOS 应用
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local mac_path
    for mac_path in \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
      if [[ -x "$mac_path" ]]; then
        CHROME_CMD=("$mac_path")
        CHROME_KIND="mac"
        return
      fi
    done
  fi

  cat >&2 <<'EOF'
未找到 Chrome/Chromium。请通过以下任一方式提供：
  • 系统包管理器安装（apt/dnf/brew 等），确保 google-chrome 或 chromium 在 PATH
  • Flatpak: flatpak install com.google.Chrome
  • 设置 WR_CHROME_BIN=/path/to/chrome（也支持 wrapper 脚本或 "flatpak run com.google.Chrome"）
EOF
  return 1
}

# Flatpak 沙箱不授予 home 目录的通用访问权，只暴露 xdg-documents 等特定路径。
# 因此无论 profile 在哪个位置，都需显式授权 --filesystem。
adjust_flatpak_permissions() {
  [[ "$CHROME_KIND" != flatpak ]] && return
  local fp_app="${CHROME_CMD[2]}"
  CHROME_CMD=(flatpak run --filesystem="$profile_dir" "$fp_app")
  echo "Flatpak Chrome 已授权访问 Profile 目录: $profile_dir"
}

# 如果 Chrome 已在目标端口运行，直接退出。
if command -v curl >/dev/null 2>&1 && curl --fail --silent --max-time 1 "http://127.0.0.1:${cdp_port}/json/version" >/dev/null; then
  echo "微信读书专用 Chrome 已在运行（调试端口 $cdp_port）。"
  exit 0
fi

find_chrome
adjust_flatpak_permissions

mkdir -p "$profile_dir"
"${CHROME_CMD[@]}" \
  "--user-data-dir=$profile_dir" \
  "--remote-debugging-address=127.0.0.1" \
  "--remote-debugging-port=$cdp_port" \
  --no-first-run \
  --no-default-browser-check \
  "https://weread.qq.com/" >/dev/null 2>&1 &

echo "已启动微信读书专用 Chrome"
echo "Profile: $profile_dir"
echo "调试端口: $cdp_port（仅监听本机）"
echo "Chrome 来源: ${CHROME_KIND:-unknown}"
echo "请始终通过本文件启动，登录状态才会写回同一个 Profile。"
