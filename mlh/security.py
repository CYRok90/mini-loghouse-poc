"""HTTP hardening for a loopback, single-operator app.

- Host header allowlist (DNS rebinding defence) -> 421.
- State-changing requests need an allowed Origin, JSON content type and the
  custom X-MLH-Request header. No CORS headers are ever sent, so cross-site
  pages cannot read responses or send the custom header. (Council decision:
  no double-submit token; there is no cookie-based auth to protect.)
- Validation errors never echo the submitted input (passwords!).
- Unhandled exceptions return a fixed message; raw text is not logged either,
  because chDB exception text can contain SQL with escaped secrets.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")


def error_body(category: str, message: str, **extra) -> dict:
    return {"error": {"category": category, "message": message, **extra}}


def install(app: FastAPI, port: int) -> None:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse(error_body("bad_host", "허용되지 않은 Host입니다."), status_code=421)
        if request.method not in SAFE_METHODS:
            if request.headers.get("origin") not in origins:
                return JSONResponse(error_body("bad_origin", "허용되지 않은 Origin입니다."), status_code=403)
            if request.headers.get("x-mlh-request") != "1":
                return JSONResponse(error_body("missing_header", "요청 헤더가 올바르지 않습니다."), status_code=403)
            ctype = request.headers.get("content-type", "").split(";")[0].strip()
            if request.method != "DELETE" and ctype != "application/json":
                return JSONResponse(error_body("bad_content_type", "JSON 요청만 허용됩니다."), status_code=415)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - keep raw text (may contain SQL) out of logs
            log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
            response = JSONResponse(error_body("internal", "서버 내부 오류가 발생했습니다."), status_code=500)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:  # pages/static: always revalidate (ETag) so UI fixes are never masked by a stale cache
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation(_: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]), "message": e.get("msg", "")}
                  for e in exc.errors()]
        return JSONResponse(error_body("invalid_input", "입력값을 확인하세요.", fields=fields), status_code=422)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
        return JSONResponse(error_body("internal", "서버 내부 오류가 발생했습니다."), status_code=500)
