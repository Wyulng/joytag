import asyncio
import logging
import math
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Alibaba-NLP/gte-multilingual-base")
EMBEDDING_MODEL_REVISION = os.getenv(
    "EMBEDDING_MODEL_REVISION",
    "9bbca17d9273fd0d03d5725c7a4b0f6b45142062",
)
EMBEDDING_DIM = 768
EMBEDDING_NORMALIZE = os.getenv("EMBEDDING_NORMALIZE", "true").strip().lower() not in {
    "0", "false", "no", "off"
}

_DEFAULT_LOCAL_MODEL_DIR = Path(__file__).parent.parent / "models" / "gte-multilingual-base"


def _configured_model_path() -> Path | None:
    configured = os.getenv("EMBEDDING_MODEL_PATH")
    if not configured:
        return None

    path = Path(configured)
    if path.is_absolute():
        return path

    # The same .env is used from the repository root locally and from /app in
    # Docker, where the host's backend/ directory is mounted as /app.
    candidates = [Path.cwd() / path]
    if path.parts and path.parts[0].lower() == "backend":
        candidates.append(Path(__file__).parent.parent / Path(*path.parts[1:]))
    candidates.append(Path(__file__).parent.parent / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _is_local_model_ready(path: Path) -> bool:
    return path.is_dir() and (path / "config.json").exists() and any(
        (path / filename).exists()
        for filename in ("model.safetensors", "pytorch_model.bin")
    )


def _resolve_model() -> str:
    configured_path = _configured_model_path()
    if configured_path and _is_local_model_ready(configured_path):
        return str(configured_path)
    if _is_local_model_ready(_DEFAULT_LOCAL_MODEL_DIR):
        return str(_DEFAULT_LOCAL_MODEL_DIR)
    return EMBEDDING_MODEL


def _resolve_device() -> str:
    configured = os.getenv("EMBEDDING_DEVICE", "cpu").strip().lower()
    if configured not in {"", "auto"}:
        return configured
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _repair_position_ids(model) -> None:
    """Repair the non-persistent GTE position-id buffer after HF loading.

    The GTE remote model registers ``position_ids`` as a non-persistent buffer.
    With the current Transformers loader this buffer can be left with
    uninitialised values even though the model weights load successfully.  A
    corrupted buffer only appears when the first short text is encoded and
    produces an opaque RoPE index error.  Position ids are definitionally the
    contiguous range [0, max_position_embeddings), so restoring that invariant
    is safe and keeps the fix local to the loaded model.
    """
    try:
        import torch

        transformer = model._first_module()
        auto_model = getattr(transformer, "auto_model", None)
        embeddings = getattr(auto_model, "embeddings", None)
        position_ids = getattr(embeddings, "position_ids", None)
        if position_ids is None or position_ids.ndim != 1:
            return

        expected = torch.arange(
            position_ids.numel(), device=position_ids.device, dtype=position_ids.dtype
        )
        if not torch.equal(position_ids, expected):
            logger.warning(
                "[embedding] 检测到 GTE position_ids 缓冲区异常，已恢复为连续位置索引"
            )
            with torch.no_grad():
                position_ids.copy_(expected)
    except Exception as e:
        # Do not make model startup depend on an implementation detail of a
        # remote-code model. The first real encode still performs dimension and
        # availability checks and will surface a clear service error if needed.
        logger.debug("[embedding] position_ids 修复跳过: %s", e)


@lru_cache(maxsize=1)
def _get_model():
    """延迟加载并缓存 GTE 多语言 embedding 模型。"""
    from sentence_transformers import SentenceTransformer

    model_ref = _resolve_model()
    device = _resolve_device()
    logger.info(
        "[embedding] 正在加载多语言向量模型: %s (device=%s, revision=%s)",
        model_ref,
        device,
        EMBEDDING_MODEL_REVISION,
    )
    try:
        kwargs = {
            "trust_remote_code": True,
            "device": device,
        }
        if not Path(model_ref).exists() and EMBEDDING_MODEL_REVISION:
            kwargs["revision"] = EMBEDDING_MODEL_REVISION
        model = SentenceTransformer(model_ref, **kwargs)
        _repair_position_ids(model)
        logger.info("[embedding] 多语言向量模型加载成功: %s", model_ref)
        return model
    except Exception as e:
        logger.critical("[embedding] 模型加载失败: %s", e)
        raise


async def get_embedding(text: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("embedding text must not be empty")

    model = await asyncio.to_thread(_get_model)
    try:
        result = await asyncio.to_thread(
            lambda: model.encode(
                text,
                normalize_embeddings=EMBEDDING_NORMALIZE,
                convert_to_numpy=True,
            ).tolist()
        )
        vector = [float(value) for value in result]
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding dimension mismatch: expected {EMBEDDING_DIM}, got {len(vector)}"
            )
        if EMBEDDING_NORMALIZE:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0:
                raise ValueError("embedding vector norm must not be zero")
            vector = [value / norm for value in vector]
        return vector
    except Exception as e:
        logger.error("[embedding] 向量推理失败: %s", e)
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="向量服务暂不可用，请稍后重试")
