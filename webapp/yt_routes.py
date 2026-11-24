"""yt-dlp 的网页接口：探测链接、报告引擎能力。

- GET /api/yt/info?url=...   只解析不下载：标题、时长、清晰度
- GET /api/yt/status         引擎是否就绪、支持多少站点
- GET /api/yt/sites          界面用的站点清单
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import ytdlp_engine

router = APIRouter()

_SITES_CACHE: Dict[str, Any] = {"count": 0, "at": 0}


@router.get("/api/yt/status")
async def yt_status() -> Dict[str, Any]:
    exe = ytdlp_engine.find_ytdlp()
    ff = ytdlp_engine.find_ffmpeg()
    version = ""
    count = _SITES_CACHE.get("count") or 0
    if exe:
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            version = (out or b"").decode("utf-8", "replace").strip()
        except Exception:
            version = ""
        # 提取器数量只在第一次问的时候算，之后缓存
        if not count:
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, "--list-extractors",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                count = len([x for x in (out or b"").decode("utf-8", "replace").splitlines() if x.strip()])
                _SITES_CACHE["count"] = count
            except Exception:
                pass
    return {
        "ready": bool(exe),
        "path": exe or "",
        "version": version,
        "ffmpeg": bool(ff),
        "ffmpeg_path": ff or "",
        "extractors": count,
    }


@router.get("/api/yt/sites")
async def yt_sites() -> Dict[str, Any]:
    """界面展示用：常见站点 + 引擎总数。"""
    items = [{"host": k, "name": v} for k, v in ytdlp_engine.KNOWN_SITES.items()]
    return {"known": items, "extractors": _SITES_CACHE.get("count") or 0}


@router.get("/api/yt/info")
async def yt_info(url: str = "", timeout: int = 60) -> Dict[str, Any]:
    url = (url or "").strip()
    if not url:
        return JSONResponse({"detail": "缺少 url"}, status_code=400)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse({"detail": "需要一个 http(s) 链接"}, status_code=400)

    if ytdlp_engine.is_douyin(url):
        return JSONResponse(
            {"detail": "抖音链接会走专用引擎（能抓主页/合集/直播），这里不解析", "engine": "douyin"},
            status_code=400,
        )

    try:
        info = await ytdlp_engine.probe(url, timeout=max(15, min(timeout, 180)))
    except Exception as exc:
        return JSONResponse({"detail": f"解析失败：{exc}"}, status_code=502)
    return info


@router.get("/api/engine")
async def which_engine(url: str = "") -> Dict[str, Any]:
    """这条链接会交给谁 —— 前端用来在提交前给提示。

    输入可能是整段分享文案，先抠链接再判断，
    否则 host 为空会被误报成「未知站点」。
    """
    import url_clean

    raw = (url or "").strip()
    if not raw:
        return {"engine": "", "site": ""}

    found = url_clean.extract_urls(raw)
    target = found[0] if found else raw

    if not url_clean.is_probably_url(target):
        return {
            "engine": "",
            "site": "",
            "host": "",
            "no_link": True,
            "detail": "这段内容里没有 http 开头的链接",
        }

    return {
        "engine": ytdlp_engine.engine_for(target),
        "site": ytdlp_engine.site_label(target),
        "host": ytdlp_engine.host_of(target),
        "no_link": False,
    }
