"""幂等存储：相同幂等键并发/重复提交只产生一次求解。

状态机：RUNNING -> SUCCEEDED / FAILED
- SUCCEEDED 永久保留；同键再次提交：指纹一致 -> 返回同一份结果，
  指纹冲突 -> 409 CONFLICT。
- FAILED 为终态但允许同指纹重试（记录重置为 RUNNING、使用全新取消事件）；
  冲突指纹仍返回 409。取消/失败不会污染重试（新记录不继承旧取消信号）。

并发安全：所有登记与迁移均在同一把可重入锁内完成；``begin`` 与状态写入
合在一起，保证“只有一个调用方启动求解”。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from app.errors import ConflictError
from app.logging_setup import get_logger
from app.models import OptimizeResponse

logger = get_logger("app.service.idempotency")


class JobStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class JobRecord:
    key: str
    fingerprint: str
    status: JobStatus
    result: OptimizeResponse | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict = field(default_factory=dict)
    attempts: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)


class IdempotencyStore:
    """进程内幂等存储（线程安全）。"""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}
        self._lock = threading.RLock()

    def begin(self, key: str, fingerprint: str) -> tuple[JobRecord, bool]:
        """登记一次提交，返回 (记录, 是否由本次调用启动求解)。

        - 新键：建 RUNNING 记录，started=True；
        - RUNNING / SUCCEEDED 且指纹一致：started=False（复用）；
        - FAILED 且指纹一致：重置为 RUNNING（新取消事件），started=True；
        - 指纹不一致：抛 ConflictError。
        """
        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = JobRecord(
                    key=key,
                    fingerprint=fingerprint,
                    status=JobStatus.RUNNING,
                    attempts=1,
                )
                self._records[key] = record
                logger.info("状态迁移 key=%s -> RUNNING（首次提交 attempts=1）", key)
                return record, True

            if record.fingerprint != fingerprint:
                logger.warning(
                    "幂等键冲突 key=%s：指纹不一致 stored=%s incoming=%s status=%s",
                    key,
                    record.fingerprint[:12],
                    fingerprint[:12],
                    record.status.value,
                )
                raise ConflictError(
                    "相同幂等键绑定了不同的请求内容",
                    details={"idempotency_key": key, "status": record.status.value},
                )

            if record.status is JobStatus.FAILED:
                # 同指纹重试：全新记录与取消事件，旧失败不污染本次执行。
                record = JobRecord(
                    key=key,
                    fingerprint=fingerprint,
                    status=JobStatus.RUNNING,
                    attempts=record.attempts + 1,
                )
                self._records[key] = record
                logger.info(
                    "状态迁移 key=%s FAILED -> RUNNING（同指纹重试 attempts=%d）",
                    key,
                    record.attempts,
                )
                return record, True

            logger.info(
                "复用已有作业 key=%s status=%s started=False",
                key,
                record.status.value,
            )
            return record, False

    def succeed(self, key: str, result: OptimizeResponse) -> None:
        """标记求解成功并固化结果（仅 RUNNING 可迁移）。"""
        with self._lock:
            record = self._records.get(key)
            if record is None or record.status is not JobStatus.RUNNING:
                logger.warning(
                    "忽略迟到成功 key=%s（记录不存在或非 RUNNING）", key
                )
                return
            record.status = JobStatus.SUCCEEDED
            record.result = result
            logger.info("状态迁移 key=%s RUNNING -> SUCCEEDED", key)

    def fail(
        self,
        key: str,
        code: str,
        message: str,
        details: dict,
    ) -> None:
        """标记求解失败（仅 RUNNING 可迁移；FAILED 允许同键重试）。"""
        with self._lock:
            record = self._records.get(key)
            if record is None or record.status is not JobStatus.RUNNING:
                logger.warning(
                    "忽略迟到失败 key=%s code=%s（记录不存在或非 RUNNING）",
                    key,
                    code,
                )
                return
            record.status = JobStatus.FAILED
            record.error_code = code
            record.error_message = message
            record.error_details = details
            logger.info(
                "状态迁移 key=%s RUNNING -> FAILED code=%s", key, code
            )

    def get(self, key: str) -> JobRecord | None:
        with self._lock:
            return self._records.get(key)

    def request_cancel(self, key: str) -> bool:
        """置位取消事件；仅对存在且 RUNNING 的作业生效。"""
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return False
            if record.status is not JobStatus.RUNNING:
                logger.info(
                    "取消请求被忽略 key=%s status=%s（非 RUNNING）",
                    key,
                    record.status.value,
                )
                return False
            record.cancel_event.set()
            logger.info("取消信号已置位 key=%s", key)
            return True
