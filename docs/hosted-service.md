# Hosted service design

## Goal

Run one shared Google Analytics MCP service for Tuft while keeping each user's
Google identity, grants, and Analytics data isolated. Users connect through the
normal Tuft MCP OAuth flow and sign in to Google in the browser.

The upstream server is a local stdio process. It obtains one set of Google
Application Default Credentials and caches it globally, so it cannot safely
serve multiple users unchanged.

## Request flow

1. Tuft begins an MCP OAuth authorization request against this service.
2. The service redirects the user to Google with the
   `analytics.readonly` scope and a state value bound to the MCP authorization
   transaction.
3. Google redirects back to this service. The service exchanges the code and
   stores the Google refresh token encrypted, associated with the MCP grant.
4. The service finishes the MCP authorization flow and returns control to
   Tuft.
5. For each MCP request, the service authenticates the Tuft access token,
   resolves its grant, loads that grant's Google credentials, and constructs
   Analytics API clients for that request.

No Google access or refresh token is returned to Tuft or shared between users.

## Architecture

- **Transport:** MCP Streamable HTTP rather than stdio.
- **MCP authorization server:** OAuth authorization-server metadata,
  authorization, token, and client-registration support required by Tuft.
- **Google OAuth adapter:** Google authorization-code flow with offline access
  and the `https://www.googleapis.com/auth/analytics.readonly` scope.
- **Grant store:** MCP clients, authorization codes, access/refresh tokens, and
  encrypted Google refresh tokens. Postgres is appropriate for the first
  deployment.
- **Credential context:** Resolve Google credentials from the authenticated MCP
  grant for every request. Remove the process-global `_CREDENTIALS` cache from
  the hosted path.
- **Deployment:** One stateless HTTP service plus Postgres and an encryption key
  supplied by the host's secret manager. Multiple replicas can share the same
  store.

## Isolation requirements

- Never select Google credentials from caller-controlled account or property
  identifiers.
- Bind every Google credential to an authenticated MCP grant and Tuft user.
- Encrypt Google refresh tokens at rest and avoid logging authorization codes,
  access tokens, or refresh tokens.
- Use PKCE, exact redirect URI matching, short-lived authorization codes, and
  rotating/revocable access tokens.
- Revoke the Google grant and delete stored credentials when a connection is
  removed.

## MVP milestones

The fork includes Streamable HTTP, MCP dynamic client registration and OAuth
endpoints, Google OAuth, encrypted grant storage, token refresh/revocation, and
request-scoped credentials. Hosted deployments use Postgres through
`DATABASE_URL`; SQLite remains available for local single-machine testing. The
existing stdio entry point is unchanged.

Before production use:

1. Persist refreshed Google access tokens to avoid refreshing once per request
   after their original expiry.
2. Add end-to-end tests against a Google OAuth test project.
3. Deploy a staging service and connect it to the GA4 entry in Tuft.

## Running hosted mode

Create a Google web OAuth client with this exact redirect URI:

```text
https://YOUR_HOST/oauth/google/callback
```

Generate the encryption key once with
`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
Then run the container with:

```shell
docker build -t ga4-mcp .
docker run --rm -p 8000:8000 -v ga4-mcp-data:/data \
  -e GA4_MCP_SERVER_URL=https://YOUR_HOST \
  -e GOOGLE_OAUTH_CLIENT_ID=... \
  -e GOOGLE_OAUTH_CLIENT_SECRET=... \
  -e GA4_MCP_ENCRYPTION_KEY=... \
  -e GA4_MCP_DATABASE_PATH=/data/analytics-mcp.db \
  ga4-mcp
```

The MCP URL is `https://YOUR_HOST/mcp`; health checks use `/health`.

For a production-shaped Fly deployment backed by Managed Postgres, see
[Deploy to Fly.io](fly.md).

## Upstream sync

Keep the Google repository as the `upstream` Git remote. Changes to Analytics
tools should continue to arrive from upstream; hosted transport,
authentication, and credential-context changes should remain isolated in this
fork where possible.
