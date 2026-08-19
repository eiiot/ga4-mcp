import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from analytics_mcp.hosted import (
    GMAIL_SCOPE,
    GoogleOAuthProvider,
    GrantStore,
    HostedSettings,
    create_server,
)


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
            self.store.put(
                "access_grant", f"mcp-{grant_id}", {"grant_id": grant_id}
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
        self.store.put(
            "state",
            "oauth-state",
            {
                "expires_at": time.time() + 60,
                "product": "ga4",
            },
        )

        url = self.provider.google_authorization_url("oauth-state")

        self.assertTrue(
            url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        )
        self.assertIn("client_id=client-id", url)
        self.assertIn("state=oauth-state", url)
        self.assertIn("analytics.readonly", url)

    def test_gmail_resource_requests_only_gmail_read_scope(self):
        self.store.put(
            "state",
            "gmail-state",
            {
                "expires_at": time.time() + 60,
                "product": "gmail",
            },
        )

        url = self.provider.google_authorization_url("gmail-state")
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["scope"], [GMAIL_SCOPE])
        self.assertNotIn("analytics.readonly", query["scope"][0])

    def test_grant_cannot_cross_google_products(self):
        self.store.put(
            "grant",
            "gmail-user",
            {
                "product": "gmail",
                "access_token": self.store.encrypt("google-access"),
                "refresh_token": self.store.encrypt("google-refresh"),
                "expires_at": time.time() + 3600,
            },
        )
        self.store.put(
            "access_grant", "mcp-gmail-user", {"grant_id": "gmail-user"}
        )
        token = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="mcp-gmail-user",
                    client_id="tuft",
                    scopes=["google.gmail.read"],
                    subject="gmail-user",
                )
            )
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "not valid for ga4"):
                self.provider.credentials_for_current_request("ga4")
        finally:
            auth_context_var.reset(token)

    def test_each_product_advertises_only_its_mcp_scope(self):
        settings = HostedSettings(
            server_url="https://google.example.com",
            google_client_id="client-id",
            google_client_secret="client-secret",
            encryption_key=Fernet.generate_key().decode(),
            database_path=str(
                Path(self.temporary_directory.name) / "server-grants.db"
            ),
        )

        with TestClient(create_server(settings)) as client:
            ga4 = client.get(
                "/.well-known/oauth-protected-resource/ga4/mcp"
            ).json()
            gmail = client.get(
                "/.well-known/oauth-protected-resource/gmail/mcp"
            ).json()

        self.assertEqual(ga4["scopes_supported"], ["google.ga4.read"])
        self.assertEqual(gmail["scopes_supported"], ["google.gmail.read"])
        self.assertEqual(ga4["resource"], "https://google.example.com/ga4/mcp")
        self.assertEqual(
            gmail["resource"], "https://google.example.com/gmail/mcp"
        )


if __name__ == "__main__":
    unittest.main()
