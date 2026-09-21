"""AI 筛选的底层工具库：微信文章抓取、二维码解码、长图切片、GLM 识别与报名链接清洗。

screen_update.py 只负责按日期编排与结果落盘，具体能力都在这里。
zxing-cpp 与 pillow 未安装时自动降级为纯文本分析（海报式推送不读图）。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.request
import html as htmllib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# 宣讲会省份推断映射的唯一事实源在 server_common（bootstrap 的 geo 字段同源下发前端）
from server_common import ACCOUNT_PROVINCES, CITY_PROVINCES, PROVINCES

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

try:
    from PIL import Image
    import zxingcpp
except ImportError:  # 容器镜像未装图像依赖时降级为纯文本分析
    Image = None
    zxingcpp = None

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SLICE_H = 2000
MAX_W = 1000
MAX_IMAGES = 20         # 单组最多下载多少张文章图片
MAX_SLICES = 10         # 单组最多送多少张切片给 GLM
DEFAULT_CONCURRENCY = 10

# 微信反爬验证页/删除页的标记：命中说明这次没抓到正文，应记为可重试失败而不是"无内容"
BLOCK_MARKERS = ("当前环境异常", "完成验证后即可继续访问", "此内容因违规", "该内容已被发布者删除")


class FetchBlockedError(RuntimeError):
    """微信返回验证/删除页，正文暂时抓不到；应落盘为可重试的失败条目，下次运行自动补析。"""


JUDGE_PROMPT = """你是招聘信息提取助手。下面是一篇微信公众号文章（日期 {date}，来源公众号：{account}，标题：{title}）。
{正文块}
{二维码块}
{图片说明}请完整阅读全部内容（图片与正文同样重要，岗位表、时间地点常在长图里），只输出一个 JSON 对象（不要输出其他文字）：
{{
  "保留": true 或 false,
  "招聘单位": "单位全称",
  "摘要": "一句话说明这篇内容",
  "招聘对象": "如 2027届本硕博",
  "岗位列表": [{{"岗位": "...", "类别": "方向，如 软件开发/计算机/电子信息/网络安全/机械/土木/医护/教师/行政", "地点": "城市"}}],
  "工作地点": "城市汇总",
  "报名方式": "网申链接/邮箱，原样写出",
  "宣讲会": {{"是宣讲会": false, "时间": "", "地点": "", "多公司": false}},
  "跳过原因": "不保留时的原因"
}}
判断标准：凡面向求职者的招聘信息——任何方向的岗位招聘、校园招聘、宣讲会/招聘会/双选会/组团招聘通知——都保留 true，并完整提取岗位列表，不限专业方向（教师、医护、机械、土木等同样保留）；跳过 false 的只有：与招聘无关的内容（纯活动通知、政策宣传、新闻资讯、就业宣传稿），以及**面向用人单位/企业的参会邀请函**（邀请企业报名设摊的，不是给求职者看的，跳过原因注明"面向用人单位的邀请函"）。
"宣讲会"字段：正文中出现的面向毕业生的线下宣讲会、招聘会、双选会、组团招聘等场次都要填——即使文章主体是网申招聘（宣讲只是其中一场活动），也要提取"时间"（如 9月12日 14:00-16:30）和"地点"（省+市+场馆，原文怎么写就怎么提取）；完全没有线下活动信息才填 false。
"多公司"：多家不同用人单位联合参加同一活动（组团招聘、大型双选会、巡回招聘会等）填 true；单一公司举办的人才日/专场活动（即使同时包含宣讲与双选环节）填 false。岗位列表最多列 8 条。"""


# ---------- 配置 ----------

def load_env() -> None:
    """读取项目根目录 .env，且不覆盖已有环境变量（与 generate_summary.py 约定一致）。"""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and not os.environ.get(key):
            os.environ[key] = value


def api_config() -> tuple[str, str, str]:
    """返回 (api_base, model, api_key)，SCREEN_* 优先，回落到 DEDUP_*。"""
    load_env()
    base = (os.environ.get("SCREEN_API_BASE") or os.environ.get("DEDUP_API_BASE")
            or "http://127.0.0.1:8000/v1")
    model = (os.environ.get("SCREEN_MODEL") or os.environ.get("DEDUP_MODEL")
             or "zai-org/GLM-5.3-Flash")
    key = (os.environ.get("SCREEN_API_KEY") or os.environ.get("DEDUP_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY") or "")
    return base.rstrip("/"), model, key


def concurrency_config(override: int | None) -> int:
    load_env()
    raw = override if override is not None else int(os.environ.get("WR_SCREEN_CONCURRENCY", str(DEFAULT_CONCURRENCY)))
    return max(1, min(20, raw))


# ---------- HTTP 与文章抓取 ----------

def http_get(url: str, timeout: int = 25) -> str:
    last: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as err:
            last = err
            time.sleep(1.5)
    raise RuntimeError(f"GET 失败 {url}: {last}")


def extract_content_html(page: str) -> str:
    i = page.find('id="js_content"')
    if i < 0:
        return ""
    start = page.rfind("<div", 0, i)
    depth = 0
    for m in re.finditer(r"<div\b|</div>", page[start:]):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            return page[start:start + m.end()]
    return page[start:]


def strip_to_text(fragment: str) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    s = re.sub(r"<(br|/p|/section|/h[1-6]|/li|/tr)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\u00a0\u200b]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def scrape_article(url: str) -> dict:
    page = http_get(url)
    marker = next((m for m in BLOCK_MARKERS if m in page), None)
    if marker:
        # 反爬验证页是正常的 200 响应；退避重试几次，仍命中就抛给上层记为可重试失败
        for delay in (5, 10):
            time.sleep(delay)
            page = http_get(url)
            marker = next((m for m in BLOCK_MARKERS if m in page), None)
            if marker is None:
                break
    if marker:
        raise FetchBlockedError(f"微信返回验证/删除页（{marker}）")
    m = re.search(r'<span class="js_title_inner">(.*?)</span>', page, re.S)
    title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None
    m2 = re.search(r'var msg_source_url\s*=\s*[\'"]([^\'"]+)[\'"]', page)
    readmore = htmllib.unescape(m2.group(1)).strip() if m2 else None
    content = extract_content_html(page)
    images = [htmllib.unescape(x) for x in re.findall(r'<img[^>]*data-src="([^"]+)"', content)]
    return {"text": strip_to_text(content), "images": images, "readmore": readmore}


def download_images(urls: list[str], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, src in enumerate(urls):
        dest = out_dir / f"{i:02d}.jpg"
        if not (dest.exists() and dest.stat().st_size > 0):
            try:
                req = urllib.request.Request(src, headers={"User-Agent": UA, "Referer": "https://mp.weixin.qq.com/"})
                dest.write_bytes(urllib.request.urlopen(req, timeout=25).read())
            except Exception as err:
                print(f"      图片下载失败 {i}: {err}", flush=True)
                continue
        paths.append(str(dest))
    return paths


# ---------- 图片处理（下载、二维码、切片） ----------

def open_image(path: str):
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def collect_image_inputs(urls: list[str], cache_dir: Path) -> tuple[list, list[str]]:
    """下载文章图片并做本地处理，返回 (送模型的切片列表, 二维码解码结果)。

    岗位表、时间地点常在长图里，因此不再按正文字数决定是否读图：有图就读。
    二维码对所有下载图解码（本地、便宜）；切片先过滤图标/分隔线等装饰图，
    再按面积降序排序（稳定排序保持同一张图内部的先后），配合 MAX_SLICES 截断。
    """
    if Image is None or zxingcpp is None or not urls:
        return [], []
    slices: list = []
    qr_urls: list[str] = []
    for path in download_images(urls[:MAX_IMAGES], cache_dir):
        for u in decode_qr(path):
            if u not in qr_urls:
                qr_urls.append(u)
        im = open_image(path)
        if im is None:
            continue
        w, h = im.size
        if (h < 100 and w < 200) or h < 60 or w < 60:
            continue  # 图标、分隔线等装饰图
        slices.extend(slice_image(im))
    slices.sort(key=lambda im: im.size[0] * im.size[1], reverse=True)
    return slices, qr_urls


# ---------- 二维码解码（整图 → 缩图 → 滑窗） ----------

def decode_qr(path: str) -> list[str]:
    found: list[str] = []
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return found
    variants = [im]
    small = im.copy()
    small.thumbnail((1400, 1400))
    variants.append(small)
    for cand in variants:
        try:
            for r in zxingcpp.read_barcodes(cand, try_downscale=True, try_invert=True, try_rotate=True):
                if r.text not in found:
                    found.append(r.text)
        except Exception:
            pass
        if found:
            return found
    w, h = im.size
    win, step = 1300, 800
    y = 0
    while y < h:
        crop = im.crop((0, y, w, min(y + win, h)))
        try:
            for r in zxingcpp.read_barcodes(crop, try_downscale=True, try_invert=True, try_rotate=True):
                if r.text not in found:
                    found.append(r.text)
        except Exception:
            pass
        if y + win >= h:
            break
        y += step
    return found


# ---------- GLM 识别 ----------

def slice_image(im) -> list:
    w, h = im.size
    if w > MAX_W:
        im = im.resize((MAX_W, int(h * MAX_W / w)), Image.LANCZOS)
        w, h = im.size
    if h <= SLICE_H + 200:
        return [im]
    out = []
    step = SLICE_H - 120
    y = 0
    while y < h:
        out.append(im.crop((0, y, w, min(y + SLICE_H, h))))
        if y + SLICE_H >= h:
            break
        y += step
    return out


def encode_jpeg(im, quality: int = 80) -> str:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def glm_analyze(api_base: str, model: str, api_key: str,
                date: str, account: str, title: str, info: dict, local_imgs: list) -> dict:
    text = info["text"]
    text_block = ("正文文本：\n" + text[:8000]) if text.strip() else "（正文无文本，信息在图片里）"
    qr_block = ""
    if info.get("readmore"):
        qr_block = "阅读原文链接（可作报名链接）： " + info["readmore"]
    if info.get("qr_urls"):
        qr_block += "\n二维码解码结果： " + "；".join(info["qr_urls"])
    prompt = JUDGE_PROMPT.format(date=date, account=account, title=title,
                                 正文块=text_block, 二维码块=qr_block,
                                 图片说明="下面是文章长图切片。" if local_imgs else "（无图片）")
    parts = [{"type": "text", "text": prompt}]
    for im in local_imgs[:MAX_SLICES]:
        if im.size[1] < 20 or im.size[0] < 20:
            continue
        b64 = encode_jpeg(im, 82)
        if len(b64) > 3_500_000:
            im2 = im.copy()
            im2.thumbnail((760, 100000))
            b64 = encode_jpeg(im2, 78)
        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
    payload = {"model": model, "messages": [{"role": "user", "content": parts}],
               "max_tokens": 16384, "temperature": 0.1}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(f"{api_base}/chat/completions",
                                         data=json.dumps(payload).encode(), headers=headers)
            resp = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
            raw = resp["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw[raw.find("{"):]
            return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as err:
            last = err
            time.sleep(2)
    raise RuntimeError(f"GLM 分析失败: {last}")


# ---------- 报名链接清洗 ----------

# 只匹配纯 ASCII、无空白的 URL 片段：遇到中文标点/汉字/空格即停
URL_CANDIDATE_RE = re.compile(r"https?://[!-~]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ---------- 宣讲会结构化 ----------

FAIR_YMD_RE = re.compile(r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})")
FAIR_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
FAIR_NUMERIC_MD_RE = re.compile(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?!\d)")


def parse_fair_date(time_text: str, publish_date: str) -> str:
    """把宣讲会时间文本解析为 ISO 日期（YYYY-MM-DD），解析不出返回空串。

    年份默认取文章发布年份；解析结果早于发布日（跨年场景，如 12 月发的"1月5日"）时进一年。
    """
    if not time_text:
        return ""
    m = FAIR_YMD_RE.search(time_text)
    if m:
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    else:
        m = FAIR_MD_RE.search(time_text) or FAIR_NUMERIC_MD_RE.search(time_text)
        if not m:
            return ""
        month, day = int(m[1]), int(m[2])
        year = int(publish_date[:4]) if len(publish_date) >= 4 and publish_date[:4].isdigit() else 0
        if not year:
            return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    try:
        parsed = datetime(year, month, day)
        if len(publish_date) >= 10:
            try:
                published = datetime.strptime(publish_date[:10], "%Y-%m-%d")
                # 跨年推断只认"年末发的年初活动"（12 月发的 1月5日 → 明年）；
                # 发布前的开场日期（9月8日发的"9月7日起"系列宣讲）保持当年
                if published.month - parsed.month >= 8:
                    parsed = datetime(year + 1, month, day)
            except ValueError:
                pass
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return ""


def infer_province(location: str, time_text: str, accounts: list[str]) -> str:
    """宣讲会省份推断：地点文本扫省份/城市 → 时间文本扫（有的把地点写进时间）→ 公众号所属高校省份兜底。"""
    for text in (location, time_text):
        hit = next((p for p in PROVINCES if p in text), None)
        if hit:
            return hit
    for text in (location, time_text):
        for city, province in CITY_PROVINCES.items():
            if city in text:
                return province
    for account in accounts:
        if account in ACCOUNT_PROVINCES:
            return ACCOUNT_PROVINCES[account]
    return ""


def build_fair(parsed: dict, publish_date: str, accounts: list[str]) -> dict | None:
    """从 GLM 输出里取"宣讲会"对象，整理成 entry 的 fair 字段；非宣讲会返回 None。"""
    raw = parsed.get("宣讲会")
    if not isinstance(raw, dict) or not raw.get("是宣讲会"):
        return None
    time_text = str(raw.get("时间") or "").strip()
    location = str(raw.get("地点") or "").strip()
    return {
        "is_fair": True,
        "time": time_text,
        "location": location,
        "province": infer_province(location, time_text, accounts),
        "multi_company": bool(raw.get("多公司")),
        "date": parse_fair_date(time_text, publish_date),
    }


def sanitize_apply_url(raw: str, fallback: str | None = None) -> str:
    """从 GLM 输出的「报名方式」里提取干净的可点击链接。

    模型经常把标签文字、链接和邮箱拼在一个字符串里，例如
    “网申链接：https://a.com/x ；联系邮箱：hr@a.com”，直接放进 href 会被
    浏览器当相对路径解析成 404。这里只保留纯 ASCII 的 URL 候选，取路径最长的
    一个；没有链接时回落到阅读原文，再没有则把邮箱转成 mailto:。
    """
    if raw:
        candidates = [m.rstrip(".,;:") for m in URL_CANDIDATE_RE.findall(raw)]
        candidates = [c for c in candidates if urlparse(c).netloc]
        if candidates:
            return max(candidates, key=lambda c: len(urlparse(c).path) + len(urlparse(c).query))
        email = EMAIL_RE.search(raw)
        if email:
            return "mailto:" + email.group(0)
    return (fallback or "").strip()
