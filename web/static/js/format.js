// 展示格式化与文本清洗工具（无状态，纯函数）

export function formatRange(range) {
  if (!range?.start || !range?.end) return "最新招聘机会";
  const date = (value) => `${value.slice(0, 4)}.${value.slice(4, 6)}.${value.slice(6, 8)}`;
  return `${date(range.start)} — ${date(range.end)}`;
}

export function chinaToday() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function formatUpdatedAt(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

// 投递时间线用的短日期：ISO 时间串 → "MM/DD"
export function shortDate(value) {
  return value && value.length >= 10 ? value.slice(5, 10).replace("-", "/") : "";
}

// GLM 的“报名方式”可能混着标签文字、多个链接和邮箱；取纯 ASCII 链接里路径
// 最长的一个，没有链接时把邮箱转 mailto，避免浏览器把整段文字当相对路径打开。
export function cleanApplyUrl(raw) {
  if (!raw) return "";
  const candidates = (raw.match(/https?:\/\/[!-~]+/g) || [])
    .map((item) => item.replace(/[.,;:]+$/, ""))
    .filter((item) => { try { new URL(item); return true; } catch { return false; } });
  if (candidates.length) {
    const score = (url) => {
      const parsed = new URL(url);
      return parsed.pathname.length + parsed.search.length;
    };
    return candidates.reduce((best, item) => (score(item) > score(best) ? item : best));
  }
  const email = raw.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  return email ? `mailto:${email[0]}` : "";
}

// 对象键顺序不敏感的 JSON 比较，用于判断远端数据是否真的变了、避免无谓重渲染
export function stableStringify(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return JSON.stringify(value);
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
}
