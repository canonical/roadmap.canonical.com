# Authentication flow

The app uses OpenID Connect (OIDC) for authentication, providing transparent single sign-on (SSO) for internal users.

## Design goals

1. **Zero-friction SSO** — no login page, no login button. If the user has a corporate SSO session, authentication is completely silent.
2. **Graceful disable** — authentication can be turned off entirely for local development by leaving `OIDC_CLIENT_ID` empty.
3. **No server-side session store** — sessions are stored in signed cookies, avoiding the need for Redis or a session table.
4. **No logout** — this is an internal-only tool. Users rely on corporate SSO session lifecycle.

## How the flow works

```
User                    App                     IdP (Hydra)
 │                       │                        │
 │  GET /                │                        │
 │──────────────────────▶│                        │
 │                       │ No session cookie       │
 │  302 → /login         │                        │
 │◀──────────────────────│                        │
 │                       │                        │
 │  GET /login           │                        │
 │──────────────────────▶│                        │
 │                       │ Build authorize URL     │
 │  302 → IdP/authorize  │                        │
 │◀──────────────────────│                        │
 │                       │                        │
 │  (IdP authenticates — silent if SSO session exists)
 │                       │                        │
 │  302 → /callback?code=...                      │
 │◀───────────────────────────────────────────────│
 │                       │                        │
 │  GET /callback        │                        │
 │──────────────────────▶│                        │
 │                       │  POST /token (code)     │
 │                       │───────────────────────▶│
 │                       │  {access_token, id_token}
 │                       │◀───────────────────────│
 │                       │                        │
 │                       │ Store user in session   │
 │  302 → /              │ Set cookie              │
 │◀──────────────────────│                        │
 │                       │                        │
 │  GET / (with cookie)  │                        │
 │──────────────────────▶│                        │
 │                       │ Session valid           │
 │  200 OK (roadmap)     │                        │
 │◀──────────────────────│                        │
```

## Middleware architecture

Authentication is enforced by `OIDCAuthMiddleware`, a custom Starlette middleware:

```
Request → CORS middleware → Session middleware → OIDC Auth middleware → Route handler
```

The middleware stack is applied in reverse order (`add_middleware` uses a stack):

1. **CORS** (outermost) — handles cross-origin headers
2. **Session** — decodes/encodes the signed session cookie
3. **OIDC Auth** (innermost) — checks `request.session["user"]` and redirects if missing

### Public paths

The paths `/login` and `/callback` are excluded from authentication checks (they are part of the auth flow itself).

### API vs browser requests

| Request type | Unauthenticated response |
|-------------|-------------------------|
| Browser (`GET /`) | `302 Redirect` to `/login` |
| API (`/api/*`) | `401 {"detail": "Authentication required"}` |

## Session management

- **Storage**: Signed cookie (`roadmap_session`) using Starlette's `SessionMiddleware` + `itsdangerous`.
- **Lifetime**: 24 hours (`max_age=86400`).
- **Contents**: The OIDC `userinfo` dict (email, name, sub).
- **Re-authentication**: After cookie expiry, the next request triggers a silent re-auth through the IdP. If the user still has an active SSO session, this is transparent.

## Why Authlib?

Authlib was chosen over alternatives like `python-jose` or raw OIDC implementation because:

- It handles **OIDC Discovery** automatically (fetches `/.well-known/openid-configuration`)
- It manages **JWKS rotation** (key refresh)
- It integrates directly with **Starlette** (which FastAPI is built on)
- Token exchange, userinfo endpoint, and session management are handled with minimal code

## Production considerations

| Concern | Status |
|---------|--------|
| `SESSION_SECRET` must be stable across restarts and replicas | Default is random — **must be explicitly set in production** |
| Cookie `https_only` flag | Currently `False` — should be `True` behind HTTPS |
| CSRF protection | Starlette's `same_site="lax"` provides basic protection |
| Token refresh | Not implemented — sessions expire and re-auth silently |
