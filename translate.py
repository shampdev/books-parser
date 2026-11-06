"""Перевод на русский: категории — по словарю, описания — через Google Переводчик.

Переводы кешируются в cache/translations.json, поэтому повторный запуск
не переводит уже переведённое и проходит мгновенно."""
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger(__name__)

CATEGORIES_RU = {
    "Academic": "Учебная литература",
    "Add a comment": "Разное",
    "Adult Fiction": "Проза для взрослых",
    "Art": "Искусство",
    "Autobiography": "Автобиографии",
    "Biography": "Биографии",
    "Business": "Бизнес",
    "Childrens": "Детские книги",
    "Christian": "Христианство",
    "Christian Fiction": "Христианская проза",
    "Classics": "Классика",
    "Contemporary": "Современная проза",
    "Crime": "Криминал",
    "Cultural": "Культура",
    "Default": "Без категории",
    "Erotica": "Эротика",
    "Fantasy": "Фэнтези",
    "Fiction": "Художественная литература",
    "Food and Drink": "Кулинария",
    "Health": "Здоровье",
    "Historical": "Историческое",
    "Historical Fiction": "Историческая проза",
    "History": "История",
    "Horror": "Ужасы",
    "Humor": "Юмор",
    "Music": "Музыка",
    "Mystery": "Детективы",
    "New Adult": "Нью-эдалт",
    "Nonfiction": "Нон-фикшн",
    "Novels": "Романы",
    "Paranormal": "Паранормальное",
    "Parenting": "Воспитание детей",
    "Philosophy": "Философия",
    "Poetry": "Поэзия",
    "Politics": "Политика",
    "Psychology": "Психология",
    "Religion": "Религия",
    "Romance": "Любовные романы",
    "Science": "Наука",
    "Science Fiction": "Научная фантастика",
    "Self Help": "Саморазвитие",
    "Sequential Art": "Комиксы",
    "Short Stories": "Рассказы",
    "Spirituality": "Духовность",
    "Sports and Games": "Спорт и игры",
    "Suspense": "Остросюжетное",
    "Thriller": "Триллеры",
    "Travel": "Путешествия",
    "Womens Fiction": "Женская проза",
    "Young Adult": "Подростковая литература",
}

PROBE_SIZE = 2          # столько текстов переводим «на пробу», прежде чем запускать потоки
MAX_FAILS_IN_ROW = 10   # после стольких ошибок подряд считаем переводчик недоступным
MAX_RATE_WAITS = 5      # сколько раз подряд готовы переждать лимит Google
CHUNK_SIZE = 900        # текст уходит в адресе запроса: короткие куски Google принимает охотнее
REQUEST_INTERVAL = 0.4  # минимальная пауза между запросами к переводчику, секунд


def category_ru(name: str) -> str:
    return CATEGORIES_RU.get(name, name)


def clean_description(text: str) -> str:
    """На сайте описания обрезаны и заканчиваются на « ...more» — убираем хвост."""
    return re.sub(r"\s*\.\.\.\s*more\s*$", "…", text.strip())


def split_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Режет текст по предложениям на куски не длиннее size символов."""
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        while len(sentence) > size:  # очень длинное «предложение» режем как есть
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:size])
            sentence = sentence[size:]
        if current and len(current) + 1 + len(sentence) > size:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


# ---------- Google Переводчик ----------

class RateLimited(Exception):
    """Google ответил 429 — слишком много запросов."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
})
_rate_lock = threading.Lock()
_last_request = 0.0


def _request_json(url: str, params: dict):
    """GET с паузой между запросами (общей для всех потоков)."""
    global _last_request
    with _rate_lock:
        wait = _last_request + REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    response = _session.get(url, params=params, timeout=15)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        raise RateLimited("429 Too Many Requests", float(retry_after) if retry_after.isdigit() else None)
    response.raise_for_status()
    return response.json()


def gtx_translate(text: str) -> str:
    data = _request_json(
        "https://translate.googleapis.com/translate_a/single",
        {"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": text},
    )
    # [[["перевод", "оригинал", ...], ...], ...] — по куску на предложение
    return "".join(part[0] for part in data[0] if part and part[0])


def dict_translate(text: str) -> str:
    data = _request_json(
        "https://clients5.google.com/translate_a/t",
        {"client": "dict-chrome-ex", "sl": "en", "tl": "ru", "q": text},
    )
    # Бывает ["перевод"], [["перевод", "en"]] или {"sentences": [{"trans": ...}]}
    if isinstance(data, dict):
        return "".join(s.get("trans", "") for s in data.get("sentences", []))
    parts = []
    for item in data:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, list) and item and isinstance(item[0], str):
            parts.append(item[0])
    return "".join(parts)


# Адреса Google по порядку: если первый упёрся в лимит, переходим ко второму
PROVIDERS: list[tuple[str, Callable[[str], str]]] = [
    ("translate.googleapis.com", gtx_translate),
    ("clients5.google.com", dict_translate),
]
_cooldown_until: dict[str, float] = {}


def google_translate(text: str) -> str:
    """Пробует адреса по очереди. Адрес, ответивший 429, на время пропускается.
    Если лимит на всех адресах — RateLimited (Translator подождёт и повторит)."""
    errors: list[str] = []
    other_error: Exception | None = None
    for name, fn in PROVIDERS:
        if _cooldown_until.get(name, 0) > time.monotonic():
            continue  # этот адрес сейчас на паузе после 429
        try:
            result = fn(text)
            if result:
                return result
            errors.append(f"{name}: пустой ответ")
        except RateLimited as e:
            _cooldown_until[name] = time.monotonic() + (e.retry_after or 60)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:100]}")
            other_error = e

    if errors:  # были настоящие ошибки (сеть, формат ответа) — сообщаем о них
        raise RuntimeError("; ".join(errors)) from other_error
    soonest = min(_cooldown_until.values(), default=time.monotonic() + 60)
    raise RateLimited("Google ограничил запросы на всех адресах", max(1.0, soonest - time.monotonic()))


def check_translator() -> bool:
    """Проверка доступности адресов Google: python main.py --check-translator"""
    long_text = ("It's hard to imagine a world without A Light in the Attic. This now-classic collection "
                 "of poetry and drawings from Shel Silverstein celebrates its 20th anniversary. ") * 5
    ok_any = False
    for name, fn in PROVIDERS:
        for label, text in (("короткий текст", "Hello, world!"), ("длинный текст", long_text[:CHUNK_SIZE])):
            try:
                result = fn(text)
                print(f"  OK     {name} · {label}: {result[:60]}…")
                ok_any = ok_any or bool(result)
            except Exception as e:
                print(f"  ОШИБКА {name} · {label}: {type(e).__name__}: {str(e)[:120]}")
    return ok_any


# ---------- Перевод множества текстов ----------

class Translator:
    def __init__(
        self,
        cache_path: Path = Path("cache") / "translations.json",
        workers: int = 2,
        retries: int = 3,
        translate_fn: Callable[[str], str] | None = None,
        on_progress: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cache_path = cache_path
        self.workers = workers
        self.retries = retries
        self.translate_fn = translate_fn or google_translate
        self.on_progress = on_progress or (lambda: None)
        self.sleep = sleep
        self.failed = 0
        self.last_error: str | None = None
        self.stopped = False  # переводчик недоступен — дальше не пытаемся
        self._fails_in_row = 0
        self._probing = False
        self._lock = threading.Lock()
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Кеш переводов повреждён, начинаю заново")

    def _translate_chunk(self, chunk: str) -> str:
        attempts = 1 if self._probing else self.retries  # пробные запросы — без повторов, чтобы не ждать
        attempt = rate_waits = 0
        while True:
            try:
                result = self.translate_fn(chunk)
                if result:
                    return result
                raise RuntimeError("Пустой ответ переводчика")
            except RateLimited as e:
                # Лимит — не ошибка: пережидаем и продолжаем (кроме пробы — там важна скорость)
                rate_waits += 1
                if self._probing or rate_waits > MAX_RATE_WAITS or self.stopped:
                    raise
                pause = min(e.retry_after or 30 * rate_waits, 120)
                print(f"\n  Google просит подождать, пауза {pause:.0f} с …", flush=True)
                self.sleep(pause)
            except Exception as e:
                attempt += 1
                if attempt >= attempts:
                    raise
                logger.info("Ошибка перевода (%s), повтор через %s с", e, attempt * 2)
                self.sleep(attempt * 2)

    def _translate_one(self, text: str) -> bool:
        if self.stopped:
            with self._lock:
                self.failed += 1
            return False
        try:
            translated = " ".join(self._translate_chunk(c) for c in split_text(text))
            with self._lock:
                self.cache[text] = translated
                self._fails_in_row = 0
            ok = True
        except Exception as e:
            with self._lock:
                self.failed += 1
                self._fails_in_row += 1
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                if self._fails_in_row >= MAX_FAILS_IN_ROW:
                    self.stopped = True  # много ошибок подряд — сервис недоступен, не тратим время
            logger.warning("Не удалось перевести текст: %s", e)
            ok = False
        self.on_progress()
        return ok

    def translate_many(self, texts: list[str]) -> dict[str, str]:
        """Переводит тексты. Возвращает {оригинал: перевод}; непереведённые — без изменений.

        Сначала пробует перевести несколько текстов по одному: если не вышел ни один,
        переводчик недоступен — сразу сдаёмся, а не ждём ошибок по каждому тексту."""
        todo = sorted({t for t in texts if t and t not in self.cache})
        if not todo:
            return {t: self.cache.get(t, t) for t in texts}
        try:
            probe, rest = todo[:PROBE_SIZE], todo[PROBE_SIZE:]
            self._probing = True
            probe_ok = [self._translate_one(t) for t in probe]
            self._probing = False
            if not any(probe_ok):
                self.stopped = True
                self.failed += len(rest)
            else:
                with ThreadPoolExecutor(self.workers) as pool:
                    list(pool.map(self._translate_one, rest))
        finally:
            self.save()  # даже если прервали — переведённое не пропадёт
        return {t: self.cache.get(t, t) for t in texts}

    def translate_short(self, texts: list[str], max_chars: int = CHUNK_SIZE) -> dict[str, str]:
        """Перевод коротких строк (названий) пачками: строки склеиваются через перенос строки,
        уходят одним запросом и разбиваются обратно. Если Google вернул другое число строк,
        эта пачка переводится по одной строке."""
        todo = sorted({t.strip() for t in texts if t and t.strip() and t.strip() not in self.cache})
        batches, current = [], []
        for t in todo:
            if current and len("\n".join(current + [t])) > max_chars:
                batches.append(current)
                current = []
            current.append(t)
        if current:
            batches.append(current)

        failed_batches = 0
        try:
            for batch in batches:
                if self.stopped:
                    self.failed += len(batch)
                    continue
                try:
                    parts = [p.strip() for p in self._translate_chunk("\n".join(batch)).split("\n")]
                except Exception as e:
                    self.failed += len(batch)
                    self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                    failed_batches += 1
                    if failed_batches >= 3:
                        self.stopped = True
                    continue
                failed_batches = 0
                if len(parts) == len(batch) and all(parts):
                    with self._lock:
                        self.cache.update(zip(batch, parts))
                    for _ in batch:
                        self.on_progress()
                else:
                    for t in batch:  # строки «поехали» — переводим по одной
                        self._translate_one(t)
        finally:
            self.save()
        return {t: self.cache.get(t.strip(), t) for t in texts}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = json.dumps(self.cache, ensure_ascii=False, indent=0)
        self.cache_path.write_text(data, encoding="utf-8")
