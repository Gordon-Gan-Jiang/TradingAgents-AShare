from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class HttpRetry:
    attempts: int = 4
    base_sleep_s: float = 0.6
    timeout_s: float = 10.0


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retry: HttpRetry = HttpRetry(),
) -> Any:
    """GET JSON with retry + exponential backoff.

    This is intentionally small and dependency-light so providers can share
    consistent behavior and errors.
    """
    last_exc: Exception | None = None
    for attempt in range(retry.attempts):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=retry.timeout_s,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < retry.attempts - 1:
                time.sleep(retry.base_sleep_s * (2**attempt))
                continue
            raise RuntimeError(
                f"http_get_json failed after retries: {type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError(f"http_get_json failed: {last_exc}")

