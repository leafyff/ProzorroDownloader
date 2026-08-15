"""HTTP-клієнт: пул з'єднань, ретраї з експоненційною паузою, обмеження швидкості."""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter

from .. import APP_NAME, APP_VERSION

USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (+open data client)"

#: Коди, які має сенс повторити.
RETRY_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class Cancelled(Exception):
    """Користувач зупинив операцію."""


class RateLimiter:
    """Простий потокобезпечний обмежувач «не більше N запитів за секунду»."""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.min_interval


class Stats:
    """Лічильники для відображення в UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.retries = 0
        self.errors = 0
        self.bytes = 0

    def add(self, *, requests: int = 0, retries: int = 0, errors: int = 0, nbytes: int = 0) -> None:
        with self._lock:
            self.requests += requests
            self.retries += retries
            self.errors += errors
            self.bytes += nbytes

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "errors": self.errors,
                "bytes": self.bytes,
            }


class HttpClient:
    """Обгортка над requests.Session із ретраями та скасуванням."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        max_retries: int = 4,
        rps: float = 12.0,
        pool_size: int = 32,
        cancel_event: threading.Event | None = None,
        on_log: Callable[[str, str], None] | None = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = RateLimiter(rps)
        self.cancel_event = cancel_event or threading.Event()
        self.stats = Stats()
        self._on_log = on_log

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
        })

    # --- службове ---------------------------------------------------------

    def log(self, level: str, msg: str) -> None:
        if self._on_log:
            self._on_log(level, msg)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # --- базовий запит ----------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
    ) -> requests.Response:
        """Виконує запит із ретраями. Кидає requests.RequestException після вичерпання спроб."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.check_cancel()
            self.limiter.acquire()
            self.check_cancel()
            try:
                resp = self.session.request(
                    method, url,
                    json=json_body,
                    params=params,
                    stream=stream,
                    timeout=timeout or self.timeout,
                )
                self.stats.add(requests=1)
                if resp.status_code in RETRY_CODES and attempt < self.max_retries:
                    delay = self._retry_delay(attempt, resp)
                    self.stats.add(retries=1)
                    self.log("warn", f"HTTP {resp.status_code} — повтор через {delay:.1f} с: {url[:120]}")
                    resp.close()
                    self._sleep(delay)
                    continue
                return resp
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                last_exc = exc
                self.stats.add(requests=1)
                if attempt >= self.max_retries:
                    break
                delay = self._retry_delay(attempt, None)
                self.stats.add(retries=1)
                self.log("warn", f"Мережева помилка ({type(exc).__name__}) — повтор через {delay:.1f} с")
                self._sleep(delay)
        self.stats.add(errors=1)
        raise last_exc or requests.RequestException(f"Не вдалося виконати запит: {url}")

    def note_error(self) -> None:
        """Зафіксувати помилку, що сталася поза :meth:`request` (напр. під час читання тіла)."""
        self.stats.add(errors=1)

    def note_bytes(self, count: int) -> None:
        self.stats.add(nbytes=count)

    def _retry_delay(self, attempt: int, resp: requests.Response | None) -> float:
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    return min(float(ra), 60.0)
                except ValueError:
                    pass
        return min(1.5 * (2 ** attempt), 30.0) * (0.7 + 0.6 * random.random())

    def _sleep(self, seconds: float) -> None:
        """Пауза, яку можна перервати скасуванням."""
        if self.cancel_event.wait(seconds):
            raise Cancelled()

    def backoff(self, attempt: int) -> None:
        """Пауза перед повторною спробою вищого рівня (наприклад, цілого файлу)."""
        self.stats.add(retries=1)
        self._sleep(self._retry_delay(attempt, None))

    # --- зручні обгортки --------------------------------------------------

    def get_json(self, url: str, params: dict | None = None) -> Any:
        resp = self.request("GET", url, params=params)
        if resp.status_code >= 400:
            self.stats.add(errors=1)
            raise requests.HTTPError(f"HTTP {resp.status_code} для {url}: {resp.text[:200]}", response=resp)
        return resp.json()

    def post_json(self, url: str, body: dict) -> Any:
        resp = self.request("POST", url, json_body=body)
        if resp.status_code >= 400:
            self.stats.add(errors=1)
            raise requests.HTTPError(f"HTTP {resp.status_code} для {url}: {resp.text[:300]}", response=resp)
        return resp.json()
