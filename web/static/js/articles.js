// 文章看板：筛选与分页由后端 /api/articles 完成（按筛选条件分页返回），
// 本模块负责拉取当前页、渲染卡片/分页器/统计、AI 筛选触发轮询与筛选偏好。
import { request } from "./api.js";
import { articleList, emptyState, pager, text } from "./dom.js";
import { favoriteToolbar } from "./favorites.js";
import { updateClick } from "./clicks.js";
import { locationBadges, screenPositions } from "./screen-info.js";
import { state } from "./state.js";
import { registerView } from "./views.js";
import { navigate } from "./router.js";

function renderStats() {
  const stats = state.stats;
  document.querySelector("#read-count").textContent = stats.read;
  document.querySelector("#total-count").textContent = stats.total;
  document.querySelector("#progress-bar").style.width = `${stats.total ? (stats.read / stats.total) * 100 : 0}%`;
  document.querySelector("#today-read-count").textContent = stats.today_read;
  // 英雄区大数字与悬浮组件、AI 筛选行的「同时满足 N 篇」同源，
  // 标签随阅读状态变化；进度条与「X / Y 已浏览」保持全库口径
  const filteredLabel = { unread: "筛选未读", read: "筛选已读", all: "筛选结果" }[state.filter] || "筛选结果";
  document.querySelector("#unread-count").textContent = state.board.total;
  const heroLabel = document.querySelector("#unread-label");
  if (heroLabel.textContent !== filteredLabel) heroLabel.textContent = filteredLabel;
  document.querySelector("#hero-filter-number").title =
    "同时满足当前阅读状态、搜索词、公众号与 AI 筛选条件的文章总数（下方进度条为全库阅读进度）";
  document.querySelector("#fw-filter-count").textContent = state.board.total;
  const fwLabelNode = document.querySelector("#fw-filter-label");
  if (fwLabelNode.textContent !== filteredLabel) fwLabelNode.textContent = filteredLabel;
  document.querySelector("#fw-filter-item").title =
    "同时满足当前阅读状态、搜索词、公众号与 AI 筛选条件的文章总数";
  document.querySelector("#floating-widget").hidden = false;
}

// 本会话内的即时状态：服务端页是按查询时刻过滤的，点开/跳过后本地再过滤一次，
// 保持"点开的变暗保留、跳过的立即消失"直到下次查询
function visibleItems() {
  const items = state.board.items;
  if (state.filter === "unread") {
    return items.filter((group) => !group.clicked_at || state.sessionViewedIds.has(group.id));
  }
  if (state.filter === "read") return items.filter((group) => group.clicked_at);
  return items;
}

function entryLocations(entry) {
  return [...(entry.positions || []).map((position) => position.location || ""), entry.locations || ""];
}

function firstHitCity(entry) {
  for (const city of state.prefs.cities) {
    if (entryLocations(entry).some((text) => text.includes(city))) return city;
  }
  return null;
}

function dateHeading(value) {
  const date = new Date(`${value}T00:00:00+08:00`);
  const weekday = new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(date);
  const label = `${Number(value.slice(5, 7))} 月 ${Number(value.slice(8, 10))} 日`;
  const heading = document.createElement("div");
  heading.className = "date-heading";
  heading.append(text("h3", "", label), text("span", "", weekday));
  return heading;
}

function createArticle(group) {
  const card = document.createElement("article");
  card.className = `article-card${group.clicked_at ? " is-read" : ""}`;
  card.dataset.groupId = group.id;

  const marker = document.createElement("button");
  marker.type = "button";
  marker.className = "read-marker";
  marker.setAttribute("aria-label", group.clicked_at ? "标记为未读" : "标记为已读");
  marker.title = group.clicked_at ? "标记为未读" : "标记为已读";
  marker.addEventListener("click", () => updateClick(group, !group.clicked_at));

  const content = document.createElement("div");
  content.className = "article-content";
  const meta = document.createElement("div");
  meta.className = "article-meta";
  meta.append(text("span", "account-pill", group.account));
  if (group.members.length > 1) meta.append(text("span", "duplicate-pill", `${group.members.length} 个来源`));
  if (group.screen?.kind === "kept") {
    const hitCity = firstHitCity(group.screen);
    if (hitCity) meta.append(text("span", "screen-pill hit", `意向 · ${hitCity}`));
  }
  if (group.screen?.kind === "skipped") {
    const pill = text("span", "screen-pill skipped", "非招聘");
    pill.title = group.screen.reason || "AI 判定为与招聘无关的内容";
    meta.append(pill);
  }

  const link = document.createElement("a");
  link.className = "article-title";
  link.href = group.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = group.title;
  link.addEventListener("click", () => {
    if (!group.clicked_at) updateClick(group, true, true);
  });
  content.append(meta, link);

  if (group.screen?.kind === "kept") content.append(screenPositions(group.screen));
  if (group.screen?.kind === "skipped" && group.screen.reason) {
    content.append(text("p", "screen-reason", group.screen.reason));
  }

  if (group.members.length > 1) {
    const details = document.createElement("details");
    details.className = "sources";
    const summary = document.createElement("summary");
    summary.textContent = "查看其他来源";
    const list = document.createElement("div");
    list.className = "source-list";
    for (const member of group.members.slice(1)) {
      const sourceLink = document.createElement("a");
      sourceLink.href = member.url;
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener";
      sourceLink.textContent = `${member.date} · ${member.account} · ${member.title}`;
      sourceLink.addEventListener("click", () => {
        if (!group.clicked_at) updateClick(group, true, true);
      });
      list.append(sourceLink);
    }
    details.append(summary, list);
    content.append(details);
  }

  content.append(favoriteToolbar(group));

  const actions = document.createElement("div");
  actions.className = "article-actions";
  if (!group.clicked_at) {
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "skip-button";
    skip.textContent = "跳过";
    skip.title = "标记为已读，不打开文章";
    skip.addEventListener("click", () => updateClick(group, true));
    actions.append(skip);
  }
  card.append(marker, content, actions);
  return card;
}

export async function fetchBoard() {
  const params = new URLSearchParams();
  params.set("status", state.filter);
  if (state.account) params.set("account", state.account);
  if (state.screens.size) params.set("screens", [...state.screens].join(","));
  if (state.query) params.set("q", state.query);
  params.set("page", String(state.page));
  // exclude：本会话点开过的文章——未读查询里保留它们（变暗），刷新后自然消失
  if (state.filter === "unread" && state.sessionViewedIds.size) {
    params.set("exclude", [...state.sessionViewedIds].join(","));
  }
  try {
    const payload = await request(`/api/articles?${params.toString()}`);
    state.board = payload;
    state.page = payload.page;
  } catch (error) {
    if (error.status !== 401) return; // 拉取失败保留旧页，不打断界面
    throw error;
  }
  populateScreens();
  renderArticles();
}

export function renderArticles() {
  // 保留用户当前展开的“其他来源”面板：整体重建 DOM 前记下展开状态，重建后原样恢复。
  const openIds = new Set(
    [...articleList.querySelectorAll("details.sources[open]")].map((d) => d.closest(".article-card")?.dataset.groupId),
  );
  const visible = visibleItems();
  const totalPages = Math.max(1, state.board.pages);
  articleList.replaceChildren();
  let lastDate = "";
  for (const group of visible) {
    if (group.date !== lastDate) {
      articleList.append(dateHeading(group.date));
      lastDate = group.date;
    }
    const card = createArticle(group);
    if (openIds.has(group.id)) card.querySelector("details.sources")?.setAttribute("open", "");
    articleList.append(card);
  }
  emptyState.hidden = visible.length > 0;
  renderPager(state.board.total, totalPages);
  renderStats();
}

// 页码按钮序列：固定展示头 3 页和尾 3 页，当前页左右各带 1 页，缺口用省略号补齐；
// 总页数不多（或区间恰好相接）时自然合并为连续页码，不出现省略号。
function pageSequence(totalPages, current) {
  const HEAD = 3;
  const TAIL = 3;
  const SIBLING = 1;
  if (totalPages <= HEAD + TAIL + 2) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  const head = Array.from({ length: HEAD }, (_, index) => index + 1);
  const tail = Array.from({ length: TAIL }, (_, index) => totalPages - TAIL + 1 + index);
  const midStart = Math.max(HEAD + 1, current - SIBLING);
  const midEnd = Math.min(totalPages - TAIL, current + SIBLING);
  const items = [...head];
  if (midStart <= midEnd) {
    if (midStart - items[items.length - 1] > 1) items.push("…");
    for (let page = midStart; page <= midEnd; page += 1) items.push(page);
  }
  if (tail[0] - items[items.length - 1] > 1) items.push("…");
  items.push(...tail);
  return items;
}

function renderPager(total, totalPages) {
  if (totalPages <= 1) {
    pager.hidden = true;
    pager.replaceChildren();
    return;
  }
  const scrollTopToArticles = () => {
    const quickbar = document.querySelector(".quickbar");
    window.scrollTo({ top: articleList.getBoundingClientRect().top + window.scrollY - quickbar.offsetHeight - 12 });
  };
  const goToPage = (page) => {
    const target = Math.min(Math.max(1, page), totalPages);
    if (target === state.page) return;
    navigate("articles", { status: state.filter, account: state.account, screens: state.screens, q: state.query, page: target });
    scrollTopToArticles();
  };

  const prev = document.createElement("button");
  prev.type = "button";
  prev.textContent = "上一页";
  prev.disabled = state.page <= 1;
  prev.addEventListener("click", () => goToPage(state.page - 1));

  const pagesWrap = document.createElement("span");
  pagesWrap.className = "pager-pages";
  for (const item of pageSequence(totalPages, state.page)) {
    if (item === "…") {
      pagesWrap.append(text("span", "page-ellipsis", "…"));
      continue;
    }
    const pageButton = document.createElement("button");
    pageButton.type = "button";
    pageButton.className = `page-btn${item === state.page ? " active" : ""}`;
    pageButton.textContent = item;
    pageButton.disabled = item === state.page;
    pageButton.addEventListener("click", () => goToPage(item));
    pagesWrap.append(pageButton);
  }

  const next = document.createElement("button");
  next.type = "button";
  next.textContent = "下一页";
  next.disabled = state.page >= totalPages;
  next.addEventListener("click", () => goToPage(state.page + 1));

  const info = text("span", "pager-info", `共 ${total} 条`);

  const jump = document.createElement("span");
  jump.className = "pager-jump";
  const jumpInput = document.createElement("input");
  jumpInput.type = "number";
  jumpInput.min = "1";
  jumpInput.max = String(totalPages);
  jumpInput.setAttribute("aria-label", "跳转页码");
  const jumpButton = document.createElement("button");
  jumpButton.type = "button";
  jumpButton.textContent = "跳转";
  const submitJump = () => {
    const value = Number.parseInt(jumpInput.value, 10);
    if (Number.isNaN(value)) return;
    goToPage(value);
    jumpInput.value = "";
    jumpInput.blur();
  };
  jumpButton.addEventListener("click", submitJump);
  jumpInput.addEventListener("keydown", (event) => { if (event.key === "Enter") submitJump(); });
  jump.append(text("span", "", "跳至"), jumpInput, text("span", "", "页"), jumpButton);

  pager.replaceChildren(prev, pagesWrap, next, info, jump);
  pager.hidden = false;
}

export function populateAccounts() {
  const select = document.querySelector("#account-filter");
  const accounts = state.accounts.map((item) => item.account).sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.replaceChildren(new Option("全部公众号", ""), ...accounts.map((account) => new Option(account, account)));
  select.value = state.account;
  if (select.value !== state.account) state.account = "";
}

// AI 筛选 chips：多选、条件取交集；关键词/城市两个 chip 用的是「我的筛选偏好」里保存的值
const SCREEN_FILTERS = [
  { key: "cs", label: "含计算机类岗位", title: "只看岗位类别/名称含计算机方向的（前端即时过滤，不影响后台提取）" },
  { key: "keywords", label: "方向关键词", title: "只看岗位/单位/标题命中你保存的方向关键词的文章（在下方偏好里设置）" },
  { key: "cities", label: "意向城市", title: "只看工作地点命中你保存的意向城市的文章（在下方偏好里设置）" },
  { key: "skipped", label: "非招聘信息", title: "只看 AI 判定为与招聘无关的内容" },
  { key: "none", label: "未筛选", title: "只看还没跑过 AI 筛选的文章" },
];

export function populateScreens() {
  const wrap = document.querySelector("#screen-filter");
  const counts = state.board.counts || {};
  wrap.replaceChildren();
  for (const item of SCREEN_FILTERS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip screen-chip${state.screens.has(item.key) ? " active" : ""}`;
    chip.title = item.title;
    chip.append(text("span", "", item.label));
    chip.append(text("span", "chip-count", String(counts[item.key] ?? 0)));
    chip.addEventListener("click", () => {
      if (state.screens.has(item.key)) state.screens.delete(item.key);
      else state.screens.add(item.key);
      navigate("articles", { status: state.filter, account: state.account, screens: state.screens, q: state.query, page: 1 });
    });
    wrap.append(chip);
  }
  // 组合结果：当前所有条件（阅读状态、搜索、公众号、AI 筛选）取交集后的总数。
  // 每个 chip 上的数字是"单选这个条件"的数，这里给的是全部已选条件的最终组合数。
  const combined = text("span", "chip-total", `同时满足 ${state.board.total} 篇`);
  combined.title = "当前阅读状态、搜索词、公众号与已选 AI 筛选条件全部同时满足的文章总数";
  wrap.append(combined);
}

// 路由进入时把 hash 里的筛选条件同步回控件（搜索框、下拉、分段按钮）
export function syncControls() {
  document.querySelectorAll("[data-status]").forEach((button) => {
    button.classList.toggle("active", button.dataset.status === state.filter);
  });
  const search = document.querySelector("#search-input");
  if (search.value !== state.query) search.value = state.query;
  const select = document.querySelector("#account-filter");
  if (select.value !== state.account) select.value = state.account;
}

// ---------- 看板筛选交互（改写 hash，由路由统一处理） ----------

let searchTimer = null;
document.querySelector("#search-input").addEventListener("input", (event) => {
  const value = event.target.value.trim().toLocaleLowerCase("zh-CN");
  if (value === state.query) return;
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    // 搜索逐字输入用 replace：不往历史里塞一串中间态
    navigate("articles", { status: state.filter, account: state.account, screens: state.screens, q: value, page: 1 }, true);
  }, 300);
});

document.querySelector("#account-filter").addEventListener("change", (event) => {
  navigate("articles", { status: state.filter, account: event.target.value, screens: state.screens, q: state.query, page: 1 });
});

document.querySelectorAll("[data-status]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.status === state.filter) return;
    navigate("articles", { status: button.dataset.status, account: state.account, screens: state.screens, q: state.query, page: 1 });
  });
});

// ---------- AI 筛选触发与进度轮询 ----------
// 手动触发 AI 筛选的 UI 已下线（日常筛选由后台脚本完成），
// /api/screen 与 /api/screen-status 端点保留供脚本/调试使用。

// ---------- 我的筛选偏好（意向城市 / 方向关键词，按用户保存在服务端） ----------

const PREF_SPLIT = /[、，,;；\s]+/;
const parsePrefInput = (value) => [...new Set(value.split(PREF_SPLIT).map((item) => item.trim()).filter(Boolean))];

document.querySelector("#pref-save-button").addEventListener("click", async () => {
  const statusNode = document.querySelector("#pref-status");
  const button = document.querySelector("#pref-save-button");
  button.disabled = true;
  try {
    const payload = await request("/api/preferences", {
      method: "POST",
      body: JSON.stringify({
        cities: parsePrefInput(document.querySelector("#pref-cities").value),
        keywords: parsePrefInput(document.querySelector("#pref-keywords").value),
      }),
    });
    state.prefs = payload.preferences;
    document.querySelector("#pref-cities").value = state.prefs.cities.join("、");
    document.querySelector("#pref-keywords").value = state.prefs.keywords.join("、");
    statusNode.textContent = "偏好已保存，多设备同步 ✓";
    await fetchBoard(); // 关键词/城市 chips 的命中数依赖偏好，重拉当前页与计数
  } catch (error) {
    statusNode.textContent = `保存失败：${error.message}`;
  } finally {
    button.disabled = false;
    window.setTimeout(() => { if (statusNode.textContent.startsWith("偏好已保存")) statusNode.textContent = ""; }, 2500);
  }
});

registerView("articles", renderArticles);
