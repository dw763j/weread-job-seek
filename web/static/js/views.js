// 视图切换与渲染分发：各视图模块加载时通过 registerView 注册自己的渲染函数，
// 避免本模块反向依赖具体视图（articles/favorites/applications）造成循环引用。
import { state } from "./state.js";
import { floatingWidget, syncNotice } from "./dom.js";

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

export function switchView(name) {
  state.currentView = name;
  document.querySelectorAll(".view-tabs [data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  document.querySelector("#articles-view").hidden = name !== "articles";
  document.querySelector("#favorites-view").hidden = name !== "favorites";
  document.querySelector("#applications-view").hidden = name !== "applications";
  floatingWidget.hidden = name !== "articles";
  // 文章看板也要重渲染：在其他视图里改过收藏/分组后，卡片上的收藏按钮需要刷新
  viewRenderers[name]?.();
  window.scrollTo({ top: 0 });
}

export function rerenderViews() {
  viewRenderers[state.currentView]?.();
  updateTabCounts();
}

export function updateTabCounts() {
  const favCount = Object.keys(state.favIndex).length;
  const favBadge = document.querySelector("#fav-tab-count");
  favBadge.hidden = !favCount;
  favBadge.textContent = favCount;
  const appBadge = document.querySelector("#app-tab-count");
  appBadge.hidden = !state.applications.length;
  appBadge.textContent = state.applications.length;
}

document.querySelectorAll(".view-tabs [data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
