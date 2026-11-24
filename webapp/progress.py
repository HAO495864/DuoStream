"""下载进度：把引擎的真实进度事件接成前端能轮询的结构。

引擎侧真实可用的事件（见 engine/core/downloader_base.py 的 ProgressReporter 协议）：

- `update_step(step, detail)`  阶段文字：初始化 / 解析链接 / 创建下载器 / 执行下载 / 收尾
- `set_item_total(total)`      本次任务要下多少个条目（作品数）
- `advance_item(status, id)`   一个条目结算完成
- `on_item_progress(aweme_id, bytes_read, bytes_total)`
                               单个文件的字节级进度

所以百分比是算出来的真值，不是定时涨的假条：

    进度 = (已完成条目 + 在途条目的字节占比) / 总条目

并发下载时（config 里 thread 默认 5）会有多个文件同时在传，所以在途部分
按各文件字节数加权汇总，而不是只看最后一个。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from server.jobs import DownloadJob, JobManager, JobStatus


class DownloadProgress:
    """单个任务的进度记录器，同时满足引擎的 ProgressReporter 协议。

    引擎在 asyncio 事件循环里同步调用这些方法，只做字典/字段更新，
    不需要加锁；HTTP 侧读同一份数据。
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        # 阶段
        self.step = "排队中"
        self.detail = ""
        # 条目计数
        self.total_items = 0
        self.done_items = 0
        self.failed_items = 0
        self.skipped_items = 0
        # 失败原因（引擎按条目回调时带上），用来告诉用户到底哪里不对
        self.item_reasons: List[str] = []
        # 在途文件的字节进度：aweme_id -> [read, total]
        self._inflight: Dict[str, list] = {}
        # 最近一次字节事件的时间，用于判断是否还在动
        self.last_byte_at = 0.0
        self.updated_at = time.time()
        # 是否已经收到过真实数据
        self.started = False

    # ── 引擎 ProgressReporter 协议 ──────────────────────────────

    def update_step(self, step: str, detail: str = "") -> None:
        self.step = step or self.step
        if detail:
            self.detail = detail
        self.updated_at = time.time()
        self.started = True

    def set_item_total(self, total: int, detail: str = "") -> None:
        try:
            self.total_items = max(0, int(total or 0))
        except Exception:
            self.total_items = 0
        if detail:
            self.detail = detail
        self.updated_at = time.time()
        self.started = True

    def advance_item(self, status: str, detail: str = "", reason: str = "") -> None:
        s = (status or "").strip().lower()
        if s in ("skip", "skipped"):
            self.skipped_items += 1
        elif s in ("ok", "success", "succeeded"):
            self.done_items += 1
        else:
            self.failed_items += 1
            # 记下原因，别让界面只显示一个「失败」
            if reason:
                self.item_reasons.append(str(reason))
        # 该条目已结算，清掉它的字节进度，避免重复计入
        if detail:
            self._inflight.pop(detail, None)
        self.updated_at = time.time()

    def on_item_progress(self, *, aweme_id: str, bytes_read: int, bytes_total: int) -> None:
        """引擎逐个文件回调的真实字节数。"""
        if not aweme_id:
            return
        try:
            read = int(bytes_read or 0)
            total = int(bytes_total or 0)
        except Exception:
            return
        if total <= 0:
            return
        self._inflight[aweme_id] = [read, total]
        self.last_byte_at = time.time()
        self.updated_at = self.last_byte_at

    def on_output_dir(self, *args: Any, **kwargs: Any) -> None:
        """引擎可能回调作者目录，这里不需要，留空避免 AttributeError。"""
        return None

    # ── 对外快照 ────────────────────────────────────────────────

    def _inflight_fraction(self) -> float:
        if not self._inflight:
            return 0.0
        read_sum = 0
        total_sum = 0
        for read, total in self._inflight.values():
            read_sum += min(read, total)
            total_sum += total
        if total_sum <= 0:
            return 0.0
        return max(0.0, min(1.0, read_sum / total_sum))

    def snapshot(self, status: str) -> Dict[str, Any]:
        total = self.total_items
        settled = self.done_items + self.failed_items + self.skipped_items
        frac = self._inflight_fraction()

        if total > 0:
            # 在途部分折成「不足一个条目」的份额，避免和已结算条目重复计数
            value = (self.done_items + frac) / total
            value = max(0.0, min(1.0, value))
            determinate = True
        else:
            value = 0.0
            determinate = False

        if status == JobStatus.SUCCESS and determinate:
            value = 1.0

        inflight_bytes = 0
        inflight_total = 0
        for read, t in self._inflight.values():
            inflight_bytes += min(read, t)
            inflight_total += t

        return {
            "job_id": self.job_id,
            "status": status,
            "step": self.step,
            "detail": self.detail,
            "percent": round(value * 100, 1),
            "determinate": determinate,
            "total_items": total,
            "done_items": self.done_items,
            "failed_items": self.failed_items,
            "skipped_items": self.skipped_items,
            "settled_items": settled,
            "inflight_files": len(self._inflight),
            "inflight_bytes": inflight_bytes,
            "inflight_total_bytes": inflight_total,
            "started": self.started,
            "updated_at": self.updated_at,
            "reasons": self.item_reasons[-3:],
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.snapshot(JobStatus.RUNNING)


class ProgressJobManager(JobManager):
    """与引擎 JobManager 行为一致，只是把 job 对象和进度记录器一起交给执行器。

    引擎的 JobManager 只传 url，拿不到 job 本体，也就没法把进度挂到具体任务上。
    这里覆盖 _run，用同一个并发信号量和同一套剪裁逻辑，只是换一下调用形状。
    """

    def __init__(self, executor, **kwargs):
        # executor 的签名是 (job, progress) -> dict
        super().__init__(executor=executor, **kwargs)
        self.progress: Dict[str, DownloadProgress] = {}

    async def submit(self, url: str) -> DownloadJob:
        job = await super().submit(url)
        self.progress[job.job_id] = DownloadProgress(job.job_id)
        return job

    async def _run(self, job: DownloadJob) -> None:
        async with self._semaphore:
            job.status = JobStatus.RUNNING
            job.started_at = _iso_now()
            prog = self.progress.get(job.job_id)
            try:
                counts = await self.executor(job, prog)
                job.total = int(counts.get("total", 0))
                job.success = int(counts.get("success", 0))
                job.failed = int(counts.get("failed", 0))
                job.skipped = int(counts.get("skipped", 0))
                job.status = JobStatus.SUCCESS if job.failed == 0 else JobStatus.FAILED
                # 没有抛异常但一条都没成功时，把原因带到任务记录上。
                # 否则界面上只有一个「失败」，用户不知道该做什么。
                if job.status == JobStatus.FAILED and not job.error and prog is not None:
                    reason = (prog.detail or "").strip()
                    if not reason and prog.item_reasons:
                        reason = prog.item_reasons[-1]
                    if reason:
                        job.error = reason
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                if prog is not None:
                    prog.step = "失败"
                    prog.detail = job.error
            finally:
                job.finished_at = _iso_now()
                job.finished_monotonic = time.monotonic()
                if prog is not None:
                    prog.updated_at = time.time()

    def _prune_locked(self) -> None:
        super()._prune_locked()
        # 顺带清掉已淘汰任务的进度记录，避免长跑进程里无限堆积
        for jid in list(self.progress.keys()):
            if jid not in self._jobs:
                self.progress.pop(jid, None)

    def get_progress(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        prog = self.progress.get(job_id)
        if prog is None:
            return None
        return prog.snapshot(job.status)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
