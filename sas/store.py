"""幂等记录存储(进程内、线程安全)。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("sas.store")


class Status(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


# 终态集合:成功结果不可变;FAILED/CANCELLED/TIMEOUT 允许同载荷重试
TERMINAL_STATUSES = frozenset(
    {Status.SUCCEEDED, Status.FAILED, Status.CANCELLED, Status.TIMEOUT}
)
# 允许重试的(非成功)终态
RETRYABLE_STATUSES = frozenset({Status.FAILED, Status.CANCELLED, Status.TIMEOUT})


@dataclass
class IdempotencyRecord:
    key: str
    payload_hash: str
    status: Status
    result: object | None = None
    error_code: str | None = None
    error_message: str | None = None
    transition_log: list[str] = field(default_factory=list)


class IdempotencyStore:
    """按幂等键保存状态机记录,所有变更在同一把锁内原子完成。"""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.RLock()
        # 每条记录一把事件,供等待终态
        self._done_events: dict[str, threading.Event] = {}

    def _event_for(self, key: str) -> threading.Event:
        ev = self._done_events.get(key)
        if ev is None:
            ev = threading.Event()
            self._done_events[key] = ev
        return ev

    def get(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(key)

    def create(self, key: str, payload_hash: str) -> tuple[IdempotencyRecord, bool]:
        """原子创建 PENDING 记录。

        返回 (记录, created)。键已存在时返回旧记录与 False。
        """
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                return existing, False
            record = IdempotencyRecord(key=key, payload_hash=payload_hash, status=Status.PENDING)
            record.transition_log.append("(init) -> PENDING")
            self._records[key] = record
            self._event_for(key).clear()
            logger.info("状态迁移 | key=%s (init) -> PENDING payload=%s", key, payload_hash[:12])
            return record, True

    def reset_for_retry(
        self, key: str, payload_hash: str
    ) -> IdempotencyRecord:
        """同载荷重试:把可重试终态的记录重置回 PENDING(调用方需先核实状态与载荷)。"""
        with self._lock:
            record = self._records[key]
            old = record.status
            record.status = Status.PENDING
            record.result = None
            record.error_code = None
            record.error_message = None
            record.transition_log.append(f"{old.value} -> PENDING (retry same payload)")
            self._event_for(key).clear()
            logger.info("状态迁移 | key=%s %s -> PENDING (retry)", key, old.value)
            return record

    def transition(
        self, key: str, expected: Status, new: Status, **fields: object
    ) -> bool:
        """CAS 式状态迁移:仅当当前状态==expected 时写入 new 与附加字段。"""
        with self._lock:
            record = self._records.get(key)
            if record is None or record.status != expected:
                logger.info(
                    "状态迁移被拒 | key=%s 期望=%s 实际=%s 目标=%s",
                    key,
                    expected.value,
                    record.status.value if record else "<missing>",
                    new.value,
                )
                return False
            record.status = new
            for name, value in fields.items():
                setattr(record, name, value)
            record.transition_log.append(f"{expected.value} -> {new.value}")
            logger.info(
                "状态迁移 | key=%s %s -> %s fields=%s",
                key,
                expected.value,
                new.value,
                sorted(fields),
            )
            if new in TERMINAL_STATUSES:
                self._event_for(key).set()
            return True

    def wait_terminal(self, key: str, timeout: float | None = None) -> bool:
        """阻塞至记录进入终态;返回是否已终态。

        Event 可能因线程调度先于状态写入被观察到,故醒来后以记录状态为准复核。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.status in TERMINAL_STATUSES:
                return True
            event = self._event_for(key)
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            if not event.wait(remaining):
                break
            with self._lock:
                record = self._records.get(key)
                if record is not None and record.status in TERMINAL_STATUSES:
                    return True
        return False
