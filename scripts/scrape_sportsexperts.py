#!/usr/bin/env python3
import argparse
import json
import os
import re
import time
from pathlib import Path
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.sportsexperts.ca"
TARGET_URL = "https://www.sportsexperts.ca/fr-CA/rabais/liquidation"

GRID_SELECTORS = [
    '[data-testid*="product"]',
    '[data-test*="product"]',
    '[data-qa*="product"]',
    ".product-tile",
    ".productTile",
    'article:has(a[href*="/p-"])',
    'article:has(a[href*="/p/"])',
    'a[href*="/p-"]',
    'a[href*="/p/"]',
    'li:has(a[href*="/p-"])',
    'li:has(a[href*="/p/"])',
]

CARD_SELECTORS = [
    "[data-testid='product-card']",
    ".product-card",
    ".product-card__wrapper",
    ".product-tile",
    "li.product",
    'article:has(a[href*="/p-"])',
    'article:has(a[href*="/p/"])',
    'li:has(a[href*="/p-"])',
    'li:has(a[href*="/p/"])',
    'a[href*="/p-"]',
    'a[href*="/p/"]',
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
    "a[href*='/p/']",
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

CLOUDFLARE_SELECTORS = [
    "#cf-wrapper",
    "div#cf-error-details",
    "form#challenge-form",
    "div.cf-browser-verification",
    "div#challenge-form",
    "iframe[src*='challenge']",
    "iframe[src*='captcha']",
]

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
STEALTH_HEADERS = {"Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7"}


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


def has_display_server() -> bool:
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


def normalize_headless_mode(headless: bool, headed: bool) -> bool:
    if headed:
        headless = False
    if not headless and not has_display_server():
        print(
            "[browser] Aucun serveur d'affichage détecté (DISPLAY/WAYLAND_DISPLAY). "
            "Bascule automatique en mode headless."
        )
        return True
    return headless


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
        match = re.search(r"/p[-/]([A-Za-z0-9_-]+)", url)
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


def save_debug(page, debug_dir: Path, prefix: str = "debug") -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    html_path = debug_dir / f"{prefix}.html"
    png_path = debug_dir / f"{prefix}.png"
    page.screenshot(path=str(png_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")
    print(f"[debug] saved: {html_path} {png_path}")


def get_page_title(page) -> str:
    try:
        return page.title()
    except Exception as exc:
        print(f"Erreur récupération title: {exc}")
        return ""


def get_body_text_excerpt(page, limit: int = 500) -> str:
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception as exc:
        print(f"Erreur récupération body text: {exc}")
        return ""
    cleaned = " ".join(body_text.split())
    return cleaned[:limit]


def log_blocking_details(page) -> None:
    title = get_page_title(page)
    excerpt = get_body_text_excerpt(page)
    if title:
        print(f"[blocage] page.title(): {title}")
    if excerpt:
        print(f"[blocage] extrait texte: {excerpt}")


def write_failure_artifacts(
    page,
    debug_dir: str,
    response_headers: Optional[Dict[str, str]],
) -> None:
    ensure_output_dir(debug_dir)
    fail_png = os.path.join(debug_dir, "fail.png")
    fail_html = os.path.join(debug_dir, "fail.html")
    try:
        page.screenshot(path=fail_png, full_page=True)
    except Exception as screenshot_exc:
        print(f"Erreur screenshot fail: {screenshot_exc}")
    try:
        with open(fail_html, "w", encoding="utf-8") as handle:
            handle.write(page.content())
    except Exception as html_exc:
        print(f"Erreur HTML fail: {html_exc}")
    if response_headers:
        headers_path = os.path.join(debug_dir, "response_headers.txt")
        try:
            with open(headers_path, "w", encoding="utf-8") as handle:
                for key, value in response_headers.items():
                    handle.write(f"{key}: {value}\n")
        except Exception as headers_exc:
            print(f"Erreur headers fail: {headers_exc}")


def is_antibot_page(html: str, title: str) -> bool:
    html_lower = html.lower()
    title_lower = title.lower()
    if any(keyword in html_lower for keyword in ANTI_BOT_HTML_KEYWORDS):
        return True
    if any(keyword in title_lower for keyword in ANTI_BOT_TITLE_KEYWORDS):
        return True
    return False


def get_dom_excerpt(page, source: str, limit: int = 1500) -> str:
    selector = "main"
    locator = page.locator(selector)
    if locator.count() == 0:
        selector = "body"
        locator = page.locator(selector)
    try:
        if source == "text":
            content = locator.first.inner_text(timeout=5000)
            content = " ".join(content.split())
        else:
            content = locator.first.inner_html(timeout=5000)
    except Exception:
        return ""
    content = content.strip()
    return content[:limit]


def log_dom_debug(page) -> None:
    print("[debug] DOM selector counts (grid):")
    for selector in GRID_SELECTORS:
        try:
            count = page.locator(selector).count()
        except Exception:
            count = 0
        print(f"[debug] {selector}: {count}")
    print("[debug] DOM selector counts (cards):")
    for selector in CARD_SELECTORS:
        try:
            count = page.locator(selector).count()
        except Exception:
            count = 0
        print(f"[debug] {selector}: {count}")


def log_no_grid_details(page) -> None:
    try:
        anchor_count = page.locator('a[href*="/p-"], a[href*="/p/"]').count()
    except Exception:
        anchor_count = 0
    print(f'[grid] Fallback count product anchors: {anchor_count}')
    print(f"[grid] page.url(): {page.url}")
    title = get_page_title(page)
    if title:
        print(f"[grid] page.title(): {title}")
    text_excerpt = get_dom_excerpt(page, "text")
    html_excerpt = get_dom_excerpt(page, "html")
    if text_excerpt:
        print(f"[grid] main.inner_text excerpt: {text_excerpt}")
    if html_excerpt:
        print(f"[grid] main.inner_html excerpt: {html_excerpt}")


def is_blocked_page(page) -> bool:
    """
    Détection stricte: on déclare bloqué seulement si on voit des indices explicites
    (Cloudflare / captcha / access denied). Ne pas confondre avec "selector introuvable".
    """
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""

    blocked_title_kw = [
        "access denied",
        "attention required",
        "just a moment",
        "verify you are human",
        "captcha",
        "cloudflare",
        "forbidden",
        "denied",
    ]
    if any(keyword in title for keyword in blocked_title_kw):
        return True

    blocked_selectors = [
        "#cf-wrapper",
        "div[class*='cf-']",
        "iframe[src*='captcha']",
        "input[name='cf-turnstile-response']",
        "[data-sitekey]",
        "text=/verify you are human/i",
        "text=/access denied/i",
        "text=/attention required/i",
        "text=/captcha/i",
    ]
    for selector in blocked_selectors:
        try:
            if page.locator(selector).first.count() > 0:
                return True
        except Exception:
            continue

    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        body = ""
    blocked_text_kw = [
        "access denied",
        "attention required",
        "verify you are human",
        "captcha",
        "cloudflare",
        "temporarily blocked",
        "forbidden",
        "request blocked",
    ]
    if any(keyword in body for keyword in blocked_text_kw):
        return True

    return False


def wait_for_hydration(page, timeout_ms: int = 45000) -> None:
    page.wait_for_function(
        """() => {
            const text = document.body?.innerText || "";
            const hasPlaceholder = text.includes("{{TotalCount}}");
            const hasProducts = /\\b\\d+[\\s\\u00A0]*produits\\b/i.test(text);
            return !hasPlaceholder && hasProducts;
        }""",
        timeout=timeout_ms,
    )


def is_product_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("hits"), list) and payload["hits"]:
        return True
    results = payload.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and isinstance(entry.get("hits"), list) and entry["hits"]:
                return True
    for key in ["Products", "products", "items", "Items"]:
        items = payload.get(key)
        if isinstance(items, list) and items:
            return True
    return False


def capture_products_via_json(page, timeout_ms: int = 45000) -> List[Any]:
    payloads: List[Any] = []
    last_payload_count = 0
    last_change = time.time()

    def handle_response(response) -> None:
        nonlocal last_payload_count, last_change
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if not is_product_payload(payload):
            return
        payloads.append(payload)
        last_payload_count = len(payloads)
        last_change = time.time()

    page.on("response", handle_response)

    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        page.wait_for_timeout(500)
        if payloads and time.time() - last_change > 2:
            break

    page.remove_listener("response", handle_response)
    return payloads


def flatten_products(payloads: List[Any]) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def item_key(item: Dict[str, Any]) -> str:
        for key in ["objectID", "ProductId", "productId", "id", "sku", "SKU"]:
            value = item.get(key)
            if value:
                return f"{key}:{value}"
        dumped = json.dumps(item, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha1(dumped[:2000].encode("utf-8")).hexdigest()
        return f"hash:{digest}"

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        items: List[Any] = []
        hits = payload.get("hits")
        if isinstance(hits, list) and hits:
            items.extend(hits)
        results = payload.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict) and isinstance(result.get("hits"), list):
                    items.extend(result["hits"])
        for key in ["Products", "products", "items", "Items"]:
            collection = payload.get(key)
            if isinstance(collection, list) and collection:
                items.extend(collection)
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item_key(item)
            if key in seen:
                continue
            seen.add(key)
            flattened.append(item)
    return flattened


def wait_for_grid(page, debug_dom: bool, debug_dir: Path) -> str:
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    if is_blocked_page(page):
        if debug_dom:
            save_debug(page, debug_dir, prefix="blocked")
        raise RuntimeError("Page semble bloquée (Cloudflare / Captcha / Access Denied).")

    candidate_selectors = [
        "a[href*='/p-']",
        "a[href*='/p/']",
        "[data-testid*='product']",
        "[data-test*='product']",
        ".product-tile, .productTile, .product-card, .productCard",
        "[itemtype*='Product']",
        "li:has(a[href*='/p-'])",
        "li:has(a[href*='/p/'])",
    ]

    last_counts: List[Tuple[str, int]] = []
    for selector in candidate_selectors:
        try:
            locator = page.locator(selector)
            locator.first.wait_for(state="visible", timeout=20000)
            count = locator.count()
            last_counts.append((selector, count))
            if count > 0:
                print(f"[grid] Found tile selector: {selector} (count={count})")
                return selector
        except Exception:
            last_counts.append((selector, 0))
            continue

    if debug_dom:
        save_debug(page, debug_dir, prefix="no_grid")
        try:
            main_txt = page.locator("main").inner_text(timeout=3000)[:1500]
        except Exception:
            main_txt = ""
        print("[debug_dom] Aucun selector produit trouvé.")
        print("[debug_dom] URL:", page.url)
        try:
            print("[debug_dom] Title:", page.title())
        except Exception:
            pass
        print("[debug_dom] main excerpt:", main_txt)
        print("[debug_dom] counts:", last_counts)

    raise RuntimeError(
        "Grille produits introuvable (pas un blocage). "
        f"selectors_testés={last_counts}"
    )


def find_card_elements(page) -> List[Any]:
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


def count_tiles(page, selector: str) -> int:
    return page.locator(selector).count()


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


def load_all_products(page, max_pages: int, tile_selector: str) -> LoadMetrics:
    metrics = LoadMetrics()
    stable_cycles = 3
    stability = 0
    last_count = count_tiles(page, tile_selector)
    max_iterations = max_pages if max_pages > 0 else 200

    for iteration in range(max_iterations):
        metrics.iterations += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        clicked = click_load_more(page)
        if clicked:
            metrics.load_more_clicks += 1
            page.wait_for_timeout(1500)
        page.wait_for_timeout(500)
        current_count = count_tiles(page, tile_selector)
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
    default_headless = parse_bool(os.getenv("HEADLESS", "true"))
    default_save_debug = parse_bool(os.getenv("SAVE_DEBUG", "true"))
    default_debug_dom = parse_bool(os.getenv("DEBUG_DOM", "true"))
    default_max_pages = int(os.getenv("MAX_PAGES", "0") or 0)
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
        default=default_max_pages,
        help="Maximum number of load cycles (0 = no limit).",
    )
    parser.add_argument(
        "--headless",
        type=parse_bool,
        default=default_headless,
        help="Run browser in headless mode (true/false).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Shortcut to run with a visible browser window.",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=int(os.getenv("SLOW_MO", "0") or 0),
        help="Slow motion delay in ms for each Playwright action.",
    )
    parser.add_argument(
        "--save-debug",
        type=parse_bool,
        default=default_save_debug,
        help="Save debug HTML/PNG artifacts (true/false).",
    )
    parser.add_argument(
        "--debug-dom",
        type=parse_bool,
        default=default_debug_dom,
        help="Enable DOM debug logging (true/false).",
    )
    args = parser.parse_args()
    args.headless = normalize_headless_mode(args.headless, args.headed)
    return args


def parse_product_from_payload(payload: Dict[str, Any], page_url: str) -> Optional[Product]:
    title = (
        payload.get("title")
        or payload.get("name")
        or payload.get("productName")
        or payload.get("product_name")
    )
    if isinstance(title, dict):
        title = title.get("value") or title.get("text")
    title = clean_text(str(title)) if title else None

    url = payload.get("url") or payload.get("productUrl") or payload.get("link")
    if isinstance(url, dict):
        url = url.get("href") or url.get("url")
    if url:
        url = urljoin(page_url, str(url))

    image = (
        payload.get("image")
        or payload.get("imageUrl")
        or payload.get("image_url")
        or payload.get("primaryImage")
    )
    if isinstance(image, dict):
        image = image.get("url") or image.get("src")
    if image:
        image = urljoin(page_url, str(image))

    sku = payload.get("sku") or payload.get("productId") or payload.get("id") or payload.get("objectID")
    if isinstance(sku, dict):
        sku = sku.get("value") or sku.get("id")
    if sku:
        sku = clean_text(str(sku))
    if not sku and url:
        match = re.search(r"/p[-/]([A-Za-z0-9_-]+)", url)
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

    for key in ["salePrice", "sale_price", "currentPrice", "current_price", "sale", "priceValue"]:
        if key in payload:
            price_values.extend(parse_price_values(str(payload.get(key))))

    for key in [
        "originalPrice",
        "regularPrice",
        "regular_price",
        "listPrice",
        "original",
        "regular",
    ]:
        if key in payload:
            price_values.extend(parse_price_values(str(payload.get(key))))

    if "prices" in payload and isinstance(payload["prices"], list):
        for entry in payload["prices"]:
            if isinstance(entry, dict):
                price_values.extend(parse_price_values(json.dumps(entry)))

    regular_price, price = derive_prices(price_values)
    discount_pct = compute_discount(regular_price, price)

    if not title and not sku and not url:
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
    debug_dir = Path("outputs") / "debug"
    ensure_output_dir(public_dir)
    if args.save_debug:
        ensure_output_dir(debug_dir)

    print(f"Scrape URL: {args.url}")
    if args.max_pages == 0:
        print("Max pages: 0 (no limit)")
    else:
        print(f"Max pages: {args.max_pages}")
    print(f"Headless: {args.headless}")
    print(f"Slow mo: {args.slow_mo}ms")
    print(f"Save debug: {args.save_debug}")
    print(f"Debug DOM: {args.debug_dom}")

    captured_products: List[Product] = []
    payloads: List[Any] = []
    json_response_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=args.headless,
            args=STEALTH_ARGS,
            slow_mo=args.slow_mo if args.slow_mo > 0 else None,
        )
        context = browser.new_context(
            locale="fr-CA",
            timezone_id="America/Montreal",
            user_agent=STEALTH_USER_AGENT,
            extra_http_headers=STEALTH_HEADERS,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        last_response_headers: Optional[Dict[str, str]] = None

        def handle_response(response) -> None:
            nonlocal last_response_headers
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if not is_product_payload(payload):
                return
            payloads.append(payload)
            last_response_headers = dict(response.headers)

        page.on("response", handle_response)

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            accept_cookies(page)
            wait_for_hydration(page)

            if args.save_debug:
                save_debug_artifacts(page, str(debug_dir), public_dir, "debug")

            tile_selector = wait_for_grid(page, args.debug_dom, debug_dir)
            load_metrics = load_all_products(page, args.max_pages, tile_selector)
            print(
                "Chargement DOM: iterations={iterations} clicks={clicks} stop={stop}".format(
                    iterations=load_metrics.iterations,
                    clicks=load_metrics.load_more_clicks,
                    stop=load_metrics.stop_reason,
                )
            )

            page.wait_for_timeout(2000)
            json_response_count = len(payloads)
            raw_items = flatten_products(payloads)

            for item in raw_items:
                product = parse_product_from_payload(item, page.url)
                if product:
                    captured_products.append(product)

            if not captured_products:
                cards = find_card_elements(page)
                print(f"Fallback DOM activé: {len(cards)} cartes candidates")
                for card in cards:
                    product = extract_product_from_card(card, page.url)
                    if not product.url and not product.title and not product.sku:
                        continue
                    captured_products.append(product)

            if not captured_products:
                raise RuntimeError(
                    "Aucun produit exploitable trouvé après capture JSON et fallback DOM."
                )

            combined_products = merge_products(captured_products)

            combined_products.sort(
                key=lambda item: (
                    -(item.discount_pct or 0),
                    item.price if item.price is not None else float("inf"),
                )
            )

            output_index = os.path.join(public_dir, "products-index.json")
            output_all = os.path.join(public_dir, "products.json")
            write_products(output_index, combined_products)
            write_products(output_all, combined_products)

            end_time = time.time()
            duration = end_time - start_time
            print(f"Extracted {len(combined_products)} products")
            print(f"Total produits: {len(combined_products)}")
            print(f"Total JSON capturés: {len(captured_products)}")
            print(f"Payloads JSON capturés: {json_response_count}")
            print(f"Durée totale: {duration:.2f}s")
        except Exception as exc:
            print(f"Erreur globale: {exc}")
            write_failure_artifacts(page, str(debug_dir), last_response_headers)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
