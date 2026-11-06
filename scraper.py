"""Сбор данных с books.toscrape.com.

Разбор HTML вынесен в отдельные функции parse_*, которые не ходят в сеть,
поэтому их легко тестировать на сохранённых страницах (см. tests/)."""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models import Book
from translate import category_ru, clean_description

logger = logging.getLogger(__name__)

BASE_URL = "https://books.toscrape.com/"
RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


# ---------- Разбор HTML ----------

def parse_price(text: str) -> float:
    """'£51.77' -> 51.77"""
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        raise ValueError(f"Не удалось разобрать цену: {text!r}")
    return float(match.group())


def parse_categories(soup: BeautifulSoup, page_url: str) -> dict[str, str]:
    """Категории из бокового меню главной страницы: {название: ссылка}."""
    return {
        a.get_text(strip=True): urljoin(page_url, a["href"])
        for a in soup.select("div.side_categories ul li ul li a")
    }


def parse_list_page(soup: BeautifulSoup, page_url: str, category: str) -> list[Book]:
    """Книги со страницы каталога/категории."""
    books = []
    for card in soup.select("article.product_pod"):
        link = card.select_one("h3 a")
        rating_tag = card.select_one("p.star-rating")
        rating_word = next((c for c in rating_tag.get("class", []) if c in RATINGS), None) if rating_tag else None
        availability = card.select_one("p.availability")
        image = card.select_one("img")
        books.append(Book(
            title=link.get("title") or link.get_text(strip=True),
            category=category,
            price=parse_price(card.select_one("p.price_color").get_text()),
            rating=RATINGS.get(rating_word, 0),
            in_stock=bool(availability) and "in stock" in availability.get_text().lower(),
            url=urljoin(page_url, link["href"]),
            image_url=urljoin(page_url, image["src"]) if image else "",
        ))
    return books


def parse_next_page(soup: BeautifulSoup, page_url: str) -> str | None:
    link = soup.select_one("li.next a")
    return urljoin(page_url, link["href"]) if link else None


def parse_product_page(soup: BeautifulSoup, book: Book) -> Book:
    """Дополняет книгу данными из карточки товара."""
    table = {
        row.th.get_text(strip=True): row.td.get_text(strip=True)
        for row in soup.select("table.table-striped tr")
        if row.th and row.td
    }
    book.upc = table.get("UPC")
    if "Number of reviews" in table and table["Number of reviews"].isdigit():
        book.reviews = int(table["Number of reviews"])
    stock = re.search(r"\((\d+) available\)", table.get("Availability", ""))
    book.stock_count = int(stock.group(1)) if stock else 0
    book.in_stock = book.stock_count > 0

    description = soup.select_one("#product_description ~ p")
    book.description = clean_description(description.get_text()) if description else ""
    return book


# ---------- Загрузка ----------

class Progress:
    """Простая строка прогресса в консоли (потокобезопасная)."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + n
            if self.enabled:
                line = " | ".join(f"{k}: {v}" for k, v in self.counts.items())
                print(f"\r  {line}   ", end="", flush=True)

    def done(self) -> None:
        if self.enabled and self.counts:
            print()


class Scraper:
    def __init__(
        self,
        base_url: str = BASE_URL,
        workers: int = 8,
        timeout: float = 15,
        retries: int = 3,
        fetch: Callable[[str], str] | None = None,
        progress: Progress | None = None,
    ):
        self.base_url = base_url
        self.workers = max(1, workers)
        self.timeout = timeout
        self.progress = progress or Progress(enabled=False)
        self.errors: list[str] = []
        self._fetch = fetch or self._http_get

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        retry = Retry(total=retries, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=self.workers)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _http_get(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = "utf-8"  # сайт не указывает кодировку, иначе вместо £ будет «Â£»
        return response.text

    def soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self._fetch(url), "html.parser")

    # --- категории ---

    def get_categories(self) -> dict[str, str]:
        return parse_categories(self.soup(self.base_url), self.base_url)

    def resolve_categories(self, names: Iterable[str] | None) -> dict[str, str]:
        """Названия категорий -> ссылки. Можно по-английски или по-русски, регистр не важен.
        None — все категории."""
        available = self.get_categories()
        if not names:
            return available
        lookup = {}
        for name, url in available.items():
            lookup[name.lower()] = (name, url)
            lookup[category_ru(name).lower()] = (name, url)
        result, unknown = {}, []
        for name in names:
            found = lookup.get(name.strip().lower())
            if found:
                result[found[0]] = found[1]
            else:
                unknown.append(name)
        if unknown:
            raise ValueError(
                f"Неизвестные категории: {', '.join(unknown)}. "
                "Список всех категорий: python main.py --list-categories"
            )
        return result

    # --- страницы ---

    def scrape_category(self, name: str, url: str, max_pages: int | None = None) -> list[Book]:
        books, page = [], 0
        while url and (max_pages is None or page < max_pages):
            try:
                soup = self.soup(url)
            except Exception as e:  # сеть, 404 и т.п. — не роняем весь сбор
                self.errors.append(f"{url}: {e}")
                logger.warning("Не удалось загрузить %s: %s", url, e)
                break
            page_books = parse_list_page(soup, url, name)
            books.extend(page_books)
            page += 1
            self.progress.add("Страниц", 1)
            self.progress.add("Книг", len(page_books))
            url = parse_next_page(soup, url)
        return books

    def add_details(self, book: Book) -> Book:
        try:
            parse_product_page(self.soup(book.url), book)
        except Exception as e:
            self.errors.append(f"{book.url}: {e}")
            logger.warning("Не удалось загрузить карточку %s: %s", book.url, e)
        self.progress.add("Карточек", 1)
        return book

    def scrape(
        self,
        categories: Iterable[str] | None = None,
        max_pages: int | None = None,
        details: bool = False,
    ) -> list[Book]:
        targets = self.resolve_categories(categories)

        books: list[Book] = []
        with ThreadPoolExecutor(self.workers) as pool:
            futures = [pool.submit(self.scrape_category, name, url, max_pages) for name, url in targets.items()]
            for future in as_completed(futures):
                books.extend(future.result())

        # Одна книга не должна попасть в выгрузку дважды
        unique = list({b.url: b for b in books}.values())

        if details and unique:
            with ThreadPoolExecutor(self.workers) as pool:
                list(pool.map(self.add_details, unique))

        self.progress.done()
        return sorted(unique, key=lambda b: (b.category, b.title))
