"""Keycloak OIDC 认证（EU 合规改造新增，2026-08）。

两部分职责：
1. 资源服务器：authlib 校验 JWT（JWKS 缓存 + 未知 kid 重取），供服务间调用
   /v1/tag/recommend（client_credentials，scope `joytag:recommend`）。
2. 管理会话：授权码 + PKCE 登录 → itsdangerous 签名会话 cookie（HttpOnly，
   token 不进 JS），双提交 CSRF（joytag_csrf cookie ↔ X-CSRF-Token 头）。

双主机名问题显式解耦：浏览器访问 Keycloak 用外部地址（OIDC_ISSUER 的
authorize/logout 端点），backend 校验/兑换 token 走内网地址（OIDC_JWKS_URL /
OIDC_TOKEN_URL，默认同 issuer，可分别覆盖）。

AUTH_ENABLED=false 仅用于本地开发（dev.ps1 只跑 qdrant），生产必须为 true。
"""
import os
import re
import json
import time
import base64
import asyncio
import hashlib
import secrets
import logging
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from authlib.jose import jwt, JsonWebKey
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from services.http_client import get_http_client

load_dotenv()
logger = logging.getLogger(__name__)

# ---------- 环境配置 ----------
# 默认 false（本地 dev.ps1 只跑 qdrant，无 Keycloak）；生产 docker-compose 显式设 true
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() in ("1", "true", "yes")
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "http://localhost:8080/realms/joytag")
OIDC_JWKS_URL = os.getenv("OIDC_JWKS_URL", f"{OIDC_ISSUER}/protocol/openid-connect/certs")
OIDC_TOKEN_URL = os.getenv("OIDC_TOKEN_URL", f"{OIDC_ISSUER}/protocol/openid-connect/token")
OIDC_AUTH_URL = os.getenv("OIDC_AUTH_URL", f"{OIDC_ISSUER}/protocol/openid-connect/auth")
OIDC_LOGOUT_URL = os.getenv("OIDC_LOGOUT_URL", f"{OIDC_ISSUER}/protocol/openid-connect/logout")
OIDC_ADMIN_CLIENT_ID = os.getenv("OIDC_ADMIN_CLIENT_ID", "joytag-admin")
OIDC_API_CLIENT_ID = os.getenv("OIDC_API_CLIENT_ID", "joytag-service")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
TLS_ENABLED = os.getenv("TLS_ENABLED", "false").lower() in ("1", "true", "yes")
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", "43200"))  # 12h

SESSION_COOKIE = "joytag_session"
CSRF_COOKIE = "joytag_csrf"
STATE_COOKIE = "joytag_oidc_state"

if AUTH_ENABLED and not SESSION_SECRET:
    raise RuntimeError("AUTH_ENABLED=true 时 SESSION_SECRET 必须设置（.env）")

# ---------- 序列化器 ----------
_session_serializer = URLSafeTimedSerializer(SESSION_SECRET or "dev", salt="joytag-admin-session")
_state_serializer = URLSafeTimedSerializer(SESSION_SECRET or "dev", salt="joytag-oidc-state")

# ---------- JWKS 缓存 ----------
_jwks = {"set": None, "at": 0.0}
_jwks_lock = asyncio.Lock()
_JWKS_TTL = 3600.0
_ALLOWED_ALGS = {"RS256", "RS384", "RS512", "ES256", "ES384"}


def _claims_options(audience: str) -> dict:
    return {
        "iss": {"essential": True, "value": OIDC_ISSUER},
        "exp": {"essential": True},
        "aud": {"essential": True, "value": audience},
    }


def _token_header(token: str) -> dict:
    b64 = token.split(".")[0]
    b64 += "=" * (-len(b64) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(b64))
    except Exception:
        raise ValueError("token header 解析失败")


async def _get_jwks_set(force: bool = False):
    """JWKS 缓存（TTL 3600s）；未知 kid 时 force 重取一次。"""
    async with _jwks_lock:
        if force or _jwks["set"] is None or time.monotonic() - _jwks["at"] > _JWKS_TTL:
            logger.info(f"[auth] 拉取 JWKS: {OIDC_JWKS_URL}")
            resp = await get_http_client().get(OIDC_JWKS_URL)
            resp.raise_for_status()
            _jwks["set"] = JsonWebKey.import_key_set(resp.json())
            _jwks["at"] = time.monotonic()
    return _jwks["set"]


async def verify_bearer_token(token: str, *, audience: str) -> dict:
    """校验 access/id token：签名（JWKS，按 kid）+ iss/aud/exp。失败抛异常。"""
    header = _token_header(token)
    if header.get("alg") not in _ALLOWED_ALGS:
        raise ValueError(f"不支持的签名算法: {header.get('alg')}")
    try:
        key_set = await _get_jwks_set()
        key = key_set.find_by_kid(header.get("kid"))
    except Exception:
        key_set = await _get_jwks_set(force=True)  # 未知 kid → 重取 JWKS 一次
        key = key_set.find_by_kid(header.get("kid"))
    claims = jwt.decode(token, key, claims_options=_claims_options(audience))
    claims.validate()
    return claims


# ---------- 管理会话 ----------
def _dev_session() -> dict:
    """本地开发旁路会话（AUTH_ENABLED=false）。"""
    if not getattr(_dev_session, "_warned", False):
        logger.warning("[auth] AUTH_ENABLED=false，认证已旁路（仅限本地开发，严禁生产使用）")
        _dev_session._warned = True
    return {"sub": "dev", "username": "dev", "roles": ["admin"], "csrf": "dev"}


def _set_cookie(response, key: str, value: str, *, max_age: int | None, http_only: bool):
    response.set_cookie(
        key, value, max_age=max_age, httponly=http_only,
        samesite="lax", secure=TLS_ENABLED, path="/",
    )


def _safe_next(next_url: str | None) -> str:
    """防开放重定向：仅允许站内绝对路径。"""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/admin"


async def login_redirect(request: Request, next_url: str | None = None):
    """发起授权码 + PKCE 登录。生成 verifier/challenge/state/nonce，
    短效签名 cookie 暂存 verifier（HttpOnly，不进 JS），302 到 Keycloak。"""
    from fastapi.responses import RedirectResponse
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)

    redirect_uri = f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    params = {
        "response_type": "code",
        "client_id": OIDC_ADMIN_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{OIDC_AUTH_URL}?{urlencode(params)}")
    _set_cookie(
        response, STATE_COOKIE,
        _state_serializer.dumps({"verifier": verifier, "state": state,
                                 "nonce": nonce, "next": _safe_next(next_url)}),
        max_age=600, http_only=True,
    )
    return response


async def handle_callback(request: Request):
    """授权码回调：state 校验 → 内网 token 端点换 token（PKCE）→ 验 ID token
    （签名 + iss/aud/nonce）→ 签发服务端会话 cookie + CSRF cookie。"""
    from fastapi.responses import RedirectResponse
    error = request.query_params.get("error")
    if error:
        logger.warning(f"[auth] Keycloak 登录错误: {error}")
        raise HTTPException(status_code=401, detail=f"登录失败: {error}")

    state_token = request.cookies.get(STATE_COOKIE)
    try:
        state_data = _state_serializer.loads(state_token, max_age=600)
    except (BadSignature, SignatureExpired, TypeError):
        raise HTTPException(status_code=401, detail="登录状态已过期，请重试")

    if not secrets.compare_digest(state_data.get("state", ""), request.query_params.get("state", "")):
        raise HTTPException(status_code=401, detail="state 校验失败（可能为 CSRF 攻击）")

    code = request.query_params.get("code")
    redirect_uri = f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    resp = await get_http_client().post(
        OIDC_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": OIDC_ADMIN_CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": state_data["verifier"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        logger.error(f"[auth] token 兑换失败: {resp.status_code}")
        raise HTTPException(status_code=401, detail="登录失败，token 兑换失败")
    token_data = resp.json()

    try:
        claims = await verify_bearer_token(token_data["id_token"], audience=OIDC_ADMIN_CLIENT_ID)
    except Exception as e:
        logger.error(f"[auth] ID token 校验失败: {e}")
        raise HTTPException(status_code=401, detail="ID token 校验失败")

    if not secrets.compare_digest(claims.get("nonce", ""), state_data.get("nonce", "")):
        raise HTTPException(status_code=401, detail="nonce 校验失败")

    roles = list((claims.get("realm_access") or {}).get("roles") or [])
    if not roles:
        raise HTTPException(status_code=403, detail="账号未分配任何角色，请联系管理员")
    csrf = secrets.token_urlsafe(24)
    session = {
        "sub": claims.get("sub", ""),
        "username": claims.get("preferred_username", ""),
        "name": claims.get("name", ""),
        "email": claims.get("email", ""),
        "roles": roles,
        "csrf": csrf,
        "id_token": token_data.get("id_token", ""),  # 供 logout id_token_hint
    }
    logger.info(f"[auth] 登录成功: {session['username']} (roles={roles})")

    response = RedirectResponse(state_data.get("next", "/admin"))
    _set_cookie(response, SESSION_COOKIE, _session_serializer.dumps(session),
                max_age=SESSION_MAX_AGE, http_only=True)
    # CSRF 双提交：cookie 供 JS 读取，会话内的 csrf 值供服务端比对
    _set_cookie(response, CSRF_COOKIE, csrf, max_age=SESSION_MAX_AGE, http_only=False)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


async def logout(request: Request):
    """退出：清会话 + 跳 Keycloak end_session（带 id_token_hint）。"""
    from fastapi.responses import RedirectResponse
    id_token_hint = ""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            id_token_hint = _session_serializer.loads(token, max_age=SESSION_MAX_AGE).get("id_token", "")
        except (BadSignature, SignatureExpired):
            pass
    post_logout = f"{request.url.scheme}://{request.url.netloc}/admin"
    params = {"post_logout_redirect_uri": post_logout}
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    response = RedirectResponse(f"{OIDC_LOGOUT_URL}?{urlencode(params)}")
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


# ---------- FastAPI 依赖 ----------
async def require_admin_session(request: Request) -> dict:
    """管理会话校验。失败 401（API）——页面路由用 require_admin_page 做 302。"""
    if not AUTH_ENABLED:
        return _dev_session()
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        session = _session_serializer.loads(token, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录")
    except BadSignature:
        raise HTTPException(status_code=401, detail="会话无效")
    request.state.session = session
    return session


async def require_admin_page(request: Request) -> dict:
    """页面守卫：未登录 302 到 /auth/login（而非 401 JSON）。"""
    if not AUTH_ENABLED:
        return _dev_session()
    try:
        return await require_admin_session(request)
    except HTTPException as e:
        if e.status_code == 401:
            raise HTTPException(
                status_code=302,
                headers={"Location": f"/auth/login?next={request.url.path}"},
            )
        raise


async def require_csrf(request: Request, session: dict = Depends(require_admin_session)):
    """CSRF 双提交校验（仅写方法）。会话内的 csrf 值 ↔ X-CSRF-Token 头。"""
    if not AUTH_ENABLED:
        # 仅本地开发旁路（无真实会话，CSRF 无攻击面）；生产恒 true，以下双提交严格生效
        return session
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return session
    header = request.headers.get("X-CSRF-Token", "")
    if not header or not secrets.compare_digest(header, session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面重试")
    return session


def require_role(*roles: str):
    """角色依赖工厂：admin 角色通行全部；否则需命中指定角色之一。"""
    async def dep(session: dict = Depends(require_admin_session)) -> dict:
        user_roles = session.get("roles", [])
        if "admin" in user_roles:
            return session
        if not set(roles) & set(user_roles):
            raise HTTPException(status_code=403, detail="权限不足")
        return session
    return dep


def require_scope(scope: str):
    """服务间 Bearer token 依赖（/v1/tag/recommend）。"""
    async def dep(request: Request):
        if not AUTH_ENABLED:
            return None
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少 Bearer token")
        try:
            claims = await verify_bearer_token(auth_header[7:], audience=OIDC_API_CLIENT_ID)
        except Exception:
            raise HTTPException(status_code=401, detail="token 无效或已过期")
        if scope not in (claims.get("scope") or "").split():
            raise HTTPException(status_code=403, detail=f"缺少 scope: {scope}")
        request.state.claims = claims
        return claims
    return dep
