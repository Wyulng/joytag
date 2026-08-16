"""共享 HTTPX 异步客户端，复用连接池以减少 LLM API 调用的 TCP 握手开销。"""

import httpx
import logging

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        logger.info("[http_client] 创建共享 HTTPX 客户端（timeout=180s）")
        _client = httpx.AsyncClient(timeout=180.0)
    return _client


async def close_http_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("[http_client] 共享客户端已关闭")
