# Configuring authentication

The app uses OpenID Connect (OIDC) for authentication. When configured, unauthenticated users are automatically redirected to the identity provider — no login page or button is needed.

## Disable authentication (local development)

Authentication is **disabled** when `OIDC_CLIENT_ID` is empty (the default). No changes are needed for local development.

## Enable authentication

Add these to your `.env` file (or set them as environment variables in production):

```bash
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
OIDC_ISSUER=https://iam.green.canonical.com
OIDC_REDIRECT_URI=https://roadmap.example.com/callback
SESSION_SECRET=a-random-string-at-least-32-chars
```

### Environment variable reference

| Variable | Charm alias | Default | Description |
|----------|------------|---------|-------------|
| `OIDC_CLIENT_ID` | `APP_OIDC_CLIENT_ID` | `""` (disabled) | OIDC client ID from your IdP |
| `OIDC_CLIENT_SECRET` | `APP_OIDC_CLIENT_SECRET` | `""` | OIDC client secret |
| `OIDC_ISSUER` | `APP_OIDC_ISSUER` | `https://iam.green.canonical.com` | OIDC issuer URL (must serve `/.well-known/openid-configuration`) |
| `OIDC_REDIRECT_URI` | `APP_OIDC_REDIRECT_URI` | `http://localhost:8000/callback` | Callback URL registered with the IdP |
| `SESSION_SECRET` | `APP_SESSION_SECRET` | Random on startup | Secret for signing session cookies. **Set explicitly** for multi-replica deployments. |

## How the flow works

1. User visits `/` → no session → redirected to `/login`
2. `/login` redirects to the OIDC authorization endpoint
3. IdP authenticates the user (SSO) and redirects to `/callback`
4. `/callback` exchanges the authorization code for tokens, stores user info in a signed session cookie
5. User is redirected back to `/` — now authenticated

The session cookie (`roadmap_session`) is valid for **24 hours**. After expiry, the user is silently re-authenticated via the IdP.

## API authentication

When OIDC is enabled, **all routes** are protected:

- **Browser requests** (HTML pages) → redirect to `/login`
- **API requests** (`/api/*`) → return `401 JSON` with `{"detail": "Authentication required"}`

To call API endpoints with `curl` when auth is enabled:

1. Log in via the browser
2. Visit `/token` to get a ready-to-copy `curl` command with your session cookie
3. Use the cookie in API calls:
   ```bash
   curl -b 'roadmap_session=<cookie-value>' http://localhost:8000/api/v1/status
   ```

## OIDC provider requirements

The identity provider must support:

- **OpenID Connect Discovery** at `{issuer}/.well-known/openid-configuration`
- **Authorization Code Grant** with `response_type=code`
- **Scopes**: `openid email profile`
- **Token endpoint auth method**: `client_secret_post`

## Production considerations

- Set `SESSION_SECRET` to a stable value — a random default is generated on startup, which breaks sessions when the app restarts or across replicas.
- Set `OIDC_REDIRECT_URI` to your production URL (e.g. `https://roadmap.canonical.com/callback`).
- The session cookie's `https_only` is currently `False` — set it to `True` when served behind HTTPS (requires code change in `app.py`).
- There is **no logout flow** — this is an internal-only tool. Users rely on corporate SSO session lifecycle.
