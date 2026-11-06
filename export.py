"""Сохранение результатов в Excel и CSV."""
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from models import Book
from translate import category_ru

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="1F4E9E", underline="single")
BOLD = Font(bold=True)
MUTED = Font(color="6B7280")
TOP_BORDER = Border(top=Side(style="thin", color="000000"))
OUT_OF_STOCK_FILL = PatternFill("solid", fgColor="FDE2E1")

BOOKS_SHEET = "Книги"


def _header(ws, headers: list[str], widths: list[int]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 24
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"


def save_excel(books: list[Book], path: Path, params: dict[str, str], details: bool) -> None:
    wb = Workbook()

    # ---------- Лист «Книги» ----------
    # Колонки описаны списком (ключ, заголовок, ширина): так их легко добавлять,
    # а формулы сводки сами находят нужные буквы колонок.
    has_title_ru = any(b.title_ru for b in books)
    columns = [("title", "Название", 48)]
    if has_title_ru:
        columns.append(("title_ru", "Название на русском", 48))
    columns += [("category", "Категория", 22), ("price", "Цена, £", 10), ("rating", "Рейтинг", 10),
                ("in_stock", "В наличии", 11)]
    if details:
        columns += [("stock", "Остаток, шт", 12), ("upc", "UPC", 19), ("description", "Описание", 70)]
    col = {key: i for i, (key, _, _) in enumerate(columns, start=1)}
    letter = {key: get_column_letter(i) for key, i in col.items()}

    ws = wb.active
    ws.title = BOOKS_SHEET
    _header(ws, [title for _, title, _ in columns], [width for _, _, width in columns])

    for b in books:
        values = {
            "title": b.title,
            "title_ru": b.title_ru or "",
            "category": category_ru(b.category),
            "price": b.price,
            "rating": b.rating,
            "in_stock": "Да" if b.in_stock else "Нет",
            "stock": b.stock_count,
            "upc": b.upc,
            "description": b.description_ru or b.description,
        }
        ws.append([values[key] for key, _, _ in columns])
        r = ws.max_row
        title = ws.cell(r, col["title"])
        title.hyperlink = b.url
        title.font = LINK_FONT
        ws.cell(r, col["price"]).number_format = "£#,##0.00"
        ws.cell(r, col["rating"]).number_format = '0 "★"'
        ws.cell(r, col["rating"]).alignment = Alignment(horizontal="center")
        ws.cell(r, col["in_stock"]).alignment = Alignment(horizontal="center")
        if not b.in_stock:
            ws.cell(r, col["in_stock"]).fill = OUT_OF_STOCK_FILL

    last = ws.max_row
    if books:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last}"
        # Цвет цены: дешёвые — зелёные, дорогие — красные
        ws.conditional_formatting.add(
            f"{letter['price']}2:{letter['price']}{last}",
            ColorScaleRule(start_type="min", start_color="D9F2E3", mid_type="percentile", mid_value=50,
                           mid_color="FFFFFF", end_type="max", end_color="FBD5D3"),
        )

    # ---------- Лист «Сводка» по категориям (формулы пересчитываются в Excel) ----------
    summary = wb.create_sheet("Сводка")
    _header(summary, ["Категория", "Книг", "Средняя цена, £", "Средний рейтинг", "В наличии", "Доля в наличии"],
            [24, 9, 17, 17, 12, 16])

    def rng(key: str) -> str:
        c = letter[key]
        return f"'{BOOKS_SHEET}'!${c}$2:${c}${max(last, 2)}"

    cat, price, rating, stock = rng("category"), rng("price"), rng("rating"), rng("in_stock")
    for category, _ in Counter(category_ru(b.category) for b in books).most_common():
        r = summary.max_row + 1
        summary.append([
            category,
            f"=COUNTIF({cat},A{r})",
            f"=AVERAGEIF({cat},A{r},{price})",
            f"=AVERAGEIF({cat},A{r},{rating})",
            f'=COUNTIFS({cat},A{r},{stock},"Да")',
            f"=IF(B{r}=0,0,E{r}/B{r})",
        ])
    first, end = 2, summary.max_row
    if books:
        total = end + 1
        summary.append([
            "Итого",
            f"=SUM(B{first}:B{end})",
            f"=AVERAGE({price})",
            f"=AVERAGE({rating})",
            f"=SUM(E{first}:E{end})",
            f"=IF(B{total}=0,0,E{total}/B{total})",
        ])
        for cell in summary[total]:
            cell.font = BOLD
            cell.border = TOP_BORDER
    for r in range(2, summary.max_row + 1):
        summary.cell(r, 3).number_format = "£#,##0.00"
        summary.cell(r, 4).number_format = "0.0"
        summary.cell(r, 6).number_format = "0%"

    # ---------- Лист «Параметры» ----------
    info = wb.create_sheet("Параметры")
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 60
    info.append(["Дата выгрузки", datetime.now().strftime("%d.%m.%Y %H:%M")])
    for key, value in params.items():
        info.append([key, value])
    for row in info.iter_rows():
        row[0].font = MUTED

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def save_csv(books: list[Book], path: Path, details: bool) -> None:
    """CSV с разделителем «;» и BOM — так его правильно открывает русский Excel."""
    has_title_ru = any(b.title_ru for b in books)
    headers = ["title"] + (["title_ru"] if has_title_ru else []) + [
        "category", "price_gbp", "rating", "in_stock", "url", "image_url"]
    translated = any(b.description_ru for b in books)
    if details:
        headers += ["stock_count", "upc", "reviews", "description"] + (["description_ru"] if translated else [])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        for b in books:
            row = [b.title] + ([b.title_ru or ""] if has_title_ru else []) + [
                category_ru(b.category), f"{b.price:.2f}", b.rating, int(b.in_stock), b.url, b.image_url]
            if details:
                row += [b.stock_count, b.upc, b.reviews, b.description] + ([b.description_ru] if translated else [])
            writer.writerow(row)
