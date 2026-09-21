// 页面入口：登录/登出、数据装载（loadApp）、hash 路由分发、多端同步定时器。
// bootstrap 只拿全局轻量信息（统计/账号/偏好/geo/收藏索引），文章看板按筛选
// 条件经 /api/articles 分页拉取，宣讲会经 /api/fairs 拉取；视图与筛选状态
// 全部编码在 URL hash 里，回退/前进/刷新可还原。
// 各视图模块在这里被导入以完成监听绑定与视图注册（模块副作用）。
import { request } from "./api.js";
import { appView, loginView } from "./dom.js";
import { formatRange, formatUpdatedAt, stableStringify } from "./format.js";
import { applyServerPayload, loadFavorites } from "./favorites.js";
import { fetchBoard, populateAccounts, syncControls } from "./articles.js";
import { loadFairsData, renderFairsView } from "./fairs.js";
import { configureGeo } from "./fair-core.js";
import { state } from "./state.js";
import { showView, rerenderViews, updateTabCounts } from "./views.js";
import { initRouter, parseHash } from "./router.js";

function setView(loggedIn) {
  loginView.hidden = loggedIn;
  appView.hidden = !loggedIn;
}

// 导出供 articles.js 的筛选轮询在任务完成后动态调用（静态互引会成环）
export async function loadApp() {
  try {
    const payload = await request("/api/bootstrap");
    configureGeo(payload.geo);
    state.user = payload.user;
    state.accounts = payload.accounts || [];
    state.stats = payload.stats || state.stats;
    document.querySelector("#user-name").textContent = payload.user.display_name;
    document.querySelector("#range-label").textContent = formatRange(payload.range);
    document.querySelector("#source-label").textContent = `汇集 ${payload.source_rows} 条原始推送，合并为 ${state.stats.total} 篇招聘文章${state.stats.fair_total ? `、${state.stats.fair_total} 场宣讲会` : ""}。点击文章后，状态立即保存。`;
    const updatedAt = document.querySelector("#updated-at");
    const updatedLabel = formatUpdatedAt(payload.generated_at);
    if (updatedLabel) {
      updatedAt.hidden = false;
      updatedAt.textContent = `数据更新于 ${updatedLabel}`;
    }
    populateAccounts();
    if (payload.preferences) {
      state.prefs = {
        cities: Array.isArray(payload.preferences.cities) ? payload.preferences.cities : [],
        keywords: Array.isArray(payload.preferences.keywords) ? payload.preferences.keywords : [],
      };
    }
    document.querySelector("#pref-cities").value = state.prefs.cities.join("、");
    document.querySelector("#pref-keywords").value = state.prefs.keywords.join("、");
    await loadFavorites();
    await loadFairsData();
    updateTabCounts();
    setView(true);
    initRouter(applyRoute);
    refreshClicks(); // 立即拉一次已读状态：收藏页的已读展示不等 15 秒轮询
  } catch (error) {
    if (error.status === 401) setView(false);
    else {
      setView(false);
      document.querySelector("#login-error").textContent = error.message;
    }
  }
}

// hash → 状态 → 拉数/渲染。所有 UI 操作都只是改写 hash，统一走这里。
async function applyRoute({ view, params }) {
  if (!state.user) return;
  const valid = ["articles", "fairs", "favorites"];
  const target = valid.includes(view) ? view : "articles";
  const viewChanged = target !== state.currentView;

  if (target === "articles") {
    const status = params.get("status");
    state.filter = ["unread", "all", "read"].includes(status) ? status : "unread";
    state.account = params.get("account") || "";
    state.screens = new Set((params.get("screens") || "").split(",").filter(Boolean));
    state.query = params.get("q") || "";
    state.page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
    syncControls();
    showView("articles", viewChanged);
    await fetchBoard();
    return;
  }

  if (target === "fairs") {
    if (!state.fairs.items.length) await loadFairsData();
    state.fairShowPast = params.get("past") === "1";
    state.fairProvinces = new Set((params.get("provinces") || "").split(",").filter(Boolean));
    const day = params.get("day") || "";
    state.fairSelectedDay = day === "待定" ? "待定" : (day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : "");
    showView("fairs", viewChanged);
    return;
  }

  // favorites：collection = all / none / 分组 id
  const collection = params.get("collection") || "all";
  state.favFilter = /^\d+$/.test(collection) ? Number(collection) : collection;
  showView("favorites", viewChanged);
}

async function refreshClicks() {
  if (!state.user || document.hidden) return;
  try {
    const payload = await request("/api/clicks");
    let changed = false;
    // 只更新当前在用的数据集（看板当前页与宣讲会列表）里的已读状态；
    // 全量分组状态存 clickStates，收藏页据此显示已读
    if (stableStringify(payload.states) !== stableStringify(state.clickStates)) {
      state.clickStates = payload.states;
      changed = true;
    }
    const apply = (items) => {
      for (const item of items) {
        const next = payload.states[item.id] || null;
        if (item.clicked_at !== next) { item.clicked_at = next; changed = true; }
      }
    };
    apply(state.board.items);
    apply(state.fairs.items);
    if (payload.stats && stableStringify(payload.stats) !== stableStringify(state.stats)) {
      state.stats = payload.stats;
      changed = true;
    }
    // 仅在状态确有变化（多端同步等）时重渲染；数据没变就不动 DOM，
    // 避免每 15 秒无谓地拆掉用户正展开查看的面板。
    if (changed) rerenderViews();
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      setView(false);
    }
  }
  refreshUserData();
}

// 收藏与分组级投递标记的多端同步：数据确实变化时才重渲染，避免拆掉正在编辑的面板
async function refreshUserData() {
  if (!state.user) return;
  try {
    const favorites = await request("/api/favorites");
    const changed =
      stableStringify([favorites.collections, favorites.favorites, favorites.items, favorites.group_states])
        !== stableStringify([state.collections, state.favIndex, state.favorites, state.appliedIndex]);
    if (!changed) return;
    applyServerPayload(favorites);
    rerenderViews();
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      setView(false);
    }
  }
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.querySelector("#login-error");
  errorNode.textContent = "";
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    await request("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#username").value.trim(),
        access_code: document.querySelector("#access-code").value,
      }),
    });
    document.querySelector("#access-code").value = "";
    await loadApp();
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  await request("/api/logout", { method: "POST", body: "{}" });
  state.user = null;
  setView(false);
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshClicks(); });
window.setInterval(refreshClicks, 15_000);
loadApp();
