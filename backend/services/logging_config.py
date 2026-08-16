import logging
import json
import re
import hashlib
import contextvars
from datetime import datetime, timezone

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# ---------- 日志脱敏（GDPR 最小化：日志中不出现 email/IBAN/电话/IP/长数字串） ----------
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_IBAN_RE = re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b')
_PHONE_RE = re.compile(r'(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}[\s.-]?\d{2,4}[\s.-]?\d{2,4}\b')
_IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_LONG_DIGITS_RE = re.compile(r'\b\d{12,}\b')

_REDACTION_PATTERNS = [
    (_EMAIL_RE, "<EMAIL>"),
    (_IBAN_RE, "<IBAN>"),
    (_IP_RE, "<IP>"),
    (_PHONE_RE, "<PHONE>"),
    (_LONG_DIGITS_RE, "<DIGITS>"),
]


def sanitize_for_log(text: str) -> str:
    """对日志文本应用脱敏规则（供外部复用，如 trace 写入前）。"""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def log_safe_hash(text: str, prefix_len: int = 12) -> str:
    """日志脱敏哈希约定：sha256 前缀（日志中不落原文，仅留可关联哈希）。

    所有「查询哈希」日志（recommend/app 推荐请求）共用本 helper，
    升级哈希约定（前缀长度/加盐）只改此处，避免各处内联漂移。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:prefix_len]


class JSONFormatter(logging.Formatter):
    def format(self, record):
        message = sanitize_for_log(record.getMessage())
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "request_id": _request_id_var.get(),
            "module": record.module,
            "func": record.funcName,
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = sanitize_for_log(str(record.exc_info[1]))
        return json.dumps(log_entry, ensure_ascii=False)


def set_request_id(rid: str):
    _request_id_var.set(rid)


def get_request_id() -> str:
    """当前请求的 request_id（LLM 留痕关联用）。"""
    return _request_id_var.get()


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
