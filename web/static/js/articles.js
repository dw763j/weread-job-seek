// 文章看板：筛选、文章卡片与分页、已读状态、AI 筛选触发轮询、筛选偏好
import { request } from "./api.js";
import { articleList, emptyState, pager, syncNotice, text } from "./dom.js";
import { chinaToday, cleanApplyUrl } from "./format.js";
import { favoriteToolbar } from "./favorites.js";
import { PAGE_SIZE, state } from "./state.js";
import { registerView } from "./views.js";

function todayReadCount() {
  const today = chinaToday();
  return state.groups.filter(
    (group) => group.clicked_at && group.clicked_at.slice(0, 10) === today
  ).length;
}

function renderStats() {
  const read = state.groups.filter((group) => group.clicked_at).length;
  const total = state.groups.length;
  const unread = total - read;
  document.querySelector("#read-count").textContent = read;
  document.querySelector("#total-count").textContent = total;
  document.querySelector("#unread-count").textContent = unread;
  document.querySelector("#progress-bar").style.width = `${total ? (read / total) * 100 : 0}%`;
  document.querySelector("#today-read-count").textContent = todayReadCount();
  document.querySelector("#fw-unread-count").textContent = unread;
  document.querySelector("#floating-widget").hidden = false;
}

function matches(group) {
  if (state.filter === "read" && !group.clicked_at) return false;
  if (state.filter === "unread" && group.clicked_at) return false;
  if (state.account && group.account !== state.account) return false;
  if (state.screen === "kept" && group.screen?.kind !== "kept") return false;
  if (state.screen === "skipped" && group.screen?.kind !== "skipped") return false;
  if (state.screen === "none" && group.screen) return false;
  if (state.screen === "mine" && (group.screen?.kind !== "kept" || !matchesMine(group))) return false;
  if (state.query) {
    const haystack = [group.title, group.account, ...group.members.map((member) => `${member.title} ${member.account}`)]
      .join(" ").toLocaleLowerCase("zh-CN");
    if (!haystack.includes(state.query)) return false;
  }
  return true;
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

function matchesMine(group) {
  const entry = group.screen || {};
  const { keywords, cities } = state.prefs;
  let keywordOk = !keywords.length;
  if (!keywordOk) {
    const fields = [
      ...(entry.positions || []).map((position) => `${position.name} ${position.category}`),
      entry.intro || "", entry.unit || "", group.title,
    ].join(" ").toLocaleLowerCase("zh-CN");
    keywordOk = keywords.some((keyword) => fields.includes(keyword.toLocaleLowerCase("zh-CN")));
  }
  let cityOk = !cities.length;
  if (!cityOk) cityOk = Boolean(firstHitCity(entry));
  return keywordOk && cityOk;
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

function locationBadges(locationText) {
  const wrap = document.createElement("span");
  wrap.className = "pos-locs";
  const raw = (locationText || "").trim();
  if (!raw || /未标注|待定|不详|^无$/.test(raw)) {
    wrap.append(text("span", "loc missing", "未标注地点"));
    return wrap;
  }
  const tokens = raw.split(/[、，,;；\/\s]+/).filter(Boolean);
  for (const token of tokens) {
    const hit = state.prefs.cities.some((city) => token.includes(city));
    wrap.append(text("span", hit ? "loc hot" : "loc", token));
  }
  return wrap;
}

function screenPositions(entry) {
  const block = document.createElement("div");
  block.className = "screen-info";
  if (entry.intro || entry.recruit_target) {
    const intro = document.createElement("p");
    intro.className = "screen-intro";
    const parts = [];
    if (entry.intro) parts.push(entry.intro);
    if (entry.recruit_target) parts.push(`招聘对象：${entry.recruit_target}`);
    intro.textContent = parts.join(" · ");
    block.append(intro);
  }
  const positions = entry.positions || [];
  if (positions.length) {
    const list = document.createElement("ul");
    list.className = "screen-positions";
    for (const position of positions) {
      const row = document.createElement("li");
      row.append(text("span", "pos-name", position.name));
      if (position.category) {
        row.append(text("span", `pos-cat${/计算机|软件|人工智能|大数据|算法|网络安全|开发|IT/.test(position.category) ? " cs" : ""}`, position.category));
      }
      row.append(locationBadges(position.location));
      list.append(row);
    }
    block.append(list);
  }
  const applyHref = cleanApplyUrl(entry.apply_url);
  if (applyHref) {
    const apply = document.createElement("a");
    apply.className = "apply-button";
    apply.href = applyHref;
    apply.target = "_blank";
    apply.rel = "noopener";
    apply.textContent = applyHref.startsWith("mailto:") ? "邮件报名" : "网申 / 报名";
    block.append(apply);
  }
  if (entry.note) block.append(text("p", "screen-note", entry.note));
  return block;
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
    meta.append(text("span", "screen-pill kept", "计算机类"));
    const hitCity = firstHitCity(group.screen);
    if (hitCity) meta.append(text("span", "screen-pill hit", `意向 · ${hitCity}`));
  }
  if (group.screen?.kind === "skipped") {
    const pill = text("span", "screen-pill skipped", "非计算机类");
    pill.title = group.screen.reason || "AI 判定为非计算机类岗位";
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

export function renderArticles() {
  // 保留用户当前展开的“其他来源”面板：整体重建 DOM 前记下展开状态，重建后原样恢复。
  // 仅在本次页面会话内有效，刷新页面后回到默认折叠。
  const openIds = new Set(
    [...articleList.querySelectorAll("details.sources[open]")].map((d) => d.closest(".article-card")?.dataset.groupId),
  );
  const visible = state.groups.filter(matches);
  const totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  state.page = Math.min(Math.max(1, state.page), totalPages);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = visible.slice(start, start + PAGE_SIZE);
  articleList.replaceChildren();
  let lastDate = "";
  for (const group of pageItems) {
    if (group.date !== lastDate) {
      articleList.append(dateHeading(group.date));
      lastDate = group.date;
    }
    const card = createArticle(group);
    if (openIds.has(group.id)) card.querySelector("details.sources")?.setAttribute("open", "");
    articleList.append(card);
  }
  emptyState.hidden = visible.length > 0;
  renderPager(visible.length, totalPages);
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
    state.page = target;
    renderArticles();
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

async function updateClick(group, clicked, background = false) {
  if (state.pending.has(group.id)) return;
  const previous = group.clicked_at;
  group.clicked_at = clicked ? new Date().toISOString() : null;
  state.pending.add(group.id);
  renderArticles();
  try {
    const payload = await request("/api/clicks", {
      method: "POST",
      body: JSON.stringify({ group_id: group.id, clicked }),
      keepalive: background,
    });
    group.clicked_at = payload.clicked_at;
    syncNotice.textContent = "浏览状态已保存";
    window.setTimeout(() => { if (syncNotice.textContent === "浏览状态已保存") syncNotice.textContent = ""; }, 1600);
  } catch (error) {
    group.clicked_at = previous;
    syncNotice.textContent = `保存失败：${error.message}`;
  } finally {
    state.pending.delete(group.id);
    renderArticles();
  }
}

export function populateAccounts() {
  const select = document.querySelector("#account-filter");
  const accounts = [...new Set(state.groups.map((group) => group.account))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.replaceChildren(new Option("全部公众号", ""), ...accounts.map((account) => new Option(account, account)));
}

export function populateScreens() {
  const select = document.querySelector("#screen-filter");
  const current = state.screen;
  const counts = { kept: 0, skipped: 0, none: 0, mine: 0 };
  for (const group of state.groups) {
    if (group.screen?.kind === "kept") {
      counts.kept += 1;
      if (matchesMine(group)) counts.mine += 1;
    } else if (group.screen?.kind === "skipped") counts.skipped += 1;
    else counts.none += 1;
  }
  select.replaceChildren(
    new Option("全部文章", ""),
    new Option(`符合我的筛选（${counts.mine}）`, "mine"),
    new Option(`计算机类精选（${counts.kept}）`, "kept"),
    new Option(`非计算机类（${counts.skipped}）`, "skipped"),
    new Option(`未筛选（${counts.none}）`, "none"),
  );
  if ([...select.options].some((option) => option.value === current)) select.value = current;
  else state.screen = "";
  select.disabled = counts.kept + counts.skipped === 0;
}

// ---------- 看板筛选交互 ----------

document.querySelector("#search-input").addEventListener("input", (event) => {
  state.query = event.target.value.trim().toLocaleLowerCase("zh-CN");
  state.page = 1;
  renderArticles();
});

document.querySelector("#account-filter").addEventListener("change", (event) => {
  state.account = event.target.value;
  state.page = 1;
  renderArticles();
});

document.querySelector("#screen-filter").addEventListener("change", (event) => {
  state.screen = event.target.value;
  state.page = 1;
  renderArticles();
});

document.querySelectorAll("[data-status]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-status]").forEach((item) => item.classList.toggle("active", item === button));
    state.filter = button.dataset.status;
    state.page = 1;
    renderArticles();
  });
});

// ---------- AI 筛选触发与进度轮询 ----------

const screenRunButton = document.querySelector("#screen-run-button");
const screenAllButton = document.querySelector("#screen-all-button");
const screenStatus = document.querySelector("#screen-status");
let screenTimer = null;

function scopeLabel(scope) {
  return scope === "all" ? "全部文章" : scope;
}

function screenStatusLine(scope, task) {
  if (task.status === "running") {
    const progress = task.log?.length ? task.log[task.log.length - 1] : "正在启动…";
    return `${scopeLabel(scope)} 筛选中：${progress}`;
  }
  if (task.status === "done") return `${scopeLabel(scope)} 筛选完成 ✓（页面已刷新结果）`;
  return `${scopeLabel(scope)} 筛选失败，详见 web/data/screen-logs/screen-${scope}.log`;
}

async function pollScreenStatus() {
  try {
    const payload = await request("/api/screen-status");
    const runningScope = Object.keys(payload.tasks).find((scope) => payload.tasks[scope].status === "running");
    if (runningScope) {
      screenRunButton.disabled = true;
      screenAllButton.disabled = true;
      screenStatus.textContent = screenStatusLine(runningScope, payload.tasks[runningScope]);
      return;
    }
    if (screenTimer) {
      window.clearInterval(screenTimer);
      screenTimer = null;
    }
    const scopes = Object.keys(payload.tasks);
    if (scopes.length) {
      const scope = scopes[scopes.length - 1];
      const task = payload.tasks[scope];
      screenStatus.textContent = screenStatusLine(scope, task);
      // 筛选完成后重新拉取数据；loadApp 在 main.js，动态引入避免静态循环依赖
      if (task.status === "done") {
        const { loadApp } = await import("./main.js");
        loadApp();
      }
    }
    screenRunButton.disabled = false;
    screenAllButton.disabled = false;
  } catch (error) {
    if (screenTimer) {
      window.clearInterval(screenTimer);
      screenTimer = null;
    }
    screenRunButton.disabled = false;
    screenAllButton.disabled = false;
  }
}

export function resumeScreenPolling() {
  request("/api/screen-status").then((payload) => {
    if (payload.running && !screenTimer) {
      screenRunButton.disabled = true;
      screenAllButton.disabled = true;
      screenTimer = window.setInterval(pollScreenStatus, 3000);
    }
  }).catch(() => { /* 未登录或网络错误时静默 */ });
}

async function triggerScreen(body, label) {
  screenRunButton.disabled = true;
  screenAllButton.disabled = true;
  screenStatus.textContent = `${label} 筛选中…（抓取文章 + 二维码解码 + 模型识别，约需几分钟）`;
  try {
    const payload = await request("/api/screen", { method: "POST", body: JSON.stringify(body) });
    if (payload.started) {
      if (!screenTimer) screenTimer = window.setInterval(pollScreenStatus, 3000);
    } else {
      screenRunButton.disabled = false;
      screenAllButton.disabled = false;
    }
  } catch (error) {
    screenStatus.textContent = `触发失败：${error.message}`;
    if (error.status === 409 && !screenTimer) screenTimer = window.setInterval(pollScreenStatus, 3000);
    else {
      screenRunButton.disabled = false;
      screenAllButton.disabled = false;
    }
  }
}

screenRunButton.addEventListener("click", () => {
  const date = document.querySelector("#screen-date").value;
  if (!date) {
    screenStatus.textContent = "请先选择筛选日期";
    return;
  }
  triggerScreen({ date, force: document.querySelector("#screen-force").checked }, date);
});

screenAllButton.addEventListener("click", () => {
  triggerScreen({ all: true, force: document.querySelector("#screen-force").checked }, "全部文章");
});

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
    populateScreens();
    state.page = 1;
    renderArticles();
    statusNode.textContent = "偏好已保存，多设备同步 ✓";
  } catch (error) {
    statusNode.textContent = `保存失败：${error.message}`;
  } finally {
    button.disabled = false;
    window.setTimeout(() => { if (statusNode.textContent.startsWith("偏好已保存")) statusNode.textContent = ""; }, 2500);
  }
});

registerView("articles", renderArticles);
