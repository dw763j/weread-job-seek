// 全局常量与页面状态（唯一的可变状态源，各视图模块共享）
export const DEFAULT_CITIES = ["成都", "西安", "北京", "上海", "深圳", "东莞", "广州", "杭州"];
export const PAGE_SIZE = 40;

// 投递进度：按推进顺序排列的阶段 + 终止状态；「笔试」可重复推进，轮次由历史推算。
export const APP_STAGES = [
  { key: "planned", label: "待投递" },
  { key: "applied", label: "已投递" },
  { key: "test", label: "笔试" },
  { key: "interview1", label: "一面" },
  { key: "interview2", label: "二面" },
  { key: "interview3", label: "三面" },
  { key: "final", label: "终面/HR面" },
  { key: "offer", label: "已录用" },
];
export const APP_CLOSED = [
  { key: "rejected", label: "未通过" },
  { key: "withdrawn", label: "已放弃" },
];
export const APP_STATUS_MAP = Object.fromEntries([...APP_STAGES, ...APP_CLOSED].map((item) => [item.key, item.label]));
export const APP_CLOSED_KEYS = new Set(APP_CLOSED.map((item) => item.key));

export const state = {
  groups: [],
  user: null,
  filter: "unread",
  query: "",
  account: "",
  screen: "",
  page: 1,
  prefs: { cities: DEFAULT_CITIES.slice(), keywords: [] },
  pending: new Set(),
  currentView: "articles",
  collections: [],
  favIndex: {},
  favorites: [],
  favFilter: "all",
  favManageOpen: false,
  applications: [],
  appliedGroupIds: new Set(),
  appFilter: "all",
  appQuery: "",
  appFormStatus: "applied",
  appFormArticle: null,
  appReturnView: null,
};
