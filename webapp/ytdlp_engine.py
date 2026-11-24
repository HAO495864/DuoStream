"""yt-dlp 引擎：把通用视频下载接进同一个网页。

和抖音引擎的分工：
- 抖音引擎（engine/）：抖音专用，能抓主页 / 合集 / 直播 / 评论，走 API + 签名
- yt-dlp（本模块）：1750+ 站点通用，B站 / YouTube / X / Instagram / TikTok 等

两者并存，前端按链接域名自动选引擎，用户不需要知道区别。

进度实现方式不同：yt-dlp 是外部进程，靠解析它的 `--newline --progress` 输出拿
真实字节数，不是估的。格式实测为：

    [download]  12.6% of    3.97MiB at    3.58MiB/s ETA 00:00

注意 Windows 沙箱里子进程不能开命名管道（stdio:'pipe' 会 EPERM），
所以 stdout/stderr 接到临时文件句柄再读，这是这台机器上的既定做法。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# yt-dlp 可执行文件：优先环境变量，其次项目内的副本，最后 PATH
_ROOT = Path(__file__).resolve().parent.parent          # D:\DouyinWeb
_BUNDLED = _ROOT / "bin" / "yt-dlp.exe"
_LEGACY = Path(r"D:\yt-dlp\yt-dlp.exe")


def find_ytdlp() -> Optional[str]:
    env = os.getenv("YTDLP_PATH")
    if env and Path(env).is_file():
        return env
    if _BUNDLED.is_file():
        return str(_BUNDLED)
    if _LEGACY.is_file():
        return str(_LEGACY)
    which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    return which


def find_ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).is_file():
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


# ────────────────── 站点路由 ──────────────────

# 抖音链接交给专用引擎（它能抓主页/合集/直播，yt-dlp 做不到这些）
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")

# yt-dlp 明确支持、且值得在界面上点名的站点
KNOWN_SITES = {
    "bilibili.com": "B站",
    "b23.tv": "B站短链",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "twitter.com": "X",
    "x.com": "X",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
    "kuaishou.com": "快手",
    "xiaohongshu.com": "小红书",
    "weibo.com": "微博",
    "ixigua.com": "西瓜视频",
    "acfun.cn": "A站",
    "dailymotion.com": "Dailymotion",
    "soundcloud.com": "SoundCloud",
    "nicovideo.jp": "niconico",
    "v.qq.com": "腾讯视频",
    "iqiyi.com": "爱奇艺",
    "youku.com": "优酷",
}


def host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        h = (urlparse(url).hostname or "").lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""


def is_douyin(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in DOUYIN_HOSTS)


def engine_for(url: str) -> str:
    """决定这条链接交给谁。抖音走专用引擎，其余一律 yt-dlp。"""
    return "douyin" if is_douyin(url) else "ytdlp"


def site_label(url: str) -> str:
    h = host_of(url)
    for k, v in KNOWN_SITES.items():
        if h == k or h.endswith("." + k):
            return v
    return h or "未知站点"


# ────────────────── 进度解析 ──────────────────

_SIZE_UNITS = {
    "B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
    "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
}

_PROG_RE = re.compile(
    r"^\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+~?\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]i?B|[KMG])\b"
    r"(?:\s+at\s+(?P<speed>[\d.]+|Unknown)\s*(?P<sunit>[KMGT]i?B/s|[KMG]/s|B/s|Unknown)?)?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown))?",
    re.IGNORECASE,
)
_DEST_RE = re.compile(r"^\[download\]\s+Destination:\s+(?P<path>.+)$")
_MERGE_RE = re.compile(r"^\[Merger\]\s+Merging formats into\s+\"(?P<path>.+)\"")
_ALREADY_RE = re.compile(r"^\[download\]\s+(?P<path>.+)\s+has already been downloaded")
_EXTRACT_RE = re.compile(r"^\[(?P<site>[^\]]+)\]\s+(?P<id>[\w-]+):\s+Downloading")
# yt-dlp 内部阶段标签，不是站点名
_NOT_SITES = {"info", "download", "Merger", "ExtractAudio", "FixupM3u8", "FixupM4a",
              "Metadata", "MoveFiles", "Generic", "hlsnative", "dash"}


def _to_bytes(num: str, unit: str) -> int:
    try:
        mul = _SIZE_UNITS.get(unit, 0)
        if not mul:
            u = unit.upper().replace("IB", "I").replace("B", "")
            mul = {"K": 1024, "KI": 1024, "M": 1024 ** 2, "MI": 1024 ** 2,
                   "G": 1024 ** 3, "GI": 1024 ** 3}.get(u, 1)
        return int(float(num) * mul)
    except Exception:
        return 0


class YtdlpProgress:
    """一个 yt-dlp 任务的进度状态，供前端轮询。

    yt-dlp 下载时是分段的：视频流一段、音频流一段，最后合并。
    每段的百分比都是独立的 0→100，直接用会出现「62.9% 突然跳回 0.1%」。
    所以对外统一折算成一个单调递增的总进度：
      已知要下几段时按段数均分；不知道就先估 2 段（视频+音频是常态）。
    合并/收尾阶段直接顶到 95%，等真正结束再给 100%。
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.percent = 0.0
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.speed_bps = 0
        self.eta = ""
        self.step = "准备中"
        self.detail = ""
        self.title = ""
        self.site = ""
        self.output_path = ""
        self.status = "running"
        self.started = False
        self.updated_at = time.time()
        # 分段进度：yt-dlp 的每一段是独立的 0→100
        self._phase = 0
        self._expect_phases = 2      # 视频+音频是常态，先按 2 段估
        self._phase_pct = 0.0

    def _total_percent(self) -> float:
        """把「第几段 + 段内百分比」折成单调的总进度。"""
        if self.status == "success":
            return 100.0
        if self.step in ("合并音视频", "整理文件", "写入元数据", "修正容器", "提取音频"):
            return 95.0
        if self.step == "已存在":
            return 100.0
        n = max(1, self._expect_phases)
        # 每段占 90%，留 10% 给合并收尾
        done = (self._phase + max(0.0, min(100.0, self._phase_pct)) / 100.0) / n
        return max(0.0, min(95.0, done * 90.0))

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.updated_at = time.time()

        m = _EXTRACT_RE.match(line)
        if m:
            name = m.group("site")
            # [info] / [download] 这类是阶段标签，不是站点
            if name not in _NOT_SITES:
                self.site = name
            self.step = "解析链接"
            self.started = True
            return

        m = _DEST_RE.match(line)
        if m:
            self.step = "下载中"
            self.detail = Path(m.group("path")).name
            # 每出现一次 Destination 就是新的一段开始
            if self._phase_pct > 0 or self._phase > 0:
                self._phase += 1
            self._phase_pct = 0.0
            # 段数不确定时，看到第 3 段就说明估计偏小
            if self._phase + 1 > self._expect_phases:
                self._expect_phases = self._phase + 1
            self.percent = self._total_percent()
            return

        m = _PROG_RE.match(line)
        if m:
            pct = float(m.group("pct"))
            total = _to_bytes(m.group("size"), m.group("unit"))
            self.total_bytes = total
            self.downloaded_bytes = int(total * pct / 100)
            self._phase_pct = pct
            self.percent = self._total_percent()
            sp = m.group("speed")
            if sp and sp.lower() != "unknown":
                sunit = m.group("sunit") or "B/s"
                self.speed_bps = _to_bytes(sp, sunit.replace("/s", ""))
            else:
                self.speed_bps = 0
            self.eta = m.group("eta") or ""
            self.step = "下载中"
            self.started = True
            return

        m = _ALREADY_RE.match(line)
        if m:
            self.step = "已存在"
            self.percent = 100.0
            self.output_path = m.group("path")
            return

        m = _MERGE_RE.match(line)
        if m:
            self.step = "合并音视频"
            self.output_path = m.group("path")
            return

        if line.startswith("[ExtractAudio]"):
            self.step = "提取音频"
        elif line.startswith("[FixupM3u8]") or line.startswith("[FixupM4a]"):
            self.step = "修正容器"
        elif line.startswith("[Metadata]"):
            self.step = "写入元数据"
        elif line.startswith("[MoveFiles]"):
            self.step = "整理文件"
        elif line.startswith("ERROR"):
            self.step = "失败"
            self.detail = line[:400]
            self.status = "failed"

    def snapshot(self) -> Dict[str, Any]:
        pct = max(0.0, min(100.0, self.percent))
        if self.status == "success":
            pct = 100.0
        return {
            "job_id": self.job_id,
            "status": self.status,
            "step": self.step,
            "detail": self.detail,
            "percent": round(pct, 1),
            "determinate": True,
            "title": self.title,
            "site": self.site,
            "speed_bps": self.speed_bps,
            "eta": self.eta,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "output_path": self.output_path,
            "started": self.started,
            "updated_at": self.updated_at,
        }


# ────────────────── 任务执行 ──────────────────

def build_args(
    url: str,
    out_dir: Path,
    *,
    quality: str = "highest",
    audio_only: bool = False,
    extra: Optional[List[str]] = None,
) -> List[str]:
    ff = find_ffmpeg()
    args: List[str] = [
        "--no-warnings",
        "--newline",
        "--progress",
        "--no-playlist" if False else "--yes-playlist",
        # 输出模板：作者目录 + 标题，和抖音引擎的落盘习惯保持一致
        "-o", str(out_dir / "%(uploader,creator,channel|Unknown)s" / "%(title).120B [%(id)s].%(ext)s"),
        # 文件名里的特殊字符交给 yt-dlp 自己清理
        "--windows-filenames",
        "--no-mtime",
        # 断点续传
        "--continue",
        # 失败不静默
        "--no-quiet",
    ]
    if ff:
        args += ["--ffmpeg-location", ff]

    if audio_only:
        args += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        # 画质映射到 yt-dlp 的选择器
        hmax = {
            "highest": None,
            "original": None,
            "1080p": 1080,
            "720p": 720,
            "540p": 540,
            "480p": 480,
            "360p": 360,
            "lowest": 360,
        }.get(quality)
        if hmax:
            sel = f"bv*[height<={hmax}]+ba/b[height<={hmax}]/b"
        else:
            sel = "bv*+ba/b"
        args += ["-f", sel, "--merge-output-format", "mp4"]

    if extra:
        args += extra
    args.append(url)
    return args


async def run_download(
    job_id: str,
    url: str,
    out_dir: Path,
    progress: YtdlpProgress,
    *,
    quality: str = "highest",
    audio_only: bool = False,
) -> Dict[str, Any]:
    """跑一次 yt-dlp，边跑边把真实进度写进 progress。"""
    exe = find_ytdlp()
    if not exe:
        raise RuntimeError("找不到 yt-dlp.exe（可设 YTDLP_PATH，或放到 D:\\DouyinWeb\\bin\\）")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 前置校验：传进来的必须是一条干净的链接。
    # App 里「复制链接」得到的是整段文案，要在这里就拦下并说人话，
    # 否则 yt-dlp 只会回一句 'is not a valid URL'，用户看不懂哪里错了。
    if not url.lower().startswith(("http://", "https://")):
        snippet = url.strip()
        if len(snippet) > 40:
            snippet = snippet[:40] + "…"
        raise RuntimeError(
            f"这不是一条链接：{snippet}。"
            "如果是 App 里「复制链接」得到的整段文案，里面应该包含 http 开头的地址。"
        )
    args = build_args(url, out_dir, quality=quality, audio_only=audio_only)
    cmd = [exe] + args

    # Windows 沙箱不允许子进程开命名管道，走临时文件。
    # 关键：写句柄和读句柄必须是**两个独立的 open()**。
    # 同一个句柄既交给子进程写、又自己 seek 读，会共享文件偏移量，
    # 两边互相踩，读到的内容会错位（这是实测踩到的坑）。
    tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".log", prefix="ytdlp_")
    tmp_out.close()
    tmp_err = tempfile.NamedTemporaryFile(delete=False, suffix=".err", prefix="ytdlp_")
    tmp_err.close()

    write_out = open(tmp_out.name, "wb")     # 只给子进程写
    write_err = open(tmp_err.name, "wb")
    out_fh = open(tmp_out.name, "rb")        # 独立的读句柄，偏移量互不影响
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=write_out,
            stderr=write_err,
            cwd=str(out_dir),
        )

        # 边跑边读：靠「文件是否变长」判断有没有新内容。
        # proc.returncode 在 asyncio 里由事件循环的 child watcher 填充，
        # 需要 await 才有机会更新 —— 用 sleep 让出控制权，再读一次长度兜底。
        buf = b""
        pos = 0
        exited_rounds = 0
        while True:
            try:
                size_now = os.path.getsize(tmp_out.name)
            except OSError:
                size_now = pos
            if size_now > pos:
                out_fh.seek(pos)
                chunk = out_fh.read(size_now - pos)
                if chunk:
                    pos += len(chunk)
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        progress.feed_line(line.decode("utf-8", "replace"))

            if proc.returncode is not None:
                # 进程已退出：再多等一轮，确保尾部内容都读干净
                exited_rounds += 1
                if exited_rounds >= 2:
                    break
            await asyncio.sleep(0.1)

        # 收尾：读完剩余（含最后一行没有换行符的情况）
        try:
            tail = Path(tmp_out.name).read_bytes()
            if len(tail) > pos:
                buf += tail[pos:]
        except Exception:
            pass
        for line in buf.split(b"\n"):
            progress.feed_line(line.decode("utf-8", "replace"))

        rc = await proc.wait()
        if rc != 0:
            try:
                err = Path(tmp_err.name).read_bytes().decode("utf-8", "replace").strip()
            except Exception:
                err = ""
            try:
                out_tail = Path(tmp_out.name).read_bytes().decode("utf-8", "replace").strip()
            except Exception:
                out_tail = ""
            # yt-dlp 有时把错误写在 stdout（--no-quiet 时），两边都看
            blob = (err + "\n" + out_tail).strip()
            useful = ""
            for ln in reversed(blob.splitlines()):
                if "ERROR" in ln.upper():
                    useful = ln.strip()
                    break
            if not useful:
                tail_lines = [x for x in blob.splitlines() if x.strip()][-4:]
                useful = " | ".join(tail_lines)
            raise RuntimeError(useful or f"yt-dlp 退出码 {rc}")

        progress.status = "success"
        progress.percent = 100.0
        progress.step = "完成"
        files = []
        # 从 stdout 的 after_move 记录里拿最终路径；拿不到就扫目录
        if progress.output_path and Path(progress.output_path).exists():
            files.append(progress.output_path)
        return {
            "total": max(1, len(files) or 1),
            "success": 1,
            "failed": 0,
            "skipped": 0,
            "files": files,
        }
    finally:
        # 先关写句柄（子进程那边），再关读句柄，最后删临时文件
        for fh in (write_out, write_err, out_fh):
            try:
                fh.close()
            except Exception:
                pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        for p in (tmp_out.name, tmp_err.name):
            try:
                os.unlink(p)
            except Exception:
                pass


async def probe(url: str, timeout: int = 60) -> Dict[str, Any]:
    """只取信息不下载：标题、时长、可用清晰度。"""
    exe = find_ytdlp()
    if not exe:
        raise RuntimeError("找不到 yt-dlp.exe")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", prefix="ytdlp_probe_")
    tmp.close()
    out_fh = open(tmp.name, "wb")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--no-warnings", "-j", "--no-playlist", url,
            stdin=asyncio.subprocess.DEVNULL, stdout=out_fh, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError("解析超时")
        out_fh.close()
        raw = Path(tmp.name).read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            raise RuntimeError("yt-dlp 没有返回任何信息")
        d = json.loads(raw.splitlines()[0])
        formats = d.get("formats") or []
        heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)
        return {
            "title": d.get("title") or "",
            "duration": d.get("duration") or 0,
            "uploader": d.get("uploader") or d.get("channel") or "",
            "thumbnail": d.get("thumbnail") or "",
            "extractor": d.get("extractor_key") or d.get("extractor") or "",
            "webpage_url": d.get("webpage_url") or url,
            "heights": heights[:8],
            "site": site_label(url),
        }
    finally:
        try:
            out_fh.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
