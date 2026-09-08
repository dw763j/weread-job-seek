// 收藏与收藏分组：文章卡工具条、收藏列表页与分组管理
import { request } from "./api.js";
import { text } from "./dom.js";
import { cleanApplyUrl } from "./format.js";
import { state } from "./state.js";
import { companyFromArticle, openApplicationForm, prefillFromArticle } from "./applications.js";
import { registerView, rerenderViews, toast } from "./views.js";

function collectionName(id) {
  if (id == null) return "未分组";
  return state.collections.find((collection) => collection.id === id)?.name || "未分组";
}

export function applyServerPayload(payload) {
  if (payload.collections) state.collections = payload.collections;
  if (payload.favorites) state.favIndex = payload.favorites;
  if (payload.items) state.favorites = payload.items;
}

export async function loadFavorites() {
  applyServerPayload(await request("/api/favorites"));
}

export async function setFavorite(group, collectionId, favorited) {
  const backup = { favIndex: { ...state.favIndex }, favorites: state.favorites };
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
    toast(favorited ? (collectionId == null ? "已收藏" : `已收藏到「${collectionName(collectionId)}」`) : "已取消收藏");
  } catch (error) {
    state.favIndex = backup.favIndex;
    state.favorites = backup.favorites;
    toast(`收藏保存失败：${error.message}`, true);
  } finally {
    rerenderViews();
  }
}

// 文章卡片下方的收藏/投递工具条：有分组时为每个分组展示一个「收藏到xx组」按钮
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
  star.title = starred && current != null ? "已收藏到分组，点击改为未分组收藏" : "收藏（未分组）";
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

  const applied = state.appliedGroupIds.has(group.id);
  const apply = document.createElement("button");
  apply.type = "button";
  apply.className = "tool-btn apply";
  apply.textContent = applied ? "已记录投递" : "记投递";
  apply.title = "把这篇招聘对应的公司加入「投递管理」";
  apply.disabled = applied;
  apply.addEventListener("click", () => prefillFromArticle(group));
  toolbar.append(apply);
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
  const list = document.querySelector("#fav-list");
  list.replaceChildren();
  for (const row of visible) list.append(createFavoriteCard(row));
  document.querySelector("#fav-empty").hidden = visible.length > 0;
}

function favChip(label, count, value) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `chip${state.favFilter === value ? " active" : ""}`;
  chip.append(text("span", "", label));
  if (count) chip.append(text("span", "chip-count", String(count)));
  chip.addEventListener("click", () => {
    state.favFilter = value;
    renderFavoritesView();
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

// 收藏行可能对应的文章已不在当前看板数据里，构造一个仅含快照字段的替身供交互使用
function rowToGroup(row) {
  const live = state.groups.find((group) => group.id === row.group_id);
  return live || { id: row.group_id, title: row.title, url: row.url, account: row.account, date: row.date, screen: null };
}

function createFavoriteCard(row) {
  const card = document.createElement("article");
  card.className = "fav-card";
  const head = document.createElement("div");
  head.className = "fav-head";
  const link = document.createElement("a");
  link.className = "fav-title";
  link.href = row.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = row.title;
  head.append(link, text("span", `fav-pill${row.collection_id == null ? " none" : ""}`, collectionName(row.collection_id)));
  card.append(head);
  card.append(text("p", "fav-meta", [
    row.account, row.date, row.created_at ? `收藏于 ${row.created_at.slice(0, 10)}` : "",
  ].filter(Boolean).join(" · ")));

  const toolbar = document.createElement("div");
  toolbar.className = "card-toolbar";
  for (const collection of state.collections) {
    const active = row.collection_id === collection.id;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tool-btn group${active ? " active" : ""}`;
    button.textContent = active ? `★ ${collection.name}` : collection.name;
    button.title = active ? "已收藏到该分组，点击取消收藏" : `移入「${collection.name}」`;
    button.addEventListener("click", () => setFavorite(rowToGroup(row), collection.id, !active));
    toolbar.append(button);
  }
  if (state.collections.length) {
    const ungrouped = document.createElement("button");
    ungrouped.type = "button";
    ungrouped.className = "tool-btn star";
    ungrouped.textContent = "未分组";
    ungrouped.title = "移到未分组";
    ungrouped.addEventListener("click", () => setFavorite(rowToGroup(row), null, true));
    toolbar.append(ungrouped);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "tool-btn";
  remove.textContent = "取消收藏";
  remove.addEventListener("click", () => setFavorite(rowToGroup(row), null, false));
  toolbar.append(remove);
  const toApply = document.createElement("button");
  toApply.type = "button";
  toApply.className = "tool-btn apply";
  toApply.textContent = "转为投递";
  toApply.title = "把这篇招聘对应的公司加入「投递管理」";
  toApply.addEventListener("click", () => prefillFromFavorite(row));
  toolbar.append(toApply);
  card.append(toolbar);
  return card;
}

function prefillFromFavorite(row) {
  const live = state.groups.find((group) => group.id === row.group_id);
  openApplicationForm({
    company: companyFromArticle(live || { title: row.title }),
    job_url: cleanApplyUrl(live?.screen?.apply_url) || "",
    article_group_id: row.group_id,
    article_url: row.url,
    article_title: row.title,
    status: "applied",
  }, "favorites");
}

document.querySelector("#fav-manage-toggle").addEventListener("click", () => {
  state.favManageOpen = !state.favManageOpen;
  document.querySelector("#fav-manage-toggle").textContent = state.favManageOpen ? "收起管理" : "管理分组";
  renderFavoritesView();
});

registerView("favorites", renderFavoritesView);
