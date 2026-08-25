"""HTTP-клієнт: пул з'єднань, ретраї з експоненційною паузою, обмеження швидкості."""
from __future__ import annotations

import collections
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

#: Квоти хостів: ``підрядок URL → (запитів, вікно в секундах)``.
#:
#: Портал рахує **не миттєвий темп, а кількість запитів у вікні**. Виміряно на
#: живому сервері: рівно 60 успішних запитів поспіль, 61-й — ``429`` із
#: ``Retry-After``, що дорівнює 60 секундам мінус час від першого запиту. Тобто
#: бюджет — 60 запитів на 60 секунд, і будь-який сталий темп понад один запит
#: на секунду вичерпує його достроково, після чого сервер мовчить до кінця
#: вікна. Саме тому попередні 2 запити/с ловили 429 через півхвилини роботи.
#:
#: Просимо трохи менше за виміряне: похибка годинників і повторні спроби не
#: мають з'їдати останній запит вікна.
#:
#: Хости зіставляються **точно за іменем**, а не за підрядком. Це не дрібниця:
#: ``market-api.prozorro.gov.ua`` містить ``prozorro.gov.ua`` як підрядок, але
#: має власну квоту — перевірено, що поки портал відповідає 429, каталог віддає
#: картки без жодної відмови. Зіставлення за підрядком душило його дарма.
HOST_QUOTAS = {"prozorro.gov.ua": (58, 60.0)}

#: Темп для хостів із виміряною пропускною здатністю: ``підрядок → запитів/с``.
#:
#: Центральна база квоти не має, але загальний ліміт (типово 12 з/с) душив і
#: її. Виміряно на живому сервері: 8 з'єднань дають ~17–23 з/с, 16 з'єднань —
#: ~29–34 з/с, далі темп падає через змагання за з'єднання. Жодної відмови на
#: жодному рівні. Беремо 30 — це середина виміряного оптимуму.
#:
#: Загальний ліміт із налаштувань лишається для хостів без власної політики.
#: Числа тут — виміряна верхня межа зі **запасом**: на короткому сплеску
#: Центральна база тримала 29–34 запити/с, але під тривалим навантаженням
#: (дві тисячі запитів поспіль) зрідка відповідала 429. Темп до того ж
#: підлаштовується сам: на відмову :class:`RateLimiter` його знижує, а за
#: серії вдалих запитів поволі повертає.
HOST_RATES = {
    "public.api.openprocurement.org": 28.0,
    #: Каталог товарів: виміряно 23–24 запити/с на 4–8 з'єднаннях, далі темп
    #: падає. Квота в нього своя, окрема від порталу.
    "market-api.prozorro.gov.ua": 20.0,
}


class Cancelled(Exception):
    """Користувач зупинив операцію."""


class WindowLimiter:
    """Обмежувач за квотою «N запитів на вікно завдовжки W секунд».

    Тримає позначки часу зроблених запитів і, коли бюджет вичерпано, чекає
    рівно доти, доки найстаріша з них не вийде за межі вікна. На відміну від
    :class:`RateLimiter`, темп не підбирається навпомацки: він відомий наперед,
    тож і 429 отримувати не доводиться. Рівномірний темп теж вкладався б у
    квоту, але після вимушеної паузи (мережева помилка, повтор) залишок
    бюджету пропав би — а так він використовується повністю.
    """

    def __init__(self, limit: int, window: float):
        self.limit = max(1, int(limit))
        self.window = float(window)
        self._lock = threading.Lock()
        self._marks: collections.deque[float] = collections.deque()

    def acquire(self, cancel: threading.Event | None = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._marks and now - self._marks[0] >= self.window:
                    self._marks.popleft()
                if len(self._marks) < self.limit:
                    self._marks.append(now)
                    return
                wait = self.window - (now - self._marks[0])
            # Чекаємо скибками, щоб зупинка користувача спрацьовувала одразу,
            # а не аж наприкінці вікна.
            if cancel is not None:
                if cancel.wait(min(wait, 0.25)):
                    raise Cancelled()
            else:
                time.sleep(min(wait, 0.25))

    def penalize(self) -> float:
        """Сервер усе одно відмовив — вважаємо вікно вичерпаним і перечікуємо його.

        Розбіжність можлива: сервер міг рахувати й наші попередні запити з
        іншого запуску програми.
        """
        with self._lock:
            self._marks = collections.deque([time.monotonic()] * self.limit)
        return self.limit / self.window

    def reward(self) -> None:
        """Нічого: бюджет заданий квотою, а не підбирається на льоту."""


class RateLimiter:
    """Обмежувач темпу, який сам підлаштовується під сервер.

    Prozorro не оголошує ліміт і обмежує радше квотою на вікно, ніж миттєвим
    темпом, тож підібрана наперед константа однаково буде або надто повільною,
    або ловитиме 429. Замість цього після кожної відмови інтервал зростає, а
    за серії вдалих запитів поволі повертається до бажаного.
    """

    #: Повільніше за це не сповільнюємось навіть після серії відмов.
    FLOOR_RPS = 0.4

    def __init__(self, rps: float, *, penalty: float = 1.7,
                 recover_after: int = 25, floor_rps: float | None = None):
        """``penalty`` і ``recover_after`` задають, наскільки різко реагувати на 429.

        Типові значення обережні: темп сервера наперед невідомий, тож після
        відмови краще різко відступити. Для хостів із **виміряним** темпом
        (:data:`HOST_RATES`) різкість шкідлива: одна випадкова відмова на дві
        тисячі запитів просідала цілий етап на п'яту частину часу, хоч сервер
        насправді тримає темп. Там доречні м'яке покарання і швидке повернення.
        """
        self.base_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self.min_interval = self.base_interval
        self.penalty = penalty
        self.recover_after = max(1, recover_after)
        self.floor_interval = 1.0 / (floor_rps or self.FLOOR_RPS)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._ok_streak = 0

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

    def penalize(self) -> float:
        """Сервер відмовив — розтягуємо інтервал. Повертає новий темп."""
        if self.base_interval <= 0:
            return 0.0
        with self._lock:
            self._ok_streak = 0
            self.min_interval = min(self.min_interval * self.penalty, self.floor_interval)
            return 1.0 / self.min_interval

    def reward(self) -> None:
        """Запит пройшов — після довгої серії вдалих поволі прискорюємось."""
        if self.base_interval <= 0 or self.min_interval <= self.base_interval:
            return
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= self.recover_after:
                self._ok_streak = 0
                self.min_interval = max(self.base_interval, self.min_interval * 0.8)


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
        self._host_limiters = {
            host: WindowLimiter(limit, window) for host, (limit, window) in HOST_QUOTAS.items()
        }
        #: Темп виміряний, тож відступаємо м'яко й повертаємось швидко, а нижче
        #: чверті від виміряного не опускаємось узагалі.
        self._host_rates = {
            host: RateLimiter(rate, penalty=1.25, recover_after=10, floor_rps=rate / 4)
            for host, rate in HOST_RATES.items()
        }
        self._throttle_lock = threading.Lock()
        self._last_reported_rps = 0.0
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
        host_limiter = self._limiter_for(url)
        # Хост із власним виміряним темпом не підпадає під загальний ліміт:
        # інакше швидка Центральна база чекала б нарівні з повільним порталом.
        rate_limiter = self._rate_for(url) or self.limiter
        for attempt in range(self.max_retries + 1):
            self.check_cancel()
            rate_limiter.acquire()
            if host_limiter is not None:
                host_limiter.acquire(self.cancel_event)
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
                if resp.status_code == 429:
                    if host_limiter is not None:
                        self._note_throttle(host_limiter, url)
                    elif rate_limiter is not self.limiter:
                        # Хост із власним темпом: виміряна межа — оцінка, а не
                        # обіцянка сервера, тож на відмову знижуємо темп самі.
                        # Без цього постійна константа ловила б 429 знову й знову.
                        rate_limiter.penalize()
                if resp.status_code in RETRY_CODES and attempt < self.max_retries:
                    delay = self._retry_delay(attempt, resp)
                    self.stats.add(retries=1)
                    self.log("warn", f"HTTP {resp.status_code} — повтор через {delay:.1f} с: "
                                     f"{url[:110]}")
                    resp.close()
                    self._sleep(delay)
                    continue
                if resp.status_code < 400:
                    if host_limiter is not None:
                        host_limiter.reward()
                    elif rate_limiter is not self.limiter:
                        rate_limiter.reward()
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

    def _note_throttle(self, limiter: WindowLimiter, url: str) -> None:
        """Квота вичерпана — перечікуємо вікно й повідомляємо про це один раз."""
        rps = limiter.penalize()
        with self._throttle_lock:
            if abs(rps - self._last_reported_rps) < 0.05:
                return
            self._last_reported_rps = rps
        self.log("warn", f"Вичерпано квоту {limiter.limit} запитів на "
                         f"{limiter.window:.0f} с для {url.split('/')[2]} — "
                         f"перечікую вікно")

    @staticmethod
    def host_of(url: str) -> str:
        """Ім'я хоста з URL — без користувача, порту й регістру."""
        parts = url.split("/", 3)
        if len(parts) < 3:
            return ""
        return parts[2].split("@")[-1].split(":")[0].lower()

    def _limiter_for(self, url: str) -> WindowLimiter | None:
        return self._host_limiters.get(self.host_of(url))

    def _rate_for(self, url: str) -> RateLimiter | None:
        return self._host_rates.get(self.host_of(url))

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
