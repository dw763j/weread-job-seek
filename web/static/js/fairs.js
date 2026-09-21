// 宣讲会视图：宣讲类文章（线下宣讲/招聘会/双选会/组团招聘）单独一页展示。
// 数据来自 /api/fairs（整包，量小）；日期/省份/已结束状态编码在 hash 里
// （#/fairs?day=…&provinces=…&past=…），回退/前进/刷新可还原。
// 顶部时间轴按举办日期导航，点哪天加载哪天（默认选今天/最近的将来）；
// 省份做多选筛选；多企业联合的排省内置顶并加徽标。
import { request } from "./api.js";
import { text } from "./dom.js";
import { chinaToday } from "./format.js";
import { favoriteToolbar } from "./favorites.js";
import { updateClick } from "./clicks.js";
import { fairInfo, isFair } from "./fair-core.js";
import { state } from "./state.js";
import { registerView } from "./views.js";
import { navigate } from "./router.js";

const NO_PROVINCE = "未标注省份";
const NO_DATE = "待定";

export async function loadFairsData() {
  const payload = await request("/api/fairs");
  state.fairs = payload;
}

function weekdayOf(dateIso) {
  const date = new Date(`${dateIso}T00:00:00+08:00`);
  return new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(date);
}

function shortDateLabel(dateIso) {
  return `${dateIso.slice(5, 7).replace(/^0/, "")}/${dateIso.slice(8, 10).replace(/^0/, "")}`;
}

// ---------- 卡片与分组 ----------

function createFairCard(group, info) {
  const card = document.createElement("article");
  card.className = `fair-card${group.clicked_at ? " is-read" : ""}`;
  card.dataset.groupId = group.id;

  const marker = document.createElement("button");
  marker.type = "button";
  marker.className = "read-marker";
  marker.setAttribute("aria-label", group.clicked_at ? "标记为未读" : "标记为已读");
  marker.title = group.clicked_at ? "标记为未读" : "标记为已读";
  marker.addEventListener("click", () => updateClick(group, !group.clicked_at));

  const content = document.createElement("div");
  content.className = "fair-content";
  const meta = document.createElement("div");
  meta.className = "article-meta";
  meta.append(text("span", "account-pill", group.account));
  if (info.multiCompany) meta.append(text("span", "multi-badge", "多企业联合"));
  if (info.dateFromTitle) meta.append(text("span", "screen-pill skipped", "时间取自标题"));
  if (group.members.length > 1) meta.append(text("span", "duplicate-pill", `${group.members.length} 个来源`));

  const link = document.createElement("a");
  link.className = "fair-title";
  link.href = group.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = group.title;
  link.addEventListener("click", () => {
    if (!group.clicked_at) updateClick(group, true, true);
  });
  content.append(meta, link);

  const detail = document.createElement("p");
  detail.className = "fair-detail";
  const parts = [];
  if (info.time) parts.push(`时间：${info.time}`);
  else if (info.date && info.date !== NO_DATE) parts.push(`时间：${info.date}`);
  if (info.location) parts.push(`地点：${info.location}`);
  detail.textContent = parts.join(" · ");
  if (parts.length) content.append(detail);

  content.append(favoriteToolbar(group));
  card.append(marker, content);
  return card;
}

// 省内排序：多企业联合在前，其后按发布日期新→旧
function compareEntries(a, b) {
  if (a.info.multiCompany !== b.info.multiCompany) return a.info.multiCompany ? -1 : 1;
  return (b.group.date || "").localeCompare(a.group.date || "");
}

// 省份分组顺序：场次数多→少，未标注省份排最后
function groupByProvince(entries) {
  const byProvince = new Map();
  for (const entry of entries) {
    const key = entry.info.province || NO_PROVINCE;
    if (!byProvince.has(key)) byProvince.set(key, []);
    byProvince.get(key).push(entry);
  }
  return [...byProvince.entries()].sort((a, b) => {
    if (a[0] === NO_PROVINCE) return 1;
    if (b[0] === NO_PROVINCE) return -1;
    return b[1].length - a[1].length || a[0].localeCompare(b[0], "zh-CN");
  });
}

function createDaySection(key, entries) {
  const section = document.createElement("section");
  section.className = "fair-day";
  if (key !== NO_DATE) section.id = `fair-day-${key}`;
  const heading = document.createElement("div");
  heading.className = "date-heading";
  const label = key === NO_DATE ? "时间待定" : `${Number(key.slice(5, 7))} 月 ${Number(key.slice(8, 10))} 日`;
  heading.append(text("h3", "", label));
  const tags = [];
  if (key !== NO_DATE) tags.push(weekdayOf(key));
  const today = chinaToday();
  if (key === today) tags.push("今天");
  if (key !== NO_DATE && key < today) tags.push("已结束");
  if (tags.length) heading.append(text("span", "", tags.join(" · ")));
  section.append(heading);

  for (const [province, items] of groupByProvince(entries)) {
    const group = document.createElement("div");
    group.className = "fair-province";
    const head = document.createElement("div");
    head.className = "fair-province-head";
    head.append(
      text("span", "fair-province-name", province),
      text("span", "fair-province-count", `${items.length} 场`),
    );
    group.append(head);
    items.sort(compareEntries);
    for (const item of items) group.append(createFairCard(item.group, item.info));
    section.append(group);
  }
  return section;
}

// ---------- 省份筛选（多选，空集 = 全部） ----------

function renderProvinceFilter(counts) {
  const bar = document.querySelector("#fair-province-filter");
  const selected = state.fairProvinces;
  const all = document.createElement("button");
  all.type = "button";
  all.className = `chip${selected.size ? "" : " active"}`;
  all.append(text("span", "", "全部"));
  all.append(text("span", "chip-count", String([...counts.values()].reduce((sum, n) => sum + n, 0))));
  all.addEventListener("click", () => {
    state.fairProvinces.clear();
    navigate("fairs", { day: state.fairSelectedDay, provinces: state.fairProvinces, past: state.fairShowPast ? "1" : "" });
  });
  bar.replaceChildren(all);
  for (const [province, count] of [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN"))) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${selected.has(province) ? " active" : ""}`;
    chip.append(text("span", "", province), text("span", "chip-count", String(count)));
    chip.addEventListener("click", () => {
      if (selected.has(province)) selected.delete(province);
      else selected.add(province);
      navigate("fairs", { day: state.fairSelectedDay, provinces: state.fairProvinces, past: state.fairShowPast ? "1" : "" });
    });
    bar.append(chip);
  }
}

// ---------- 视图渲染 ----------

// 默认选中：今天 → 最近的将来 → 时间待定；全都不行时取最后一天（已开启显示已结束）
function pickDefaultDay(dayKeys, undatedCount, today) {
  if (dayKeys.includes(today)) return today;
  const future = dayKeys.filter((key) => key > today);
  if (future.length) return future[0];
  if (undatedCount) return NO_DATE;
  return dayKeys[dayKeys.length - 1] || NO_DATE;
}

function fairEntries() {
  return state.fairs.items.filter(isFair).map((group) => ({ group, info: fairInfo(group) }));
}

export function renderFairsView() {
  const today = chinaToday();
  const entries = fairEntries();

  const provinceCounts = new Map();
  for (const entry of entries) {
    const key = entry.info.province || NO_PROVINCE;
    provinceCounts.set(key, (provinceCounts.get(key) || 0) + 1);
  }
  renderProvinceFilter(provinceCounts);

  const selectedProvinces = state.fairProvinces;
  const visible = selectedProvinces.size
    ? entries.filter((entry) => selectedProvinces.has(entry.info.province || NO_PROVINCE))
    : entries;
  const dated = visible.filter((entry) => entry.info.date);
  const undated = visible.filter((entry) => !entry.info.date);
  const upcoming = dated.filter((entry) => entry.info.date >= today).sort((a, b) => a.info.date.localeCompare(b.info.date));
  const past = dated.filter((entry) => entry.info.date < today).sort((a, b) => a.info.date.localeCompare(b.info.date));

  const dayEntries = new Map();
  for (const entry of [...upcoming, ...(state.fairShowPast ? past : [])]) {
    if (!dayEntries.has(entry.info.date)) dayEntries.set(entry.info.date, []);
    dayEntries.get(entry.info.date).push(entry);
  }

  // 当前选中日失效（不在时间轴上 / 待定被筛空）时重新挑默认日
  if (state.fairSelectedDay !== NO_DATE && !dayEntries.has(state.fairSelectedDay)) {
    state.fairSelectedDay = "";
  }
  if (state.fairSelectedDay === NO_DATE && !undated.length) state.fairSelectedDay = "";
  if (!state.fairSelectedDay) {
    state.fairSelectedDay = pickDefaultDay([...dayEntries.keys()], undated.length, today);
  }

  const summary = document.querySelector("#fair-summary");
  const unreadCount = visible.filter((entry) => !entry.group.clicked_at).length;
  summary.textContent = visible.length
    ? `即将举行 ${upcoming.length} 场 · 未读 ${unreadCount}${undated.length ? ` · 时间待定 ${undated.length}` : ""}`
    : "";

  const pastToggle = document.querySelector("#fair-past-toggle");
  pastToggle.textContent = state.fairShowPast ? "隐藏已结束" : `显示已结束（${past.length}）`;
  pastToggle.hidden = !past.length;

  // 时间轴：只列有场次的日期，点击切换当天内容；场次数带未读/已读明细
  const timeline = document.querySelector("#fair-timeline");
  timeline.replaceChildren();
  const countLabel = (items) => {
    const unread = items.filter((entry) => !entry.group.clicked_at).length;
    return `未读 ${unread} · 已读 ${items.length - unread} · 共 ${items.length}`;
  };
  for (const [key, items] of dayEntries) {
    const chip = document.createElement("button");
    chip.type = "button";
    const active = key === state.fairSelectedDay;
    chip.className = `fair-tl-chip${active ? " active" : ""}${key < today ? " past" : ""}${key === today ? " today" : ""}`;
    chip.append(text("span", "fair-tl-date", `${shortDateLabel(key)} ${weekdayOf(key)}`));
    chip.append(text("span", "fair-tl-count", countLabel(items)));
    chip.addEventListener("click", () => {
      navigate("fairs", { day: key, provinces: state.fairProvinces, past: state.fairShowPast ? "1" : "" });
    });
    timeline.append(chip);
  }
  if (undated.length) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `fair-tl-chip undated${state.fairSelectedDay === NO_DATE ? " active" : ""}`;
    chip.append(text("span", "fair-tl-date", `时间待定`));
    chip.append(text("span", "fair-tl-count", countLabel(undated)));
    chip.addEventListener("click", () => {
      navigate("fairs", { day: NO_DATE, provinces: state.fairProvinces, past: state.fairShowPast ? "1" : "" });
    });
    timeline.append(chip);
  }
  timeline.hidden = !dayEntries.size && !undated.length;

  // 只渲染选中那一天的内容
  const list = document.querySelector("#fair-list");
  list.replaceChildren();
  if (state.fairSelectedDay === NO_DATE) {
    if (undated.length) list.append(createDaySection(NO_DATE, undated));
  } else if (dayEntries.has(state.fairSelectedDay)) {
    list.append(createDaySection(state.fairSelectedDay, dayEntries.get(state.fairSelectedDay)));
  }
  document.querySelector("#fair-empty").hidden = entries.length > 0;
}

document.querySelector("#fair-past-toggle").addEventListener("click", () => {
  navigate("fairs", {
    day: state.fairSelectedDay,
    provinces: state.fairProvinces,
    past: state.fairShowPast ? "" : "1",
  });
});

registerView("fairs", renderFairsView);
