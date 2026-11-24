"""媒体串流：让手机浏览器直接播放主机上的视频。

分工是：**主机只传字节，解码在手机上做**（手机的硬件解码器）。

所以能不能播，只由两件事决定：
1. **编码格式** —— H.264 基本通吃；HEVC/H.265 看设备和浏览器；AV1 只有新设备能放
2. **服务器支不支持 Range** —— iOS Safari 播 <video> 必须收到 206 分段响应，
   不支持就是黑屏（不是加载慢，是根本不播）

本模块只做这两件事，**不转码**（转码很吃 CPU，和"解码在手机上"的目标相反）。
真要转换封装，走 /api/media/prepare，用 ffmpeg 的 -c copy 只搬流不重编码。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter()

CHUNK = 512 * 1024          # 每次读 512KB，太大占内存，太小切片开销高
MOOV_CAP = 24 * 1024 * 1024  # moov 一般几百 KB，24MB 足够兜住异常文件

VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv", ".ts", ".m4a", ".mp3", ".aac", ".wav"}

# 编码 fourcc → (可读名, 手机能不能放)
CODEC_INFO: Dict[bytes, Tuple[str, str, str]] = {
    b"avc1": ("H.264 / AVC", "yes", "所有手机浏览器都能放"),
    b"avc3": ("H.264 / AVC", "yes", "所有手机浏览器都能放"),
    b"hvc1": ("HEVC / H.265", "maybe", "iPhone 能放；安卓和桌面看浏览器，Chrome 多数不行"),
    b"hev1": ("HEVC / H.265", "maybe", "iPhone 能放；安卓和桌面看浏览器，Chrome 多数不行"),
    b"av01": ("AV1", "maybe", "只有较新的设备和浏览器能放"),
    b"vp09": ("VP9", "maybe", "安卓 Chrome 可以，iOS 多数不行"),
    b"vp08": ("VP8", "no", "手机浏览器普遍不支持"),
    b"mp4a": ("AAC 音频", "yes", "通用"),
    b"ac-3": ("AC-3 音频", "no", "手机浏览器不支持"),
    b"ec-3": ("E-AC-3 音频", "no", "手机浏览器不支持"),
}


def _download_root() -> Path:
    from extras import _download_root as root

    return root()


def _resolve_media(raw: str) -> Optional[Path]:
    """把请求里的 path 解析成真实文件，并挡住目录穿越。"""
    if not raw:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"'))))
    if not p.is_absolute():
        p = _download_root() / p
    try:
        p = p.resolve()
    except Exception:
        return None
    root = _download_root().resolve()
    # 只允许下载目录里面的文件，防止 ../ 读到别处
    try:
        p.relative_to(root)
    except Exception:
        return None
    if not p.is_file():
        return None
    if p.suffix.lower() not in VIDEO_EXT:
        return None
    return p


# ────────────────────── MP4 结构探测 ──────────────────────

def _iter_boxes(f, start: int, end: int) -> Iterator[Tuple[bytes, int, int, int]]:
    """遍历同级 box，返回 (type, 起点, 总长, 头部长度)。"""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return
        size = int.from_bytes(hdr[0:4], "big")
        typ = hdr[4:8]
        hsize = 8
        if size == 1:                      # 64 位长度
            if len(hdr) < 16:
                return
            size = int.from_bytes(hdr[8:16], "big")
            hsize = 16
        elif size == 0:                    # 直到文件尾
            size = end - pos
        if size < hsize or pos + size > end:
            return
        yield typ, pos, size, hsize
        pos += size


def _probe_mp4(path: Path) -> Dict[str, Any]:
    """读 box 结构拿编码、时长、是否 faststart。不依赖 ffprobe。"""
    info: Dict[str, Any] = {
        "codec": "",
        "codec_family": "",
        "compatible": "maybe",
        "note": "",
        "duration": 0.0,
        "faststart": False,
        "audio": "",
    }
    try:
        size = path.stat().st_size
        moov_pos = None
        moov_size = 0
        mdat_pos = None
        with open(path, "rb") as f:
            for typ, pos, sz, _h in _iter_boxes(f, 0, size):
                if typ == b"moov" and moov_pos is None:
                    moov_pos, moov_size = pos, sz
                elif typ == b"mdat" and mdat_pos is None:
                    mdat_pos = pos
                if moov_pos is not None and mdat_pos is not None:
                    break

            if moov_pos is None:
                return info
            # moov 在 mdat 前面 = faststart，Safari 边下边播才顺
            info["faststart"] = mdat_pos is None or moov_pos < mdat_pos

            read_len = min(moov_size, MOOV_CAP)
            f.seek(moov_pos)
            blob = f.read(read_len)

        video_fourcc = b""
        audio_fourcc = b""
        for cc in CODEC_INFO:
            idx = blob.find(cc)
            if idx < 0:
                continue
            is_audio = cc in (b"mp4a", b"ac-3", b"ec-3")
            if is_audio and not audio_fourcc:
                audio_fourcc = cc
            elif not is_audio and not video_fourcc:
                video_fourcc = cc

        if video_fourcc:
            name, compat, note = CODEC_INFO[video_fourcc]
            info["codec"] = name
            info["codec_family"] = video_fourcc.decode("ascii", "replace")
            info["compatible"] = compat
            info["note"] = note
        if audio_fourcc:
            info["audio"] = CODEC_INFO[audio_fourcc][0]

        # 时长：mvhd 里取 timescale / duration
        mv = blob.find(b"mvhd")
        if mv >= 0:
            try:
                ver = blob[mv + 4]
                if ver == 1:
                    ts = int.from_bytes(blob[mv + 24:mv + 28], "big")
                    du = int.from_bytes(blob[mv + 28:mv + 36], "big")
                else:
                    ts = int.from_bytes(blob[mv + 16:mv + 20], "big")
                    du = int.from_bytes(blob[mv + 20:mv + 24], "big")
                if ts:
                    info["duration"] = round(du / ts, 1)
            except Exception:
                pass
    except Exception:
        pass
    return info


# ────────────────────── 路由 ──────────────────────

def _parse_range(header: str, total: int) -> Optional[Tuple[int, int]]:
    """解析 Range 头，返回闭区间 (start, end)；不合法返回 None。"""
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header[6:].strip()
    if "," in spec:                      # 多段请求，只取第一段（够用且不会出错）
        spec = spec.split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    a, b = a.strip(), b.strip()
    try:
        if not a:                        # 后缀范围：bytes=-500 表示最后 500 字节
            n = int(b)
            if n <= 0:
                return None
            start = max(0, total - n)
            end = total - 1
        else:
            start = int(a)
            end = int(b) if b else total - 1
    except Exception:
        return None
    if start > end or start >= total:
        return None
    return start, min(end, total - 1)


@router.api_route("/api/media", methods=["GET", "HEAD"])
async def media(request: Request, path: str = ""):
    """流式送出媒体文件，支持 Range。

    iOS Safari 必须有 206；Android Chrome 也靠它做进度条拖动。
    """
    f_path = _resolve_media(path)
    if f_path is None:
        return JSONResponse({"detail": "文件不存在或不在下载目录内"}, status_code=404)

    total = f_path.stat().st_size
    ctype = mimetypes.guess_type(str(f_path))[0] or "application/octet-stream"
    # mp4 的 mime 用明确的，别让个别浏览器猜错
    if f_path.suffix.lower() in (".mp4", ".m4v"):
        ctype = "video/mp4"
    elif f_path.suffix.lower() == ".webm":
        ctype = "video/webm"

    rng = _parse_range(request.headers.get("range", ""), total)
    headers_base = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        # 允许局域网里任何一个前端页面跨端拉取
        "Access-Control-Allow-Origin": "*",
    }

    if request.method == "HEAD":
        length = (rng[1] - rng[0] + 1) if rng else total
        h = dict(headers_base)
        h["Content-Length"] = str(length)
        h["Content-Type"] = ctype
        if rng:
            h["Content-Range"] = f"bytes {rng[0]}-{rng[1]}/{total}"
        return Response(status_code=206 if rng else 200, headers=h)

    if rng is None:
        # 没有 Range 或者是非法 Range
        if request.headers.get("range"):
            # 明确回 416，让浏览器知道该怎么重来
            return Response(
                status_code=416,
                headers={**headers_base, "Content-Range": f"bytes */{total}"},
            )
        start, end = 0, total - 1
        status = 200
    else:
        start, end = rng
        status = 206

    length = end - start + 1
    headers = dict(headers_base)
    headers["Content-Length"] = str(length)
    headers["Content-Type"] = ctype
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    def stream() -> Iterator[bytes]:
        with open(f_path, "rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                data = fh.read(min(CHUNK, remain))
                if not data:
                    break
                remain -= len(data)
                yield data

    return StreamingResponse(stream(), status_code=status, headers=headers, media_type=ctype)


@router.get("/api/media/info")
async def media_info(path: str = "") -> Dict[str, Any]:
    """播放前先问一下：什么编码、多久、手机放不放得了。"""
    f_path = _resolve_media(path)
    if f_path is None:
        return JSONResponse({"detail": "文件不存在或不在下载目录内"}, status_code=404)

    st = f_path.stat()
    info = _probe_mp4(f_path)
    info.update(
        {
            "path": str(f_path),
            "name": f_path.name,
            "size_mb": round(st.st_size / 1048576, 1),
            "suffix": f_path.suffix.lower(),
            "url": f"/api/media?path={_q(str(f_path))}",
        }
    )
    return info


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


# ────────────────────── 可选：换封装（不重编码） ──────────────────────

_REMUX_LOCK = threading.Lock()
_REMUXING: Dict[str, bool] = {}


def _ffmpeg() -> Optional[str]:
    from extras import _find_ffmpeg

    return _find_ffmpeg()


@router.get("/api/media/prepare")
async def media_prepare(path: str = "") -> Dict[str, Any]:
    """把放不了的文件换成 Safari 友好的封装：-c copy 只搬流，不重新编码。

    用于两种情况：
    - moov 在文件尾（非 faststart），iPhone 上要等整个文件才敢播
    - 容器不被浏览器认（mkv / flv）→ 换成 mp4
    产物缓存在 downloads/_webplay/，同名直接复用。
    """
    f_path = _resolve_media(path)
    if f_path is None:
        return JSONResponse({"detail": "文件不存在或不在下载目录内"}, status_code=404)

    out_dir = _download_root() / "_webplay"
    out = out_dir / (f_path.stem + ".mp4")
    if out.exists() and out.stat().st_mtime >= f_path.stat().st_mtime:
        return {"ok": True, "cached": True, "path": str(out), "url": f"/api/media?path={_q(str(out))}"}

    ff = _ffmpeg()
    if not ff:
        return JSONResponse({"detail": "找不到 ffmpeg"}, status_code=500)

    key = str(f_path)
    with _REMUX_LOCK:
        if _REMUXING.get(key):
            return {"ok": False, "busy": True, "detail": "这个文件正在处理中"}
        _REMUXING[key] = True
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            ff, "-y", "-i", str(f_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not out.exists():
            return JSONResponse(
                {"detail": "换封装失败：" + (r.stderr or "")[-400:]}, status_code=500
            )
    except Exception as exc:
        return JSONResponse({"detail": f"换封装出错：{exc}"}, status_code=500)
    finally:
        with _REMUX_LOCK:
            _REMUXING.pop(key, None)

    return {"ok": True, "cached": False, "path": str(out), "url": f"/api/media?path={_q(str(out))}"}


@router.get("/api/media/transcode")
async def media_transcode(path: str = "") -> Dict[str, Any]:
    """真正转码成 H.264 —— 解决「换封装也没用」的编码问题。

    和 /prepare 的区别（很重要）：
    - prepare：`-c copy` 只换容器，0.5 秒，画质零损失。但 **HEVC 转完还是 HEVC**，
      容器变了编码没变，安卓 Chrome 照样放不了。
    - transcode：重新编码成 H.264，任何手机都能放，但**吃 CPU、按片长耗时**，
      画质有轻微损失。

    所以先试 prepare，prepare 之后编码还是手机不认的，才走这里。
    """
    f_path = _resolve_media(path)
    if f_path is None:
        return JSONResponse({"detail": "文件不存在或不在下载目录内"}, status_code=404)

    out_dir = _download_root() / "_webplay"
    out = out_dir / (f_path.stem + ".h264.mp4")
    if out.exists() and out.stat().st_mtime >= f_path.stat().st_mtime:
        return {"ok": True, "cached": True, "path": str(out), "url": f"/api/media?path={_q(str(out))}"}

    ff = _ffmpeg()
    if not ff:
        return JSONResponse({"detail": "找不到 ffmpeg"}, status_code=500)

    key = "tc:" + str(f_path)
    with _REMUX_LOCK:
        if _REMUXING.get(key):
            return {"ok": False, "busy": True, "detail": "这个文件正在转码中"}
        _REMUXING[key] = True
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            ff, "-y", "-i", str(f_path),
            # H.264 + yuv420p 是手机兼容性最高的组合
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            # 音频统一转 AAC，AC-3 / E-AC-3 手机都不认
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            # 让高度/宽度都能被 2 整除，个别解码器对奇数尺寸挑剔
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-movflags", "+faststart",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not out.exists():
            return JSONResponse(
                {"detail": "转码失败：" + (r.stderr or "")[-400:]}, status_code=500
            )
    except Exception as exc:
        return JSONResponse({"detail": f"转码出错：{exc}"}, status_code=500)
    finally:
        with _REMUX_LOCK:
            _REMUXING.pop(key, None)

    return {"ok": True, "cached": False, "path": str(out), "url": f"/api/media?path={_q(str(out))}"}
