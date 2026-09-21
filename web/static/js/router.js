// 极简 hash 路由：#/view?key=value&…。视图与筛选条件都编码在 URL 里，
// 浏览器回退/前进、刷新恢复都由 hash 驱动；UI 操作改写 hash（默认 push 历史）。
import { state } from "./state.js";

let routeHandler = null;

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [view, qs] = raw.split("?");
  return { view: view || "articles", params: new URLSearchParams(qs || "") };
}

export function href(view, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === "" || value === null || value === undefined) continue;
    if (value instanceof Set) {
      if (value.size) query.set(key, [...value].join(","));
    } else if (Array.isArray(value)) {
      if (value.length) query.set(key, value.join(","));
    } else {
      query.set(key, String(value));
    }
  }
  const qs = query.toString();
  return `#/${view}${qs ? "?" + qs : ""}`;
}

// replace 用于高频输入（搜索框逐字）：替换当前历史记录，不产生回退噪音
export function navigate(view, params, replace = false) {
  const target = href(view, params);
  if (location.hash === target) {
    routeHandler?.(parseHash());
    return;
  }
  if (replace) {
    history.replaceState(null, "", target);
    routeHandler?.(parseHash());
  } else {
    location.hash = target; // 触发 hashchange → routeHandler
  }
}

export function initRouter(handler) {
  routeHandler = handler;
  window.addEventListener("hashchange", () => handler(parseHash()));
  if (!location.hash) location.hash = "#/articles";
  else handler(parseHash());
}

// 各视图当前的 URL 参数（切换视图 / UI 操作改写 hash 时用）
export function currentParams(view) {
  if (view === "fairs") {
    return {
      day: state.fairSelectedDay || "",
      provinces: state.fairProvinces,
      past: state.fairShowPast ? "1" : "",
    };
  }
  if (view === "favorites") {
    return { collection: state.favFilter === "all" ? "" : state.favFilter };
  }
  return {
    status: state.filter,
    account: state.account,
    screens: state.screens,
    q: state.query,
    page: state.page,
  };
}
