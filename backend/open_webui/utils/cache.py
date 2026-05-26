import hashlib
import json
from typing import Any, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse


DEFAULT_MAX_AGE = 30


def compute_etag(content: Any) -> str:
    payload = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def etag_response(
    content: Any,
    request: Request,
    max_age: int = DEFAULT_MAX_AGE,
) -> Response:
    etag = compute_etag(content)
    quoted = f'"{etag}"'
    cache_control = f"private, max-age={max_age}"

    inm = request.headers.get("if-none-match") if request is not None else None
    if inm:
        # If-None-Match may carry a list of etags; tolerate both quoted and bare forms.
        candidates = {part.strip().strip('"') for part in inm.split(",")}
        if etag in candidates:
            return Response(
                status_code=304,
                headers={"ETag": quoted, "Cache-Control": cache_control},
            )

    return JSONResponse(
        content=content,
        headers={"ETag": quoted, "Cache-Control": cache_control},
    )
