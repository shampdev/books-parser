"""Парсер книжного магазина books.toscrape.com с выгрузкой в Excel и CSV.

Примеры:
    python main.py                                  # все книги
    python main.py -c Travel -c Poetry              # только выбранные категории
    python main.py --details                        # + остаток, UPC и описание из карточек
    python main.py --translate                      # названия и описания на русском
    python main.py --check-translator               # проверить, доступен ли переводчик
    python main.py --min-rating 4 --max-price 30 --in-stock
    python main.py --list-categories                # показать все категории
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from export import save_csv, save_excel
from scraper import BASE_URL, Progress, Scraper
from translate import Translator, check_translator


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Парсер books.toscrape.com: собирает книги и сохраняет их в Excel/CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Примеры:" + __doc__.split("Примеры:")[1],
    )
    p.add_argument("-c", "--category", action="append", metavar="НАЗВАНИЕ",
                   help="категория (можно указать несколько раз). По умолчанию — все")
    p.add_argument("--list-categories", action="store_true", help="вывести список категорий и выйти")
    p.add_argument("--details", action="store_true",
                   help="заходить в карточку каждой книги: остаток, UPC, описание (дольше)")
    p.add_argument("--translate", action="store_true",
                   help="перевести названия и описания на русский через Google Переводчик (включает --details)")
    p.add_argument("--check-translator", action="store_true", help="проверить, доступен ли Google Переводчик, и выйти")
    p.add_argument("--max-pages", type=int, metavar="N", help="не больше N страниц на категорию")
    p.add_argument("--min-rating", type=int, choices=range(1, 6), metavar="1-5", help="минимальный рейтинг")
    p.add_argument("--max-price", type=float, metavar="£", help="максимальная цена в фунтах")
    p.add_argument("--in-stock", action="store_true", help="только книги в наличии")
    p.add_argument("-o", "--output", type=Path, metavar="ФАЙЛ.xlsx",
                   help="куда сохранить Excel (по умолчанию results/books_<дата>.xlsx)")
    p.add_argument("--csv", action="store_true", help="дополнительно сохранить CSV рядом с Excel")
    p.add_argument("--workers", type=int, default=8, metavar="N", help="число параллельных загрузок (по умолчанию 8)")
    p.add_argument("--quiet", action="store_true", help="без строки прогресса")
    return p.parse_args()


def main() -> int:
    args = build_args()
    if args.check_translator:
        print("Проверка Google Переводчика:")
        ok = check_translator()
        print("Переводчик работает." if ok else "Переводчик сейчас недоступен — попробуйте позже или с другим VPN.")
        return 0 if ok else 1
    if args.translate:
        args.details = True  # описания есть только в карточках товаров
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")

    scraper = Scraper(workers=args.workers, progress=Progress(enabled=not args.quiet))

    if args.list_categories:
        for name in scraper.get_categories():
            print(name)
        return 0

    started = time.perf_counter()
    print(f"Сбор данных с {BASE_URL} …")
    try:
        books = scraper.scrape(args.category, max_pages=args.max_pages, details=args.details)
    except requests.RequestException as e:  # проверяем первым: часть сетевых ошибок наследует ValueError
        print(f"Не удалось подключиться к сайту: {e}", file=sys.stderr)
        if "SOCKS" in str(e):
            print("Похоже, включён VPN/прокси. Выполните: pip install pysocks — или выключите VPN.", file=sys.stderr)
        return 1
    except ValueError as e:  # неизвестная категория
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2

    total = len(books)
    if args.min_rating:
        books = [b for b in books if b.rating >= args.min_rating]
    if args.max_price is not None:
        books = [b for b in books if b.price <= args.max_price]
    if args.in_stock:
        books = [b for b in books if b.in_stock]

    if args.translate and books:
        translator = Translator()
        print("Перевод на русский …")

        progress = Progress(enabled=not args.quiet)
        translator.on_progress = lambda: progress.add("Названия", 1)
        titles = translator.translate_short([b.title for b in books])
        for b in books:
            b.title_ru = titles.get(b.title) if titles.get(b.title) != b.title else None

        translator.on_progress = lambda: progress.add("Описания", 1)
        if not translator.stopped:
            descriptions = translator.translate_many([b.description for b in books if b.description])
            for b in books:
                if b.description:
                    b.description_ru = descriptions.get(b.description)
        progress.done()

        if translator.stopped:
            print(f"Переводчик недоступен: {translator.last_error}")
            print("Непереведённое оставлено на английском. Проверьте интернет/VPN и запустите ещё раз.")
        elif translator.failed:
            print(f"Не удалось перевести: {translator.failed} (остались на английском; повторный запуск дозапросит их)")
            print(f"Последняя ошибка: {translator.last_error}")

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    xlsx_path = args.output or Path("results") / f"books_{stamp}.xlsx"
    params = {
        "Источник": BASE_URL,
        "Категории": ", ".join(args.category) if args.category else "все",
        "Карточки товаров": "да" if args.details else "нет",
        "Перевод на русский": "да" if args.translate else "нет",
        "Мин. рейтинг": str(args.min_rating or "—"),
        "Макс. цена, £": str(args.max_price if args.max_price is not None else "—"),
        "Только в наличии": "да" if args.in_stock else "нет",
        "Собрано книг": str(total),
        "После фильтров": str(len(books)),
    }
    save_excel(books, xlsx_path, params, details=args.details)
    print(f"Книг собрано: {total}, после фильтров: {len(books)}")
    print(f"Excel: {xlsx_path.resolve()}")
    if args.csv:
        csv_path = xlsx_path.with_suffix(".csv")
        save_csv(books, csv_path, details=args.details)
        print(f"CSV:   {csv_path.resolve()}")

    if scraper.errors:
        print(f"Страниц с ошибками: {len(scraper.errors)} (первая: {scraper.errors[0]})")
    print(f"Готово за {time.perf_counter() - started:.1f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
