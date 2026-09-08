# Ubuntu 24.04 部署说明

本项目的主流程通过“微信读书网页 + 本机 Chrome 调试端口”获取公众号文章链接，可在 Ubuntu 24.04 运行。微信桌面端 `Share Data` 缓存扫描是 macOS 特有的补充入口；Ubuntu 建议使用微信读书主流程。

## 1. 安装运行环境

```bash
sudo apt update
sudo apt install -y python3 nodejs npm curl
```

安装 Google Chrome（推荐官方下载的 `.deb`），或安装系统 Chromium。确认：

```bash
google-chrome --version || chromium --version
node --version       # 需要 Node.js 18 或更高版本
python3 --version    # 需要 Python 3.10 或更高版本
```

若 Ubuntu 仓库的 Node.js 版本低于 18，请从 NodeSource 或官方发行包安装较新 LTS 版。

## 2. 配置项目

```bash
cd /path/to/微信招聘链接提取
uv sync
chmod +x start_weread_chrome.sh start_weread_chrome.command weread_extract.mjs
```

请先按 [uv 官方安装方式](https://docs.astral.sh/uv/getting-started/installation/) 安装 `uv`。依赖由 `pyproject.toml` 管理，`uv sync` 会创建项目内 `.venv` 并安装所需包。

如需语义去重，在项目根目录创建 `.env`，内容为（指向本地 vLLM 部署的
`zai-org/GLM-5.3-Flash`，OpenAI 兼容端点均可）：

```text
DEDUP_API_BASE=http://127.0.0.1:8000/v1
DEDUP_MODEL=zai-org/GLM-5.3-Flash
DEDUP_API_KEY=noneed
```

`.env`、`.browser-profile/` 和输出中可能含个人状态，请勿提交或分享。

## 3. 首次登录微信读书

```bash
./start_weread_chrome.sh
```

脚本会依次查找 `google-chrome`、`google-chrome-stable`、`chromium` 和 `chromium-browser`。找不到浏览器时，显式指定其可执行文件：

```bash
WR_CHROME_BIN=/usr/bin/google-chrome ./start_weread_chrome.sh
```

在弹出的浏览器中打开微信读书并扫码登录。Profile 会保存到项目的 `.browser-profile/weread`；迁机时不要复制这个目录，改在新主机重新登录。

## 4. 抓取与服务

```bash
node weread_extract.mjs 9223 --check-session
node weread_extract.mjs 9223
uv run python web/server.py serve
```

主抓取脚本会自动用本机 `uv run python` 调用汇总程序（Docker 只承载网页服务，不承载汇总；也可通过 `UV_BIN=/path/to/uv` 指定 `uv` 路径）。网页服务手动启动时默认只监听 `127.0.0.1:8787`。对外提供服务时请置于 HTTPS 反向代理之后，并按 `网页服务/README.md` 开启安全 Cookie。

## 5. 容器化部署（Docker，推荐）

网页服务用 Docker Compose 常驻运行，解决「网页服务重启后不自启」的问题。镜像只提供 Python 环境（`python:3.12-slim`，server.py 仅用标准库），`网页服务/` 代码通过 bind 挂载进容器——**改代码后无需重建镜像，`docker compose restart web` 即可生效**。汇总不在容器里跑：抓取脚本与手动重跑都直接用本机 uv 执行 `generate_summary.py`：

```bash
cd /path/to/微信招聘链接提取
docker compose up -d --build        # 首次构建镜像并后台启动，随 Docker daemon 开机自启
docker compose ps                   # 查看服务状态
docker compose logs -f web          # 看日志
docker compose restart web          # 改代码后重启以加载新代码
```

- 网页服务：`0.0.0.0:8888`（可用 `WEB_BIND=IP` 固定网卡）。
- 手动重跑汇总（本机 uv 执行，`--range` 可改时间窗口）：
  ```bash
  uv run python generate_summary.py --range 20260710-99999999
  ```
  自动化路径（抓取脚本调用）已把 uv 缓存固定到项目内 `.uv-cache/`；手动执行如遇 `$HOME/.cache` 权限问题，同样加 `UV_CACHE_DIR=.uv-cache` 前缀即可。
- 容器以宿主机用户 ID(1000:1000) 运行，写出的文件归属该用户，不产生 root 权限问题。
- 数据沿用现有文件：`web/data/`（SQLite）读写挂载、`output/` 只读挂载。
- 确保 Docker daemon 开机自启：`sudo systemctl enable docker`。

## 运维建议

- Chrome 的调试端口只监听 `127.0.0.1`，不要将 9223 暴露到公网。
- 优先使用上面的 Docker Compose 守护网页服务（`restart: unless-stopped`）；无 Docker 时改用 systemd 或 supervisor 守护网页服务。抓取任务建议由 systemd timer/cron 低频定时触发，并避免并发执行。
- 备份 `web/data/clicks.sqlite3`、`output/` 与配置文件；不要备份或同步浏览器 Profile。
