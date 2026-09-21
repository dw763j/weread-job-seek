// 已读状态写入与乐观更新（文章看板与宣讲会页共用）。
// view = 用户点开文章：记入本会话查看集（未读列表里保留变暗，下次刷新移出）；
// false = 跳过/手动标记已读：立即从未读列表消失。view 同时作为请求的 keepalive
// （点开文章可能立刻切走页面）。
import { request } from "./api.js";
import { syncNotice } from "./dom.js";
import { state } from "./state.js";
import { rerenderViews } from "./views.js";

export async function updateClick(group, clicked, view = false) {
  if (state.pending.has(group.id)) return;
  const previous = group.clicked_at;
  group.clicked_at = clicked ? new Date().toISOString() : null;
  if (clicked && view) state.sessionViewedIds.add(group.id);
  if (!clicked) state.sessionViewedIds.delete(group.id);
  state.pending.add(group.id);
  rerenderViews();
  try {
    const payload = await request("/api/clicks", {
      method: "POST",
      body: JSON.stringify({ group_id: group.id, clicked }),
      keepalive: view,
    });
    group.clicked_at = payload.clicked_at;
    syncNotice.textContent = "浏览状态已保存";
    window.setTimeout(() => { if (syncNotice.textContent === "浏览状态已保存") syncNotice.textContent = ""; }, 1600);
  } catch (error) {
    group.clicked_at = previous;
    syncNotice.textContent = `保存失败：${error.message}`;
  } finally {
    state.pending.delete(group.id);
    rerenderViews();
    scheduleBoardRefresh();
  }
}

// 已读状态变化后静默重拉当前看板页（300ms 防抖合并连点）：
// chips 计数/分页总数与页面保持一致；exclude 保证点开变暗的条目仍在页面上
let boardRefreshTimer = null;
export function scheduleBoardRefresh() {
  if (state.currentView !== "articles") return;
  window.clearTimeout(boardRefreshTimer);
  boardRefreshTimer = window.setTimeout(async () => {
    try {
      const { fetchBoard } = await import("./articles.js");
      await fetchBoard();
    } catch { /* 静默：拉取失败保留旧页 */ }
  }, 300);
}
