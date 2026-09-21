#!/usr/bin/env node
/**
 * 微信读书公众号全量文章链接提取器
 *
 * 原理：微信读书把公众号收录成 bookId=MP_WXS_<__biz 解码数字>，网页版
 * /web/mp/articles 接口返回文章列表（标题 + 原文短链）。本脚本通过 CDP
 * 驱动一个已登录微信读书的 Chrome 实例，逐号订阅、翻页拉取，输出 CSV/MD。
 *
 * 前置条件：
 *   1. 通过“start_weread_chrome.sh”启动固定 Profile，并登录微信读书；
 *      Chrome 使用 --remote-debugging-port=9223 和项目内私有 .browser-profile。
 *   2. 运行本脚本需要放行 localhost:9223 以及 mp.weixin.qq.com / weread.qq.com 网络。
 *
 * 用法：node weread_extract.mjs [CDP端口=9223]
 *   - 有历史 CSV 的账号默认“增量模式”：从最新开始翻页，整页都是已收录链接即停，
 *     新文章合并进原文件；
 *   - 首次拉取（无 CSV）自动全量；加 --force 可强制全量重拉；
 *   - 抓取深度：增量账号默认只翻最近 14 天（--crawl-days N / WR_CRAWL_DAYS 可调）。
 *     微信读书回填期每页都混有旧文，“整页已知即停”长期不触发，会每天翻穿全号
 *     （约 10 页/号）而频繁触发风控，故默认限窗；--deep 关闭窗口翻穿整个号，
 *     供每周全量深扫补齐回填旧文；显式 --range 仍同时约束抓取与汇总窗口；
 *   - 断点续跑：账号中途触发风控或接口异常时，已翻到的链接仍会先合并进该号 CSV，
 *     并把“中断深度”（未验证到的 offset）记入进度。下次运行在该深度之内不触发
 *     “整页已知即停”，翻过中断点后缺口自动补齐；若下次是限窗运行且窗口未越过
 *     中断深度，断点保留，待下次深扫补齐（批次仍会整体停止，剩余账号照常跳过）。
 */

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI_ARGS = process.argv.slice(2);
const PORT = CLI_ARGS.find((a) => /^\d+$/.test(a)) || "9223";
const CDP_BASE = `http://127.0.0.1:${PORT}`;
const CONFIG = JSON.parse(
  readFileSync(join(__dirname, "微信读书提取配置.json"), "utf-8"),
);
const OUT_DIR = join(__dirname, "output", "weread_extract");
const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
// 默认采用保守频率，环境变量只用于在确有需要时进一步调慢。
// 不建议调小：连续切换公众号、固定高频翻页会明显增加微信读书风控概率。
const PAGE_INTERVAL_MS = Number(process.env.WR_PAGE_INTERVAL_MS || 6000);
const ACCOUNT_INTERVAL_MS = Number(process.env.WR_ACCOUNT_INTERVAL_MS || 15000);
const READER_READY_WAIT_MS = Number(process.env.WR_READER_READY_WAIT_MS || 10000);
const RETRY_WAIT_MS = 120000; // 风控/空白页时的冷却等待
const MAX_RETRIES = Number(process.env.WR_MAX_RETRIES || 3); // 单账号最多等约 6 分钟（可环境变量调整）
// 翻页深度上限。需大于最大账号的文章总数（目前最多的号约 2600+ 篇），否则深扫永远
// 触不到最老的文章。留足余量防接口异常时的死循环。
const MAX_OFFSET = 5000;
const TZ_OFFSET = 8 * 3600 * 1000;
const PROGRESS_FILE = join(OUT_DIR, "进度.json");
const SUMMARY_CSV = join(OUT_DIR, "汇总-全部公众号.csv");
const SUMMARY_PY = join(__dirname, "generate_summary.py");

class RiskControlError extends Error {
  constructor(message) {
    super(message);
    this.name = "RiskControlError";
    this.code = "WR_RISK_CONTROL";
  }
}

/** 解析 --range YYYYMMDD-YYYYMMDD（单日可只写 YYYYMMDD）。 */
function parseRangeArg(raw) {
  const m = /^(\d{8})(?:-(\d{8}))?$/.exec(String(raw || ""));
  if (!m) throw new Error(`--range 格式应为 YYYYMMDD-YYYYMMDD（如 20260710-20260805），收到：${raw}`);
  const startText = m[1];
  const endText = m[2] || m[1];
  const toTs = (text, hour, min, sec) =>
    new Date(`${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}T${hour}:${min}:${sec}+08:00`).getTime() / 1000;
  return {
    startText,
    endText,
    startTs: toTs(startText, "00", "00", "00"),
    endTs: toTs(endText, "23", "59", "59"),
  };
}

function argValue(name) {
  const idx = CLI_ARGS.indexOf(name);
  return idx >= 0 ? CLI_ARGS[idx + 1] : undefined;
}

const RANGE = (() => {
  const raw = argValue("--range");
  return raw ? parseRangeArg(raw) : null;
})();

// 深扫模式：关闭增量账号的滚动抓取窗口（每周全量补齐回填旧文时使用）。
const DEEP = CLI_ARGS.includes("--deep");
// 增量模式的滚动抓取窗口（天）。只约束抓取深度，不影响汇总窗口。
const CRAWL_DAYS = Number(argValue("--crawl-days") || process.env.WR_CRAWL_DAYS || 14);

/** 滚动抓取窗口：now-N 天 ~ now。 */
function rollingRange(days) {
  const endTs = Math.floor(Date.now() / 1000);
  const startTs = endTs - days * 86400;
  return { startText: fmtDate(startTs), endText: fmtDate(endTs), startTs, endTs };
}

/** 生成汇总：直接用本机 uv 运行 generate_summary.py（Docker 只承载网页服务，不承载汇总）。 */
function runSummary() {
  const rangeArg = RANGE ? `${RANGE.startText}-${RANGE.endText}` : "";
  const rangeArgs = rangeArg ? ["--range", rangeArg] : [];

  // 用 uv 统一管理 Python 版本和依赖（可通过 UV_BIN 指定 uv 路径）。
  // 缓存目录固定到项目内，避免依赖 $HOME/.cache（sudo/守护环境下易权限错乱）。
  const uvBin = process.env.UV_BIN || "uv";
  console.log("生成汇总：运行 generate_summary.py…");
  const pyResult = spawnSync(uvBin, ["run", "python", SUMMARY_PY, ...rangeArgs], {
    // 必须从项目根目录运行，确保 uv 读取当前项目的 pyproject.toml / uv.lock。
    cwd: __dirname,
    env: { ...process.env, UV_CACHE_DIR: join(__dirname, ".uv-cache") },
    // 汇总耗时数分钟，实时透传子进程输出，避免缓冲到退出才打印、看起来像卡死。
    stdio: ["ignore", "inherit", "inherit"],
  });
  if (pyResult.error) {
    console.error("调用 generate_summary.py 失败：", pyResult.error.message);
  } else if (pyResult.status !== 0) {
    console.error(`generate_summary.py 退出码 ${pyResult.status}`);
  }
}

function fmtDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000 + TZ_OFFSET);
  return d.toISOString().slice(0, 10);
}

function stableSort(items) {
  return items.sort((a, b) => (b.t || 0) - (a.t || 0));
}

/* ---------- CDP 最小客户端 ---------- */
async function listTabs() {
  return (await fetch(`${CDP_BASE}/json/list`)).json();
}

async function newTab(url) {
  const res = await fetch(`${CDP_BASE}/json/new?${encodeURIComponent(url)}`, {
    method: "PUT",
  });
  return res.json();
}

async function evaluate(wsUrl, expression, timeoutMs = 60000) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("CDP websocket error"));
  });
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("evaluate timeout")), timeoutMs);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id === 1) {
        clearTimeout(timer);
        resolve(msg);
      }
    };
    ws.send(
      JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: { expression, returnByValue: true, awaitPromise: true },
      }),
    );
  });
  ws.close();
  if (result.error) throw new Error(JSON.stringify(result.error));
  if (result.result?.exceptionDetails) {
    throw new Error(JSON.stringify(result.result.exceptionDetails));
  }
  return result.result?.result?.value;
}

async function findWereadTab() {
  const tabs = await listTabs();
  return (
    tabs.find((t) => t.url.includes("/web/mp/reader/")) ||
    tabs.find((t) => t.url.includes("/web/reader/")) ||
    tabs.find((t) => t.url.includes("weread.qq.com")) ||
    tabs.find((t) => t.url.startsWith("http"))
  );
}

async function ensureWereadTab() {
  let tab = await findWereadTab();
  if (!tab) {
    tab = await newTab("https://weread.qq.com/");
    await new Promise((r) => setTimeout(r, 3000));
  }
  return tab;
}

async function pageProbe(wsUrl) {
  const p = await evaluate(
    wsUrl,
    `(() => {
      const txt = (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim();
      const capSel = '[id*="captcha"], [class*="tcaptcha"], iframe[src*="captcha"]';
      const capVisible = [].slice.call(document.querySelectorAll(capSel)).some(n => {
        for (let cur = n; cur; cur = cur.parentElement) {
          const s = getComputedStyle(cur);
          if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
        }
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      const capText = /验证|验证码|安全验证|captcha|滑块|点选|完成验证/i.test(txt);
      return { path: location.pathname, title: document.title, len: txt.length,
               capVisible, capText, capArming: typeof window.TencentCaptcha === "function" };
    })()`,
  );
  return p;
}

async function evaluateOnPage(wsUrl, expression) {
  return evaluate(wsUrl, expression);
}

/* ---------- 微信读书业务 ---------- */
async function addToShelf(wsUrl, bookId) {
  return evaluateOnPage(
    wsUrl,
    `fetch("/mp/shelf/addToShelf",{method:"POST",credentials:"include",
       headers:{"Content-Type":"application/json;charset=UTF-8"},
       body:JSON.stringify({bookIds:[${JSON.stringify(bookId)}]})})
       .then(r=>r.text())`,
  );
}

async function shelfSync(wsUrl) {
  return evaluateOnPage(
    wsUrl,
    `fetch("/web/shelf/sync?synckey=0&teenmode=0&album=1",{credentials:"include"})
       .then(r=>r.json()).then(o=>JSON.stringify({
         errCode:o.errCode||o.errcode||0,
         errMsg:o.errmsg||o.errMsg||"",
         books:(o.books||[]).filter(b=>String(b.bookId||"").indexOf("MP_WXS_")===0)
           .map(b=>({name:b.title,bookId:b.bookId,deepLink:b.deepLink||""}))}))`,
  );
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 单页拉取：每次页面求值只取一页，避免长任务超时。 */
async function fetchPage(wsUrl, bookId, offset) {
  const expression = `fetch("/web/mp/articles?bookId=${bookId}&offset=${offset}", {credentials:"include"})
  .then(r => r.json())
  .then(o => ({
    errCode: o.errCode || 0,
    reviews: (o.reviews || []).map(grp =>
      (grp.subReviews || []).map(s => {
        const rr = s.review || {};
        const mi = rr.mpInfo || {};
        return {
          t: rr.createTime || grp.createTime || 0,
          title: mi.title || "",
          url: mi.originalId ? "https://mp.weixin.qq.com/s/" + mi.originalId.replace(/~/g, "_") : "",
          rid: rr.reviewId || ""
        };
      })
    ).flat()
  }))`;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      return await evaluateOnPage(wsUrl, expression, 60000);
    } catch (error) {
      if (attempt < 3) {
        console.warn(`    页 offset=${offset} 求值失败（${error.message}），10s 后重试（${attempt}/3）`);
        await sleep(10000);
      } else {
        throw error;
      }
    }
  }
  throw new Error("fetchPage 意外结束");
}

/**
 * 增量抓取：从最新页开始，整页都是已知链接即停；
 * 无已知链接时（首次/全量）一直翻到结尾。
 * partialDepth：断点续跑深度（上次中断时未验证到的 offset）。在 offset <= partialDepth
 *   范围内**不触发**“整页已知即停”——回填散布在中断点之上，可能存在整页已知但
 *   缺口在更深处的页，只有翻过中断深度才能保证缺口被补齐。
 * 回传 stoppedAtOffset（触发停止的页 offset）与 reachedEnd（是否翻到空页/上限，
 * 即整号扫完），供调用方判断本次是否已越过断点深度、能否清除断点。
 */
async function fetchNewArticles(wsUrl, bookId, knownUrls = [], range = null, partialDepth = 0) {
  const known = new Set(knownUrls);
  const seen = new Set();
  const items = [];
  let offset = 0;
  let emptyPages = 0;
  let stoppedAtKnown = false;
  let stoppedAtRange = false;
  let stoppedAtOffset = 0;
  let reachedEnd = false;
  while (offset < MAX_OFFSET && emptyPages < 2) {
    let page = null;
    try {
      for (let pg = 1; pg <= 3; pg++) {
        page = await fetchPage(wsUrl, bookId, offset);
        if (!page.errCode) break;
        if (page.errCode === -2041 || page.errCode === -2014) {
          console.error(`  ❌ ${riskControlMessage(page.errCode)}`);
          return { fatal: true, riskControl: true, errCode: page.errCode, items, sweptUntil: offset };
        }
        if (pg < 3) {
          console.warn(`  页 offset=${offset} 接口 errCode=${page.errCode}，30s 后重试（${pg}/3）`);
          await sleep(30000);
        }
      }
    } catch (evalError) {
      // 页面求值持续失败：当作中断返回，已翻到的 items 交由调用方落盘（断点续跑）。
      return { fatal: true, error: evalError.message, items, sweptUntil: offset };
    }
    if (page.errCode) {
      const hint = page.errCode === -2041
        ? "持续返回 -2041：请确认 Chrome 窗口中的验证码已完成后重跑；也可能是阅读器上下文未就绪"
        : `接口返回 errCode=${page.errCode}`;
      console.error(`  ❌ ${hint}`);
      return { fatal: true, errCode: page.errCode, items, sweptUntil: offset };
    }
    let added = 0;
    let oldCount = 0;
    let hitRangeStart = false;
    for (const it of page.reviews) {
      if (!it.title || !it.url) continue;
      if (range && it.t) {
        if (it.t > range.endTs) continue; // 晚于截止日：跳过，继续向下翻
        if (it.t < range.startTs) { hitRangeStart = true; break; } // 早于起始日：本页及后续都超范围
      }
      if (known.has(it.url)) { oldCount++; continue; }
      if (!seen.has(it.url)) {
        seen.add(it.url);
        items.push(it);
        added++;
      }
    }
    if (hitRangeStart) {
      stoppedAtRange = true;
      stoppedAtOffset = offset;
      break;
    }
    if (added === 0 && oldCount > 0) {
      if (partialDepth > 0 && offset <= partialDepth) {
        // 断点深度之内不停止：中断点之下可能有未翻到的缺口，必须翻过该深度。
        console.log(`    页 offset=${offset}：断点续跑区域内（中断深度 ${partialDepth}），继续向下穿透`);
      } else {
        stoppedAtKnown = true;
        stoppedAtOffset = offset;
        break;
      }
    }
    if (page.reviews.length === 0) emptyPages++; else emptyPages = 0;
    offset += 50;
    if (added > 0) console.log(`    页 offset=${offset - 50}：新增 ${added} 条`);
    await sleep(PAGE_INTERVAL_MS);
  }
  if (!stoppedAtKnown && !stoppedAtRange) reachedEnd = true; // 翻到空页或深度上限：整号扫完
  items.sort((a, b) => (b.t || 0) - (a.t || 0));
  return { fatal: false, total: items.length, items, stoppedAtKnown, stoppedAtRange, stoppedAtOffset, reachedEnd };
}

async function waitThenProbe(wsUrl, waitMs) {
  await new Promise((r) => setTimeout(r, waitMs));
  return pageProbe(wsUrl);
}

/**
 * 就绪探测：直接调用一次文章列表接口。
 * 阅读器页常显示纯图片文章（innerText 很短），不能用文本长度判断页面是否正常。
 */
async function apiProbe(wsUrl, bookId) {
  return evaluateOnPage(
    wsUrl,
    `fetch("/web/mp/articles?bookId=${bookId}&offset=0",{credentials:"include"})
       .then(r=>r.json()).then(o=>({errCode:o.errCode||0,reviews:(o.reviews||[]).length}))`,
  );
}

function probeErrorHint(errCode, dom) {
  if (dom?.capVisible || dom?.capText) {
    return "检测到验证码：请在 Chrome 窗口中完成验证码";
  }
  if (errCode === -2041) {
    return "接口返回 -2041（可能是验证码拦截，或阅读器上下文未就绪）：请在弹出的 Chrome 窗口完成验证码";
  }
  if (errCode === -2014) {
    return "接口返回 -2014（当前微信号被封控，需更换微信读书账号）";
  }
  if (errCode === -2010) {
    return "微信读书登录失效（-2010），请重新扫码";
  }
  return `接口未就绪（errCode=${errCode}，dom.len=${dom?.len ?? "?"}）`;
}

/** -2041 验证码 / -2014 封控 虽都立即停止，但提示与恢复方式不同。 */
function riskControlMessage(errCode) {
  if (errCode === -2014) {
    return `微信读书账号被封控（-2014）：已停止整个批次。请更换微信读书账号（备用窗口 / 端口 9224）后再重跑当前账号`;
  }
  return `检测到微信读书验证码（-2041）：已停止整个批次。请在专用 Chrome 完成验证码后再重跑当前账号`;
}

function loadProgress() {
  try {
    return JSON.parse(readFileSync(PROGRESS_FILE, "utf-8"));
  } catch {
    return { accounts: {} };
  }
}

function saveProgress(progress) {
  writeFileSync(PROGRESS_FILE, JSON.stringify(progress, null, 2) + "\n", "utf-8");
}

/** 解析一段 CSV 文本（跳过表头）。 */
function parseCsvText(text) {
  const lines = text.replace(/^\uFEFF/, "").trim().split("\n");
  if (lines.length < 2) return [];
  const head = lines[0].split(",");
  const idx = Object.fromEntries(head.map((h, i) => [h, i]));
  const rows = [];
  for (const line of lines.slice(1)) {
    const cells = [];
    let cur = "";
    let inQ = false;
    let i = 0;
    // 标准 CSV 状态机：引号内的 "" 是转义的引号（标题含英文双引号时必须还原，
    // 否则读写往返会永久丢掉引号——如「“睿创杯”」这类标题）。
    while (i < line.length) {
      const ch = line[i];
      if (inQ) {
        if (ch === '"') {
          if (line[i + 1] === '"') { cur += '"'; i += 2; continue; } // 转义引号
          inQ = false; i++; continue; // 字段结束引号
        }
        cur += ch; i++; continue;
      }
      if (ch === '"') { inQ = true; i++; continue; }
      if (ch === ",") { cells.push(cur); cur = ""; i++; continue; }
      cur += ch; i++;
    }
    cells.push(cur);
    rows.push({
      name: cells[idx["公众号"]] || "",
      date: cells[idx["发布日期"]] || "",
      title: cells[idx["文章标题"]] || "",
      url: cells[idx["文章链接"]] || "",
    });
  }
  return rows;
}

/** 读取某个账号已有的 CSV（无文件时返回空数组）。 */
function readAccountCsv(name) {
  try {
    return parseCsvText(readFileSync(join(OUT_DIR, `${name}.csv`), "utf-8"));
  } catch {
    return [];
  }
}

/** 合并已有记录与本次新抓取：按 URL 去重，新数据优先。 */
function mergeRows(existing, fresh) {
  const byUrl = new Map();
  for (const r of existing) if (r.url) byUrl.set(r.url, r);
  for (const r of fresh) if (r.url) byUrl.set(r.url, r);
  return [...byUrl.values()].sort((a, b) =>
    (b.date || "").localeCompare(a.date || "") || b.title.localeCompare(a.title),
  );
}

/**
 * 断点续跑：账号中途失败（风控/接口异常）时，把已翻到的链接合并进该号 CSV/MD。
 * 返回本次落盘的新增行（可能为空数组）；下次增量会把这些链接视为已知，不必重翻深页。
 */
function persistPartialRows(name, existing, items) {
  const fresh = (items || [])
    .filter((it) => it.title && it.url)
    .map((it) => ({ name, date: fmtDate(it.t), title: it.title, url: it.url, t: it.t }));
  if (fresh.length === 0) return [];
  stableSort(fresh);
  const merged = mergeRows(existing, fresh);
  writeCsv(join(OUT_DIR, `${name}.csv`), merged);
  writeMd(join(OUT_DIR, `${name}.md`), merged, `${name} · 全部文章链接（微信读书）`);
  console.log(
    `  💾 断点续跑：已把中断前翻到的 ${fresh.length} 条合并进 ${name}.csv` +
      `（累计 ${merged.length} 条，下次重跑不必重翻这些页）`,
  );
  return fresh;
}

/* ---------- 文章页解析 __biz ---------- */
function extractBiz(text) {
  const m =
    text.match(/var\s+biz\s*=\s*["']([A-Za-z0-9+/=]+)["']/) ||
    text.match(/__biz=([A-Za-z0-9+/=%]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function extractNickname(text) {
  const m =
    text.match(/nickname\s*=\s*htmlDecode\(\s*["']([^"']+)["']/) ||
    text.match(/id=["']js_name["'][^>]*>\s*([^<]+?)\s*</);
  return m ? m[1].trim() : "";
}

async function resolveBookId(seedUrl) {
  const inline = extractBiz(seedUrl);
  let html = "";
  let source = "链接内 __biz";
  let biz = inline;
  let nickname = "";
  if (!biz) {
    const res = await fetch(seedUrl, { headers: { "User-Agent": UA } });
    if (!res.ok) throw new Error(`文章页抓取失败 HTTP ${res.status}: ${seedUrl}`);
    html = await res.text();
    biz = extractBiz(html);
    nickname = extractNickname(html);
    source = "文章页解析";
  }
  if (!biz) throw new Error(`无法从 ${seedUrl} 解析 __biz`);
  const decoded = Buffer.from(biz, "base64").toString("utf-8");
  if (!/^\d+$/.test(decoded)) throw new Error(`__biz 解码不是数字: ${biz}`);
  return { bookId: `MP_WXS_${decoded}`, biz, nickname, source };
}

/* ---------- 输出 ---------- */
function writeCsv(path, rows) {
  const head = "公众号,发布日期,文章标题,文章链接,来源\n";
  const body = rows
    .map((r) =>
      [r.name, r.date, r.title, r.url, "微信读书"]
        .map((v) => `"${String(v).replace(/"/g, '""')}"`)
        .join(","),
    )
    .join("\n");
  writeFileSync(path, "\uFEFF" + head + body + "\n", "utf-8");
}

function writeMd(path, rows, title) {
  const lines = [
    `# ${title}`,
    "",
    `共 ${rows.length} 条，来源：微信读书（mp.weixin.qq.com 原文直链）。`,
    "",
  ];
  for (const r of rows) {
    lines.push(`- **${r.date || "发布日期待确认"} · ${r.name}** [${r.title}](${r.url})`);
  }
  lines.push("");
  writeFileSync(path, lines.join("\n"), "utf-8");
}

/* ---------- 主流程 ---------- */
async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  if (!Number.isFinite(CRAWL_DAYS) || CRAWL_DAYS <= 0) {
    throw new Error(`--crawl-days / WR_CRAWL_DAYS 应为正数，收到：${CRAWL_DAYS}`);
  }
  if (RANGE) {
    console.log(`时间范围：${RANGE.startText} ~ ${RANGE.endText}`);
  } else if (DEEP) {
    console.log("深扫模式：增量账号也翻穿整个号（用于每周补齐回填旧文）");
  } else {
    console.log(
      `抓取窗口：增量账号默认近 ${CRAWL_DAYS} 天（--crawl-days 或 WR_CRAWL_DAYS 可调；` +
        `每周用 --deep 深扫一次补齐更早的回填旧文；全量模式不受限）`,
    );
  }
  const progress = loadProgress();
  const tab = await ensureWereadTab();
  const ws = tab.webSocketDebuggerUrl;
  const shelfRaw = await shelfSync(ws);
  let shelf = JSON.parse(shelfRaw);
  if (CLI_ARGS.includes("--check-session")) {
    const ok = shelf.errCode === 0 && shelf.books.length > 0;
    console.log(JSON.stringify({
      ok,
      errCode: shelf.errCode,
      errMsg: shelf.errMsg,
      shelfAccounts: shelf.books.length,
      names: shelf.books.map((b) => b.name),
    }, null, 2));
    if (!ok) process.exitCode = 2;
    return;
  }
  if (shelf.errCode !== 0) {
    throw new Error(`微信读书会话不可用（errCode=${shelf.errCode}${shelf.errMsg ? `，${shelf.errMsg}` : ""}），请先运行 --check-session`);
  }
  console.log("微信读书已连接，书架公众号数：", shelf.books.length);

  const onlyIdx = CLI_ARGS.indexOf("--only");
  let accounts = CONFIG.accounts;
  if (onlyIdx >= 0) {
    // --only 后续直到下一个 -- 开头的参数都被视作公众号名（支持多个号）。
    const onlyNames = [];
    for (let i = onlyIdx + 1; i < CLI_ARGS.length; i++) {
      const token = CLI_ARGS[i];
      if (token.startsWith("--")) break;
      onlyNames.push(token);
    }
    accounts = CONFIG.accounts.filter((a) => onlyNames.includes(a.name));
    if (accounts.length === 0) {
      throw new Error(`--only 指定的公众号不在配置中: ${onlyNames.join(" ") || "(空)"}`);
    }
  }
  const report = [];
  const allRows = [];
  const force = CLI_ARGS.includes("--force");
  for (let accountIndex = 0; accountIndex < accounts.length; accountIndex++) {
    const account = accounts[accountIndex];
    const { name, url } = account;
    console.log(`\n===== ${name} =====`);
    const existing = readAccountCsv(name);
    const incremental = existing.length > 0 && !force;
    // 抓取边界：显式 --range 优先（同时约束汇总）；增量模式默认滚动窗口（--deep 关闭）；全量模式不设滚动窗口。
    const crawlRange = RANGE || (incremental && !DEEP ? rollingRange(CRAWL_DAYS) : null);
    const windowDesc = crawlRange ? `（窗口 ${crawlRange.startText} ~ ${crawlRange.endText}）` : "";
    console.log(incremental ? `  已有 ${existing.length} 条，增量模式${windowDesc}` : `  全量模式（首次拉取或 --force）${windowDesc}`);
    let bookId = account.bookId || progress.accounts?.[name]?.bookId || "";
    let partialSaved = 0; // 断点续跑：本号中断前已落盘的新增条数
    let pendingPartialDepth = null; // 断点续跑：登记的中断深度（成功越过断点后清空）
    const partialDepth = Number(progress.accounts?.[name]?.partialDepth || 0);
    if (partialDepth > 0) {
      console.log(`  🔁 断点续跑：上次中断于深度 ${partialDepth}，本次翻过该深度，补齐其下缺口`);
    }
    try {
      let source = bookId ? "本地进度缓存" : "";
      let nickname = "";
      if (!bookId) {
        const resolved = await resolveBookId(url);
        bookId = resolved.bookId;
        source = resolved.source;
        nickname = resolved.nickname;
      }
      console.log(`  bookId=${bookId} (${source})${nickname ? ` 页面号名: ${nickname}` : ""}`);
      if (nickname && !nickname.includes(name.slice(0, 4))) {
        console.warn(`  ⚠️ 页面号名「${nickname}」与目标「${name}」不一致，仍继续（请留意结果）`);
      }

      let book = shelf.books.find((b) => b.bookId === bookId);
      if (book) {
        console.log("  书架：已存在，跳过重复订阅");
      } else {
        const addRes = await addToShelf(ws, bookId);
        console.log("  订阅：", String(addRes).slice(0, 120));
        shelf = JSON.parse(await shelfSync(ws));
        book = shelf.books.find((b) => b.bookId === bookId);
      }
      if (!book) throw new Error("订阅后书架仍找不到该书，可能该号未被微信读书收录");
      const m = String(book.deepLink).match(/[?&]v=([^&]+)/);
      if (!m) throw new Error(`deepLink 缺少 v 参数: ${book.deepLink}`);
      const readerUrl = `https://weread.qq.com/web/reader/${m[1]}`;
      console.log("  阅读器：", readerUrl);

      let probe2 = null;
      for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        // 每次都重新导航到阅读器 URL，避免页面停留在"加载失败"状态时单纯等待无效。
        // 加 cache-busting hash 防止浏览器复用旧的失败响应。
        const reloadUrl = `${readerUrl}${readerUrl.includes("#") ? "" : "#"}wr=${Date.now()}`;
        await evaluateOnPage(ws, `location.href = ${JSON.stringify(reloadUrl)}; "ok"`);
        await new Promise((r) => setTimeout(r, attempt === 1 ? READER_READY_WAIT_MS : RETRY_WAIT_MS));
        const dom = await pageProbe(ws);
        probe2 = await apiProbe(ws, bookId);
        if (probe2.errCode === 0) {
          console.log(`  阅读器页就绪（第 ${attempt} 次尝试）`);
          break;
        }
        if (probe2.errCode === -2010) throw new Error("微信读书登录失效（-2010），请重新扫码");
        if (probe2.errCode === -2041 || probe2.errCode === -2014) {
          throw new RiskControlError(riskControlMessage(probe2.errCode));
        }
        console.warn(
          `  ⚠️ ${probeErrorHint(probe2.errCode, dom)}；` +
          `已重新加载阅读器页，等待 ${RETRY_WAIT_MS / 1000}s 后重试（${attempt}/${MAX_RETRIES}）`,
        );
      }
      if (probe2?.errCode !== 0) {
        const hint = probe2?.errCode === -2041
          ? "持续返回 -2041：请确认 Chrome 窗口中的验证码已完成后重跑；也可能是阅读器上下文未就绪"
          : "阅读器接口持续未就绪，请稍后重跑";
        throw new Error(hint);
      }

      const data = await fetchNewArticles(
        ws, bookId,
        // --force：真正全量重拉（不因已知链接提前停，翻到空页为止；合并时按 URL 去重，历史不丢）。
        force ? [] : existing.map((r) => r.url),
        crawlRange, partialDepth,
      );
      if (data.riskControl || data.fatal) {
        // 断点续跑：停止批次前，先把该号已翻到的链接落盘，并登记中断深度
        // （取既有深度与本次翻到深度的较大者：浅中断不得抹掉更深的断点）。
        const partialRows = persistPartialRows(name, existing, data.items);
        allRows.push(...partialRows);
        partialSaved = partialRows.length;
        const newDepth = Math.max(partialDepth, Number(data.sweptUntil || 0));
        pendingPartialDepth = newDepth > 0 ? newDepth : null;
        if (data.riskControl) {
          throw new RiskControlError(riskControlMessage(data.errCode));
        }
        throw new Error(data.error || `接口返回 errCode=${data.errCode}`);
      }
      if (data.total === 0 && !incremental) throw new Error("接口返回 0 条，可能仍在风控");
      const fresh = data.items.map((it) => ({
        name,
        date: fmtDate(it.t),
        title: it.title,
        url: it.url,
        t: it.t,
      }));
      stableSort(fresh);
      const existingUrls = new Set(existing.map((r) => r.url));
      allRows.push(...fresh.filter((r) => !existingUrls.has(r.url))); // 只计真新增（--force 重拉的已知链接不计）
      const merged = mergeRows(existing, fresh);
      writeCsv(join(OUT_DIR, `${name}.csv`), merged);
      writeMd(join(OUT_DIR, `${name}.md`), merged, `${name} · 全部文章链接（微信读书）`);
      const stopped = data.stoppedAtRange
        ? "（到达起始日期停止）"
        : data.stoppedAtKnown
          ? "（遇到已知链接停止）"
          : "";
      const newCountVal = Math.max(0, merged.length - existing.length); // 真新增（--force 重拉时 fresh 含已存在链接）
      console.log(`  新增 ${newCountVal} 条，累计 ${merged.length} 条${stopped}`);
      // 断点推进/清除规则（关键：限窗/限范围运行**不清除**断点，只推进断点前沿）：
      //   - stoppedAtKnown / reachedEnd：本次已翻过断点深度（穿透抑制保证“整页已知”
      //     只能在断点之下触发；reachedEnd 则整个号翻穿）→ 断点清除；
      //   - stoppedAtRange：本次只验证到窗口边界，边界之下仍是未验证区 →
      //     断点推进到 max(原深度, 窗口边界)。若在此清除，窗口之下的深扫欠账会永久丢失。
      let retainedDepth = 0;
      if (partialDepth > 0) {
        retainedDepth = data.stoppedAtKnown || data.reachedEnd
          ? 0
          : Math.max(partialDepth, Number(data.stoppedAtOffset || 0));
      }
      const entry = {
        name, bookId, ok: true, mode: incremental ? "增量" : "全量",
        newCount: newCountVal, count: merged.length,
        stoppedAtKnown: !!data.stoppedAtKnown,
        stoppedAtRange: !!data.stoppedAtRange,
        ...(retainedDepth > 0
          ? { partialDepth: retainedDepth, note: `断点深度 ${retainedDepth} 保留（本次窗口只清扫到 offset=${data.stoppedAtOffset}），待深扫补齐` }
          : {}),
        at: new Date().toISOString(),
      };
      report.push(entry);
      progress.accounts[name] = entry;
      saveProgress(progress);
    } catch (err) {
      console.error(`  ❌ ${name}: ${err.message}`);
      report.push({ name, bookId, ok: false, error: err.message, count: 0, newCount: 0, partialSaved });
      progress.accounts[name] = {
        ...(progress.accounts[name] || {}),
        name, bookId, ok: false, error: err.message, at: new Date().toISOString(),
        ...(partialSaved ? { partialSaved } : {}),
        ...(pendingPartialDepth ? { partialDepth: pendingPartialDepth } : {}),
      };
      saveProgress(progress);
      if (err.code === "WR_RISK_CONTROL") {
        for (const skipped of accounts.slice(accountIndex + 1)) {
          report.push({
            name: skipped.name,
            bookId: progress.accounts?.[skipped.name]?.bookId || skipped.bookId || "",
            ok: false,
            skipped: true,
            error: "前序账号触发风控，为避免继续请求而跳过",
            count: 0,
            newCount: 0,
          });
        }
        console.error("  为避免加重风控，已跳过本批次剩余公众号。已有历史数据不会被覆盖。");
        break;
      }
    }
    if (accountIndex < accounts.length - 1) {
      await new Promise((r) => setTimeout(r, ACCOUNT_INTERVAL_MS));
    }
  }

  allRows.sort((a, b) => (b.t || 0) - (a.t || 0));
  runSummary();
  writeFileSync(
    join(OUT_DIR, "运行报告.json"),
    JSON.stringify(
      {
        generated_at: new Date().toISOString(),
        new_links_this_run: allRows.length,
        summary_window: RANGE ? `${RANGE.startText}-${RANGE.endText}` : "since-20260710",
        crawl_window: RANGE
          ? `${RANGE.startText}-${RANGE.endText}`
          : DEEP
            ? "deep（增量不限深）"
            : `近${CRAWL_DAYS}天（仅增量；全量模式不限）`,
        summary_csv: SUMMARY_CSV,
        accounts: report,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );

  console.log("\n===== 汇总 =====");
  for (const r of report) {
    const detail = r.ok
      ? `${r.mode || "?"}：新增 ${r.newCount ?? r.count} / 累计 ${r.count}${r.stoppedAtKnown ? "（已停止）" : ""}`
      : `${r.error}${r.partialSaved ? `（中断前已落盘 ${r.partialSaved} 条）` : ""}`;
    console.log(`${r.ok ? "✓" : "✗"} ${r.name}: ${detail}`);
  }
  console.log(`本次新增 ${allRows.length} 条；汇总由 generate_summary.py 生成，输出目录：${OUT_DIR}`);
}

main().catch((err) => {
  console.error("运行失败：", err.message);
  process.exit(1);
});
