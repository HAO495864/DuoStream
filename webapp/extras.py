"""抖音下载器 · 网页版扩展功能

补上网页之前没有的几块（原项目只有 CLI 能做）：
- 热搜榜     GET  /api/hot            不需要登录
- 关键词搜索 POST /api/search         需要主机先扫码登录（抖音接口返回 2483）
- 视频转写   POST /api/transcribe     后台线程跑 Whisper，前端轮询状态
             GET  /api/transcribe/status
             GET  /api/videos          列出下载目录里的视频
             GET  /api/transcript      读一份已生成的转写文本

设计约束：
- 不动 engine/ 里任何文件，全部逻辑放在网页层
- 占位 Cookie（YOUR_ 开头）不塞进 CookieManager，避免覆盖磁盘上真实 .cookies.json
- Whisper 是同步阻塞 + 吃显存，放独立线程跑，主服务不卡
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent          # D:\DouyinWeb
ENGINE = ROOT / "engine"
ENGINE_CONFIG = ENGINE / "config.yml"

router = APIRouter()


# ────────────────────────── 公共小工具 ──────────────────────────

def _is_placeholder(value: Any) -> bool:
    s = str(value or "").strip()
    return (not s) or s.upper().startswith("YOUR_") or s.lower() in {"none", "null"}


def _cookies_real(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    return any(isinstance(v, str) and not _is_placeholder(v) for v in data.values())


def _load_engine_config():
    from config import ConfigLoader

    return ConfigLoader(str(ENGINE_CONFIG))


def _make_cookie_manager(cfg):
    """真实 Cookie 优先；配置里是占位值就退回磁盘上的 .cookies.json。"""
    from auth import CookieManager

    cm = CookieManager(cookie_file=str(ENGINE / ".cookies.json"))
    cfg_cookies = cfg.get("cookies")
    if _cookies_real(cfg_cookies):
        cm.set_cookies(cfg_cookies)
    else:
        cm.get_cookies()          # 触发从磁盘加载
    return cm


def _logged_in() -> bool:
    f = ENGINE / ".cookies.json"
    try:
        import json

        if f.exists() and _cookies_real(json.loads(f.read_text(encoding="utf-8"))):
            return True
    except Exception:
        pass
    try:
        return _cookies_real(_load_engine_config().get("cookies"))
    except Exception:
        return False


def _download_root() -> Path:
    """跟 web_server 用同一个下载目录（config.json 里的 download_path）。"""
    import json

    p = ROOT / "downloads"
    try:
        data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        raw = str(data.get("download_path") or "").strip()
        if raw:
            p = Path(os.path.expandvars(os.path.expanduser(raw)))
            if not p.is_absolute():
                p = ROOT / p
    except Exception:
        pass
    return p


# ────────────────────────── 热搜榜 ──────────────────────────

@router.get("/api/hot")
async def hot(limit: int = 50) -> Dict[str, Any]:
    from core.api_client import DouyinAPIClient

    cfg = _load_engine_config()
    cm = _make_cookie_manager(cfg)
    try:
        async with DouyinAPIClient(cm.get_cookies(), proxy=cfg.get("proxy")) as api:
            page = await api.get_hot_search_board()
    except Exception as exc:
        return JSONResponse(
            {"detail": f"热搜获取失败：{exc}", "items": []}, status_code=502
        )

    items = list(page.get("items") or [])
    # 置顶条目（sentence_id 有、hot_value=0）排在真正榜单前面，别让它顶掉第 1 名
    items.sort(key=lambda x: (1 if not (x.get("hot_value") or 0) else 0,
                              -(x.get("hot_value") or 0)))
    if limit and limit > 0:
        items = items[:limit]

    out: List[Dict[str, Any]] = []
    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        if not (it.get("word") or "").strip():
            continue
        cover = ""
        wc = it.get("word_cover") or {}
        urls = wc.get("url_list") or []
        if urls:
            cover = urls[0]
        out.append(
            {
                "rank": idx,
                "word": it.get("word") or "",
                "hot_value": it.get("hot_value") or 0,
                "view_count": it.get("view_count") or 0,
                "video_count": it.get("video_count") or 0,
                "label": it.get("label"),
                "cover": cover,
                "sentence_id": it.get("sentence_id") or "",
                "search_url": "https://www.douyin.com/search/"
                + str(it.get("word") or "").replace(" ", "%20"),
            }
        )
    return {"items": out, "count": len(out), "fetched_at": int(time.time())}


# ────────────────────────── 关键词搜索 ──────────────────────────

class SearchBody(BaseModel):
    keyword: str = ""
    max_items: int = 20


def _fmt_count(n: Any) -> str:
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


@router.post("/api/search")
async def search(body: SearchBody) -> Dict[str, Any]:
    from core.api_client import DouyinAPIClient, LoginRequiredError

    keyword = (body.keyword or "").strip()
    if not keyword:
        return JSONResponse({"detail": "先输入关键词"}, status_code=400)

    max_items = max(1, min(int(body.max_items or 20), 100))
    cfg = _load_engine_config()
    cm = _make_cookie_manager(cfg)

    if not _cookies_real(cm.get_cookies()):
        return JSONResponse(
            {
                "detail": "搜索需要登录：在主机上双击 D:\\DouyinWeb\\登录抖音.bat 扫码，再回来搜。",
                "need_login": True,
                "items": [],
            },
            status_code=401,
        )

    collected: List[Dict[str, Any]] = []
    seen: set = set()
    offset = 0
    page_size = 10
    try:
        async with DouyinAPIClient(cm.get_cookies(), proxy=cfg.get("proxy")) as api:
            while len(collected) < max_items:
                page = await api.search_aweme(keyword, offset=offset, count=page_size)
                items = page.get("items") or []
                if not items:
                    break
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    aid = str(it.get("aweme_id") or "")
                    if aid and aid in seen:
                        continue
                    if aid:
                        seen.add(aid)
                    author = it.get("author") or {}
                    stats = it.get("statistics") or {}
                    video = it.get("video") or {}
                    cover = ""
                    cv = video.get("cover") or video.get("origin_cover") or {}
                    cu = cv.get("url_list") or []
                    if cu:
                        cover = cu[0]
                    collected.append(
                        {
                            "aweme_id": aid,
                            "desc": str(it.get("desc") or "").strip(),
                            "author": author.get("nickname") or "",
                            "cover": cover,
                            "digg": _fmt_count(stats.get("digg_count")),
                            "comment": _fmt_count(stats.get("comment_count")),
                            "share": _fmt_count(stats.get("share_count")),
                            "duration": int((video.get("duration") or 0) // 1000),
                            "url": f"https://www.douyin.com/video/{aid}" if aid else "",
                        }
                    )
                    if len(collected) >= max_items:
                        break
                if len(collected) >= max_items:
                    break
                if not page.get("has_more"):
                    break
                nxt = int(page.get("max_cursor") or 0)
                if nxt == offset:
                    break
                offset = nxt
    except LoginRequiredError as exc:
        return JSONResponse(
            {
                "detail": f"登录已失效，请重新扫码：{exc}",
                "need_login": True,
                "items": collected,
            },
            status_code=401,
        )
    except Exception as exc:
        return JSONResponse(
            {"detail": f"搜索失败：{exc}", "items": collected}, status_code=502
        )

    return {"keyword": keyword, "items": collected, "count": len(collected)}


# ────────────────────────── 转写（Whisper） ──────────────────────────

_TRANSCRIBE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()

TRANSCRIPT_DIR_NAME = "transcripts"


def _find_ffmpeg() -> Optional[str]:
    p = shutil.which("ffmpeg")
    if p:
        return p
    local = ENGINE / "ffmpeg.exe"
    if local.exists():
        return str(local)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _safe_stem(stem: str) -> str:
    try:
        from utils.validators import sanitize_filename

        return sanitize_filename(stem, max_length=150)
    except Exception:
        bad = '<>:"/\\|?*\r\n\t'
        out = "".join("_" if c in bad else c for c in stem).strip()
        return (out or "video")[:150]


def _extract_audio(video: Path, audio: Path, ffmpeg: str) -> bool:
    cmd = [
        ffmpeg, "-i", str(video), "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio), "-y", "-loglevel", "error",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception:
        return False
    return r.returncode == 0 and audio.exists()


def _srt_time(seconds: float) -> str:
    h, r = divmod(float(seconds), 3600)
    m, r = divmod(r, 60)
    s = int(r)
    ms = int(round((r - s) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{int(h):02d}:{int(m):02d}:{s:02d},{ms:03d}"


def _get_model(name: str):
    with _MODEL_LOCK:
        if name in _MODEL_CACHE:
            return _MODEL_CACHE[name]
        import whisper

        # 模型缓存放 D 盘（用户要求别往 C 盘塞东西）。
        # whisper.load_model(name, download_root=...) 是官方支持的参数，
        # 已经存在的文件直接复用，不会重复下载。
        root = Path(os.getenv("WHISPER_MODEL_DIR") or (ROOT / "whisper-models"))
        root.mkdir(parents=True, exist_ok=True)
        model = whisper.load_model(name, download_root=str(root))
        _MODEL_CACHE[name] = model
        return model


def _set(task_id: str, **kw: Any) -> None:
    with _LOCK:
        t = _TRANSCRIBE.get(task_id)
        if t is not None:
            t.update(kw)
            t["updated_at"] = time.time()


def _run_transcribe(
    task_id: str,
    video: Path,
    model_name: str,
    language: str,
    want_srt: bool,
    out_dir: Path,
) -> None:
    tmpdir = None
    try:
        _set(task_id, status="loading", message=f"加载模型 {model_name}")
        model = _get_model(model_name)
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，装一下 imageio-ffmpeg 或把 ffmpeg.exe 放进 engine")

        _set(task_id, status="extracting", message="提取音频")
        tmpdir = tempfile.mkdtemp(prefix="dyweb_whisper_")
        audio = Path(tmpdir) / "audio.wav"
        if not _extract_audio(video, audio, ffmpeg):
            raise RuntimeError("音频提取失败（文件损坏或没有音轨）")

        _set(task_id, status="transcribing", message="Whisper 识别中")
        result = model.transcribe(str(audio), language=language, verbose=False)
        segments = result.get("segments") or []
        if not segments:
            raise RuntimeError("没识别到语音内容")

        stem = _safe_stem(video.stem)
        out_dir.mkdir(parents=True, exist_ok=True)
        txt_path = out_dir / f"{stem}.transcript.txt"
        srt_path = out_dir / f"{stem}.transcript.srt"
        lines = [seg["text"].strip() for seg in segments if seg.get("text", "").strip()]
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        saved = [txt_path.name]
        if want_srt:
            srt_lines = []
            for i, seg in enumerate(segments, 1):
                text = seg.get("text", "").strip()
                if not text:
                    continue
                srt_lines.append(
                    f"{i}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{text}\n"
                )
            srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
            saved.append(srt_path.name)

        _set(
            task_id,
            status="success",
            message=f"完成：{len(segments)} 段，{len(''.join(lines))} 字",
            txt=str(txt_path),
            srt=str(srt_path) if want_srt else "",
            segments=len(segments),
            chars=len("".join(lines)),
            detected_language=str(result.get("language") or language),
            saved=saved,
        )
    except Exception as exc:
        _set(task_id, status="failed", message=str(exc), trace=traceback.format_exc()[-1500:])
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TranscribeBody(BaseModel):
    path: str = ""
    model: str = "small"
    language: str = "zh"
    srt: bool = True


def _size_text(n: int) -> str:
    """人话的文件大小：小文件别显示成 0 MB。"""
    try:
        n = int(n)
    except Exception:
        return "0 B"
    if n >= 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024 / 1024:.2f} GB"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


@router.get("/api/videos")
async def videos(limit: int = 100) -> Dict[str, Any]:
    root = _download_root()
    tdir = root / TRANSCRIPT_DIR_NAME
    out: List[Dict[str, Any]] = []
    if root.exists():
        # 不只 mp4：mkv / flv / webm 也算，能不能播由前端按容器和编码判断
        exts = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv")
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        # 换封装产物目录不算源视频，避免列表里出现重复条目
        files = [p for p in files if "_webplay" not in p.parts]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[: max(1, min(limit, 500))]:
            try:
                st = p.stat()
            except Exception:
                continue
            stem = _safe_stem(p.stem)
            # 文字稿可能落在视频同级目录，也可能统一在 downloads/transcripts/
            found_txt = ""
            for cand in (p.parent / f"{stem}.transcript.txt", tdir / f"{stem}.transcript.txt"):
                if cand.exists():
                    found_txt = str(cand)
                    break
            # 播放地址与编码可播性：手机上能不能放，前端据此给提示
            play, codec, compat, note, dur = "", "", "maybe", "", 0.0
            try:
                from media import _probe_mp4, _q
                from urllib.parse import quote

                play = "/api/media?path=" + quote(str(p), safe="")
                if p.suffix.lower() in (".mp4", ".m4v", ".mov"):
                    probe = _probe_mp4(p)
                    codec = probe.get("codec") or ""
                    compat = probe.get("compatible") or "maybe"
                    note = probe.get("note") or ""
                    dur = probe.get("duration") or 0.0
                else:
                    compat = "no"
                    note = "容器不是 MP4，手机浏览器不认，需要转换封装"
            except Exception:
                pass
            out.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "rel": str(p.relative_to(root)),
                    "size_mb": round(st.st_size / 1024 / 1024, 2),
                    "size_bytes": st.st_size,
                    "size_text": _size_text(st.st_size),
                    "mtime": int(st.st_mtime),
                    "transcript": found_txt,
                    "url": play,
                    "codec": codec,
                    "compatible": compat,
                    "note": note,
                    "duration": dur,
                    "suffix": p.suffix.lower(),
                }
            )
    return {"items": out, "count": len(out), "root": str(root)}


@router.post("/api/transcribe")
async def transcribe(body: TranscribeBody) -> Dict[str, Any]:
    raw = (body.path or "").strip()
    if not raw:
        return JSONResponse({"detail": "先选一个视频"}, status_code=400)
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = _download_root() / p
    if not p.exists() or not p.is_file():
        return JSONResponse({"detail": f"文件不存在：{p}"}, status_code=400)
    if p.suffix.lower() not in {".mp4", ".mov", ".mkv", ".flv", ".m4a", ".mp3", ".wav"}:
        return JSONResponse({"detail": "只支持视频/音频文件"}, status_code=400)

    model_name = (body.model or "small").strip()
    if model_name not in {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"}:
        model_name = "small"
    language = (body.language or "zh").strip() or "zh"

    task_id = f"tr_{int(time.time() * 1000)}_{abs(hash(str(p))) % 10000}"
    with _LOCK:
        _TRANSCRIBE[task_id] = {
            "id": task_id,
            "path": str(p),
            "name": p.name,
            "model": model_name,
            "language": language,
            "srt": bool(body.srt),
            "status": "queued",
            "message": "排队中",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    out_dir = _download_root() / TRANSCRIPT_DIR_NAME
    threading.Thread(
        target=_run_transcribe,
        args=(task_id, p, model_name, language, bool(body.srt), out_dir),
        daemon=True,
    ).start()
    return {"task_id": task_id, "status": "queued"}


@router.get("/api/transcribe/status")
async def transcribe_status(task_id: str = "") -> Dict[str, Any]:
    with _LOCK:
        if task_id:
            t = _TRANSCRIBE.get(task_id)
            if not t:
                return JSONResponse({"detail": "任务不存在"}, status_code=404)
            return dict(t)
        items = sorted(_TRANSCRIBE.values(), key=lambda x: x["created_at"], reverse=True)
        return {"items": [dict(i) for i in items[:50]], "count": len(items)}


@router.get("/api/transcript")
async def transcript(path: str = "") -> Dict[str, Any]:
    if not path:
        return JSONResponse({"detail": "缺少 path"}, status_code=400)
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_absolute():
        p = _download_root() / p
    if not p.exists():
        return JSONResponse({"detail": "文件不存在"}, status_code=404)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return JSONResponse({"detail": f"读取失败：{exc}"}, status_code=500)
    return {"path": str(p), "text": text, "chars": len(text)}
