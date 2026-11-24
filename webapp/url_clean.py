"""从用户粘贴的文本里把真正的链接抠出来。

为什么需要这个：
抖音 App 的「复制链接」给的不是一条 URL，而是一整段分享文案：

    3.82 复制打开抖音，看看【鸽子蛋的作品】快乐过好每一天 https://v.douyin.com/9M5HtT0FXq8/ O@X.zT wFh:/ :2pm 04/10

直接把这整段当 URL 交给下载器必然失败（urlparse 的 hostname 是 None）。
同理，B站/YouTube 的分享也常带标题、时间戳、话题标签。

所以提交前统一做一次清洗：从文本里找出所有 http(s) 链接，
只把链接交给下载引擎，文案丢掉。
"""

from __future__ import annotations

import re
from typing import List

# 匹配 http(s) 链接。故意写得宽松一点：
# - 允许链接后面直接跟中文标点或空格（抖音文案里常见）
# - 不允许把尾部的中文/引号/标点吃进去
_URL_RE = re.compile(
    r"https?://"
    r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)

# 有些平台复制出来是不带 http 的裸域名（如 v.douyin.com/9M5.../），
# 也要认。限定成常见平台，避免把正文里的普通英文当链接。
_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9.\-/])"
    r"(?:v\.douyin\.com|v\.iesdouyin\.com|www\.douyin\.com|douyin\.com"
    r"|b23\.tv|www\.bilibili\.com|bilibili\.com|m\.bilibili\.com"
    r"|youtu\.be|www\.youtube\.com|youtube\.com|m\.youtube\.com"
    r"|x\.com|twitter\.com|v\.kuaishou\.com|www\.kuaishou\.com"
    r"|www\.xiaohongshu\.com|xhslink\.com)"
    r"(?:/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*)?",
    re.IGNORECASE,
)

# 链接尾部常见的「不该属于链接」的字符，逐个剥掉
_TRAILING = ".,;:!?、。，；：！？）》】」』\"'"


def _strip_tail(url: str) -> str:
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    return url.rstrip("/") if url.endswith("//") else url


def extract_urls(text: str) -> List[str]:
    """从任意文本里抠出所有链接，按出现顺序、去重。

    带 http 的优先；没有的话再找裸域名（v.douyin.com/xxx 这种）。
    """
    if not text:
        return []

    found: List[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = _strip_tail(u)
        if not u or len(u) < 8:
            return
        if u not in seen:
            seen.add(u)
            found.append(u)

    for m in _URL_RE.finditer(text):
        add(m.group(0))

    if not found:
        # 没有完整链接时，找裸域名并补上 https://
        for m in _BARE_RE.finditer(text):
            add("https://" + m.group(0).lstrip("/"))

    return found


def clean_one(text: str) -> str:
    """单条输入：能抠出链接就返回第一条，否则原样返回（交给下游报错）。"""
    text = (text or "").strip()
    urls = extract_urls(text)
    if urls:
        return urls[0]
    return text


def is_probably_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("http://") or t.startswith("https://")
