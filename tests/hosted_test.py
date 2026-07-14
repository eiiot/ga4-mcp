import time

from cryptography.fernet import Fernet
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.provider import AccessToken

from analytics_mcp.hosted import GoogleOAuthProvider, GrantStore, HostedSettings


def make_provider(tmp_path):
    key = Fernet.generate_key().decode()
    store = GrantStore(str(tmp_path / "grants.db"), key)
    settings = HostedSettings(
        server_url="https://ga4.example.com",
        google_client_id="client-id",
        google_client_secret="client-secret",
        encryption_key=key,
        database_path=str(tmp_path / "grants.db"),
    )
    return GoogleOAuthProvider(settings, store), store


def test_credentials_are_selected_from_authenticated_grant(tmp_path):
    provider, store = make_provider(tmp_path)
    for grant_id, access, refresh in (
        ("user-a", "google-access-a", "google-refresh-a"),
        ("user-b", "google-access-b", "google-refresh-b"),
    ):
        store.put(
            "grant",
            grant_id,
            {
                "access_token": store.encrypt(access),
                "refresh_token": store.encrypt(refresh),
                "expires_at": time.time() + 3600,
            },
        )

    credentials = []
    for grant_id in ("user-a", "user-b"):
        token = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token=f"mcp-{grant_id}",
                    client_id="tuft",
                    scopes=["analytics.read"],
                    subject=grant_id,
                )
            )
        )
        try:
            credentials.append(provider.credentials_for_current_request())
        finally:
            auth_context_var.reset(token)

    assert credentials[0].token == "google-access-a"
    assert credentials[0].refresh_token == "google-refresh-a"
    assert credentials[0].expiry is not None
    assert credentials[1].token == "google-access-b"
    assert credentials[1].refresh_token == "google-refresh-b"


def test_grant_tokens_are_encrypted_at_rest(tmp_path):
    _provider, store = make_provider(tmp_path)
    encrypted = store.encrypt("super-secret-refresh-token")
    assert "super-secret-refresh-token" not in encrypted
    assert store.decrypt(encrypted) == "super-secret-refresh-token"
