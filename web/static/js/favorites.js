// 收藏与收藏分组：文章卡工具条、收藏列表页与分组管理
import { request } from "./api.js";
import { text } from "./dom.js";
import { cleanApplyUrl } from "./format.js";
import { updateClick, scheduleBoardRefresh } from "./clicks.js";
import { screenPositions } from "./screen-info.js";
import { state } from "./state.js";
import { registerView, rerenderViews, toast } from "./views.js";
import { navigate } from "./router.js";

function collectionName(id) {
  if (id == null) return "未分组";
  return state.collections.find((collection) => collection.id === id)?.name || "未分组";
}

// 已读判定：点击记录优先；标记过已投递/不投递的组视为已读。
// 存量收藏的分组 id 可能因数据重新去重而失效，服务端无法为其写点击记录，
// 但意向标记还在，靠它兜底，避免这类卡片永远卡在未读。
function isReadGroup(groupId) {
  return Boolean(state.clickStates[groupId] || state.appliedIndex[groupId]);
}

export function applyServerPayload(payload) {
  if (payload.collections) state.collections = payload.collections;
  if (payload.favorites) state.favIndex = payload.favorites;
  if (payload.items) state.favorites = payload.items;
  if (payload.group_states) state.appliedIndex = payload.group_states;
}

export async function loadFavorites() {
  applyServerPayload(await request("/api/favorites"));
}

export async function setFavorite(group, collectionId, favorited) {
  const backup = { favIndex: { ...state.favIndex }, favorites: state.favorites };
  // 收藏即已读：收藏的文章以后从收藏页看，立即移出看板的未读列表
  const wasUnread = Boolean(group && !group.clicked_at);
  if (favorited && wasUnread) {
    group.clicked_at = new Date().toISOString();
    // 同步分组已读索引：收藏页据此立即显示已读样式，不等 15 秒轮询
    state.clickStates[group.id] = group.clicked_at;
  }
  if (favorited) {
    state.favIndex[group.id] = collectionId;
    state.favorites = [
      {
        group_id: group.id,
        collection_id: collectionId,
        title: group.title,
        url: group.url,
        account: group.account,
        date: group.date,
        created_at: new Date().toISOString(),
      },
      ...state.favorites.filter((row) => row.group_id !== group.id),
    ];
  } else {
    delete state.favIndex[group.id];
    state.favorites = state.favorites.filter((row) => row.group_id !== group.id);
  }
  rerenderViews();
  try {
    const payload = await request("/api/favorites", {
      method: "POST",
      body: JSON.stringify({ group_id: group.id, favorited, collection_id: collectionId }),
    });
    applyServerPayload(payload);
    toast(favorited ? (collectionId == null ? "已收藏，可稍后在收藏页查看" : `已收藏到「${collectionName(collectionId)}」`) : "已取消收藏");
  } catch (error) {
    state.favIndex = backup.favIndex;
    state.favorites = backup.favorites;
    if (favorited && wasUnread) {
      group.clicked_at = null;
    }
    toast(`收藏保存失败：${error.message}`, true);
  } finally {
    rerenderViews();
  }
}

// 文章卡片下方的工具条：收藏（含分组）+ 分组级投递标记（已投递/不投递）
export function favoriteToolbar(group) {
  const toolbar = document.createElement("div");
  toolbar.className = "card-toolbar";
  const starred = Object.prototype.hasOwnProperty.call(state.favIndex, group.id);
  const current = state.favIndex[group.id];

  const star = document.createElement("button");
  star.type = "button";
  const starActive = starred && current == null;
  star.className = `tool-btn star${starActive ? " active" : ""}`;
  star.textContent = starActive ? "★ 已收藏" : "☆ 收藏";
  star.title = starred && current != null ? "已收藏到分组，点击改为未分组收藏" : "收藏（未分组），收藏后从收藏页查看";
  star.addEventListener("click", () => setFavorite(group, null, !starActive));
  toolbar.append(star);

  for (const collection of state.collections) {
    const active = starred && current === collection.id;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tool-btn group${active ? " active" : ""}`;
    button.textContent = active ? `★ ${collection.name}` : `收藏到${collection.name}`;
    button.title = active ? `已收藏到「${collection.name}」，点击取消收藏` : `收藏到「${collection.name}」`;
    button.addEventListener("click", () => setFavorite(group, collection.id, !active));
    toolbar.append(button);
  }

  toolbar.append(appliedButtons(group.id));
  return toolbar;
}

export function renderFavoritesView() {
  const bar = document.querySelector("#fav-group-bar");
  const counts = new Map([[null, 0]]);
  for (const collection of state.collections) counts.set(collection.id, 0);
  for (const row of state.favorites) {
    const key = counts.has(row.collection_id) ? row.collection_id : null;
    counts.set(key, counts.get(key) + 1);
  }
  bar.replaceChildren(
    favChip("全部", state.favorites.length, "all"),
    favChip("未分组", counts.get(null), "none"),
    ...state.collections.map((collection) => favChip(collection.name, counts.get(collection.id), collection.id)),
  );
  renderFavManagePanel();

  const visible = state.favorites.filter((row) => {
    if (state.favFilter === "all") return true;
    if (state.favFilter === "none") return row.collection_id == null;
    return row.collection_id === state.favFilter;
  });
  // 未读在前、已读在后（已读含已投递/不投递标记过的），各自保持收藏时间倒序
  const sorted = [...visible].sort((a, b) =>
    (isReadGroup(a.group_id) ? 1 : 0) - (isReadGroup(b.group_id) ? 1 : 0));
  const list = document.querySelector("#fav-list");
  list.replaceChildren();
  for (const row of sorted) list.append(createFavoriteCard(row));
  document.querySelector("#fav-empty").hidden = visible.length > 0;
}

function favChip(label, count, value) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `chip${state.favFilter === value ? " active" : ""}`;
  chip.append(text("span", "", label));
  if (count) chip.append(text("span", "chip-count", String(count)));
  chip.addEventListener("click", () => {
    // 收藏分组筛选写入 hash（#/favorites?collection=…）
    navigate("favorites", { collection: value === "all" ? "" : String(value) });
  });
  return chip;
}

function renderFavManagePanel() {
  const panel = document.querySelector("#fav-manage-panel");
  panel.hidden = !state.favManageOpen;
  if (!state.favManageOpen) return;
  panel.replaceChildren();

  const createRow = document.createElement("div");
  createRow.className = "manage-row";
  const createInput = document.createElement("input");
  createInput.maxLength = 24;
  createInput.placeholder = "新分组名称（如：成都、西安）";
  const createButton = document.createElement("button");
  createButton.type = "button";
  createButton.className = "secondary-btn";
  createButton.textContent = "创建分组";
  const createError = text("span", "manage-count", "");
  const doCreate = async () => {
    const name = createInput.value.trim();
    if (!name) {
      createError.textContent = "请输入分组名称";
      return;
    }
    try {
      applyServerPayload(await request("/api/collections", {
        method: "POST",
        body: JSON.stringify({ action: "create", name }),
      }));
      createInput.value = "";
      createError.textContent = "";
      rerenderViews();
    } catch (error) {
      createError.textContent = `创建失败：${error.message}`;
    }
  };
  createButton.addEventListener("click", doCreate);
  createInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      doCreate();
    }
  });
  createRow.append(text("span", "manage-label", "新建分组："), createInput, createButton, createError);
  panel.append(createRow);

  for (const collection of state.collections) {
    const count = state.favorites.filter((row) => row.collection_id === collection.id).length;
    const row = document.createElement("div");
    row.className = "manage-row";
    const input = document.createElement("input");
    input.value = collection.name;
    input.maxLength = 24;
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "secondary-btn";
    rename.textContent = "重命名";
    rename.addEventListener("click", async () => {
      const name = input.value.trim();
      if (!name || name === collection.name) return;
      try {
        applyServerPayload(await request("/api/collections", {
          method: "POST",
          body: JSON.stringify({ action: "rename", id: collection.id, name }),
        }));
        rerenderViews();
      } catch (error) {
        toast(`重命名失败：${error.message}`, true);
      }
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "text-button danger";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      if (!window.confirm(`删除分组「${collection.name}」？组内收藏会移到「未分组」，不会被删除。`)) return;
      try {
        applyServerPayload(await request("/api/collections", {
          method: "POST",
          body: JSON.stringify({ action: "delete", id: collection.id }),
        }));
        if (state.favFilter === collection.id) state.favFilter = "all";
        rerenderViews();
      } catch (error) {
        toast(`删除失败：${error.message}`, true);
      }
    });
    row.append(
      text("span", "manage-label", "分组："), input,
      text("span", "manage-count", `${count} 条收藏`), rename, del,
    );
    panel.append(row);
  }
  if (!state.collections.length) {
    panel.append(text("p", "manage-count", "还没有分组。建几个常用分组（比如城市），文章看板上就会出现「收藏到某分组」按钮。"));
  }
}

// 收藏行对应的文章对象：数据已全量分页化，收藏交互只用快照字段构造替身
function rowToGroup(row) {
  return { id: row.group_id, title: row.title, url: row.url, account: row.account, date: row.date };
}

// 报名链接：收藏时快照的 apply_url
function favoriteApplyHref(row) {
  return cleanApplyUrl(row.apply_url || "");
}

// AI 提取信息：收藏时快照的 screen_snapshot（含单位/摘要/岗位列表）
function favoriteScreenInfo(row) {
  if (row.screen_snapshot && Object.keys(row.screen_snapshot).length) return row.screen_snapshot;
  return null;
}

// 分组级投递标记：与 group_states.applied 对应，1 已投递 / 2 不投递，互斥；
// 挂在文章分组上（无需先收藏），看板卡片与收藏卡片都能标记
const APPLIED_STATUS = { applied: 1, skipped: 2 };

// 「已投递 / 不投递」标记：点击即保存并转为高亮，再点同一下取消；不打开表单也不跳转
async function toggleApplied(groupId, target) {
  const current = state.appliedIndex[groupId] || 0;
  const next = current === APPLIED_STATUS[target] ? 0 : APPLIED_STATUS[target];
  const previous = current;
  if (next) state.appliedIndex[groupId] = next;
  else delete state.appliedIndex[groupId];
  // 标记 = 已做决定：顺带整组标已读，未读列表立即消失（服务端同请求落已读；
  // 若请求失败，下一次轮询会把状态纠正回来）
  let markedRead = false;
  if (next && !state.clickStates[groupId]) {
    const stamp = new Date().toISOString();
    state.clickStates[groupId] = stamp;
    for (const item of state.board.items) if (item.id === groupId) item.clicked_at = stamp;
    for (const item of state.fairs.items) if (item.id === groupId) item.clicked_at = stamp;
    markedRead = true;
  }
  rerenderViews();
  try {
    const payload = await request("/api/groups/applied", {
      method: "POST",
      body: JSON.stringify({
        group_id: groupId,
        status: next === APPLIED_STATUS.applied ? "applied" : next === APPLIED_STATUS.skipped ? "skipped" : "none",
      }),
    });
    applyServerPayload(payload);
  } catch (error) {
    if (previous) state.appliedIndex[groupId] = previous;
    else delete state.appliedIndex[groupId];
    toast(`投递标记保存失败：${error.message}`, true);
  } finally {
    rerenderViews();
    if (markedRead) scheduleBoardRefresh();
  }
}

export function appliedButtons(groupId) {
  const wrap = document.createDocumentFragment();
  const applied = document.createElement("button");
  applied.type = "button";
  const appliedOn = state.appliedIndex[groupId] === APPLIED_STATUS.applied;
  applied.className = `tool-btn applied${appliedOn ? " active" : ""}`;
  applied.textContent = appliedOn ? "✓ 已投递" : "已投递";
  applied.title = appliedOn ? "点击取消「已投递」标记" : "标记为已投递，点击即保存";
  applied.addEventListener("click", () => toggleApplied(groupId, "applied"));
  const skipped = document.createElement("button");
  skipped.type = "button";
  const skippedOn = state.appliedIndex[groupId] === APPLIED_STATUS.skipped;
  skipped.className = `tool-btn skipped${skippedOn ? " active" : ""}`;
  skipped.textContent = skippedOn ? "✗ 不投递" : "不投递";
  skipped.title = skippedOn ? "点击取消「不投递」标记" : "标记为不投递，不再看这个机会";
  skipped.addEventListener("click", () => toggleApplied(groupId, "skipped"));
  wrap.append(applied, skipped);
  return wrap;
}

function createFavoriteCard(row) {
  const card = document.createElement("article");
  card.className = `fav-card${isReadGroup(row.group_id) ? " is-read" : ""}`;
  const head = document.createElement("div");
  head.className = "fav-head";
  const link = document.createElement("a");
  link.className = "fav-title";
  link.href = row.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = row.title;
  // 收藏页点开文章同样记已读：先写分组状态再调 updateClick，
  // 让它的重渲染立即把卡片变暗（与看板语义一致；失败时由下一次轮询纠正）
  link.addEventListener("click", () => {
    if (isReadGroup(row.group_id)) return;
    const group = rowToGroup(row);
    group.clicked_at = null;
    state.clickStates[row.group_id] = new Date().toISOString();
    updateClick(group, true, true);
  });
  head.append(link, text("span", `fav-pill${row.collection_id == null ? " none" : ""}`, collectionName(row.collection_id)));
  const appliedState = state.appliedIndex[row.group_id];
  if (appliedState === APPLIED_STATUS.applied) head.append(text("span", "fav-pill applied", "已投递"));
  if (appliedState === APPLIED_STATUS.skipped) head.append(text("span", "fav-pill skipped", "不投递"));
  card.append(head);
  card.append(text("p", "fav-meta", [
    row.account, row.date, row.created_at ? `收藏于 ${row.created_at.slice(0, 10)}` : "",
  ].filter(Boolean).join(" · ")));

  // 收藏时快照的 AI 提取信息：文章移出看板后，收藏页仍能看到岗位列表与摘要
  const screenInfo = favoriteScreenInfo(row);
  if (screenInfo && screenInfo.kind !== "skipped") {
    card.append(screenPositions(screenInfo, { noApply: true }));
  }

  const toolbar = document.createElement("div");
  toolbar.className = "card-toolbar";
  // 只展示当前所在分组的按钮，其他分组不出现；点它即取消收藏
  const collection = state.collections.find((item) => item.id === row.collection_id);
  if (collection) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tool-btn group active";
    button.textContent = `★ ${collection.name}`;
    button.title = "已收藏到该分组，点击取消收藏";
    button.addEventListener("click", () => setFavorite(rowToGroup(row), collection.id, false));
    toolbar.append(button);
  }
  const applyHref = favoriteApplyHref(row);
  if (applyHref) {
    const apply = document.createElement("a");
    apply.className = "tool-btn apply";
    apply.href = applyHref;
    apply.target = "_blank";
    apply.rel = "noopener";
    apply.textContent = applyHref.startsWith("mailto:") ? "邮件报名" : "网申 / 报名";
    apply.title = "打开 AI 筛选识别出的报名链接";
    toolbar.append(apply);
  }
  toolbar.append(appliedButtons(row.group_id));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "tool-btn";
  remove.textContent = "取消收藏";
  remove.addEventListener("click", () => setFavorite(rowToGroup(row), null, false));
  toolbar.append(remove);
  card.append(toolbar);
  return card;
}

document.querySelector("#fav-manage-toggle").addEventListener("click", () => {
  state.favManageOpen = !state.favManageOpen;
  document.querySelector("#fav-manage-toggle").textContent = state.favManageOpen ? "收起管理" : "管理分组";
  renderFavoritesView();
});

registerView("favorites", renderFavoritesView);
