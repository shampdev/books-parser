"""Тесты парсера на сохранённых страницах — без обращения к сайту.

Запуск: python -m pytest
"""
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from export import save_csv, save_excel  # noqa: E402
from models import Book  # noqa: E402
from scraper import (  # noqa: E402
    BASE_URL, Scraper, parse_categories, parse_list_page, parse_next_page, parse_price, parse_product_page,
)

PAGES = Path(__file__).resolve().parent / "pages"
TRAVEL = BASE_URL + "catalogue/category/books/travel_2/index.html"
HISTFIC = BASE_URL + "catalogue/category/books/historical-fiction_4/index.html"

# Адрес на сайте -> сохранённая страница
FAKE_SITE = {
    BASE_URL: "index.html",
    TRAVEL: "travel_1.html",
    BASE_URL + "catalogue/category/books/travel_2/page-2.html": "travel_2.html",
    HISTFIC: "histfic_1.html",
    BASE_URL + "catalogue/its-only-the-himalayas_981/index.html": "product_himalayas.html",
    BASE_URL + "catalogue/full-moon-over-noahs-ark_811/index.html": "product_no_description.html",
    BASE_URL + "catalogue/see-america_595/index.html": "product_himalayas.html",
    BASE_URL + "catalogue/tipping-the-velvet_999/index.html": "product_himalayas.html",
}


def page(name: str) -> BeautifulSoup:
    return BeautifulSoup((PAGES / name).read_text(encoding="utf-8"), "html.parser")


def fake_fetch(url: str) -> str:
    if url not in FAKE_SITE:
        raise RuntimeError(f"404 Not Found: {url}")
    return (PAGES / FAKE_SITE[url]).read_text(encoding="utf-8")


# ---------- Разбор отдельных страниц ----------

@pytest.mark.parametrize("text, expected", [("£51.77", 51.77), ("Â£49.43", 49.43), ("£1,234.50", 1234.5), ("£7", 7.0)])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_parse_price_invalid():
    with pytest.raises(ValueError):
        parse_price("бесплатно")


def test_parse_categories():
    categories = parse_categories(page("index.html"), BASE_URL)
    assert categories == {"Travel": TRAVEL, "Historical Fiction": HISTFIC}


def test_parse_list_page():
    books = parse_list_page(page("travel_1.html"), TRAVEL, "Travel")
    assert len(books) == 2
    first, second = books
    assert first.title == "It's Only the Himalayas"
    assert first.price == 45.17 and first.rating == 2 and first.in_stock
    assert first.url == BASE_URL + "catalogue/its-only-the-himalayas_981/index.html"
    assert first.image_url.startswith(BASE_URL + "media/cache/")
    # Полное название берётся из title, а не из обрезанного текста ссылки
    assert second.title.endswith("Mount Ararat and Beyond")
    assert second.rating == 4 and second.price == 49.43


def test_parse_next_page():
    assert parse_next_page(page("travel_1.html"), TRAVEL) == BASE_URL + "catalogue/category/books/travel_2/page-2.html"
    assert parse_next_page(page("travel_2.html"), TRAVEL) is None


def test_parse_product_page():
    book = parse_list_page(page("travel_1.html"), TRAVEL, "Travel")[0]
    parse_product_page(page("product_himalayas.html"), book)
    assert book.upc == "a22124811bfa8350"
    assert book.stock_count == 19 and book.in_stock
    assert book.reviews == 0
    assert book.description.startswith("“Wherever you go")
    assert book.description.endswith("My Mother…")  # хвост «...more» убран


def test_parse_product_page_out_of_stock_without_description():
    book = Book("X", "Travel", 1.0, 1, True, "u", "i")
    parse_product_page(page("product_no_description.html"), book)
    assert book.stock_count == 0 and not book.in_stock
    assert book.description == "" and book.reviews == 3


# ---------- Сбор целиком (сеть подменена) ----------

def test_scrape_all_categories_with_pagination():
    books = Scraper(fetch=fake_fetch).scrape()
    assert [b.title for b in books if b.category == "Travel"][-1].startswith("See America")
    assert len(books) == 4
    assert {b.category for b in books} == {"Travel", "Historical Fiction"}
    assert all(b.upc is None for b in books)  # без --details в карточки не заходим


def test_scrape_selected_category_case_insensitive_with_details():
    scraper = Scraper(fetch=fake_fetch)
    books = scraper.scrape(["travel"], details=True)
    assert len(books) == 3 and all(b.category == "Travel" for b in books)
    assert all(b.upc for b in books)
    assert not scraper.errors


def test_category_in_russian():
    books = Scraper(fetch=fake_fetch).scrape(["путешествия"])
    assert len(books) == 3 and all(b.category == "Travel" for b in books)


def test_max_pages():
    books = Scraper(fetch=fake_fetch).scrape(["Travel"], max_pages=1)
    assert len(books) == 2


def test_unknown_category():
    with pytest.raises(ValueError, match="Неизвестные категории: Cooking"):
        Scraper(fetch=fake_fetch).scrape(["Cooking"])


def test_broken_page_does_not_stop_scraping():
    broken = dict(FAKE_SITE)
    del broken[BASE_URL + "catalogue/category/books/travel_2/page-2.html"]
    scraper = Scraper(fetch=lambda url: (PAGES / broken[url]).read_text(encoding="utf-8")
                      if url in broken else (_ for _ in ()).throw(RuntimeError("404")))
    books = scraper.scrape()
    assert len(books) == 3 and len(scraper.errors) == 1


# ---------- Выгрузка ----------

def test_excel_and_csv(tmp_path):
    books = Scraper(fetch=fake_fetch).scrape(details=True)
    xlsx = tmp_path / "out" / "books.xlsx"
    save_excel(books, xlsx, {"Источник": BASE_URL}, details=True)
    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Книги", "Сводка", "Параметры"]
    ws = wb["Книги"]
    assert ws["A1"].value == "Название" and ws.max_row == 5
    assert ws["A2"].hyperlink.target.startswith(BASE_URL)
    summary = wb["Сводка"]
    assert summary["A2"].value == "Путешествия" and summary["B2"].value.startswith("=COUNTIF(")
    assert ws["B2"].value in {"Путешествия", "Историческая проза"}
    assert summary.cell(summary.max_row, 1).value == "Итого"

    csv_path = xlsx.with_suffix(".csv")
    save_csv(books, csv_path, details=True)
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("title;category;price_gbp") and len(lines) == 5


# ---------- Перевод ----------

from translate import Translator, category_ru, clean_description, split_text  # noqa: E402


def test_category_ru():
    assert category_ru("Travel") == "Путешествия"
    assert category_ru("Something New") == "Something New"


def test_clean_description():
    assert clean_description("Great book. ...more") == "Great book.…"
    assert clean_description("No tail") == "No tail"


def test_split_text_keeps_chunks_short_and_complete():
    text = " ".join(f"Sentence number {i} is here." for i in range(300))
    chunks = split_text(text, size=500)
    assert all(len(c) <= 500 for c in chunks) and len(chunks) > 1
    assert " ".join(chunks) == text
    assert split_text("x" * 1200, size=500) == ["x" * 500, "x" * 500, "x" * 200]


def test_translator_uses_cache_and_survives_errors(tmp_path):
    calls = []

    def fake(text):
        calls.append(text)
        if text == "bad":
            raise RuntimeError("429 Too Many Requests")
        return "RU:" + text

    cache = tmp_path / "cache.json"
    t = Translator(cache_path=cache, retries=1, translate_fn=fake)
    result = t.translate_many(["hello", "world", "hello", "bad", ""])
    assert result["hello"] == "RU:hello" and result["bad"] == "bad" and t.failed == 1
    assert calls.count("hello") == 1

    # Второй запуск: переведённое берётся из кеша, повторяется только неудачное
    calls.clear()
    t2 = Translator(cache_path=cache, retries=1, translate_fn=fake)
    assert t2.translate_many(["hello", "world"]) == {"hello": "RU:hello", "world": "RU:world"}
    assert calls == []


def test_translated_description_goes_to_excel(tmp_path):
    books = Scraper(fetch=fake_fetch).scrape(["Travel"], details=True)
    for b in books:
        b.description_ru = "Перевод: " + b.description
    xlsx = tmp_path / "books.xlsx"
    save_excel(books, xlsx, {}, details=True)
    ws = load_workbook(xlsx)["Книги"]
    assert ws["H1"].value == "Описание" and ws["H2"].value.startswith("Перевод: ")
    save_csv(books, xlsx.with_suffix(".csv"), details=True)
    header = xlsx.with_suffix(".csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.endswith("description;description_ru")


def test_translator_gives_up_fast_when_service_is_down(tmp_path):
    calls = []

    def down(text):
        calls.append(text)
        raise ConnectionError("Network is unreachable")

    t = Translator(cache_path=tmp_path / "c.json", retries=3, translate_fn=down)
    texts = [f"text {i}" for i in range(500)]
    result = t.translate_many(texts)
    assert t.stopped and "Network is unreachable" in t.last_error
    assert len(calls) == 2  # только пробные запросы, без повторов
    assert t.failed == 500 and result["text 7"] == "text 7"


def test_translator_stops_after_many_errors_in_row(tmp_path):
    state = {"n": 0}

    def flaky(text):
        state["n"] += 1
        if state["n"] > 2:  # пробные прошли, потом сервис «упал»
            raise RuntimeError("429 Too Many Requests")
        return "RU"

    t = Translator(cache_path=tmp_path / "c.json", retries=1, workers=1, translate_fn=flaky)
    t.translate_many([f"text {i:03}" for i in range(300)])
    assert t.stopped and state["n"] < 20


def test_google_translate_parses_response(monkeypatch):
    import translate

    class FakeResponse:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            pass

        def json(self):
            return [[["Привет. ", "Hello. ", None], ["Как дела?", "How are you?", None], [None, None, "translit"]], None, "en"]

    seen = {}

    def fake_get(url, params, timeout):
        seen.update(params, url=url, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(translate._session, "get", fake_get)
    monkeypatch.setattr(translate, "REQUEST_INTERVAL", 0)
    assert translate.gtx_translate("Hello. How are you?") == "Привет. Как дела?"
    assert seen["sl"] == "en" and seen["tl"] == "ru" and seen["timeout"] == 15


# ---------- Адреса Google и лимиты ----------

def test_dict_translate_formats(monkeypatch):
    import translate
    for payload, expected in [(["Привет"], "Привет"), ([["Привет", "en"]], "Привет"),
                              ({"sentences": [{"trans": "При"}, {"trans": "вет"}]}, "Привет")]:
        monkeypatch.setattr(translate, "_request_json", lambda url, params, p=payload: p)
        assert translate.dict_translate("Hello") == expected


def test_google_translate_falls_back_to_second_address(monkeypatch):
    import translate
    monkeypatch.setattr(translate, "_cooldown_until", {})

    def limited(text):
        raise translate.RateLimited("429")

    monkeypatch.setattr(translate, "PROVIDERS", [("a", limited), ("b", lambda t: "RU")])
    assert translate.google_translate("x") == "RU"
    assert "a" in translate._cooldown_until  # первый адрес поставлен на паузу


def test_google_translate_all_limited(monkeypatch):
    import translate
    monkeypatch.setattr(translate, "_cooldown_until", {})

    def limited(text):
        raise translate.RateLimited("429", retry_after=5)

    monkeypatch.setattr(translate, "PROVIDERS", [("a", limited), ("b", limited)])
    with pytest.raises(translate.RateLimited) as info:
        translate.google_translate("x")
    assert 1 <= info.value.retry_after <= 5


def test_translator_waits_out_rate_limit(tmp_path):
    import translate
    state = {"n": 0}
    pauses = []

    def sometimes_limited(text):
        state["n"] += 1
        if state["n"] in (4, 5):  # после пробы Google дважды просит подождать
            raise translate.RateLimited("429", retry_after=7)
        return "RU:" + text

    t = Translator(cache_path=tmp_path / "c.json", workers=1, translate_fn=sometimes_limited, sleep=pauses.append)
    result = t.translate_many([f"text {i}" for i in range(6)])
    assert all(v.startswith("RU:") for v in result.values()) and t.failed == 0
    assert pauses == [7, 7]


# ---------- Перевод названий пачками ----------

def line_by_line(text):
    return "\n".join("RU:" + line for line in text.split("\n"))


def test_translate_short_in_batches(tmp_path):
    calls = []

    def fake(text):
        calls.append(text)
        return line_by_line(text)

    t = Translator(cache_path=tmp_path / "c.json", translate_fn=fake)
    titles = [f"Book title number {i}" for i in range(100)]
    result = t.translate_short(titles, max_chars=200)
    assert result["Book title number 7"] == "RU:Book title number 7"
    assert 1 < len(calls) < 30  # пачками, а не по одному
    assert all(len(c) <= 200 for c in calls)


def test_translate_short_falls_back_when_lines_merge(tmp_path):
    def merging(text):
        return " / ".join("RU:" + line for line in text.split("\n"))  # Google «склеил» строки

    t = Translator(cache_path=tmp_path / "c.json", translate_fn=merging)
    result = t.translate_short(["One", "Two", "Three"])
    assert result == {"One": "RU:One", "Two": "RU:Two", "Three": "RU:Three"}


def test_title_ru_column_and_summary_formulas(tmp_path):
    books = Scraper(fetch=fake_fetch).scrape(details=True)
    for b in books:
        b.title_ru = "RU " + b.title
    xlsx = tmp_path / "books.xlsx"
    save_excel(books, xlsx, {}, details=True)
    wb = load_workbook(xlsx)
    ws = wb["Книги"]
    assert [c.value for c in ws[1]][:4] == ["Название", "Название на русском", "Категория", "Цена, £"]
    assert ws["A2"].hyperlink is not None and ws["B2"].value.startswith("RU ")
    assert "$C$2:$C$" in wb["Сводка"]["B2"].value  # категория теперь в колонке C
