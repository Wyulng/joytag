"""LLM Provider 适配层（EU 合规改造新增，2026-08）。

目标：数据出境风险治理。当前默认 DeepSeek（openai_compat），未来切换到 EU 区域模型
（Mistral/OpenAI = 同适配器改 env；Azure/Bedrock = 预置薄分支）零代码改动。

切换方式（纯配置，无需改代码）：
- openai_compat（DeepSeek/Mistral/OpenAI 同一 wire format）:
    LLM_PROVIDER=openai_compat
    LLM_BASE_URL=https://api.mistral.ai/v1   # 或 https://api.openai.com/v1
    LLM_API_KEY=...
    LLM_MODEL=mistral-large-latest
- azure: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT / AZURE_OPENAI_API_VERSION
- bedrock: AWS_REGION / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / BEDROCK_MODEL_ID
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from services.http_client import get_http_client

logger = logging.getLogger(__name__)

load_dotenv()


class LLMProviderError(RuntimeError):
    """Provider 配置缺失或响应格式异常。"""


@dataclass
class LLMResult:
    content: str
    model: str
    provider: str
    usage: dict
    latency_ms: int
    retry_count: int = 0


class BaseLLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def chat_completion(self, messages: list[dict], *, temperature: float,
                              max_tokens: int | None = None) -> LLMResult:
        ...


# ==================== OpenAI 兼容（DeepSeek / Mistral / OpenAI）====================
class OpenAICompatProvider(BaseLLMProvider):
    name = "openai_compat"

    def __init__(self):
        # 向后兼容：LLM_API_KEY 未设置时回退旧 DEEPSEEK_API_KEY
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        if not self.api_key:
            raise LLMProviderError("LLM_API_KEY 未配置（或 DEEPSEEK_API_KEY）")

    async def chat_completion(self, messages, *, temperature, max_tokens=None) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        start = time.perf_counter()
        response = await get_http_client().post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMProviderError(f"OpenAI 兼容响应格式异常: {e}") from e
        return LLMResult(
            content=content,
            model=data.get("model", self.model),
            provider=self.name,
            usage=data.get("usage", {}),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )


# ==================== Azure OpenAI ====================
class AzureProvider(BaseLLMProvider):
    name = "azure"

    def __init__(self):
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")
        if not (self.endpoint and self.api_key and self.deployment):
            raise LLMProviderError("Azure 配置不完整（AZURE_OPENAI_ENDPOINT/API_KEY/DEPLOYMENT）")

    async def chat_completion(self, messages, *, temperature, max_tokens=None) -> LLMResult:
        payload: dict[str, Any] = {"messages": messages, "temperature": temperature}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        url = f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"
        start = time.perf_counter()
        response = await get_http_client().post(
            url, json=payload, headers={"api-key": self.api_key, "Content-Type": "application/json"}
        )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMProviderError(f"Azure 响应格式异常: {e}") from e
        return LLMResult(
            content=content,
            model=data.get("model", self.deployment),
            provider=self.name,
            usage=data.get("usage", {}),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )


# ==================== AWS Bedrock（Converse API + SigV4，无 boto3 依赖）====================
def _aws_sign_v4(*, method: str, url: str, body: str, region: str,
                 access_key: str, secret_key: str, service: str = "bedrock-runtime",
                 content_type: str = "application/json") -> dict[str, str]:
    """最小 AWS SigV4 签名（仅支持签名请求头，不签名 query 中的安全参数）。"""
    from urllib.parse import urlparse, quote

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    headers = {
        "host": host,
        "content-type": content_type,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        method, path, "", canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        **{k: headers[k] for k in headers},
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


class BedrockProvider(BaseLLMProvider):
    """Converse API 统一 messages 格式，避免按模型家族分叉解析。

    best-effort 分支：SigV4 为自实现，切换前需在 EU 区域实测一次。
    """
    name = "bedrock"

    def __init__(self):
        self.region = os.getenv("AWS_REGION", "eu-west-1")
        self.access_key = os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.model_id = os.getenv("BEDROCK_MODEL_ID")
        if not (self.access_key and self.secret_key and self.model_id):
            raise LLMProviderError("Bedrock 配置不完整（AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/BEDROCK_MODEL_ID）")

    async def chat_completion(self, messages, *, temperature, max_tokens=None) -> LLMResult:
        converse_messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages if isinstance(m.get("content"), str)
        ]
        inference: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            inference["maxTokens"] = max_tokens
        body = json.dumps({"messages": converse_messages, "inferenceConfig": inference})
        url = f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{self.model_id}/converse"
        start = time.perf_counter()
        headers = _aws_sign_v4(
            method="POST", url=url, body=body, region=self.region,
            access_key=self.access_key, secret_key=self.secret_key,
        )
        response = await get_http_client().post(url, content=body.encode("utf-8"), headers=headers)
        response.raise_for_status()
        data = response.json()
        try:
            content = data["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError) as e:
            raise LLMProviderError(f"Bedrock 响应格式异常: {e}") from e
        return LLMResult(
            content=content,
            model=self.model_id,
            provider=self.name,
            usage=data.get("usage", {}),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )


# ==================== 工厂 ====================
_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
    "azure": AzureProvider,
    "bedrock": BedrockProvider,
}

_provider: BaseLLMProvider | None = None


def get_llm_provider() -> BaseLLMProvider:
    """惰性单例。LLM_PROVIDER env 选择适配器，默认 openai_compat。"""
    global _provider
    if _provider is None:
        name = os.getenv("LLM_PROVIDER", "openai_compat")
        cls = _PROVIDERS.get(name)
        if cls is None:
            raise LLMProviderError(f"未知 LLM_PROVIDER: {name}（可选 {sorted(_PROVIDERS)}）")
        _provider = cls()
        logger.info(f"[llm_provider] 使用 provider: {name}")
    return _provider
