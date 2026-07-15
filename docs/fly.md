# Deploy to Fly.io

The hosted server runs as one stateless Fly app backed by Fly Managed
Postgres. Managed Postgres is preferred over the legacy unmanaged Fly Postgres
app: Fly handles backups, updates, and database operations, while the MCP app
only needs the `DATABASE_URL` added by `fly mpg attach`.

The checked-in configuration uses:

- app: `tuft-ga4-mcp`
- region: `sjc`
- public URL: `https://tuft-ga4-mcp.fly.dev`
- Google callback: `https://tuft-ga4-mcp.fly.dev/oauth/google/callback`

If the app name is unavailable, change both `app` and `GA4_MCP_SERVER_URL` in
`fly.toml` before starting.

## One-time setup

From the repository root:

```shell
fly auth login
fly apps create tuft-ga4-mcp
fly mpg create --name tuft-ga4-mcp-db --region sjc --plan Starter
```

The last command prints the managed Postgres cluster ID. Attach it to the app:

```shell
fly mpg attach YOUR_CLUSTER_ID -a tuft-ga4-mcp
```

This creates a `DATABASE_URL` secret. Do not create a Fly volume for the MCP
app; all durable state belongs in Postgres.

Add the fixed Google client ID, an encryption key, and the Google client secret
without putting the secret in shell history:

```shell
export GA4_MCP_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
read -s "GOOGLE_OAUTH_CLIENT_SECRET?Google OAuth client secret: "; echo
printf '%s\n' \
  'GOOGLE_OAUTH_CLIENT_ID=948854465141-6q80gd45eafh5uneh1ttrsne8n8agg50.apps.googleusercontent.com' \
  "GOOGLE_OAUTH_CLIENT_SECRET=$GOOGLE_OAUTH_CLIENT_SECRET" \
  "GA4_MCP_ENCRYPTION_KEY=$GA4_MCP_ENCRYPTION_KEY" \
  | fly secrets import -a tuft-ga4-mcp
unset GOOGLE_OAUTH_CLIENT_SECRET GA4_MCP_ENCRYPTION_KEY
```

Keep the encryption key backed up in the team's secret manager. Losing or
rotating it without a migration makes existing Google grants unreadable.

In Google Cloud, add this exact authorized redirect URI to the OAuth web
client:

```text
https://tuft-ga4-mcp.fly.dev/oauth/google/callback
```

## Deploy and verify

```shell
fly deploy
curl --fail https://tuft-ga4-mcp.fly.dev/health
fly logs -a tuft-ga4-mcp
```

The MCP endpoint is:

```text
https://tuft-ga4-mcp.fly.dev/mcp
```

The application creates its small `records` table idempotently during startup.
The health check also queries Postgres, so a Machine is not considered healthy
when it cannot reach the grant store.

## Scale out

Streamable HTTP is configured as stateless and all OAuth state is in Postgres,
so additional Machines do not need sticky sessions:

```shell
fly scale count 2 -a tuft-ga4-mcp
```

Keep Machines in the same region as Postgres unless a later design adds
regional database replicas.
