# 招聘链接多人网页服务

这是现有微信公众号抓取流程的展示层：它直接读取
`output/weread_extract/汇总-去重组.json`，不改变抓取与去重逻辑；每次抓取完成后，
网页会在下一次请求时自动读取新结果，无需再导出或分发 HTML。

## 代码结构

后端（Python 标准库，模块都在 `web/` 下扁平放置、直接相互导入）：

| 文件 | 职责 |
| --- | --- |
| `server.py` | 入口：组装 `RequestHandler` / `AppServer`、命令行（serve、add-user 等） |
| `server_common.py` | 常量（状态字典、上限、正则）与纯工具函数（时间/哈希/校验） |
| `database.py` | SQLite 连接、建表 schema、各业务表的行读取与序列化 |
| `stores.py` | 文章分组与 AI 筛选结果的 mtime 热重载 |
| `api_session.py` | HTTP 基座：路由分发、JSON/静态响应、会话与登录登出 |
| `api_articles.py` | 文章看板端点（bootstrap / 已读 / 偏好 / AI 筛选任务） |
| `api_favorites.py` | 收藏与收藏分组端点 |
| `api_applications.py` | 投递管理端点 |
| `screen_update.py` | AI 筛选编排：按日期调度、结果复用与落盘（CLI） |
| `screen_lib.py` | AI 筛选底层：微信文章抓取、二维码解码、长图切片、GLM 识别 |

前端（`web/static/`，无构建步骤，原生 ES 模块，入口在 `index.html` 引 `js/main.js`）：
`js/state.js`（全局状态）、`js/api.js`（请求封装）、`js/format.js`（格式化工具）、
`js/dom.js`（DOM 助手）、`js/views.js`（视图切换与渲染分发）、`js/articles.js`（文章看板）、
`js/favorites.js`（收藏）、`js/applications.js`（投递管理）、`js/main.js`（登录与数据装载入口）。

`web/tests/` 用 `uv run python -m unittest discover -s web/tests` 运行；
旧代码若从 `server` / `screen_update` 导入符号，这两个入口模块会继续再导出常用名字。

## 设计

- 每位使用者拥有独立的“用户名 + 访问码”；访问码只保存慢哈希，不以明文落盘。
- 登录后签发 30 天有效的随机会话 Cookie（HttpOnly、SameSite=Strict）。
- 点击文章时，浏览器立即调用服务端 API；点击时间按“用户 + 微信文章 URL”写入 SQLite。
- 同一招聘主题有多个公众号来源时，点击任一来源会把整组标为已读；以后分组新增来源时，
  只要组内已有任一 URL 被该用户点过，仍会显示为已读。
- 页面每 15 秒及重新切回标签页时同步一次，多个设备或标签页能看到同一用户的最新状态。
- 状态以服务端数据库为准，浏览器本地存储不再承担数据保存职责。

## 容器化部署（推荐）

项目根目录已提供 `docker-compose.yaml`，一键构建镜像并后台常驻，容器随系统重启自动拉起
（`restart: unless-stopped`，前提是 Docker daemon 开机自启）：

```sh
cd <项目根目录>
docker compose up -d --build
```

- 网页服务监听 `0.0.0.0:8888`；如需固定到某网卡 IP，用 `WEB_BIND=192.168.1.x docker compose up -d`。
- SQLite 与用户数据保存在 `web/data/`，抓取结果挂载为只读，都自动沿用现有数据，无需迁移。
- 汇总不在容器里跑，手动重跑用本机 uv 执行：
  ```sh
  uv run python generate_summary.py --range 20260710-99999999
  ```
- 容器统一以宿主机用户 ID(1000:1000) 运行，容器写出的文件归属该用户，
  规避此前写输出目录时的 root 权限错乱问题。

查看状态与日志：`docker compose ps`、`docker compose logs -f web`。

下面是**无 Docker 环境的手动方式**（等效于容器内的运行逻辑，仅作为兜底）。

## 首次使用

在项目根目录执行：

```sh
uv run python web/server.py init
uv run python web/server.py add-user wang --name 王承杰
uv run python web/server.py serve
```

`add-user` 会输出一次随机访问码，请私下交给对应用户。浏览器打开
<http://127.0.0.1:8787>。再次执行同一个 `add-user` 命令会重置该用户访问码。

已有 `output/已点击链接.json` 可以一次性归入某位用户：

```sh
uv run python web/server.py import-clicks wang 输出/已点击链接.json
```

常用管理命令：

```sh
uv run python web/server.py list-users
uv run python web/server.py disable-user wang
```

## 局域网或公网使用

只在本机使用时保持默认 `127.0.0.1`。局域网访问可监听全部网卡：

```sh
uv run python web/server.py serve --host 0.0.0.0 --port 8787
```

也可只绑定指定网卡 IP，例如 `--host 192.168.1.10`。随后通过 `http://主机IP:8787` 访问，并通过防火墙
限制访问来源。若需公网访问，应在前面放置启用 HTTPS 的反向代理，并给 `serve` 增加
`--secure-cookie`；不要直接把 Python 端口暴露到公网。

每篇未读文章右侧的“跳过”会将该文章（以及同一招聘主题的其他来源）直接标记为已读，不会打开原文；与点击标题后的已读状态完全相同，也会同步到该用户的其他设备。

数据库默认位于 `web/data/clicks.sqlite3`。备份这一个文件即可保留所有用户与点击记录；
服务运行期间建议使用 SQLite 在线备份或先停止服务再复制。

## 收藏与收藏分组

登录后顶部可在「文章看板 / 收藏 / 投递管理」三个视图间切换。收藏按用户隔离，
同样保存在 `web/data/clicks.sqlite3`（`favorites` / `collections` 表）。

- 文章卡片下方有「☆ 收藏」按钮；创建过分组后，每个分组会各出现一个
  「收藏到某分组」按钮（多组即多个按钮），点已选中的分组按钮即取消收藏，
  点其他分组按钮即移动分组。
- 收藏页可按分组筛选（全部 / 未分组 / 各分组），「管理分组」支持新建、重命名、删除；
  删除分组只把组内收藏移回「未分组」，不会删除收藏本身。
- 收藏时服务端会快照文章标题/链接/公众号/日期：之后看板数据更新甚至文章被移出看板，
  收藏列表仍能打开原文。
- 收藏状态与其他设备每 15 秒同步一次（与已读状态相同）。

## 投递管理

记录每家公司的投递进度，按用户隔离保存（`applications` 表）。

- **从文章记录**：文章卡片或收藏卡片上的「记投递」会打开预填表单——公司名优先取
  AI 筛选抽取的「招聘单位」，没有时用文章标题；招聘链接优先取筛选结果的报名链接。
  都可以改，确认后才入库。
- **手动添加**：投递管理页右上角「添加公司」，填公司名（必填）与招聘网站/网申链接
  （可选，卡片上会生成一键跳转按钮），也可以没有公众号文章来源。
- **状态推进不用下拉框**：每张公司卡片是一排阶段按钮
  （待投递 → 已投递 → 笔试 → 一面 → 二面 → 三面 → 终面/HR面 → 已录用，
  右侧另有「未通过 / 已放弃」两个终止状态），点哪个阶段就推进到哪个阶段；
  「笔试」可以重复点，自动记为第 1 轮、第 2 轮笔试。卡片下方的时间线按时间列出
  全部进度事件，最后一条可「撤销」。
- 支持编辑公司名/链接/备注、删除记录；可按「全部 / 进行中 / 已结束」筛选并搜索。
- 已经记录过投递的文章，看板上的「记投递」按钮会显示为「已记录投递」。

## AI 筛选（计算机类岗位识别）

招聘推文里混着大量非计算机类岗位（教师、医护、活动通知等）。AI 筛选逐组
抓取微信文章正文，海报式推送（正文极短）会下载图片并解码二维码，连同长图切片一起
交给 GLM 结构化识别：判断是否软件工程/计算机类招聘，并提取招聘单位、岗位列表、
工作地点、报名链接。结果写入 `web/data/screen_results.json`，按日期组织 kept/skipped，
逐日落盘、断点可续；网页服务监听该文件变化，下一次请求即自动合并展示，无需重启。

**分析结果对所有用户通用**（一次分析、全员复用，跨日期同链接/同标题直接复用旧结果）；
每位用户的浏览（已读）状态和筛选偏好相互独立。

- 网页端：登录后在筛选区选日期点「开始 AI 筛选」，或点「批量筛选全部」补齐全部
  未分析文章；进度实时显示在按钮右侧。
- 命令行（等效）：

  ```sh
  uv run python web/screen_update.py 2026-08-31          # 筛选某一天（已分析过的组复用旧结果，只补缺）
  uv run python web/screen_update.py --all               # 批量补筛全部日期（每日更新后跑一次兜底）
  uv run python web/screen_update.py 2026-08-31 --force  # 忽略旧结果，重新分析该日期
  ```

  每日更新会把新链接回填到近 14 天的旧日期里，因此补筛按「组」判断而不是按「日期」：
  已分析过的组直接复用，只有真正没筛过的组（含回填到旧日期的）会送去识别。

- 抓取与识别默认 10 并发（`--concurrency` 或环境变量 `WR_SCREEN_CONCURRENCY` 可调，1–20）。
- 看板上的体现：
  - 「AI 筛选」下拉：计算机类精选 / 非计算机类 / 未筛选，以及**符合我的筛选**；
  - 精选文章卡片附岗位列表与「网申 / 报名」直达链接；
  - 岗位地点按你的意向城市高亮（绿色），未识别出工作地点的标为橙色「未标注地点」。
- **我的筛选偏好**（意向城市 + 方向关键词）保存在服务端、按用户记忆，多设备同步；
  默认意向城市：成都、西安、北京、上海、深圳、东莞、广州、杭州。偏好同时驱动
  地点高亮、「意向 · 城市」徽标和「符合我的筛选」（岗位/单位命中关键词，且地点命中意向城市）。
- 已读状态不受影响：看/不看仍由每位用户自己标记。
- GLM 端点复用 `.env` 的 `DEDUP_API_BASE` / `DEDUP_MODEL` / `DEDUP_API_KEY`，
  也可用 `SCREEN_API_BASE` / `SCREEN_MODEL` / `SCREEN_API_KEY` 单独指定。
- 运行日志在 `web/data/screen-logs/`，海报图片缓存在 `web/data/screen-cache/`
  （两者都不入库；`screen_results.json` 随数据提交）。
- `zxing-cpp` 与 `pillow` 负责二维码解码与长图切片，未安装时自动降级为纯文本分析。
  容器内触发筛选需重建镜像（`docker compose up -d --build`）以带入这两个依赖。

## 验证

```sh
uv run python -m unittest discover -s 网页服务/tests -v
uv run python -m py_compile web/server.py
```

自动化测试覆盖：未登录拦截、错误访问码、用户隔离、同组状态传播、取消已读、旧状态导入、
健康检查与跨站请求拦截、AI 筛选结果合并进 bootstrap、筛选任务的校验与生命周期、
收藏与分组（建组/重命名/删除/移动/快照/隔离）、投递管理（手动与从文章添加、状态轮次、
撤销、编辑、删除、隔离）。
