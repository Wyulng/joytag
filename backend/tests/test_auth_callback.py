import unittest
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from services import auth


class AuthCallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request():
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/auth/callback",
            "raw_path": b"/auth/callback",
            "query_string": b"code=authorization-code&state=expected-state",
            "headers": [(b"cookie", b"joytag_oidc_state=signed-state")],
            "client": ("127.0.0.1", 12345),
            "server": ("43.128.130.240", 8001),
            "root_path": "",
        }
        return Request(scope)

    @staticmethod
    def _token_response():
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id_token": "verified-id-token",
                "access_token": "access-token-without-trusted-role-source",
            },
        )

    def _patch_callback(self, claims, *, verify_error=None):
        state_data = {
            "state": "expected-state",
            "verifier": "pkce-verifier",
            "nonce": "expected-nonce",
            "next": "/admin",
        }
        verifier = AsyncMock(side_effect=verify_error) if verify_error else AsyncMock(
            return_value=claims
        )
        client = SimpleNamespace(post=AsyncMock(return_value=self._token_response()))
        return (
            patch.object(auth._state_serializer, "loads", return_value=state_data),
            patch.object(auth, "get_http_client", return_value=client),
            patch.object(auth, "verify_bearer_token", new=verifier),
            verifier,
        )

    async def test_valid_id_token_roles_create_session(self):
        claims = {
            "sub": "user-1",
            "preferred_username": "operator",
            "nonce": "expected-nonce",
            "realm_access": {"roles": ["operator"]},
        }
        state_patch, client_patch, verify_patch, verifier = self._patch_callback(claims)
        with state_patch, client_patch, verify_patch:
            response = await auth.handle_callback(self._request())

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/admin")
        session_header = next(
            value
            for key, value in response.raw_headers
            if key.lower() == b"set-cookie" and value.startswith(b"joytag_session=")
        )
        cookie = SimpleCookie()
        cookie.load(session_header.decode("latin-1"))
        session = auth._session_serializer.loads(
            cookie[auth.SESSION_COOKIE].value,
            max_age=auth.SESSION_MAX_AGE,
        )
        self.assertEqual(session["roles"], ["operator"])
        verifier.assert_awaited_once_with(
            "verified-id-token", audience=auth.OIDC_ADMIN_CLIENT_ID
        )

    async def test_missing_id_token_roles_does_not_fallback_to_access_token(self):
        claims = {
            "sub": "user-2",
            "preferred_username": "unassigned",
            "nonce": "expected-nonce",
        }
        state_patch, client_patch, verify_patch, verifier = self._patch_callback(claims)
        with state_patch, client_patch, verify_patch:
            with self.assertRaises(HTTPException) as context:
                await auth.handle_callback(self._request())

        self.assertEqual(context.exception.status_code, 403)
        verifier.assert_awaited_once_with(
            "verified-id-token", audience=auth.OIDC_ADMIN_CLIENT_ID
        )

    async def test_token_validation_error_returns_401(self):
        state_patch, client_patch, verify_patch, _ = self._patch_callback(
            {}, verify_error=ValueError("invalid signature or issuer")
        )
        with state_patch, client_patch, verify_patch:
            with self.assertRaises(HTTPException) as context:
                await auth.handle_callback(self._request())

        self.assertEqual(context.exception.status_code, 401)

    async def test_nonce_mismatch_returns_401(self):
        claims = {
            "sub": "user-3",
            "preferred_username": "operator",
            "nonce": "different-nonce",
            "realm_access": {"roles": ["operator"]},
        }
        state_patch, client_patch, verify_patch, _ = self._patch_callback(claims)
        with state_patch, client_patch, verify_patch:
            with self.assertRaises(HTTPException) as context:
                await auth.handle_callback(self._request())

        self.assertEqual(context.exception.status_code, 401)
