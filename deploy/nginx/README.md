# Protected ingress boundary

This template is the Python canary's only public path. It has one Python upstream and no legacy
fallback, so a request cannot become a dual write. Cohort selection and sticky routing happen at
the outer protected load balancer before this signer.

Required deployment inputs are the immutable Python upstream, an HTTPS authorization service,
server TLS material, auth-service mTLS material, and the HMAC secret shared only with the Python
service. The authorization response must supply validated `X-Auth-Principal-Type`,
`X-Auth-Principal-ID`, `X-Auth-Organization-ID`, `X-Auth-Workspace-ID`, and `X-Auth-Roles` headers.
All IDs must already be canonical UUIDs and roles must be canonical comma-separated values.

Render environment placeholders through the deployment secret/config system. Keep
`AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET` in the process secret store; do not render it into this
file. The njs module must be installed in the ingress image.

Before canary traffic, retain adversarial evidence for forged/missing identity headers, bad bearer
credentials, expired auth sessions, method/path/request-ID changes, cross-workspace claims,
oversized bodies, TLS/mTLS failures, SSE reconnect with `Last-Event-ID`, and loss of either upstream.
The template is not itself authorization to expose the service.

## Fixed-identity same-origin mode

`nginx.fixed-identity.conf.template` is the no-login, single-tenant deployment requested by the
first frontend release. It serves the built SPA from `/srv/docreview/frontend` and proxies only
`/api/` to FastAPI. The browser and API therefore share an origin. This mode is appropriate only
when every person who can reach the site is allowed to act as the same configured owner.

The Nginx process needs these server-side environment variables:

| Variable | Requirement |
| --- | --- |
| `AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET` | Same secret as FastAPI; at least 32 UTF-8 bytes |
| `DOCREVIEW_FIXED_PRINCIPAL_TYPE` | `user` or `service` |
| `DOCREVIEW_FIXED_PRINCIPAL_ID` | Canonical UUID present in the identity tables |
| `DOCREVIEW_FIXED_ORGANIZATION_ID` | Canonical UUID containing the workspace |
| `DOCREVIEW_FIXED_WORKSPACE_ID` | Canonical UUID used by the frontend/backend data |
| `DOCREVIEW_FIXED_ROLES` | Comma-separated roles; use `owner,admin` for all first-release actions |

For the local identity created by `docreview-init-local`, the IDs are:

```text
DOCREVIEW_FIXED_PRINCIPAL_TYPE=user
DOCREVIEW_FIXED_PRINCIPAL_ID=33333333-3333-4333-8333-333333333333
DOCREVIEW_FIXED_ORGANIZATION_ID=11111111-1111-4111-8111-111111111111
DOCREVIEW_FIXED_WORKSPACE_ID=22222222-2222-4222-8222-222222222222
DOCREVIEW_FIXED_ROLES=owner,admin
```

Keep the HMAC secret in the runtime secret store. Do not pass it to Vite, render it into the Nginx
configuration, or expose it through a client-visible environment endpoint. Set the corresponding
FastAPI values to the same secret plus `AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE=fixed-nginx` and an
age such as `AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS=300000`.

Render only the two non-secret placeholders, so shell expansion cannot accidentally write secrets
or Nginx variables into the file:

```sh
envsubst '${DOCREVIEW_PYTHON_UPSTREAM} ${DOCREVIEW_PUBLIC_SERVER_NAME}' \
  < nginx.fixed-identity.conf.template > /etc/nginx/nginx.conf
```

Copy `fixed_identity.js` to `/etc/nginx/fixed_identity.js`, copy `frontend/dist/` into
`/srv/docreview/frontend/`, mount the TLS files named by the template, and run an Nginx build with
the njs HTTP module. The API upstream must remain on a private network; exposing FastAPI directly
would bypass this identity boundary.

The gateway preserves a valid browser `X-Request-ID`, signs that exact value, and explicitly passes
`Last-Event-ID`. This lets POST SSE reconnect with the same request/body/cursor. Caller-provided
`X-DocReview-*` headers are always overwritten before proxying.

Because this mode intentionally has no user authentication, do not expose it to an untrusted
network. Moving to multiple users or differentiated permissions requires the authorization-service
template above, not additional browser headers.
