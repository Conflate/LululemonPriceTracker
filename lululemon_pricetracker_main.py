#!/usr/bin/env python3
"""Browser-based lululemon price tracker.

This script intentionally keeps the lululemon-specific page reading isolated
from the tracking and notification logic. Retail pages change often; when that
happens, update the parser tests before changing the browser loop.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import request
from urllib.error import URLError
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright


PRODUCTS_CSV = Path("products.csv")
STATE_JSON = Path("tracker_state.json")
BROWSER_PROFILE_DIR = Path(".browser-profile")
DEFAULT_INTERVAL_MINUTES = 10
DEFAULT_COMMAND_PREFIX = "!"

PRODUCT_FIELDS = ["id", "name", "url", "target_price", "active", "notes"]
MONEY_RE = re.compile(r"\$\s*([0-9]{1,4}(?:,[0-9]{3})?(?:\.[0-9]{2})?)")
SALE_REGULAR_RE = re.compile(
    r"Sale\s+Price\s+(?P<sale>\$[^R]+?)\s+Regular\s+Price\s+(?P<regular>\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
NOW_WAS_RE = re.compile(
    r"(?:Now|Sale)\s+(?P<sale>\$[0-9][0-9,]*(?:\.[0-9]{2})?)\s+(?:Was|Regular(?:\s+Price)?)\s+(?P<regular>\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
WAS_NOW_RE = re.compile(
    r"(?:Was|Regular(?:\s+Price)?)\s+(?P<regular>\$[0-9][0-9,]*(?:\.[0-9]{2})?)\s+(?:Now|Sale)\s+(?P<sale>\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
DATA_LOCK = threading.RLock()
BROWSER_LOCK = threading.Lock()
ENV_FILE = Path(".env")
ENV_FALLBACK_FILES = [ENV_FILE, Path("env/token.env")]


@dataclass
class Product:
    id: str
    name: str
    url: str
    target_price: float | None = None
    active: bool = True
    notes: str = ""


@dataclass
class Observation:
    name: str
    url: str
    current_price: float | None
    regular_price: float | None
    sale_price: float | None
    on_sale: bool
    status: str
    source: str
    checked_at: str
    error: str = ""


@dataclass
class AlertDecision:
    should_alert: bool
    reason: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env_files(paths: Iterable[Path] = ENV_FALLBACK_FILES) -> None:
    for path in paths:
        load_env_file(path)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("pricechecker.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def product_id(url: str) -> str:
    parsed = urlparse(url.strip())
    normalized = parsed._replace(query="", fragment="").geturl().rstrip("/")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text.replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return number


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "active"}


def money_values(text: str) -> list[float]:
    values: list[float] = []
    for match in MONEY_RE.finditer(text):
        value = parse_optional_float(match.group(1))
        if value is not None:
            values.append(value)
    return values


def min_money(text: str) -> float | None:
    values = money_values(text)
    return min(values) if values else None


def max_money(text: str) -> float | None:
    values = money_values(text)
    return max(values) if values else None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_products(path: Path) -> list[Product]:
    ensure_products_file(path)
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    products: list[Product] = []
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        products.append(
            Product(
                id=(row.get("id") or product_id(url)).strip(),
                name=(row.get("name") or row.get("Product") or "").strip(),
                url=url,
                target_price=parse_optional_float(row.get("target_price")),
                active=parse_bool(row.get("active"), default=True),
                notes=(row.get("notes") or "").strip(),
            )
        )
    return products


def save_products(path: Path, products: list[Product]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCT_FIELDS)
        writer.writeheader()
        for product in products:
            writer.writerow(
                {
                    "id": product.id,
                    "name": product.name,
                    "url": product.url,
                    "target_price": "" if product.target_price is None else product.target_price,
                    "active": "true" if product.active else "false",
                    "notes": product.notes,
                }
            )


def ensure_products_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        save_products(path, [])
        return

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if all(field in fieldnames for field in PRODUCT_FIELDS):
        return

    migrated: list[Product] = []
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        migrated.append(
            Product(
                id=(row.get("id") or product_id(url)).strip(),
                name=(row.get("name") or row.get("Product") or "").strip(),
                url=url,
                target_price=parse_optional_float(row.get("target_price") or row.get("alert_price")),
                active=parse_bool(row.get("active"), default=True),
                notes=(row.get("notes") or "").strip(),
            )
        )
    save_products(path, migrated)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("State file is invalid JSON; starting with empty state: %s", path)
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def extract_json_ld(soup: BeautifulSoup) -> Iterable[Any]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def text_from_meta(soup: BeautifulSoup, names: list[str]) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def extract_name(soup: BeautifulSoup, visible_text: str, url: str) -> str:
    for item in extract_json_ld(soup):
        for node in walk_json(item):
            if isinstance(node, dict) and node.get("name"):
                node_type = node.get("@type") or node.get("type")
                if not node_type or "Product" in str(node_type):
                    return str(node["name"]).strip()

    for selector in ["h1", "[data-testid*='product-title']", "[class*='product-title']"]:
        found = soup.select_one(selector)
        if found and found.get_text(strip=True):
            return found.get_text(" ", strip=True)

    meta_title = text_from_meta(soup, ["og:title", "twitter:title"])
    if meta_title:
        return meta_title

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title:
        return title

    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[-1].replace("-", " ").title() if parts else url


def parse_offer_prices_from_json_ld(soup: BeautifulSoup) -> tuple[float | None, float | None, str]:
    prices: list[float] = []
    for item in extract_json_ld(soup):
        for node in walk_json(item):
            if not isinstance(node, dict):
                continue
            for key in ("price", "lowPrice", "highPrice"):
                price = parse_optional_float(node.get(key))
                if price is not None:
                    prices.append(price)
    if prices:
        return min(prices), max(prices), "json-ld"
    return None, None, ""


def parse_sale_regular_from_text(text: str) -> tuple[float | None, float | None, str]:
    compact = normalize_space(text)
    for pattern in (SALE_REGULAR_RE, NOW_WAS_RE, WAS_NOW_RE):
        match = pattern.search(compact)
        if not match:
            continue
        sale = min_money(match.group("sale"))
        regular = max_money(match.group("regular"))
        if sale is not None and regular is not None:
            return sale, regular, "visible-sale-text"

    sale_labels = [
        r"Sale\s+Price\s+(?P<sale>\$[0-9][0-9,]*(?:\.[0-9]{2})?(?:\s*-\s*\$[0-9][0-9,]*(?:\.[0-9]{2})?)?)",
        r"Final\s+Sale\s+(?P<sale>\$[0-9][0-9,]*(?:\.[0-9]{2})?(?:\s*-\s*\$[0-9][0-9,]*(?:\.[0-9]{2})?)?)",
    ]
    for expression in sale_labels:
        match = re.search(expression, compact, re.IGNORECASE)
        if match:
            return min_money(match.group("sale")), None, "visible-sale-label"

    return None, None, ""


def parse_regular_price_from_text(text: str) -> float | None:
    compact = normalize_space(text)
    patterns = [
        r"Regular\s+Price\s+(?P<price>\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"Price\s+(?P<price>\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
    ]
    for expression in patterns:
        match = re.search(expression, compact, re.IGNORECASE)
        if match:
            return min_money(match.group("price"))
    values = money_values(compact)
    return values[0] if len(values) == 1 else None


def parse_observation(html: str, visible_text: str, url: str) -> Observation:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize_space(visible_text or soup.get_text(" ", strip=True))
    checked_at = utc_now()

    if "Access Denied" in text or "errors.edgesuite.net" in html:
        return Observation(
            name=extract_name(soup, text, url),
            url=url,
            current_price=None,
            regular_price=None,
            sale_price=None,
            on_sale=False,
            status="blocked",
            source="blocked-page",
            checked_at=checked_at,
            error="Page returned an access denied response.",
        )

    name = extract_name(soup, text, url)
    sale_price, regular_price, source = parse_sale_regular_from_text(text)
    if sale_price is None:
        json_low, json_high, json_source = parse_offer_prices_from_json_ld(soup)
        if json_low is not None:
            sale_price = json_low
            regular_price = json_high if json_high != json_low else None
            source = json_source

    current_price = sale_price
    if current_price is None:
        current_price = parse_regular_price_from_text(text)
        source = source or "visible-price-text"

    on_sale = False
    sale_words = ("Sale Price" in text) or ("Final Sale" in text)
    if sale_price is not None and regular_price is not None and sale_price < regular_price:
        on_sale = True
    elif sale_price is not None and sale_words:
        on_sale = True

    status = "ok" if current_price is not None else "unparsed"
    error = "" if current_price is not None else "Could not find a price in page content."
    return Observation(
        name=name,
        url=url,
        current_price=current_price,
        regular_price=regular_price,
        sale_price=sale_price,
        on_sale=on_sale,
        status=status,
        source=source or "unknown",
        checked_at=checked_at,
        error=error,
    )


def decide_alert(product: Product, observation: Observation, previous: dict[str, Any]) -> AlertDecision:
    if observation.status != "ok" or observation.current_price is None:
        return AlertDecision(False, observation.status)

    current_price = observation.current_price
    last_seen = parse_optional_float(previous.get("current_price"))
    last_alerted = parse_optional_float(previous.get("last_alerted_price"))
    was_on_sale = parse_bool(previous.get("on_sale"), default=False)

    if product.target_price is not None and current_price <= product.target_price:
        if last_alerted is None or current_price < last_alerted:
            return AlertDecision(True, f"target price reached: ${current_price:.2f} <= ${product.target_price:.2f}")

    if observation.on_sale and not was_on_sale:
        if last_alerted is None or current_price != last_alerted:
            return AlertDecision(True, f"new sale detected at ${current_price:.2f}")

    if last_seen is not None and current_price < last_seen:
        if last_alerted is None or current_price < last_alerted:
            return AlertDecision(True, f"price dropped from ${last_seen:.2f} to ${current_price:.2f}")

    if observation.on_sale and last_alerted is None:
        return AlertDecision(True, f"sale price detected at ${current_price:.2f}")

    return AlertDecision(False, "no new price drop")


def observation_to_state(observation: Observation, previous: dict[str, Any], alerted: bool) -> dict[str, Any]:
    next_state = dict(previous)
    next_state.update(asdict(observation))
    if alerted and observation.current_price is not None:
        next_state["last_alerted_price"] = observation.current_price
        next_state["last_alerted_at"] = observation.checked_at
    return next_state


def format_money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.2f}"


def build_alert_message(product: Product, observation: Observation, reason: str) -> str:
    regular = ""
    if observation.regular_price and observation.current_price != observation.regular_price:
        regular = f" regular {format_money(observation.regular_price)}"
    target = ""
    if product.target_price is not None:
        target = f" target {format_money(product.target_price)}"
    return (
        f"lululemon price alert: {observation.name or product.name}\n"
        f"{reason}\n"
        f"current {format_money(observation.current_price)}{regular}{target}\n"
        f"{product.url}"
    )


def product_line(product: Product, state: dict[str, Any] | None = None) -> str:
    tracking_status = "Active" if product.active else "Paused"
    lines = [
        f"**{product.name or '(unnamed product)'}**",
        f"ID: `{product.id}`",
        f"Tracking: {tracking_status}",
    ]
    if product.target_price is not None:
        lines.append(f"Target price: {format_money(product.target_price)}")
    if state:
        current_price = parse_optional_float(state.get("current_price"))
        on_sale = parse_bool(state.get("on_sale"), default=False)
        if current_price is not None:
            lines.append(f"Current price: {format_money(current_price)}")
            lines.append(f"Sale status: {'On sale' if on_sale else 'Not currently on sale'}")
        elif state.get("status"):
            lines.append(f"Current price: unknown ({state.get('status')})")
    else:
        lines.append("Current price: not checked yet")
        lines.append("Sale status: unknown")
    lines.append(f"URL: {product.url}")
    return "\n".join(lines)


def send_discord_webhook(message: str, webhook_url: str | None) -> None:
    if not webhook_url:
        logging.info("No DISCORD_WEBHOOK_URL configured. Alert would be:\n%s", message)
        return

    payload = json.dumps({"content": message}).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "lululemon-price-tracker/1.0"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            if response.status >= 300:
                logging.error("Discord webhook returned HTTP %s", response.status)
    except URLError as exc:
        logging.error("Failed to send Discord webhook: %s", exc)


def maybe_accept_cookies(page: Page) -> None:
    labels = [
        "Accept All",
        "Accept all",
        "Accept Cookies",
        "I Accept",
        "Agree",
    ]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(label, re.IGNORECASE))
            if button.count() > 0:
                button.first.click(timeout=2000)
                return
        except Exception:
            continue


def parse_current_page(
    page: Page,
    product: Product,
    debug_dir: Path | None,
    navigation_error: Exception | None = None,
) -> Observation | None:
    try:
        html = page.content()
        visible_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return None

    if not html.strip() and not visible_text.strip():
        return None

    observation = parse_observation(html, visible_text, product.url)
    if product.name and not observation.name:
        observation.name = product.name
    if navigation_error and observation.status == "unparsed":
        observation.status = "error"
        observation.error = str(navigation_error)
    if debug_dir and observation.status != "ok":
        save_debug_artifacts(debug_dir, product, html, visible_text, page)
    return observation


def fetch_observation(context: BrowserContext, product: Product, timeout_ms: int, debug_dir: Path | None) -> Observation:
    page = context.new_page()
    try:
        response = None
        last_navigation_error: Exception | None = None
        for wait_until in ("commit", "domcontentloaded"):
            try:
                response = page.goto(product.url, wait_until=wait_until, timeout=timeout_ms)
                break
            except Exception as exc:
                last_navigation_error = exc
                partial_observation = parse_current_page(page, product, debug_dir, navigation_error=exc)
                if partial_observation and partial_observation.status == "ok":
                    return partial_observation
                logging.debug("Navigation retry for %s after %s", product.url, exc)
                time.sleep(2)
        if response is None and last_navigation_error is not None:
            partial_observation = parse_current_page(page, product, debug_dir, navigation_error=last_navigation_error)
            if partial_observation:
                return partial_observation
            raise last_navigation_error

        maybe_accept_cookies(page)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            logging.debug("Network did not become idle for %s; parsing current DOM.", product.url)

        observation = parse_current_page(page, product, debug_dir)
        if observation:
            return observation
        raise RuntimeError("Page loaded without readable HTML or body text.")
    except Exception as exc:
        logging.exception("Failed to check %s", product.url)
        return Observation(
            name=product.name,
            url=product.url,
            current_price=None,
            regular_price=None,
            sale_price=None,
            on_sale=False,
            status="error",
            source="browser",
            checked_at=utc_now(),
            error=str(exc),
        )
    finally:
        page.close()


def save_debug_artifacts(debug_dir: Path, product: Product, html: str, text: str, page: Page) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{product.id}-{int(time.time())}"
    (debug_dir / f"{prefix}.html").write_text(html, encoding="utf-8")
    (debug_dir / f"{prefix}.txt").write_text(text, encoding="utf-8")
    try:
        page.screenshot(path=str(debug_dir / f"{prefix}.png"), full_page=True)
    except Exception:
        logging.debug("Could not save screenshot for %s", product.url)


def browser_user_agent() -> str:
    if os.name == "nt":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


def open_browser_context(playwright: Any, args: argparse.Namespace) -> tuple[BrowserContext, bool]:
    if args.cdp_url:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return context, False

    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(args.browser_profile),
        headless=args.headless,
        ignore_https_errors=True,
        locale="en-US",
        timezone_id=os.getenv("TZ", "America/Vancouver"),
        user_agent=browser_user_agent(),
        viewport={"width": 1440, "height": 1200},
        args=[
            "--disable-quic",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    return context, True


def observe_product(args: argparse.Namespace, product: Product) -> Observation:
    with BROWSER_LOCK:
        with sync_playwright() as playwright:
            context, should_close = open_browser_context(playwright, args)
            try:
                return fetch_observation(context, product, args.timeout_ms, args.debug_dir)
            finally:
                if should_close:
                    context.close()


def check_products(args: argparse.Namespace, alert_handler: Callable[[str], None] | None = None) -> int:
    with DATA_LOCK:
        products = load_products(args.products)
    if args.url:
        products = [
            Product(id=product_id(args.url), name=args.name or "", url=args.url, target_price=args.target_price)
        ]
    else:
        products = [product for product in products if product.active]

    if not products:
        logging.info("No active products to check. Add one with: python3 pricetrack_updated.py add <url>")
        return 0

    with DATA_LOCK:
        state = load_state(args.state)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    with BROWSER_LOCK:
        with sync_playwright() as playwright:
            context, should_close = open_browser_context(playwright, args)
            try:
                for product in products:
                    logging.info("Checking %s", product.url)
                    previous = state.get(product.id, {})
                    observation = fetch_observation(context, product, args.timeout_ms, args.debug_dir)
                    decision = decide_alert(product, observation, previous)
                    logging.info(
                        "%s | %s | current=%s regular=%s sale=%s source=%s",
                        observation.name or product.name or product.id,
                        observation.status,
                        format_money(observation.current_price),
                        format_money(observation.regular_price),
                        observation.on_sale,
                        observation.source,
                    )

                    if decision.should_alert:
                        message = build_alert_message(product, observation, decision.reason)
                        if args.no_alert:
                            logging.info("Alert suppressed by --no-alert:\n%s", message)
                        elif alert_handler:
                            alert_handler(message)
                            logging.info("Alert handled: %s", decision.reason)
                        else:
                            send_discord_webhook(message, webhook_url)
                            logging.info("Alert handled: %s", decision.reason)
                    elif observation.error:
                        logging.warning("%s: %s", product.url, observation.error)

                    state[product.id] = observation_to_state(observation, previous, decision.should_alert)
                    if not product.name and observation.name:
                        product.name = observation.name
            finally:
                if should_close:
                    context.close()

    if not args.url:
        with DATA_LOCK:
            current_products = load_products(args.products)
            latest_state = load_state(args.state)
            checked_by_id = {product.id: product for product in products}
            for current_product in current_products:
                checked_product = checked_by_id.get(current_product.id)
                if checked_product and not current_product.name and checked_product.name:
                    current_product.name = checked_product.name
            for product_id_key in checked_by_id:
                if product_id_key in state:
                    latest_state[product_id_key] = state[product_id_key]
            save_products(args.products, current_products)
            save_state(args.state, latest_state)
    return 0


def run_forever(args: argparse.Namespace) -> int:
    while True:
        exit_code = check_products(args)
        logging.info("Sleeping for %s minute(s).", args.interval_minutes)
        time.sleep(args.interval_minutes * 60)
        if exit_code:
            return exit_code


def add_product_record(
    products_path: Path,
    url: str,
    name: str = "",
    target_price: float | None = None,
    notes: str = "",
) -> tuple[Product, bool]:
    with DATA_LOCK:
        products = load_products(products_path)
        new_id = product_id(url)
        normalized_url = url.strip()
        for product in products:
            if product.id == new_id or product.url.rstrip("/") == normalized_url.rstrip("/"):
                product.active = True
                product.name = name or product.name
                product.target_price = target_price if target_price is not None else product.target_price
                product.notes = notes or product.notes
                save_products(products_path, products)
                return product, False

        product = Product(
            id=new_id,
            name=name,
            url=normalized_url,
            target_price=target_price,
            active=True,
            notes=notes,
        )
        products.append(product)
        save_products(products_path, products)
        return product, True


def baseline_product(args: argparse.Namespace, product: Product) -> Observation:
    observation = observe_product(args, product)
    with DATA_LOCK:
        state = load_state(args.state)
        state[product.id] = observation_to_state(observation, state.get(product.id, {}), alerted=False)
        products = load_products(args.products)
        for stored_product in products:
            if stored_product.id == product.id:
                if not stored_product.name and observation.name:
                    stored_product.name = observation.name
                    product.name = observation.name
                break
        save_products(args.products, products)
        save_state(args.state, state)
    return observation


def add_product(args: argparse.Namespace) -> int:
    product, created = add_product_record(
        args.products,
        args.url,
        name=args.name or "",
        target_price=args.target_price,
        notes=args.notes or "",
    )
    action = "Added" if created else "Updated existing"
    logging.info("%s product: %s", action, product.url)
    return 0


def remove_product_record(products_path: Path, product_id_or_url: str) -> Product | None:
    with DATA_LOCK:
        products = load_products(products_path)
        kept: list[Product] = []
        removed: Product | None = None
        needle = product_id_or_url.rstrip("/")
        for product in products:
            if product.id == product_id_or_url or product.url.rstrip("/") == needle:
                removed = product
            else:
                kept.append(product)
        save_products(products_path, kept)
        return removed


def list_products(args: argparse.Namespace) -> int:
    with DATA_LOCK:
        products = load_products(args.products)
        state = load_state(args.state)
    if not products:
        print("No products tracked.")
        return 0
    for product in products:
        print(product_line(product, state.get(product.id)))
    return 0


def pause_product(args: argparse.Namespace, active: bool) -> int:
    with DATA_LOCK:
        products = load_products(args.products)
        found = False
        for product in products:
            if product.id == args.product_id or product.url.rstrip("/") == args.product_id.rstrip("/"):
                product.active = active
                found = True
        save_products(args.products, products)
    if not found:
        logging.error("No product found for %s", args.product_id)
        return 1
    return 0


def parse_target_option(options: str) -> float | None:
    if not options.strip():
        return None
    match = re.search(r"(?:target(?:-price)?\s*)?\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", options, re.IGNORECASE)
    return parse_optional_float(match.group(1)) if match else None


def build_bot_check_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        products=args.products,
        state=args.state,
        browser_profile=args.browser_profile,
        debug_dir=args.debug_dir,
        timeout_ms=args.timeout_ms,
        cdp_url=args.cdp_url,
        headless=args.headless,
        url=None,
        name="",
        target_price=None,
        no_alert=False,
    )


def discord_chunk(lines: list[str], limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        next_current = f"{current}\n\n{line}" if current else line
        if len(next_current) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = next_current
    if current:
        chunks.append(current)
    return chunks


def run_discord_bot(args: argparse.Namespace) -> int:
    try:
        import discord
        from discord.ext import commands
    except ImportError:
        logging.error("discord.py is not installed. Run: python3 -m pip install discord.py")
        return 1

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        logging.error("DISCORD_BOT_TOKEN is required for bot mode.")
        return 1

    try:
        alert_channel_id_text = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        intake_channel_id_text = os.getenv("DISCORD_CHANNELSEND_ID", "").strip()
        alert_channel_id = int(alert_channel_id_text) if alert_channel_id_text else None
        intake_channel_id = int(intake_channel_id_text) if intake_channel_id_text else alert_channel_id
    except ValueError:
        logging.error("DISCORD_CHANNEL_ID and DISCORD_CHANNELSEND_ID must be numeric Discord channel IDs.")
        return 1

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=args.command_prefix, intents=intents)
    check_args = build_bot_check_args(args)

    async def send_to_alert_channel(message: str) -> None:
        channel_id = alert_channel_id
        if channel_id is None:
            logging.info("No DISCORD_CHANNEL_ID configured. Alert would be:\n%s", message)
            return
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(message)

    def alert_handler(message: str) -> None:
        future = asyncio.run_coroutine_threadsafe(send_to_alert_channel(message), bot.loop)
        future.result(timeout=30)

    def channel_allowed(ctx: commands.Context[Any]) -> bool:
        return intake_channel_id is None or ctx.channel.id == intake_channel_id

    async def reject_wrong_channel(ctx: commands.Context[Any]) -> bool:
        if channel_allowed(ctx):
            return False
        await ctx.reply("Use the configured tracking channel for tracker commands.")
        return True

    @bot.event
    async def on_ready() -> None:
        logging.info("Logged in as %s", bot.user)
        if args.monitor and not getattr(bot, "monitor_started", False):
            bot.monitor_started = True
            bot.loop.create_task(monitor_loop())

    @bot.command(name="track", aliases=["add"])
    async def track(ctx: commands.Context[Any], url: str, *, options: str = "") -> None:
        if await reject_wrong_channel(ctx):
            return
        if not url.startswith(("http://", "https://")):
            await ctx.reply("Send a full lululemon product URL, like `!track https://shop.lululemon.com/...`.")
            return

        target_price = parse_target_option(options)
        product, created = await asyncio.to_thread(
            add_product_record,
            args.products,
            url,
            "",
            target_price,
            "",
        )
        action = "Added" if created else "Updated"
        message = await ctx.reply(f"{action} `{product.id}`. Checking the page once so I can save its current price...")

        try:
            observation = await asyncio.to_thread(baseline_product, check_args, product)
        except Exception as exc:
            logging.exception("Failed to baseline %s", url)
            await message.edit(content=f"{action} `{product.id}`, but the baseline check failed: `{exc}`")
            return

        if observation.status == "ok":
            await message.edit(
                content=(
                    f"Tracking `{product.id}`: {observation.name or product.name or product.url}\n"
                    f"Current price: {format_money(observation.current_price)}\n"
                    f"Sale status: {'On sale' if observation.on_sale else 'Not currently on sale'}"
                )
            )
        else:
            await message.edit(
                content=(
                    f"Tracking `{product.id}`, but I could not read the current price yet "
                    f"({observation.status}: {observation.error or 'unparsed'})."
                )
            )

    @bot.command(name="products", aliases=["list"])
    async def products_command(ctx: commands.Context[Any]) -> None:
        if await reject_wrong_channel(ctx):
            return
        with DATA_LOCK:
            products = load_products(args.products)
            state = load_state(args.state)
        if not products:
            await ctx.reply("No products are being tracked yet. Add one with `!track <url>`.")
            return
        lines = [product_line(product, state.get(product.id)) for product in products]
        for chunk in discord_chunk(lines):
            await ctx.reply(chunk)

    @bot.command(name="sale")
    async def sale_command(ctx: commands.Context[Any]) -> None:
        if await reject_wrong_channel(ctx):
            return
        with DATA_LOCK:
            products = load_products(args.products)
            state = load_state(args.state)
        sale_lines = [
            product_line(product, state.get(product.id))
            for product in products
            if parse_bool(state.get(product.id, {}).get("on_sale"), default=False)
        ]
        if not sale_lines:
            await ctx.reply("No tracked products are currently marked on sale.")
            return
        for chunk in discord_chunk(sale_lines):
            await ctx.reply(chunk)

    @bot.command(name="remove", aliases=["untrack"])
    async def remove_command(ctx: commands.Context[Any], product_id_or_url: str) -> None:
        if await reject_wrong_channel(ctx):
            return
        removed = await asyncio.to_thread(remove_product_record, args.products, product_id_or_url)
        if not removed:
            await ctx.reply(f"I could not find `{product_id_or_url}`.")
            return
        await ctx.reply(f"Removed `{removed.id}`: {removed.name or removed.url}")

    @bot.command(name="pause")
    async def pause_command(ctx: commands.Context[Any], product_id_or_url: str) -> None:
        if await reject_wrong_channel(ctx):
            return
        namespace = argparse.Namespace(products=args.products, product_id=product_id_or_url)
        result = await asyncio.to_thread(pause_product, namespace, False)
        await ctx.reply("Paused." if result == 0 else f"I could not find `{product_id_or_url}`.")

    @bot.command(name="resume")
    async def resume_command(ctx: commands.Context[Any], product_id_or_url: str) -> None:
        if await reject_wrong_channel(ctx):
            return
        namespace = argparse.Namespace(products=args.products, product_id=product_id_or_url)
        result = await asyncio.to_thread(pause_product, namespace, True)
        await ctx.reply("Resumed." if result == 0 else f"I could not find `{product_id_or_url}`.")

    @bot.command(name="check")
    async def check_command(ctx: commands.Context[Any]) -> None:
        if await reject_wrong_channel(ctx):
            return
        message = await ctx.reply("Checking tracked products now...")
        result = await asyncio.to_thread(check_products, check_args, alert_handler)
        await message.edit(content="Check complete." if result == 0 else f"Check failed with exit code {result}.")

    @bot.command(name="trackerhelp", aliases=["trackhelp"])
    async def tracker_help(ctx: commands.Context[Any]) -> None:
        await ctx.reply(
            "\n".join(
                [
                    "`!track <url>` add a product",
                    "`!track <url> target 79` add with a target price",
                    "`!products` list tracked products",
                    "`!sale` list tracked products currently marked on sale",
                    "`!remove <id-or-url>` stop tracking a product",
                    "`!pause <id-or-url>` pause without deleting",
                    "`!resume <id-or-url>` resume a paused product",
                    "`!check` run a check now",
                ]
            )
        )

    async def monitor_loop() -> None:
        await bot.wait_until_ready()
        while not bot.is_closed():
            try:
                await asyncio.to_thread(check_products, check_args, alert_handler)
            except Exception:
                logging.exception("Background monitor failed")
            await asyncio.sleep(args.interval_minutes * 60)

    bot.run(token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track lululemon product prices with a real browser.")
    parser.add_argument("--products", type=Path, default=PRODUCTS_CSV)
    parser.add_argument("--state", type=Path, default=STATE_JSON)
    parser.add_argument("--browser-profile", type=Path, default=BROWSER_PROFILE_DIR)
    parser.add_argument("--debug-dir", type=Path, default=Path("debug-pages"))
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument(
        "--cdp-url",
        default=os.getenv("CHROME_CDP_URL", ""),
        help="Attach to an existing Chrome debugging session, for example http://127.0.0.1:9222.",
    )
    parser.add_argument("--headed", dest="headless", action="store_false", help="Show the browser window.")
    parser.add_argument("--headless", dest="headless", action="store_true", help="Run browser in headless mode.")
    parser.set_defaults(headless=True)
    parser.add_argument("--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or migrate products.csv.")

    add = subparsers.add_parser("add", help="Add a product URL.")
    add.add_argument("url")
    add.add_argument("--name", default="")
    add.add_argument("--target-price", type=float)
    add.add_argument("--notes", default="")

    check = subparsers.add_parser("check", help="Run one check cycle.")
    check.add_argument("--url", help="Check one URL without saving it.")
    check.add_argument("--name", default="")
    check.add_argument("--target-price", type=float)
    check.add_argument("--no-alert", action="store_true")

    run = subparsers.add_parser("run", help="Continuously check active products.")
    run.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    run.add_argument("--no-alert", action="store_true")
    run.add_argument("--url", help=argparse.SUPPRESS)
    run.add_argument("--name", default="", help=argparse.SUPPRESS)
    run.add_argument("--target-price", type=float, help=argparse.SUPPRESS)

    bot = subparsers.add_parser("bot", help="Run Discord commands and the background monitor.")
    bot.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    bot.add_argument("--command-prefix", default=os.getenv("DISCORD_COMMAND_PREFIX", DEFAULT_COMMAND_PREFIX))
    bot.add_argument("--monitor", action=argparse.BooleanOptionalAction, default=True)
    bot.add_argument("--url", help=argparse.SUPPRESS)
    bot.add_argument("--name", default="", help=argparse.SUPPRESS)
    bot.add_argument("--target-price", type=float, help=argparse.SUPPRESS)
    bot.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser("list", help="List tracked products.")

    remove = subparsers.add_parser("remove", help="Remove a product by id or URL.")
    remove.add_argument("product_id")

    pause = subparsers.add_parser("pause", help="Pause a product by id or URL.")
    pause.add_argument("product_id")

    resume = subparsers.add_parser("resume", help="Resume a product by id or URL.")
    resume.add_argument("product_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.command == "init":
        ensure_products_file(args.products)
        save_state(args.state, load_state(args.state))
        logging.info("Initialized %s and %s", args.products, args.state)
        return 0
    if args.command == "add":
        return add_product(args)
    if args.command == "check":
        return check_products(args)
    if args.command == "run":
        return run_forever(args)
    if args.command == "bot":
        return run_discord_bot(args)
    if args.command == "list":
        return list_products(args)
    if args.command == "remove":
        removed = remove_product_record(args.products, args.product_id)
        if not removed:
            logging.error("No product found for %s", args.product_id)
            return 1
        logging.info("Removed product: %s", removed.url)
        return 0
    if args.command == "pause":
        return pause_product(args, active=False)
    if args.command == "resume":
        return pause_product(args, active=True)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
