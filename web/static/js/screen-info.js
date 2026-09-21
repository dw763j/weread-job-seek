// AI 筛选结构化信息（岗位列表/地点/报名链接）的卡片渲染，文章看板与收藏页共用。
import { text } from "./dom.js";
import { cleanApplyUrl } from "./format.js";
import { state } from "./state.js";

// 与岗位类别 chip 的计算机类高亮同一套判定
const CS_CATEGORY_RE = /计算机|软件|人工智能|大数据|算法|网络安全|开发|IT/;

export function locationBadges(locationText) {
  const wrap = document.createElement("span");
  wrap.className = "pos-locs";
  const raw = (locationText || "").trim();
  if (!raw || /未标注|待定|不详|^无$/.test(raw)) {
    wrap.append(text("span", "loc missing", "未标注地点"));
    return wrap;
  }
  const tokens = raw.split(/[、，,;；\/\s]+/).filter(Boolean);
  for (const token of tokens) {
    const hit = state.prefs.cities.some((city) => token.includes(city));
    wrap.append(text("span", hit ? "loc hot" : "loc", token));
  }
  return wrap;
}

export function screenPositions(entry, options = {}) {
  const block = document.createElement("div");
  block.className = "screen-info";
  if (entry.intro || entry.recruit_target) {
    const intro = document.createElement("p");
    intro.className = "screen-intro";
    const parts = [];
    if (entry.intro) parts.push(entry.intro);
    if (entry.recruit_target) parts.push(`招聘对象：${entry.recruit_target}`);
    intro.textContent = parts.join(" · ");
    block.append(intro);
  }
  const positions = entry.positions || [];
  if (positions.length) {
    const list = document.createElement("ul");
    list.className = "screen-positions";
    for (const position of positions) {
      const row = document.createElement("li");
      row.append(text("span", "pos-name", position.name));
      if (position.category) {
        row.append(text("span", `pos-cat${CS_CATEGORY_RE.test(position.category) ? " cs" : ""}`, position.category));
      }
      row.append(locationBadges(position.location));
      list.append(row);
    }
    block.append(list);
  }
  // options.noApply：调用方已自行渲染报名链接（如收藏卡片）时跳过
  if (!options.noApply) {
    const applyHref = cleanApplyUrl(entry.apply_url);
    if (applyHref) {
      const apply = document.createElement("a");
      apply.className = "apply-button";
      apply.href = applyHref;
      apply.target = "_blank";
      apply.rel = "noopener";
      apply.textContent = applyHref.startsWith("mailto:") ? "邮件报名" : "网申 / 报名";
      block.append(apply);
    }
  }
  if (entry.note) block.append(text("p", "screen-note", entry.note));
  return block;
}
