"""抖音下载器 · 多设备网页版

把打包好的下载引擎（engine/，复制自开源项目 douyin-downloader）包一层网页服务：
- 监听 0.0.0.0，同一个局域网里的手机、平板、其他电脑都能用浏览器访问
- 下载任务在主机上执行，结果落在主机的 download_path
- 可选访问码：config.json 里填 access_code，别的设备要输码才能操作
- 不影响原项目（引擎是独立副本，所有改动都在本目录）
"""

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent          # D:\DouyinWeb
ENGINE = ROOT / "engine"
WEBAPP = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"

os.chdir(ENGINE)
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(WEBAPP))

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import ConfigLoader
from server.app import _ServerDeps
from server.jobs import JobManager, JobStatus

# 进度层：JobManager 的子类 + 满足引擎 ProgressReporter 协议的记录器
from progress import DownloadProgress, ProgressJobManager

# 引擎自己的 _execute_download 不接 progress_reporter，这里照它的实现复制一份，
# 只多传一个 reporter（engine/ 目录不动）。
from auth import CookieManager
from core import (
    UNSUPPORTED_URL_TYPE_DETAIL,
    DouyinAPIClient,
    DownloaderFactory,
    URLParser,
)
from utils.validators import is_short_url, normalize_short_url


async def _execute_download_with_progress(
    url: str, deps, progress_reporter=None
) -> Dict[str, int]:
    """与 server.app._execute_download 等价，额外把 progress_reporter 交给下载器。"""
    async with DouyinAPIClient(
        deps.cookie_manager.get_cookies(),
        proxy=deps.config.get("proxy"),
    ) as api_client:
        if is_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_short_url(url))
            if not resolved:
                raise RuntimeError(f"Failed to resolve short URL: {url}")
            url = resolved

        parsed = URLParser.parse(url)
        if not parsed:
            raise RuntimeError(f"Unsupported URL: {url}")

        gated_detail = UNSUPPORTED_URL_TYPE_DETAIL.get(str(parsed.get("type") or ""))
        if gated_detail:
            raise RuntimeError(gated_detail)

        downloader = DownloaderFactory.create(
            parsed["type"],
            deps.config,
            api_client,
            deps.file_manager,
            deps.cookie_manager,
            None,
            deps.rate_limiter,
            deps.retry_handler,
            deps.queue_manager,
            progress_reporter=progress_reporter,
        )
        if downloader is None:
            raise RuntimeError(f"No downloader for url_type={parsed['type']}")

        result = await downloader.download(parsed)
        return {
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "skipped": result.skipped,
        }


ENGINE_CONFIG = ENGINE / "config.yml"


def _load_cfg() -> Dict[str, Any]:
    defaults = {
        "port": 8790,
        "download_path": str(ROOT / "downloads"),
        "quality": "highest",
        "access_code": "",
    }
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        defaults.update({k: data[k] for k in defaults if k in data})
    except Exception:
        pass
    return defaults


CFG = _load_cfg()
BASE_CONFIG = ConfigLoader(str(ENGINE_CONFIG))


def _save_cfg() -> None:
    CONFIG_FILE.write_text(
        json.dumps(CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _resolve_path(raw: str) -> Path:
    raw = (raw or "").strip().strip('"')
    if not raw:
        raw = str(ROOT / "downloads")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = ROOT / p
    return p


def _lan_ip() -> str:
    candidates: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        candidates.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.count(".") == 3 and not ip.startswith("127."):
                candidates.append(ip)
    except Exception:
        pass
    for ip in candidates:
        if not ip.startswith("169.254") and not ip.startswith("0."):
            return ip
    return "127.0.0.1"


LAN_IP = _lan_ip()


def _is_placeholder(value: Any) -> bool:
    s = str(value or "").strip()
    return (not s) or s.upper().startswith("YOUR_") or s.lower() in {"none", "null"}


def _cookies_real(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    return any(isinstance(v, str) and not _is_placeholder(v) for v in data.values())


def _logged_in() -> bool:
    f = ENGINE / ".cookies.json"
    try:
        if f.exists() and _cookies_real(json.loads(f.read_text(encoding="utf-8"))):
            return True
    except Exception:
        pass
    try:
        return _cookies_real(BASE_CONFIG.get("cookies"))
    except Exception:
        return False


async def _executor(job, progress) -> Dict[str, int]:
    """按链接域名选引擎：抖音走专用引擎，其余走 yt-dlp。

    `progress` 是 progress.DownloadProgress，满足引擎的 ProgressReporter 协议，
    所以引擎的条目计数和字节回调会直接写进它，前端轮到的就是真实进度。
    """
    import ytdlp_engine

    target = _resolve_path(CFG["download_path"])

    # 抖音以外的站点交给 yt-dlp（它支持 1750+ 个站点）
    if ytdlp_engine.engine_for(job.url) == "ytdlp":
        yp = ytdlp_engine.YtdlpProgress(job.job_id)
        # 把 yt-dlp 的进度记录器挂到 job 上，/api/progress 直接读它，
        # 不再走「桥接 + 定时搬运」——那层多余且会互相覆盖状态。
        setattr(job, "_ytdlp_progress", yp)
        progress.step = "准备中"
        progress.started = True
        try:
            result = await ytdlp_engine.run_download(
                job.job_id, job.url, target, yp, quality=CFG["quality"]
            )
            # 成功时把通用进度也对齐，保证结束态一致
            progress.step = "完成"
            progress.total_items = 1
            progress.done_items = 1
            progress._inflight = {}
            return {
                "total": result.get("total", 1),
                "success": result.get("success", 1),
                "failed": result.get("failed", 0),
                "skipped": result.get("skipped", 0),
            }
        except Exception as exc:
            progress.step = "失败"
            progress.detail = str(exc)[:300]
            progress._inflight = {}
            raise

    cfg = ConfigLoader(str(ENGINE_CONFIG))
    cfg.config["path"] = str(target)
    cfg.config["video_quality"] = CFG["quality"]
    cookies = cfg.config.get("cookies")
    if isinstance(cookies, dict) and not _cookies_real(cookies):
        cfg.config["cookies"] = {}
    deps = _ServerDeps(cfg)
    result = await _execute_download_with_progress(job.url, deps, progress_reporter=progress)

    # 引擎把真实失败原因记在日志里，但它自己不往上抛；
    # 这里补一句人话，别让界面只留一个「失败」。
    if isinstance(result, dict) and int(result.get("failed") or 0) > 0 and int(result.get("success") or 0) == 0:
        hint = _explain_failure(deps, job.url)
        if hint:
            progress.detail = hint
            progress.item_reasons.append(hint)
    return result


def _explain_failure(deps, url: str) -> str:
    """根据登录状态，给失败一个能照着做的解释。

    实测中最常见的失败是抖音的风控：
        Douyin API HTTP failure: ... status=403
        body='Blocked by ArgusSecurityPlugin Uifid Not Found'
    请求被拒之后引擎只记一条日志然后返回 0 成功，界面上就只剩一个「失败」。
    """
    try:
        logged = _logged_in()
    except Exception:
        logged = False

    if not logged:
        return (
            "抖音拒绝了这次请求（没有登录态，被风控拦下）。"
            "在主机上双击 登录抖音.bat 扫码登录后再试。"
        )
    return "抖音没有返回作品详情，可能是风控或作品已删除/不可见，稍后重试或重新登录。"


manager = ProgressJobManager(
    executor=_executor,
    max_concurrency=int(BASE_CONFIG.get("thread", 2) or 2),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.shutdown()


app = FastAPI(title="Douyin Downloader · Web", version="1.0", lifespan=lifespan)

# 不需要登录的路径
PUBLIC = {"/", "/favicon.ico", "/api/health"}


@app.middleware("http")
async def access_guard(request: Request, call_next):
    code = str(CFG.get("access_code") or "").strip()
    if code and request.url.path not in PUBLIC:
        token = request.headers.get("x-access-code") or request.query_params.get("code") or ""
        if token.strip() != code:
            return JSONResponse(
                {"detail": "需要访问码", "need_code": True}, status_code=401
            )
    return await call_next(request)


class DownloadBody(BaseModel):
    urls: List[str] = []


class SettingsBody(BaseModel):
    path: Optional[str] = None
    quality: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    # 页面本体必须每次校验。之前没设 Cache-Control，浏览器按启发式规则
    # 自己决定缓存多久 —— 改了页面用户还看到旧版，就是这个原因。
    # 静态资源（字体/图片）走 immutable 长缓存，两套策略互不影响。
    return FileResponse(
        WEBAPP / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse({}, status_code=204)


# 字体：思源宋体子集（24MB 裁到 ~170KB，见 make_font_subset.py）
@app.get("/static/fonts/{name}")
async def font_file(name: str):
    allowed = {
        "NotoSerifSC-subset.woff2": "font/woff2",
        "NotoSerifSC-subset.ttf": "font/ttf",
    }
    # 白名单校验，挡住路径穿越
    if name not in allowed:
        return JSONResponse({"detail": "not found"}, status_code=404)
    f = WEBAPP / "static" / "fonts" / name
    if not f.is_file():
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(
        f,
        media_type=allowed[name],
        # 字体内容不变，缓存一年；换字体时改文件名
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/static/img/{name}")
async def image_file(name: str):
    """LOGO 等静态图。原图 2MB，已压成几 KB（见 make_logos.py）。"""
    allowed = {
        "logo-douyin.webp": "image/webp",
        "logo-bili.webp": "image/webp",
        "logo-douyin.png": "image/png",
        "logo-bili.png": "image/png",
    }
    if name not in allowed:
        return JSONResponse({"detail": "not found"}, status_code=404)
    f = WEBAPP / "static" / "img" / name
    if not f.is_file():
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(
        f,
        media_type=allowed[name],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# 热搜 / 搜索 / 转写 三块扩展功能另放 extras.py，路由直接挂上来
try:
    from extras import router as extras_router

    app.include_router(extras_router)
except Exception as _exc:  # noqa: BLE001
    print(f"[warn] 扩展功能未加载：{_exc}")

# 媒体串流：主机只传字节，解码交给手机的硬件解码器
try:
    from media import router as media_router

    app.include_router(media_router)
except Exception as _exc:  # noqa: BLE001
    print(f"[warn] 媒体串流未加载：{_exc}")

# yt-dlp：1750+ 站点的通用下载（抖音链接仍走专用引擎）
try:
    from yt_routes import router as yt_router

    app.include_router(yt_router)
except Exception as _exc:  # noqa: BLE001
    print(f"[warn] yt-dlp 接口未加载：{_exc}")


@app.get("/api/state")
async def state() -> Dict[str, Any]:
    path = _resolve_path(CFG["download_path"])
    return {
        "path": str(path),
        "quality": CFG["quality"],
        "thread": int(BASE_CONFIG.get("thread", 2) or 2),
        "logged_in": _logged_in(),
        "exists": path.exists(),
        "need_code": bool(str(CFG.get("access_code") or "").strip()),
        "lan_url": f"http://{LAN_IP}:{CFG['port']}/",
        "port": CFG["port"],
    }


@app.post("/api/settings")
async def update_settings(body: SettingsBody) -> Dict[str, Any]:
    if body.path is not None:
        p = _resolve_path(body.path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return JSONResponse({"detail": f"无法创建目录：{exc}"}, status_code=400)
        CFG["download_path"] = str(p)
    if body.quality:
        allowed = {
            "highest", "original", "lowest",
            "1440p", "1080p", "720p", "540p", "480p", "360p",
        }
        if body.quality not in allowed:
            return JSONResponse({"detail": "不支持的画质选项"}, status_code=400)
        CFG["quality"] = body.quality
    _save_cfg()
    return await state()


@app.post("/api/download")
async def download(body: DownloadBody) -> Dict[str, Any]:
    """接收链接并入队。

    粘贴进来的可能是整段分享文案（抖音/B站/YouTube 的「复制链接」都是这样），
    直接当 URL 用必然失败，所以先抠出真正的链接。
    """
    import url_clean

    raw = [u.strip() for u in body.urls if u and u.strip()]
    if not raw:
        return JSONResponse({"detail": "至少粘贴一个链接"}, status_code=400)

    urls: List[str] = []
    rejected: List[str] = []
    for item in raw:
        found = url_clean.extract_urls(item)
        if found:
            urls.extend(found)
        else:
            rejected.append(item)

    if not urls:
        # 一条链接都没找到：明确说清楚，别让它变成一条看不懂的失败任务
        head = (rejected[0][:60] + "…") if rejected else ""
        return JSONResponse(
            {
                "detail": f"这段内容里没有找到链接：{head}",
                "rejected": rejected[:10],
            },
            status_code=400,
        )

    # 去重但保持顺序
    seen: set[str] = set()
    uniq: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    created = []
    for url in uniq[:200]:
        job = await manager.submit(url)
        created.append({"job_id": job.job_id, "url": job.url})
    return {
        "created": created,
        "count": len(created),
        "rejected": rejected[:10],
    }


@app.get("/api/jobs")
async def jobs() -> Dict[str, Any]:
    items = await manager.list_jobs()
    items.sort(key=lambda j: j.created_at, reverse=True)
    stats = {"pending": 0, "running": 0, "success": 0, "failed": 0}
    for j in items:
        stats[j.status] = stats.get(j.status, 0) + 1
    out = []
    for j in items:
        d = j.to_dict()
        prog = manager.get_progress(j.job_id)
        if prog:
            d["progress"] = prog
        out.append(d)
    return {"jobs": out, "stats": stats, "total": len(items)}


@app.get("/api/progress")
async def progress_one(job_id: str = "") -> Dict[str, Any]:
    """真实下载进度。前端在下载期间高频轮询这个端点。

    percent 由条目计数 + 在途文件字节数算出；determinate=False 表示引擎
    还没给出条目总数（比如刚开始解析链接），此时前端走不确定态动画。
    """
    if not job_id:
        return JSONResponse({"detail": "缺少 job_id"}, status_code=400)

    job = manager._jobs.get(job_id)
    if job is None:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)

    # yt-dlp 任务：直接用它自己的快照（含标题、站点、速度、ETA）
    yp = getattr(job, "_ytdlp_progress", None)
    if yp is not None:
        snap = yp.snapshot()
        snap["engine"] = "ytdlp"
        try:
            import ytdlp_engine

            snap["site"] = yp.site or ytdlp_engine.site_label(job.url)
        except Exception:
            pass
        # 任务整体状态以 JobManager 为准（它才知道最终成败）
        snap["status"] = job.status
        if job.status == "success":
            snap["percent"] = 100.0
            snap["step"] = "完成"
        elif job.status == "failed":
            snap["step"] = "失败"
            if job.error:
                snap["detail"] = job.error
        return snap

    prog = manager.get_progress(job_id)
    if prog is None:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)
    # 补充引擎与站点，前端只做展示，不做分支逻辑
    try:
        import ytdlp_engine

        prog = dict(prog)
        prog["engine"] = ytdlp_engine.engine_for(job.url) if job.url else ""
        prog["site"] = ytdlp_engine.site_label(job.url) if job.url else ""
    except Exception:
        pass
    return prog


@app.post("/api/jobs/clear")
async def clear_jobs() -> Dict[str, Any]:
    removed = 0
    try:
        for jid, job in list(manager._jobs.items()):
            if job.status in JobStatus.TERMINAL:
                manager._jobs.pop(jid, None)
                removed += 1
    except Exception as exc:
        return JSONResponse({"detail": f"清除失败：{exc}"}, status_code=500)
    return {"removed": removed}


@app.post("/api/open-folder")
async def open_folder() -> Dict[str, Any]:
    """只在主机上打开资源管理器，手机端点了也只影响主机。"""
    p = _resolve_path(CFG["download_path"])
    try:
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{p}"')
        else:
            os.system(f'xdg-open "{p}"')
    except Exception as exc:
        return JSONResponse({"detail": f"打开失败：{exc}"}, status_code=500)
    return {"ok": True, "path": str(p)}


def main() -> None:
    port = int(os.getenv("WEB_PORT", CFG.get("port", 8790)))
    CFG["port"] = port
    _resolve_path(CFG["download_path"]).mkdir(parents=True, exist_ok=True)
    banner = (
        "\n" + "=" * 56 + "\n"
        "  抖音下载器 · 网页版已启动\n"
        f"  本机访问：  http://127.0.0.1:{port}/\n"
        f"  其他设备：  http://{LAN_IP}:{port}/   （同一局域网）\n"
        + ("  访问码：    已开启\n" if CFG.get("access_code") else "  访问码：    未设置（仅建议局域网使用）\n")
        + "  关闭服务：  直接关掉这个窗口\n" + "=" * 56 + "\n"
    )
    print(banner)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
