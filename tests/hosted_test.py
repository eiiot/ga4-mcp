import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from analytics_mcp.hosted import (
    GoogleOAuthProvider,
    GrantStore,
    HostedSettings,
    create_server,
)


class FakePostgresGrantStore:
    def __init__(self, _database_url, _encryption_key):
        self.closed = False

    def close(self):
        self.closed = True


class HostedServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        key = Fernet.generate_key().decode()
        database_path = str(Path(self.temporary_directory.name) / "grants.db")
        self.store = GrantStore(database_path, key)
        settings = HostedSettings(
            server_url="https://ga4.example.com",
            google_client_id="client-id",
            google_client_secret="client-secret",
            encryption_key=key,
            database_path=database_path,
        )
        self.provider = GoogleOAuthProvider(settings, self.store)

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def test_credentials_are_selected_from_authenticated_grant(self):
        for grant_id, access, refresh in (
            ("user-a", "google-access-a", "google-refresh-a"),
            ("user-b", "google-access-b", "google-refresh-b"),
        ):
            self.store.put(
                "grant",
                grant_id,
                {
                    "access_token": self.store.encrypt(access),
                    "refresh_token": self.store.encrypt(refresh),
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
                credentials.append(
                    self.provider.credentials_for_current_request()
                )
            finally:
                auth_context_var.reset(token)

        self.assertEqual(credentials[0].token, "google-access-a")
        self.assertEqual(credentials[0].refresh_token, "google-refresh-a")
        self.assertIsNotNone(credentials[0].expiry)
        self.assertIsNone(credentials[0].expiry.tzinfo)
        self.assertEqual(credentials[1].token, "google-access-b")
        self.assertEqual(credentials[1].refresh_token, "google-refresh-b")

    def test_grant_tokens_are_encrypted_at_rest(self):
        encrypted = self.store.encrypt("super-secret-refresh-token")
        self.assertNotIn("super-secret-refresh-token", encrypted)
        self.assertEqual(
            self.store.decrypt(encrypted), "super-secret-refresh-token"
        )

    def test_google_authorization_url_uses_saved_transaction(self):
        self.store.put("state", "oauth-state", {"expires_at": time.time() + 60})

        url = self.provider.google_authorization_url("oauth-state")

        self.assertTrue(
            url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        )
        self.assertIn("client_id=client-id", url)
        self.assertIn("state=oauth-state", url)
        self.assertIn("analytics.readonly", url)

    @patch("analytics_mcp.hosted.PostgresGrantStore", FakePostgresGrantStore)
    def test_server_does_not_give_fastmcp_ownership_of_shared_store(self):
        settings = HostedSettings(
            server_url="https://ga4.example.com",
            google_client_id="client-id",
            google_client_secret="client-secret",
            encryption_key=Fernet.generate_key().decode(),
            database_url="postgresql://example.test/ga4",
        )

        server = create_server(settings)

        self.assertIsNone(server.settings.lifespan)

    def test_server_advertises_protected_resource_metadata(self):
        settings = HostedSettings(
            server_url="https://ga4.example.com",
            google_client_id="client-id",
            google_client_secret="client-secret",
            encryption_key=Fernet.generate_key().decode(),
            database_path=str(Path(self.temporary_directory.name) / "metadata.db"),
        )
        server = create_server(settings)

        with TestClient(server.streamable_http_app()) as client:
            response = client.get("/.well-known/oauth-protected-resource")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "resource": "https://ga4.example.com/mcp",
                "authorization_servers": ["https://ga4.example.com"],
                "scopes_supported": ["analytics.read"],
                "bearer_methods_supported": ["header"],
            },
        )


if __name__ == "__main__":
    unittest.main()
