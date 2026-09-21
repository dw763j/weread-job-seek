// 全局常量与页面状态（唯一的可变状态源，各视图模块共享）
export const DEFAULT_CITIES = ["成都", "西安", "北京", "上海", "深圳", "东莞", "广州", "杭州"];
export const PAGE_SIZE = 40;

export const state = {
  user: null,
  // 路由驱动的看板筛选（#/articles?status=…&account=…&screens=…&q=…&page=…）
  currentView: "articles",
  filter: "unread",
  query: "",
  account: "",
  screens: new Set(),
  page: 1,
  // 看板分页数据（/api/articles 按筛选分页拉取，不再全量下发）
  board: { items: [], total: 0, pages: 1, counts: null },
  // 宣讲会数据（/api/fairs 整包，量小；前端按日期/省份聚合）
  fairs: { items: [], total: 0, upcoming: 0 },
  fairSelectedDay: "",
  fairProvinces: new Set(),
  fairShowPast: false,
  // 全局统计（bootstrap 下发、/api/clicks 轮询刷新）
  stats: { total: 0, read: 0, unread: 0, today_read: 0, fair_total: 0, fair_upcoming: 0 },
  accounts: [],
  prefs: { cities: DEFAULT_CITIES.slice(), keywords: [] },
  pending: new Set(),
  // 分组 → 已读时间（/api/clicks 轮询下发，全量分组）：看板页、宣讲会页、
  // 收藏页共用同一份已读状态
  clickStates: {},
  // 本会话"点开查看"过的文章 id：未读列表里保留变暗（exclude 传给服务端），
  // 下次页面刷新清空——与"跳过立即消失"相区分
  sessionViewedIds: new Set(),
  collections: [],
  favIndex: {},
  favorites: [],
  favFilter: "all",
  favManageOpen: false,
  // 分组级投递意向（1 已投递 / 2 不投递），看板卡片与收藏页共用
  appliedIndex: {},
};
