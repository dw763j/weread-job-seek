// 页面入口：登录/登出、数据装载（loadApp）、多端同步定时器。
// 各视图模块在这里被导入以完成监听绑定与视图注册（模块副作用）。
import { request } from "./api.js";
import { appView, loginView } from "./dom.js";
import { formatRange, formatUpdatedAt, stableStringify } from "./format.js";
import { applyServerPayload, loadFavorites } from "./favorites.js";
import { loadApplications, rebuildAppliedIds, sortApplications } from "./applications.js";
import { populateAccounts, populateScreens, renderArticles, resumeScreenPolling } from "./articles.js";
import { state } from "./state.js";
import { rerenderViews, updateTabCounts } from "./views.js";

function setView(loggedIn) {
  loginView.hidden = loggedIn;
  appView.hidden = !loggedIn;
}

// 导出供 articles.js 的筛选轮询在任务完成后动态调用（静态互引会成环）
export async function loadApp() {
  try {
    const payload = await request("/api/bootstrap");
    state.groups = payload.groups;
    state.user = payload.user;
    document.querySelector("#user-name").textContent = payload.user.display_name;
    document.querySelector("#range-label").textContent = formatRange(payload.range);
    document.querySelector("#source-label").textContent = `汇集 ${payload.source_rows} 条原始推送，合并为 ${payload.groups.length} 个招聘主题。点击文章后，状态立即保存。`;
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
    populateScreens();
    await loadFavorites();
    await loadApplications();
    updateTabCounts();
    const dates = state.groups.map((group) => group.date).filter(Boolean).sort();
    if (dates.length) document.querySelector("#screen-date").value = dates[dates.length - 1];
    setView(true);
    renderArticles();
    resumeScreenPolling();
  } catch (error) {
    if (error.status === 401) setView(false);
    else {
      setView(false);
      document.querySelector("#login-error").textContent = error.message;
    }
  }
}

async function refreshClicks() {
  if (!state.user || document.hidden) return;
  try {
    const payload = await request("/api/clicks");
    let changed = false;
    for (const group of state.groups) {
      const next = payload.states[group.id] || null;
      if (group.clicked_at !== next) { group.clicked_at = next; changed = true; }
    }
    // 仅在点击状态确有变化（多端同步等）时重渲染；数据没变就不动 DOM，
    // 避免每 15 秒无谓地拆掉用户正展开查看的面板。
    if (changed) renderArticles();
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      setView(false);
    }
  }
  refreshUserData();
}

// 收藏与投递记录的多端同步：数据确实变化时才重渲染，避免拆掉正在编辑的表单
async function refreshUserData() {
  if (!state.user) return;
  try {
    const favorites = await request("/api/favorites");
    const applications = await request("/api/applications");
    const changed =
      stableStringify([favorites.collections, favorites.favorites, favorites.items])
        !== stableStringify([state.collections, state.favIndex, state.favorites])
      || stableStringify(applications.applications) !== stableStringify(state.applications);
    if (!changed) return;
    applyServerPayload(favorites);
    state.applications = applications.applications;
    sortApplications();
    rebuildAppliedIds();
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
