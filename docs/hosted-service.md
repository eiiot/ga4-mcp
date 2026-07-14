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

1. Add Streamable HTTP transport and health/readiness endpoints without
   changing the upstream stdio entry point.
2. Add MCP OAuth endpoints and Google OAuth callback handling.
3. Introduce a request-scoped credential provider and update Analytics client
   construction to use it.
4. Add persistent encrypted grant storage and disconnect/revocation support.
5. Add integration tests with two simultaneous users to prove credential
   isolation.
6. Deploy a staging service and connect it to the GA4 entry in Tuft.

## Upstream sync

Keep the Google repository as the `upstream` Git remote. Changes to Analytics
tools should continue to arrive from upstream; hosted transport,
authentication, and credential-context changes should remain isolated in this
fork where possible.
