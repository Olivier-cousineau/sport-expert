#!/usr/bin/env python3
import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.sportsexperts.ca"
TARGET_URL = "https://www.sportsexperts.ca/fr-CA/rabais/liquidation"

GRID_SELECTORS = [
    "[data-testid='product-grid']",
    "[data-testid='product-card']",
    ".product-grid",
    ".product-list",
    "ul.products",
    "[data-product-id]",
]

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
    "a[title]",
    "h2",
    "h3",
]

PRICE_SELECTORS = [
    "[data-testid='price']",
    ".price",
    ".price__value",
    ".product-card__price",
    ".sales",
    ".price-sales",
    ".price-regular",
]

IMAGE_SELECTORS = [
    "img[src]",
    "img[data-src]",
    "picture img",
]

PRODUCT_LINK_SELECTORS = [
    "a[href*='/p-']",
    "a[href*='/product']",
    "a[href]",
]

AVAILABILITY_SELECTORS = [
    "[data-testid='availability']",
    ".availability",
    ".product-availability",
    ".inventory-status",
]

SKU_SELECTORS = [
    "[data-product-id]",
    "[data-sku]",
    "[data-itemid]",
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

ANTI_BOT_HTML_KEYWORDS = [
    "access denied",
    "captcha",
    "pardon",
    "unusual traffic",
    "robot",
    "verify",
    "blocked",
]

ANTI_BOT_TITLE_KEYWORDS = [
    "denied",
    "attention",
    "security",
]

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
STEALTH_HEADERS = {"Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8"}


@dataclass
class Product:
    title: Optional[str]
    price: Optional[float]
    regular_price: Optional[float]
    discount_pct: Optional[int]
    url: Optional[str]
    image: Optional[str]
    sku: Optional[str]
    availability: Optional[str]


@dataclass
class LoadMetrics:
    load_more_clicks: int = 0
    stop_reason: str = ""
    iterations: int = 0


def parse_bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_price(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


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
        return None, normalize_price(price_values[0])
    return normalize_price(max(price_values)), normalize_price(min(price_values))


def compute_discount(price_regular: Optional[float], price_sale: Optional[float]) -> Optional[int]:
    if price_regular is None or price_sale is None or price_regular == 0:
        return None
    discount = round((1 - price_sale / price_regular) * 100)
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


def extract_availability_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    if "rupture" in lowered:
        return "out_of_stock"
    if "indispon" in lowered:
        return "out_of_stock"
    if "en stock" in lowered:
        return "in_stock"
    if "disponible" in lowered:
        return "in_stock"
    return None


def extract_sku_from_card(element, url: Optional[str]) -> Optional[str]:
    for selector in SKU_SELECTORS:
        handle = element.query_selector(selector)
        if handle:
            for attr in ["data-product-id", "data-sku", "data-itemid"]:
                value = handle.get_attribute(attr)
                cleaned = clean_text(value)
                if cleaned:
                    return cleaned
    if url:
        match = re.search(r"/p-([A-Za-z0-9_-]+)", url)
        if match:
            return match.group(1)
    return None


def extract_product_from_card(element, page_url: str) -> Product:
    title = get_first_text_from_selectors(element, TITLE_SELECTORS)
    link = get_first_attribute(element, PRODUCT_LINK_SELECTORS, "href")
    image = get_first_attribute(element, IMAGE_SELECTORS, "src")
    if image is None:
        image = get_first_attribute(element, IMAGE_SELECTORS, "data-src")

    full_url = urljoin(page_url, link) if link else None
    full_image = urljoin(page_url, image) if image else None

    price_values = extract_prices_from_card(element)
    regular_price, price = derive_prices(price_values)
    discount_pct = compute_discount(regular_price, price)

    availability_text = get_first_text_from_selectors(element, AVAILABILITY_SELECTORS)
    if availability_text is None:
        availability_text = clean_text(element.inner_text())
    availability = extract_availability_from_text(availability_text)

    sku = extract_sku_from_card(element, full_url)

    return Product(
        title=title,
        price=price,
        regular_price=regular_price,
        discount_pct=discount_pct,
        url=full_url,
        image=full_image,
        sku=sku,
        availability=availability,
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


def save_debug_artifacts(page, debug_dir: str, public_dir: str, prefix: str) -> None:
    os.makedirs(debug_dir, exist_ok=True)
    os.makedirs(public_dir, exist_ok=True)
    html_path = os.path.join(debug_dir, f"{prefix}.html")
    png_path = os.path.join(debug_dir, f"{prefix}.png")
    public_html = os.path.join(public_dir, f"{prefix}.html")
    public_png = os.path.join(public_dir, f"{prefix}.png")
    try:
        page.screenshot(path=png_path, full_page=True)
    except Exception as exc:
        print(f"Erreur screenshot {prefix}: {exc}")
    else:
        try:
            with open(png_path, "rb") as handle:
                with open(public_png, "wb") as public_handle:
                    public_handle.write(handle.read())
        except Exception as exc:
            print(f"Erreur copie screenshot {prefix}: {exc}")
    try:
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(html)
        with open(public_html, "w", encoding="utf-8") as public_handle:
            public_handle.write(html)
    except Exception as exc:
        print(f"Erreur HTML {prefix}: {exc}")


def is_antibot_page(html: str, title: str) -> bool:
    html_lower = html.lower()
    title_lower = title.lower()
    if any(keyword in html_lower for keyword in ANTI_BOT_HTML_KEYWORDS):
        return True
    if any(keyword in title_lower for keyword in ANTI_BOT_TITLE_KEYWORDS):
        return True
    return False


def wait_for_grid(page) -> None:
    last_error: Optional[Exception] = None
    for selector in GRID_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=20000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
    if last_error:
        raise last_error


def count_cards(page) -> int:
    unique: set = set()
    for selector in CARD_SELECTORS:
        try:
            keys = page.eval_on_selector_all(
                selector,
                """
                els => els.map(el => {
                  return (
                    el.getAttribute('data-product-id') ||
                    el.getAttribute('data-sku') ||
                    el.getAttribute('data-itemid') ||
                    el.querySelector('a')?.href ||
                    el.outerHTML
                  );
                })
                """,
            )
            unique.update(keys)
        except PlaywrightTimeoutError:
            continue
    return len(unique)


def click_load_more(page) -> bool:
    for selector in LOAD_MORE_SELECTORS:
        locator = page.locator(selector)
        try:
            if locator.first.is_visible(timeout=1500):
                locator.first.scroll_into_view_if_needed()
                locator.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def load_all_products(page, max_pages: int) -> LoadMetrics:
    metrics = LoadMetrics()
    stable_cycles = 3
    stability = 0
    last_count = count_cards(page)
    max_iterations = max_pages if max_pages > 0 else 120

    for iteration in range(max_iterations):
        metrics.iterations += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        clicked = click_load_more(page)
        if clicked:
            metrics.load_more_clicks += 1
            page.wait_for_timeout(1500)
        page.wait_for_timeout(500)
        current_count = count_cards(page)
        print(
            "Iteration {iteration}: produits={current} (prev={prev}) click={clicked}".format(
                iteration=iteration + 1,
                current=current_count,
                prev=last_count,
                clicked="oui" if clicked else "non",
            )
        )
        if current_count <= last_count:
            stability += 1
        else:
            stability = 0
        last_count = current_count
        if stability >= stable_cycles:
            metrics.stop_reason = "no_growth"
            break
        if max_pages > 0 and metrics.iterations >= max_pages:
            metrics.stop_reason = "max_pages"
            break
    if not metrics.stop_reason:
        metrics.stop_reason = "max_iterations"
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Sports Experts clearance products.",
    )
    parser.add_argument(
        "--url",
        default=TARGET_URL,
        help="Target URL to scrape.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum number of load cycles (0 = no limit).",
    )
    parser.add_argument(
        "--headless",
        type=parse_bool,
        default=True,
        help="Run browser in headless mode (true/false).",
    )
    parser.add_argument(
        "--save-debug",
        type=parse_bool,
        default=True,
        help="Save debug HTML/PNG artifacts (true/false).",
    )
    return parser.parse_args()


def parse_product_from_payload(payload: Dict[str, Any], page_url: str) -> Optional[Product]:
    title = payload.get("title") or payload.get("name")
    if title:
        title = clean_text(str(title))

    url = payload.get("url") or payload.get("productUrl") or payload.get("link")
    if isinstance(url, dict):
        url = url.get("href")
    if url:
        url = urljoin(page_url, str(url))

    image = payload.get("image") or payload.get("imageUrl") or payload.get("image_url")
    if isinstance(image, dict):
        image = image.get("url") or image.get("src")
    if image:
        image = urljoin(page_url, str(image))

    sku = payload.get("sku") or payload.get("productId") or payload.get("id")
    if isinstance(sku, dict):
        sku = sku.get("value")
    if sku:
        sku = clean_text(str(sku))
    if not sku and url:
        match = re.search(r"/p-([A-Za-z0-9_-]+)", url)
        if match:
            sku = match.group(1)

    availability = payload.get("availability") or payload.get("inventoryStatus")
    if isinstance(availability, dict):
        availability = availability.get("status")
    availability = clean_text(str(availability)) if availability else None
    normalized_availability = extract_availability_from_text(availability)
    if normalized_availability:
        availability = normalized_availability

    price_values: List[float] = []

    direct_price = payload.get("price")
    if isinstance(direct_price, dict):
        price_values.extend(parse_price_values(json.dumps(direct_price)))
    elif direct_price is not None:
        price_values.extend(parse_price_values(str(direct_price)))

    for key in ["salePrice", "sale_price", "currentPrice", "current_price", "sale"]:
        if key in payload:
            price_values.extend(parse_price_values(str(payload.get(key))))

    for key in ["originalPrice", "regularPrice", "regular_price", "listPrice", "original"]:
        if key in payload:
            price_values.extend(parse_price_values(str(payload.get(key))))

    if "prices" in payload and isinstance(payload["prices"], list):
        for entry in payload["prices"]:
            if isinstance(entry, dict):
                price_values.extend(parse_price_values(json.dumps(entry)))

    regular_price, price = derive_prices(price_values)
    discount_pct = compute_discount(regular_price, price)

    if not title or price is None:
        return None

    return Product(
        title=title,
        price=price,
        regular_price=regular_price,
        discount_pct=discount_pct,
        url=url,
        image=image,
        sku=sku,
        availability=availability,
    )


def extract_products_from_json(payload: Any, page_url: str) -> List[Product]:
    products: List[Product] = []
    seen = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            product = parse_product_from_payload(node, page_url)
            if product:
                key = (product.sku or product.url or product.title, product.price)
                if key not in seen:
                    seen.add(key)
                    products.append(product)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return products


def merge_products(products: List[Product]) -> List[Product]:
    merged: Dict[str, Product] = {}
    for product in products:
        if product.sku:
            key = f"sku:{product.sku}"
        elif product.url:
            key = f"url:{product.url}"
        else:
            key = f"title:{product.title}-{product.price}"
        if key in merged:
            existing = merged[key]
            if not existing.image and product.image:
                existing.image = product.image
            if not existing.availability and product.availability:
                existing.availability = product.availability
            if existing.regular_price is None and product.regular_price is not None:
                existing.regular_price = product.regular_price
            if existing.discount_pct is None and product.discount_pct is not None:
                existing.discount_pct = product.discount_pct
            merged[key] = existing
        else:
            merged[key] = product
    return list(merged.values())


def ensure_output_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_products(path: str, items: List[Product]) -> None:
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "products": [asdict(item) for item in items],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def main() -> None:
    args = parse_args()
    start_time = time.time()

    public_dir = os.path.join("public", "sportsexperts")
    debug_dir = os.path.join("outputs", "debug")
    ensure_output_dir(public_dir)
    if args.save_debug:
        ensure_output_dir(debug_dir)

    print(f"Scrape URL: {args.url}")
    print(f"Max pages: {args.max_pages}")
    print(f"Headless: {args.headless}")
    print(f"Save debug: {args.save_debug}")

    captured_products: List[Product] = []
    captured_urls: List[str] = []
    json_response_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless, args=STEALTH_ARGS)
        context = browser.new_context(
            locale="fr-CA",
            user_agent=STEALTH_USER_AGENT,
            extra_http_headers=STEALTH_HEADERS,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        def handle_response(response) -> None:
            nonlocal json_response_count
            content_type = response.headers.get("content-type", "").lower()
            url = response.url
            if "application/json" not in content_type:
                return
            if not any(token in url.lower() for token in ["search", "products", "plp", "listing", "graphql"]):
                return
            json_response_count += 1
            if url not in captured_urls:
                captured_urls.append(url)
            try:
                payload = response.json()
            except Exception:
                return
            extracted = extract_products_from_json(payload, page.url)
            if extracted:
                captured_products.extend(extracted)

        page.on("response", handle_response)

        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1000)
        accept_cookies(page)
        wait_for_grid(page)

        if args.save_debug:
            save_debug_artifacts(page, debug_dir, public_dir, "debug")

        metrics = load_all_products(page, args.max_pages)

        if args.save_debug:
            save_debug_artifacts(page, debug_dir, public_dir, "debug_after_load")

        html_content = page.content()
        page_title = page.title()
        if is_antibot_page(html_content, page_title):
            print("ANTI-BOT SUSPECTÉ - artefacts conservés pour diagnostic.")

        dom_products: List[Product] = []
        for selector in CARD_SELECTORS:
            for card in page.query_selector_all(selector):
                product = extract_product_from_card(card, page.url)
                if product.url or product.sku:
                    dom_products.append(product)

        combined_products = merge_products(dom_products + captured_products)
        combined_products = [item for item in combined_products if item.price is not None]

        combined_products.sort(
            key=lambda item: (
                -(item.discount_pct or 0),
                item.price if item.price is not None else float("inf"),
            )
        )

        output_path = os.path.join(public_dir, "products-index.json")
        write_products(output_path, combined_products)

        end_time = time.time()
        duration = end_time - start_time
        print(f"Total produits: {len(combined_products)}")
        print(f"Total DOM: {len(dom_products)}")
        print(f"Total JSON capturés: {len(captured_products)}")
        print(f"Réponses JSON capturées: {json_response_count}")
        print(f"Load more clicks: {metrics.load_more_clicks}")
        print(f"Raison arrêt: {metrics.stop_reason}")
        print(f"Durée totale: {duration:.2f}s")
        if captured_urls:
            print("Top 5 URLs XHR détectées:")
            for url in captured_urls[:5]:
                print(f"- {url}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
