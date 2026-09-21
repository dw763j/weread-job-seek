// 视图切换与渲染分发：各视图模块加载时通过 registerView 注册自己的渲染函数。
// 视图/筛选状态由 router.js 的 hash 驱动；本模块只负责 DOM 显隐与渲染分发，
// 避免反向依赖具体视图（articles/fairs/favorites）造成循环引用。
import { state } from "./state.js";
import { floatingWidget, syncNotice } from "./dom.js";
import { navigate, currentParams } from "./router.js";

const viewRenderers = {};

export function registerView(name, render) {
  viewRenderers[name] = render;
}

export function toast(message, isError = false) {
  syncNotice.textContent = message;
  syncNotice.classList.toggle("is-error", isError);
  window.setTimeout(() => {
    if (syncNotice.textContent === message) syncNotice.textContent = "";
  }, 2200);
}

// 只做 DOM 显隐与渲染（路由 handler 设好 state 后调用），不写 hash
export function showView(name, scrollToTop = false) {
  state.currentView = name;
  document.querySelectorAll(".view-tabs [data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  document.querySelector("#articles-view").hidden = name !== "articles";
  document.querySelector("#fairs-view").hidden = name !== "fairs";
  document.querySelector("#favorites-view").hidden = name !== "favorites";
  document.querySelector("#applications-view")?.setAttribute("hidden", "");
  floatingWidget.hidden = name !== "articles";
  viewRenderers[name]?.();
  if (scrollToTop) window.scrollTo({ top: 0 });
}

// 保留给不经过路由的局部重渲染（收藏变更、多端同步等）
export function rerenderViews() {
  viewRenderers[state.currentView]?.();
  updateTabCounts();
}

export function updateTabCounts() {
  const favCount = Object.keys(state.favIndex).length;
  const favBadge = document.querySelector("#fav-tab-count");
  favBadge.hidden = !favCount;
  favBadge.textContent = favCount;
  // 宣讲会徽标 = 即将举行的场次（/api/fairs 返回，服务端按举办日期算）
  const fairBadge = document.querySelector("#fair-tab-count");
  fairBadge.hidden = !state.fairs.upcoming;
  fairBadge.textContent = state.fairs.upcoming;
}

// 顶部视图切换走路由：hash 变化 → 回退/前进/刷新都能还原视图与筛选条件
document.querySelectorAll(".view-tabs [data-view]").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.view, currentParams(button.dataset.view)));
});
