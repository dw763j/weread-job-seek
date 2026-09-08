#!/usr/bin/env bash
# 备用微信读书 Chrome：使用独立的第二个 Profile 和调试端口，用于登录另一个微信读书
# 账号。当主力账号遇到 -2014（被标记/受限）时，用本启动器开窗口并改用对应端口补跑。
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
export WR_BROWSER_PROFILE_DIR="${WR_BROWSER_PROFILE_DIR:-$script_dir/.browser-profile/weread-备用}"
export WR_CDP_PORT="${WR_CDP_PORT:-9224}"

exec "$script_dir/start_weread_chrome.sh"
