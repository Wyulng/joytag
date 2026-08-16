import os
import json
import uuid
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)

# 使用基于项目根目录的路径
BASE_DIR = Path(__file__).parent.parent
SCHEDULE_DIR = BASE_DIR / "data" / "schedules"
SCHEDULE_FILE = SCHEDULE_DIR / "schedules.json"

# 内存缓存
_cache_data: list | None = None
_cache_time: float = 0
_CACHE_TTL = 1.0  # 缓存有效期（秒），写入后立即失效

# 并发写锁，防止并发 CRUD 操作导致数据丢失
_write_lock = threading.Lock()


def _invalidate_cache():
    global _cache_data, _cache_time
    _cache_data = None
    _cache_time = 0


def _load_raw():
    global _cache_data, _cache_time
    now = time.time()
    if _cache_data is not None and now - _cache_time < _CACHE_TTL:
        return _cache_data
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _cache_data = data
                _cache_time = now
                return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"[scheduler] 加载任务文件失败: {e}")
            return []
    logger.debug("[scheduler] 任务文件不存在，返回空列表")
    return []


def _save_raw(data: list):
    _invalidate_cache()
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"[scheduler] 保存了 {len(data)} 个定时任务")


class Schedule:
    """定时任务模型"""

    def __init__(
        self,
        name: str,
        task_type: Literal["cn", "overseas"],
        cron: str,
        enabled: bool = True,
        id: str = None,
        last_run: str = None,
        created_at: str = None,
    ):
        self.id = id or str(uuid.uuid4())
        self.name = name
        self.task_type = task_type
        self.cron = cron
        self.enabled = enabled
        self.last_run = last_run
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "task_type": self.task_type,
            "cron": self.cron,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        return cls(
            id=d["id"],
            name=d["name"],
            task_type=d["task_type"],
            cron=d["cron"],
            enabled=d.get("enabled", True),
            last_run=d.get("last_run"),
            created_at=d.get("created_at"),
        )


def load_schedules() -> list[Schedule]:
    """加载所有定时任务"""
    raw = _load_raw()
    return [Schedule.from_dict(d) for d in raw]


def save_schedules(schedules: list[Schedule]) -> None:
    """保存所有定时任务到文件"""
    _save_raw([s.to_dict() for s in schedules])


def list_schedules() -> list[dict]:
    """列出所有任务（返回 dict 列表）"""
    return [s.to_dict() for s in load_schedules()]


def get_schedule(schedule_id: str) -> Schedule | None:
    """根据 ID 获取单个任务"""
    for s in load_schedules():
        if s.id == schedule_id:
            return s
    return None


def add_schedule(name: str, task_type: Literal["cn", "overseas"], cron: str) -> Schedule:
    """创建新任务"""
    with _write_lock:
        schedules = load_schedules()
        new_schedule = Schedule(name=name, task_type=task_type, cron=cron)
        schedules.append(new_schedule)
        save_schedules(schedules)
    logger.info(f"[scheduler] 创建任务: {name} ({task_type}, {cron})")
    return new_schedule


def update_schedule(schedule_id: str, **kwargs) -> Schedule | None:
    """更新任务（enabled/cron/name）"""
    with _write_lock:
        schedules = load_schedules()
        for s in schedules:
            if s.id == schedule_id:
                if "enabled" in kwargs:
                    s.enabled = kwargs["enabled"]
                if "cron" in kwargs:
                    s.cron = kwargs["cron"]
                if "name" in kwargs:
                    s.name = kwargs["name"]
                save_schedules(schedules)
                logger.info(f"[scheduler] 更新任务 {schedule_id}: {kwargs}")
                return s
    logger.warning(f"[scheduler] 更新任务失败，未找到 {schedule_id}")
    return None


def delete_schedule(schedule_id: str) -> bool:
    """删除任务"""
    with _write_lock:
        schedules = load_schedules()
        before = len(schedules)
        schedules = [s for s in schedules if s.id != schedule_id]
        if len(schedules) == before:
            logger.warning(f"[scheduler] 删除任务失败，未找到 {schedule_id}")
            return False
        save_schedules(schedules)
    logger.info(f"[scheduler] 删除任务 {schedule_id}")
    return True


def touch_last_run(schedule_id: str) -> None:
    """更新任务的 last_run 时间"""
    with _write_lock:
        schedules = load_schedules()
        for s in schedules:
            if s.id == schedule_id:
                s.last_run = datetime.now(timezone.utc).isoformat()
                save_schedules(schedules)
                logger.info(f"[scheduler] 更新任务 {schedule_id} 最后执行时间")
                return
    logger.warning(f"[scheduler] 更新 last_run 失败，未找到 {schedule_id}")