#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.sportsexperts.ca"
TARGET_URL = (
    "https://www.sportsexperts.ca/fr-CA/rabais/liquidation?origin=dropdown&c1=apres-noel&c2=liquidation&clickedon=liquidation"
)

CARD_SELECTORS = [
    "[data-product-id]",
    "[data-testid='product-card']",
    ".product-card",
    ".product-card__wrapper",
    ".product-tile",
    "li.product",
]

TITLE_SELECTORS = [
    "[data-testid='product-name']",
    ".product-card__name",
    ".product-name",
    "h2",
    "h3",
]

PRICE_SELECTORS = [
    "[data-testid='price']",
    ".price",
    ".price__value",
    ".product-card__price",
    ".sales",
]

IMAGE_SELECTORS = [
    "img",
    "picture img",
]

PRODUCT_LINK_SELECTORS = [
    "a[href*='/p-']",
    "a[href*='/product']",
    "a",
]

LOAD_MORE_SELECTORS = [
    "button:has-text('Charger plus')",
    "button:has-text('Voir plus')",
    "button:has-text('Afficher plus')",
    "button:has-text('Load more')",
    "button:has-text('Voir davantage')",
    "button[aria-label*='plus']",
]

COOKIE_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button:has-text('Accepter')",
    "button:has-text('Tout accepter')",
    "button:has-text('Accept All')",
    "button:has-text('I Accept')",
    "button:has-text('Allow all')",
]


@dataclass
class Product:
    title: Optional[str]
    url: Optional[str]
    image: Optional[str]
    price_original: Optional[float]
    price_sale: Optional[float]
    discount_percent: Optional[int]


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def parse_price_values(text: str) -> List[float]:
    if not text:
        return []
    cleaned = text.replace("\u00a0", " ")
    matches = re.findall(r"\d{1,3}(?:[ \u00a0]\d{3})*(?:[\.,]\d{2})?|\d+", cleaned)
    values = []
    for raw in matches:
        number = raw.replace(" ", "").replace("\u00a0", "")
        if "," in number and "." in number:
            if number.rfind(",") > number.rfind("."):
                number = number.replace(".", "").replace(",", ".")
            else:
                number = number.replace(",", "")
        elif "," in number:
            number = number.replace(",", ".")
        try:
            value = float(number)
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return values


def derive_prices(price_values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not price_values:
        return None, None
    if len(price_values) == 1:
        return None, price_values[0]
    return max(price_values), min(price_values)


def compute_discount(price_original: Optional[float], price_sale: Optional[float]) -> Optional[int]:
    if price_original is None or price_sale is None:
        return None
    if price_original == 0:
        return None
    discount = round((1 - price_sale / price_original) * 100)
    return discount


def get_first_text_from_selectors(element, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        handle = element.query_selector(selector)
        if handle:
            text = handle.inner_text()
            cleaned = clean_text(text)
            if cleaned:
                return cleaned
    return None


def get_first_attribute(element, selectors: Iterable[str], attribute: str) -> Optional[str]:
    for selector in selectors:
        handle = element.query_selector(selector)
        if handle:
            value = handle.get_attribute(attribute)
            cleaned = clean_text(value)
            if cleaned:
                return cleaned
    return None


def extract_prices_from_card(element) -> List[float]:
    texts = []
    for selector in PRICE_SELECTORS:
        for handle in element.query_selector_all(selector):
            text = handle.inner_text()
            if text:
                texts.append(text)
    try:
        texts.append(element.inner_text())
    except PlaywrightTimeoutError:
        pass
    prices = []
    for text in texts:
        prices.extend(parse_price_values(text))
    return prices


def extract_product_from_card(element, page_url: str) -> Product:
    title = get_first_text_from_selectors(element, TITLE_SELECTORS)
    link = get_first_attribute(element, PRODUCT_LINK_SELECTORS, "href")
    image = get_first_attribute(element, IMAGE_SELECTORS, "src")

    full_url = urljoin(page_url, link) if link else None
    full_image = urljoin(page_url, image) if image else None

    price_values = extract_prices_from_card(element)
    price_original, price_sale = derive_prices(price_values)
    discount_percent = compute_discount(price_original, price_sale)

    return Product(
        title=title,
        url=full_url,
        image=full_image,
        price_original=price_original,
        price_sale=price_sale,
        discount_percent=discount_percent,
    )


def accept_cookies(page) -> None:
    for selector in COOKIE_SELECTORS:
        locator = page.locator(selector)
        try:
            if locator.first.is_visible(timeout=1500):
                locator.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def find_card_elements(page) -> List:
    elements = []
    seen = set()
    for selector in CARD_SELECTORS:
        for element in page.query_selector_all(selector):
            try:
                key = element.evaluate("el => el.outerHTML")
            except Exception:
                key = None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            elements.append(element)
    return elements


def count_cards(page) -> int:
    unique = set()
    for selector in CARD_SELECTORS:
        try:
            for html in page.eval_on_selector_all(selector, "els => els.map(el => el.outerHTML)"):
                unique.add(html)
        except PlaywrightTimeoutError:
            continue
    return len(unique)


def click_load_more(page) -> bool:
    for selector in LOAD_MORE_SELECTORS:
        locator = page.locator(selector)
        try:
            if locator.first.is_visible(timeout=1500):
                locator.first.scroll_into_view_if_needed()
                locator.first.click(timeout=2000)
                page.wait_for_timeout(1200)
                return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def load_all_products(page, max_cycles: int, stable_cycles: int) -> None:
    stability = 0
    last_count = 0
    for cycle in range(max_cycles):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        clicked = click_load_more(page)
        if clicked:
            page.wait_for_timeout(1200)
        current_count = count_cards(page)
        if current_count <= last_count:
            stability += 1
        else:
            stability = 0
        last_count = current_count
        if stability >= stable_cycles:
            break
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")


def ensure_output_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_csv(path: str, items: List[Product]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "title",
                "url",
                "image",
                "price_original",
                "price_sale",
                "discount_percent",
            ],
        )
        writer.writeheader()
        for item in items:
            writer.writerow(asdict(item))


def write_json(path: str, items: List[Product]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([asdict(item) for item in items], handle, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Sports Experts clearance products with 50%+ discount.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=30,
        help="Maximum scroll/load cycles before stopping.",
    )
    parser.add_argument(
        "--stable-cycles",
        type=int,
        default=5,
        help="Stop when product count is stable for this many cycles.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (headed + save screenshot/html).",
    )
    parser.add_argument(
        "--url",
        default=TARGET_URL,
        help="Target URL to scrape.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    debug_env = os.getenv("DEBUG", "0")
    debug_mode = args.debug or debug_env == "1"

    output_dir = "outputs"
    debug_dir = os.path.join(output_dir, "debug")
    ensure_output_dir(output_dir)
    ensure_output_dir(debug_dir)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not debug_mode)
        context = browser.new_context(locale="fr-CA")
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        accept_cookies(page)

        load_all_products(page, max_cycles=args.max_cycles, stable_cycles=args.stable_cycles)

        if debug_mode:
            html_path = os.path.join(debug_dir, "se_after_load.html")
            png_path = os.path.join(debug_dir, "se_after_load.png")
            page.screenshot(path=png_path, full_page=True)
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(page.content())

        cards = find_card_elements(page)
        print(f"Total candidates détectés: {len(cards)}")

        items_by_url = {}
        for card in cards:
            product = extract_product_from_card(card, page.url)
            if product.url is None:
                continue
            if product.url in items_by_url:
                continue
            items_by_url[product.url] = product

        products = list(items_by_url.values())
        filtered = [
            item
            for item in products
            if item.discount_percent is not None and item.discount_percent >= 50
        ]

        filtered.sort(
            key=lambda item: (
                -(item.discount_percent or 0),
                item.price_sale if item.price_sale is not None else float("inf"),
            )
        )

        print(f"Total filtrés 50%+: {len(filtered)}")

        csv_path = os.path.join(output_dir, "sportsexperts_liquidation_50plus.csv")
        json_path = os.path.join(output_dir, "sportsexperts_liquidation_50plus.json")
        write_csv(csv_path, filtered)
        write_json(json_path, filtered)

        print(f"CSV: {csv_path}")
        print(f"JSON: {json_path}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
