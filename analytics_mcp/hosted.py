"""Hosted, multi-user Streamable HTTP server."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from analytics_mcp.tools.admin.info import (
    get_account_summaries,
    get_property_details,
    list_google_ads_links,
    list_property_annotations,
)
from analytics_mcp.tools.client import set_credential_provider
from analytics_mcp.tools.reporting.conversions import run_conversions_report
from analytics_mcp.tools.reporting.core import run_report
from analytics_mcp.tools.reporting.funnel import run_funnel_report
from analytics_mcp.tools.reporting.metadata import (
    get_custom_dimensions_and_metrics,
)
from analytics_mcp.tools.reporting.realtime import run_realtime_report

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
MCP_SCOPE = "analytics.read"


@dataclass(frozen=True)
class HostedSettings:
    server_url: str
    google_client_id: str
    google_client_secret: str
    encryption_key: str
    database_path: str = "analytics-mcp.db"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "HostedSettings":
        required = {
            "server_url": "GA4_MCP_SERVER_URL",
            "google_client_id": "GOOGLE_OAUTH_CLIENT_ID",
            "google_client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
            "encryption_key": "GA4_MCP_ENCRYPTION_KEY",
        }
        values = {}
        missing = []
        for field, variable in required.items():
            value = os.environ.get(variable)
            if value:
                values[field] = value
            else:
                missing.append(variable)
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        values["database_path"] = os.environ.get(
            "GA4_MCP_DATABASE_PATH", "analytics-mcp.db"
        )
        values["host"] = os.environ.get("HOST", "0.0.0.0")
        values["port"] = int(os.environ.get("PORT", "8000"))
        return cls(**values)


class GrantStore:
    """Small SQLite store suitable for a single-machine MVP."""

    def __init__(self, path: str, encryption_key: str):
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind, key))"
        )
        self._connection.commit()
        self._cipher = Fernet(encryption_key.encode())
        self._lock = threading.RLock()

    def put(self, kind: str, key: str, value: dict) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO records(kind, key, value) VALUES (?, ?, ?)",
                (kind, key, json.dumps(value)),
            )
            self._connection.commit()

    def get(self, kind: str, key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM records WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM records WHERE kind = ? AND key = ?", (kind, key)
            )
            self._connection.commit()

    def encrypt(self, value: str) -> str:
        return self._cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._cipher.decrypt(value.encode()).decode()


class GoogleOAuthProvider(
    OAuthAuthorizationServerProvider[
        AuthorizationCode, RefreshToken, AccessToken
    ]
):
    def __init__(self, settings: HostedSettings, store: GrantStore):
        self.settings = settings
        self.store = store

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        value = self.store.get("client", client_id)
        return (
            OAuthClientInformationFull.model_validate(value) if value else None
        )

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        self.store.put(
            "client", client_info.client_id, client_info.model_dump(mode="json")
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        provider_state = secrets.token_urlsafe(32)
        self.store.put(
            "state",
            provider_state,
            {
                "client_id": client.client_id,
                "client_state": params.state,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "resource": params.resource,
                "expires_at": time.time() + 600,
            },
        )
        return (
            f"{self.settings.server_url.rstrip('/')}/oauth/google/start?"
            + urlencode({"state": provider_state})
        )

    def google_authorization_url(self, state: str) -> str:
        transaction = self.store.get("state", state)
        if not transaction or transaction["expires_at"] < time.time():
            raise HTTPException(400, "Invalid or expired OAuth request")
        query = urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": f"{self.settings.server_url.rstrip('/')}/oauth/google/callback",
                "response_type": "code",
                "scope": ANALYTICS_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    async def handle_google_callback(self, request: Request):
        error = request.query_params.get("error")
        state = request.query_params.get("state")
        code = request.query_params.get("code")
        if error:
            raise HTTPException(400, f"Google authorization failed: {error}")
        transaction = self.store.get("state", state or "")
        if (
            not transaction
            or transaction["expires_at"] < time.time()
            or not code
        ):
            raise HTTPException(400, "Invalid or expired OAuth callback")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": f"{self.settings.server_url.rstrip('/')}/oauth/google/callback",
                },
            )
        if response.is_error:
            raise HTTPException(502, "Google token exchange failed")
        google_token = response.json()
        refresh_token = google_token.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                400,
                "Google did not return offline access; reconnect and grant access",
            )

        grant_id = secrets.token_urlsafe(24)
        self.store.put(
            "grant",
            grant_id,
            {
                "refresh_token": self.store.encrypt(refresh_token),
                "access_token": self.store.encrypt(
                    google_token["access_token"]
                ),
                "expires_at": time.time()
                + int(google_token.get("expires_in", 3600)),
            },
        )
        mcp_code = secrets.token_urlsafe(32)
        authorization_code = AuthorizationCode(
            code=mcp_code,
            client_id=transaction["client_id"],
            scopes=[MCP_SCOPE],
            expires_at=time.time() + 300,
            code_challenge=transaction["code_challenge"],
            redirect_uri=AnyHttpUrl(transaction["redirect_uri"]),
            redirect_uri_provided_explicitly=transaction[
                "redirect_uri_provided_explicitly"
            ],
            resource=transaction["resource"],
            subject=grant_id,
        )
        self.store.put(
            "code", mcp_code, authorization_code.model_dump(mode="json")
        )
        self.store.delete("state", state)
        redirect = construct_redirect_uri(
            transaction["redirect_uri"],
            code=mcp_code,
            state=transaction["client_state"],
        )
        return RedirectResponse(redirect, status_code=302)

    async def load_authorization_code(self, client, authorization_code):
        value = self.store.get("code", authorization_code)
        return AuthorizationCode.model_validate(value) if value else None

    async def exchange_authorization_code(self, client, authorization_code):
        self.store.delete("code", authorization_code.code)
        return self._issue_tokens(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
        )

    def _issue_tokens(
        self, client_id: str, scopes: list[str], subject: str | None
    ) -> OAuthToken:
        access_value = secrets.token_urlsafe(32)
        refresh_value = secrets.token_urlsafe(32)
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 3600,
            subject=subject,
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            subject=subject,
        )
        self.store.put("access", access_value, access.model_dump(mode="json"))
        self.store.put(
            "refresh", refresh_value, refresh.model_dump(mode="json")
        )
        return OAuthToken(
            access_token=access_value,
            refresh_token=refresh_value,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client, refresh_token):
        value = self.store.get("refresh", refresh_token)
        token = RefreshToken.model_validate(value) if value else None
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.store.delete("refresh", refresh_token.token)
        return self._issue_tokens(
            client.client_id,
            scopes or refresh_token.scopes,
            refresh_token.subject,
        )

    async def load_access_token(self, token):
        value = self.store.get("access", token)
        access = AccessToken.model_validate(value) if value else None
        if access and access.expires_at and access.expires_at < time.time():
            self.store.delete("access", token)
            return None
        return access

    async def revoke_token(self, token) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        self.store.delete(kind, token.token)
        if token.subject:
            self.store.delete("grant", token.subject)

    def credentials_for_current_request(self) -> Credentials:
        access = get_access_token()
        if not access or not access.subject:
            raise RuntimeError(
                "Google Analytics tools require an authenticated grant"
            )
        grant = self.store.get("grant", access.subject)
        if not grant:
            raise RuntimeError("Google authorization has been revoked")
        credentials = Credentials(
            token=self.store.decrypt(grant["access_token"]),
            refresh_token=self.store.decrypt(grant["refresh_token"]),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.settings.google_client_id,
            client_secret=self.settings.google_client_secret,
            scopes=[ANALYTICS_SCOPE],
        )
        # google-auth compares expiry with a naive UTC datetime.
        credentials.expiry = datetime.fromtimestamp(
            grant["expires_at"], timezone.utc
        ).replace(tzinfo=None)
        return credentials


def create_server(settings: HostedSettings) -> FastMCP:
    store = GrantStore(settings.database_path, settings.encryption_key)
    provider = GoogleOAuthProvider(settings, store)
    server = FastMCP(
        name="Google Analytics MCP Server",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.server_url),
            resource_server_url=None,
            required_scopes=[MCP_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_SCOPE],
                default_scopes=[MCP_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
    )
    set_credential_provider(provider.credentials_for_current_request)

    for tool in (
        get_account_summaries,
        get_property_details,
        list_google_ads_links,
        list_property_annotations,
        get_custom_dimensions_and_metrics,
        run_report,
        run_realtime_report,
        run_funnel_report,
        run_conversions_report,
    ):
        server.add_tool(tool)

    @server.custom_route("/oauth/google/callback", methods=["GET"])
    async def google_callback(request: Request):
        return await provider.handle_google_callback(request)

    @server.custom_route("/oauth/google/start", methods=["GET"])
    async def google_start(request: Request):
        state = request.query_params.get("state")
        if not state:
            raise HTTPException(400, "Missing OAuth state")
        continue_url = provider.google_authorization_url(state)
        return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Google Analytics</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f9f9fb; color: #242429; font-size: 14px; }}
    main {{ width: min(420px, 100%); padding: 18px; border: 1px solid #d9d9df; border-radius: 16px; background: #fff; box-shadow: 0 16px 38px rgba(26, 26, 32, .08); }}
    header {{ display: flex; align-items: center; gap: 11px; margin-bottom: 18px; }}
    .mark {{ width: 38px; height: 38px; padding: 6px; border: 1px solid #e3e3e8; border-radius: 10px; background: #fff; }}
    h1 {{ margin: 0; font-size: 19px; line-height: 24px; font-weight: 650; letter-spacing: -.015em; }}
    .badge {{ margin-left: 5px; padding: 2px 6px; border-radius: 999px; background: #f0eff2; color: #67666f; font-size: 10px; font-weight: 650; vertical-align: 2px; text-transform: uppercase; letter-spacing: .04em; }}
    .explanation {{ margin: 0 0 18px; color: #4f4f58; line-height: 20px; }}
    .explanation a {{ color: inherit; text-underline-offset: 2px; }}
    ol {{ display: grid; gap: 13px; margin: 0; padding: 0; list-style: none; counter-reset: steps; }}
    li {{ position: relative; min-height: 22px; padding: 1px 0 0 32px; color: #303037; line-height: 20px; counter-increment: steps; }}
    li::before {{ content: counter(steps); position: absolute; left: 0; top: 0; display: grid; place-items: center; width: 21px; height: 21px; border-radius: 50%; background: #f0f0f3; color: #777780; font-size: 11px; font-weight: 650; }}
    .button {{ display: block; margin-top: 18px; padding: 10px 16px; border-radius: 8px; background: #29292e; color: white; text-align: center; text-decoration: none; font-weight: 600; line-height: 20px; transition: background .15s ease, transform .15s ease; }}
    .button:hover {{ background: #111114; }}
    .button:active {{ transform: translateY(1px); }}
  </style>
</head>
<body><main>
  <header>
    <svg class="mark" viewBox="0 0 32 32" role="img" aria-label="Google Analytics"><path fill="#f9ab00" d="M23 4a4 4 0 0 1 4 4v16a4 4 0 1 1-8 0V8a4 4 0 0 1 4-4Z"/><path fill="#e37400" d="M13 13a4 4 0 0 1 4 4v7a4 4 0 1 1-8 0v-7a4 4 0 0 1 4-4Z"/><circle cx="3.5" cy="24.5" r="3.5" fill="#e37400"/></svg>
    <h1>Connect Google Analytics <span class="badge">Alpha</span></h1>
  </header>
  <p class="explanation">Google is currently reviewing Tuft's Google Analytics integration. While we are in the approval process, using this feature requires a few extra steps. While Google displays a warning during this period, it has no impact on the security of Tuft's analytics or the way we safeguard your credentials. You are welcome to reach out to Eliot (<a href="mailto:eliot@expo.dev">eliot@expo.dev</a>) if you have any questions.</p>
  <ol>
    <li>Continue to Google.</li>
    <li>On the “Google hasn't verified this app” screen, select <strong>Advanced</strong>.</li>
    <li>Select <strong>Go to Tuft (unsafe)</strong> to finish connecting.</li>
  </ol>
  <a class="button" href="{escape(continue_url, quote=True)}">Continue to Google</a>
</main></body></html>""")

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request):
        return JSONResponse({"status": "ok"})

    return server


def run_server() -> None:
    settings = HostedSettings.from_env()
    create_server(settings).run(transport="streamable-http")


if __name__ == "__main__":
    run_server()
