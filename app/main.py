from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from .routes import router

app = FastAPI(title="Payments-Grade Wallet Ledger", version="1.0.0")
app.include_router(router)

@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={
        "code": detail.get("code", "HTTP_ERROR"),
        "message": detail.get("message", "Request failed"),
        "request_id": request.headers.get("X-Request-ID", detail.get("request_id", "request")),
    })

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "Invalid request", "request_id": request.headers.get("X-Request-ID", "request")})

@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "Unexpected server error", "request_id": request.headers.get("X-Request-ID", "request")})

@app.get("/health")
def health():
    return {"status": "ok"}
