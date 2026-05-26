# app/core/logger.py

import logging
import json
from datetime import datetime

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        handlers=[
            logging.StreamHandler(),                    # console
            logging.FileHandler('logs/app.log'),       # file
        ]
    )

# app/main.py — add request logging middleware:
import time
from fastapi import Request

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round(time.time() - start, 3)

    logger.info(
        f"{request.method} {request.url.path} "
        f"| status={response.status_code} "
        f"| duration={duration}s "
        f"| ip={request.client.host}"
    )
    return response