// DOM 构建助手与跨模块共享的元素引用
export const loginView = document.querySelector("#login-view");
export const appView = document.querySelector("#app-view");
export const articleList = document.querySelector("#article-list");
export const emptyState = document.querySelector("#empty-state");
export const syncNotice = document.querySelector("#sync-notice");
export const floatingWidget = document.querySelector("#floating-widget");
export const pager = document.querySelector("#pager");

export function text(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value;
  return node;
}
