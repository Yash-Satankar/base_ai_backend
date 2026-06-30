# app/core/logging_middleware.py
import time
import uuid
import json
import logging
import traceback
import re
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.auth import verify_access_token

logger = logging.getLogger("base_ai_structured")

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()
        
        # 1. Generate or propagate Request ID
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        # 2. Extract User ID from JWT
        user_id = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            try:
                token = auth_header.split(" ")[1]
                payload = verify_access_token(token)
                if payload:
                    user_id = payload.get("sub")
            except Exception:
                pass

        # 3. Extract Session and Project IDs from Path
        session_id = None
        project_id = None
        path = request.url.path
        
        session_match = re.search(r'/(?:session|conversation|download|sql|pdf)/([^/]+)', path)
        if session_match:
            # Validate it looks like a uuid or session key
            val = session_match.group(1)
            if len(val) >= 10:  # avoid matching short subroutes
                session_id = val

        project_match = re.search(r'/projects/([^/]+)', path)
        if project_match:
            val = project_match.group(1)
            if len(val) >= 10:
                project_id = val

        # Add to request state so endpoints can access it
        request.state.user_id = user_id
        request.state.session_id = session_id
        request.state.project_id = project_id

        # 4. Process Request
        response = None
        error_details = None
        try:
            response = await call_next(request)
        except Exception as exc:
            # Capture stack trace for unhandled errors
            error_details = {
                "class": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc()
            }
            raise exc
        finally:
            duration_ms = round((time.time() - start_time) * 1000, 2)
            status_code = response.status_code if response else 500

            # Fetch any state updates set by endpoint execution
            session_id = getattr(request.state, "session_id", session_id)
            project_id = getattr(request.state, "project_id", project_id)

            # Build structured log payload
            log_payload = {
                "request_id": request_id,
                "user_id": user_id,
                "project_id": project_id,
                "session_id": session_id,
                "method": request.method,
                "path": path,
                "query_params": dict(request.query_params),
                "status_code": status_code,
                "duration_ms": duration_ms,
                "client_ip": request.client.host if request.client else "unknown",
                "error": error_details
            }

            # Log as structured JSON
            if error_details or status_code >= 500:
                logger.error(json.dumps(log_payload))
            else:
                logger.info(json.dumps(log_payload))

        # Inject Request ID into response headers
        response.headers["X-Request-ID"] = request_id
        return response
