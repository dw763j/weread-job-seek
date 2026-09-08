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
from pathlib import Path
from urllib.parse import urlparse

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
POSTER_MIN = 150        # 正文短于该字数视为海报式，需要读图
MAX_SLICES = 10         # 单组最多送多少张切片给 GLM
DEFAULT_CONCURRENCY = 10

JUDGE_PROMPT = """你是招聘信息筛选助手。下面是一篇微信公众号招聘文章（日期 {date}，来源公众号：{account}，标题：{title}）。
{正文块}
{二维码块}
{图片说明}请完整阅读全部内容，只输出一个 JSON 对象（不要输出其他文字）：
{{
  "保留": true 或 false,
  "招聘单位": "单位全称",
  "摘要": "一句话说明这篇招聘",
  "招聘对象": "如 2027届本硕博",
  "岗位列表": [{{"岗位": "...", "类别": "计算机类 或 其他（注明方向）", "地点": "城市"}}],
  "工作地点": "城市汇总",
  "报名方式": "网申链接/邮箱，原样写出",
  "跳过原因": "不保留时的原因"
}}
判断标准：岗位中包含软件工程/计算机/软件开发/网络安全/人工智能/大数据类 → 保留 true；教师、医护、纯活动通知、就业宣传、纯机械/土木等非计算机类 → false，并在"跳过原因"说明。软件/计算机类岗位排在岗位列表前面，最多列 8 条。"""


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

def slice_image(path: str) -> list:
    im = Image.open(path).convert("RGB")
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
