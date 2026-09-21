// 宣讲会识别与时间/省份解析的纯函数（无 DOM 依赖）。
// 文章看板用 isFair 排除宣讲条目、宣讲会页用它组织展示；两处共用同一套兜底规则，
// 与筛选脚本 screen_update.py 的 FAIR_TITLE_RE 保持一致。
// 省份/城市/公众号映射不再在本地复制：由 bootstrap 的 geo 字段下发，
// loadApp 时通过 configureGeo 注入（单一事实源在服务端 server_common.py）。
export const FAIR_TITLE_RE = /宣讲|双选|组团|线下招聘|招聘会/;
// 面向用人单位/企业的参会邀请函（邀请企业设摊报名）不是给求职者看的，标题兜底时排除；
// 这类文章由 AI 筛选按"面向用人单位的邀请函"整体跳过
const EMPLOYER_INVITE_RE = /用人单位邀请|致用人单位|企业邀请函|诚邀(各)?(用人)?单位|参会单位邀请|单位邀请函|诚邀企业/;
// 多企业的标题兜底（仅在 AI 没给出 fair 字段时使用）：裸的"宣讲+双选"是单公司
// 常见形式（如人才日），不算多企业
const MULTI_COMPANY_TITLE_RE = /组团|联合|多企业|多单位|双选会|大型招聘会|巡回/;

let provincesList = [];
let cityProvinces = {};
let accountProvinces = {};

export function configureGeo(geo) {
  if (!geo) return;
  if (Array.isArray(geo.provinces)) provincesList = geo.provinces;
  if (geo.city_provinces) cityProvinces = geo.city_provinces;
  if (geo.account_provinces) accountProvinces = geo.account_provinces;
}

export function isFair(group) {
  if (group.screen?.fair?.is_fair) return true;
  // v2 结果已被 AI 判为非招聘信息（含面向用人单位的邀请函）→ 不再按标题兜底
  if (group.screen?.kind === "skipped" && group.screen?.v === 2) return false;
  return allTitles(group).some((title) => FAIR_TITLE_RE.test(title) && !EMPLOYER_INVITE_RE.test(title));
}

function allTitles(group) {
  return [group.title, ...(group.members || []).map((member) => member.title)];
}

// 从时间文本里解析 ISO 日期；年份缺省取发布年，"年末发的年初活动"进一年
export function parseFairDate(raw, publishDate) {
  if (!raw) return "";
  let match = raw.match(/(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})/);
  let year = 0;
  let month = 0;
  let day = 0;
  let yearInferred = false;
  if (match) {
    [year, month, day] = [+match[1], +match[2], +match[3]];
  } else {
    match = raw.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*日?/) || raw.match(/(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?!\d)/);
    if (!match) return "";
    [month, day] = [+match[1], +match[2]];
    year = Number.parseInt((publishDate || "").slice(0, 4), 10);
    if (!year) return "";
    yearInferred = true;
  }
  if (!(month >= 1 && month <= 12 && day >= 1 && day <= 31)) return "";
  const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const check = new Date(`${iso}T00:00:00+08:00`);
  if (Number.isNaN(check.getTime())) return "";
  // 跨年推断只认"年末发的年初活动"（如 12 月发的 1月5日 → 明年）；
  // 发布前的开场日期（9月8日发的"9月7日起"系列）保持当年，避免被误推到明年
  if (yearInferred && publishDate) {
    const pubMonth = Number.parseInt((publishDate || "").slice(5, 7), 10);
    if (pubMonth - month >= 8) return `${year + 1}-${iso.slice(5)}`;
  }
  return iso;
}

// 省份推断：AI 提取的 province → 地点/时间文本扫省份 → 扫城市 → 公众号所属高校兜底
function inferProvince(group, fair) {
  if (fair.province) return fair.province;
  const texts = [fair.location || "", fair.time || ""];
  for (const text of texts) {
    const hit = provincesList.find((province) => text.includes(province));
    if (hit) return hit;
  }
  for (const text of texts) {
    for (const [city, province] of Object.entries(cityProvinces)) {
      if (text.includes(city)) return province;
    }
  }
  const accounts = [group.account, ...(group.members || []).map((member) => member.account)];
  for (const account of accounts) {
    if (account in accountProvinces) return accountProvinces[account];
  }
  return "";
}

// 汇总一个宣讲条目的展示信息：AI 提取优先，标题兜底（覆盖未筛选/旧数据）
export function fairInfo(group) {
  const fair = group.screen?.fair || {};
  const titles = allTitles(group);
  let date = fair.date || "";
  let dateFromTitle = false;
  if (!date) {
    for (const title of titles) {
      date = parseFairDate(title, group.date);
      if (date) {
        dateFromTitle = true;
        break;
      }
    }
  }
  return {
    date,
    dateFromTitle,
    time: fair.time || "",
    location: fair.location || "",
    province: inferProvince(group, fair),
    // AI 已给出 fair 字段时以 AI 判定为准；只有旧数据/未筛选才用标题关键词兜底
    multiCompany: fair.multi_company ?? titles.some((t) => MULTI_COMPANY_TITLE_RE.test(t)),
  };
}
