# src/middleware.py
# Shared middleware, decorators, and request helpers

import os
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"
# Pseudo-username on in-process tool-loopback requests; require_admin trusts it and it is reserved.
INTERNAL_TOOL_USER = "internal-tool"

# Forwarding headers that mark a request as tunnel/proxy-forwarded rather
# than a direct loopback connection (mirrors app.py's _is_trusted_loopback:
# a remote visitor behind cloudflared/a reverse proxy otherwise looks like
# 127.0.0.1 and would inherit local trust).
_PROXY_FWD_HEADERS = (
    "cf-connecting-ip", "cf-ray", "cf-visitor",
    "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
)


def _is_direct_loopback(request: Request) -> bool:
    """True only for a direct loopback connection with no forwarding headers."""
    try:
        host = request.client.host if request.client else None
    except Exception:
        return False
    if host not in ("127.0.0.1", "::1"):
        return False
    try:
        headers = request.headers or {}
    except Exception:
        return False
    return not any(headers.get(h) for h in _PROXY_FWD_HEADERS)


def is_cors_preflight(method: str, headers) -> bool:
    """True for a genuine CORS preflight: an OPTIONS request carrying the
    Access-Control-Request-Method header. Such requests are credential-less by
    design and must reach CORSMiddleware to be answered -- gating them on auth
    401s the preflight and breaks every cross-origin browser/WebView client.
    Pure so it can be unit-tested without standing up the app."""
    return method == "OPTIONS" and "access-control-request-method" in headers


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Odysseus-Internal-Token), or
    # (b) the auth middleware already validated the token and stamped
    #     request.state.current_user = "internal-tool".
    # Path (a) additionally requires a DIRECT loopback connection: without
    # it a leaked/static token would grant remote admin (tunnel-forwarded
    # remotes also present as 127.0.0.1, hence the forwarding-header check).
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            if not _is_direct_loopback(request):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "Internal-tool token presented from non-loopback client %s — denied",
                    getattr(getattr(request, "client", None), "host", "?"))
                raise HTTPException(403, "Admin only")
            return
        if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
            return
    except HTTPException:
        raise
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate a per-request nonce for inline scripts
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        path = request.url.path

        # Tool render endpoints
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        # Document page previews are embedded only by this same-origin UI.
        is_document_pdf_preview = (
            path.startswith("/api/document/")
            and len(path.split("/")) == 5
            and path.rsplit("/", 1)[-1] in {"render-pdf", "render-docx"}
        )
        # Visual report pages are self-contained HTML — need inline scripts + external images
        is_report = path.startswith("/api/research/report/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"

        is_https = (
            request.url.scheme == "https"
            or request.headers.get("X-Forwarded-Proto") == "https"
        )
        if is_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        if is_report:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        elif is_tool_render:
            # Skip framing headers for tools.
            pass
        elif is_document_pdf_preview:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                "frame-ancestors 'self'"
            )
        else:
            response.headers["X-Frame-Options"] = "DENY"
            # NOTE: `style-src 'unsafe-inline'` is intentionally retained.
            # `static/index.html` and `static/login.html` ship inline <style>
            # blocks, and several JS modules build runtime `style=""` attrs.
            # Migrating to nonce-only requires templating the HTML files +
            # auditing every JS-set style attribute. Since inline styles
            # don't execute script, the residual risk is visual-only.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; "
                "media-src 'self' blob:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'none'"
            )
        return response
