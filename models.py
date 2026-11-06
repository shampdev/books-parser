from dataclasses import dataclass


@dataclass
class Book:
    title: str
    category: str
    price: float          # в фунтах, как на сайте
    rating: int           # 1–5
    in_stock: bool
    url: str
    image_url: str
    # Поля ниже заполняются только в режиме --details (из карточки товара)
    stock_count: int | None = None
    upc: str | None = None
    reviews: int | None = None
    description: str | None = None
    description_ru: str | None = None  # заполняется с --translate
    title_ru: str | None = None        # заполняется с --translate
