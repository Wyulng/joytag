import asyncio
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
# 本地模型目录（backend/models/bge-small-zh-v1.5）：优先使用，绕开 HF 下载（国内 AWS CDN 不通）
# 目录不存在时回退到 HF Hub 名称（生产容器原行为）
_LOCAL_MODEL_DIR = Path(__file__).parent.parent / "models" / "bge-small-zh-v1.5"


def _resolve_model() -> str:
    return str(_LOCAL_MODEL_DIR) if _LOCAL_MODEL_DIR.exists() else _MODEL_NAME


@lru_cache(maxsize=1)
def _get_model():
    """延迟加载并缓存本地 embedding 模型"""
    from sentence_transformers import SentenceTransformer
    model_ref = _resolve_model()
    logger.info(f"[embedding] 正在加载本地向量模型: {model_ref}")
    try:
        model = SentenceTransformer(model_ref)
        logger.info(f"[embedding] 本地向量模型加载成功: {model_ref}")
        return model
    except Exception as e:
        logger.critical(f"[embedding] 模型加载失败: {e}")
        raise


async def get_embedding(text: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("embedding text must not be empty")

    model = await asyncio.to_thread(_get_model)
    try:
        result = await asyncio.to_thread(lambda: model.encode(text).tolist())
    except Exception as e:
        logger.error(f"[embedding] 向量推理失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="向量服务暂不可用，请稍后重试")
    return result
