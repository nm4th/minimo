#!/usr/bin/env python3
"""Minimo last-minute discount automation.

Usage:
    python minimo_discount.py set                 # apply discount to all eligible menus
    python minimo_discount.py remove              # remove discount from all menus
    python minimo_discount.py set --menu "人気No.2"     # test: only that one menu
    python minimo_discount.py remove --menu "人気No.2"  # test: only that one menu

In test mode (--menu / MENU_FILTER env), only the menu whose name contains the given
substring is processed, and eligibility filters (平日限定 / 新規 / exclude keywords)
are skipped — only the required button presence is checked.

Credentials are read from environment variables:
    MINIMO_SALON_ID, MINIMO_PASSWORD, MINIMO_STAFF_HASH
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import jpholiday
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeoutError, sync_playwright

LOGIN_URL = "https://minimodel.jp/salontool/login"
MENU_URL_TEMPLATE = "https://minimodel.jp/salontool/home#/menu/staff/{staff_hash}/menu"
DISCOUNT_RATE_PERCENT = 10
EXCLUDE_NAME_KEYWORDS = ("韓国風", "パリジェンヌ")
JST = ZoneInfo("Asia/Tokyo")
DEFAULT_TIMEOUT_MS = 20_000


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: missing required env var: {name}")
    return value


def required_keyword(now: datetime) -> str:
    is_holiday = jpholiday.is_holiday(now.date())
    is_weekend = now.weekday() >= 5
    return "土日祝限定" if (is_weekend or is_holiday) else "平日限定"


def login(page: Page, salon_id: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.locator('input[name="loginId"], input[name="salonId"], input[type="text"]').first.fill(salon_id)
    page.locator('input[type="password"]').first.fill(password)
    page.locator('button[type="submit"], input[type="submit"]').first.click()
    page.wait_for_load_state("networkidle")
    if "login" in page.url:
        raise RuntimeError(f"login failed (still at {page.url})")
    print(f"logged in: {page.url}")


def open_menu_page(page: Page, staff_hash: str) -> None:
    page.goto(MENU_URL_TEMPLATE.format(staff_hash=staff_hash), wait_until="networkidle")
    page.wait_for_timeout(2000)


def menu_cards(page: Page) -> list[Locator]:
    """Each menu row/card. Best-effort selectors; verify on first run."""
    candidates = [
        '[class*="menu-item"]',
        '[class*="MenuItem"]',
        '[class*="menu-card"]',
        'li:has(button:has-text("直前割"))',
        'div:has(> button:has-text("直前割作成"))',
        'div:has(> button:has-text("直前割編集"))',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            return [loc.nth(i) for i in range(loc.count())]
    return []


def card_text(card: Locator) -> str:
    try:
        return card.inner_text(timeout=5_000)
    except PWTimeoutError:
        return ""


def extract_menu_name(card: Locator) -> str:
    """Extract menu name. Heuristic: line starting with 【...】, since menu names
    in this account follow that pattern (e.g., 【平日限定】..., 【土日祝限定】...)."""
    text = card_text(card)
    match = re.search(r"【[^】]*】[^\n]*", text)
    if match:
        return match.group(0).strip()
    return max((ln.strip() for ln in text.splitlines() if ln.strip()), key=len, default="")


def card_is_eligible_for_set(card: Locator, keyword: str) -> tuple[bool, str]:
    text = card_text(card)
    if not text:
        return False, "empty card text"
    if keyword not in text:
        return False, f"missing '{keyword}'"
    if "新規" not in text:
        return False, "missing '新規'"
    if "公開停止中" in text:
        return False, "publication stopped"
    name = extract_menu_name(card)
    for ex in EXCLUDE_NAME_KEYWORDS:
        if ex in name:
            return False, f"excluded keyword '{ex}' in menu name"
    if card.locator('button:has-text("直前割作成")').count() == 0:
        return False, "no '直前割作成' button (already set or not applicable)"
    return True, "ok"


def extract_base_price(card: Locator) -> int | None:
    """Return the ミニモ限定価格 (post-arrow / smaller value), used as the discount base."""
    text = card_text(card)
    matches = re.findall(r"[¥￥]\s*([\d,]+)", text)
    prices: list[int] = []
    for m in matches:
        try:
            prices.append(int(m.replace(",", "")))
        except ValueError:
            pass
    return min(prices) if prices else None


def calc_discounted_price(base_price: int) -> int:
    return math.floor(base_price * (1 - DISCOUNT_RATE_PERCENT / 100))


def set_discount_on_card(page: Page, card: Locator) -> None:
    base_price = extract_base_price(card)
    if base_price is None:
        raise RuntimeError("could not parse base price from card")
    target_price = calc_discounted_price(base_price)
    print(f"  base={base_price} -> target={target_price}")

    card.locator('button:has-text("直前割作成")').first.click()
    modal = page.locator('[class*="modal"], [role="dialog"]').last
    modal.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    price_input = modal.locator('input[type="number"], input[type="text"]').filter(
        has_not=modal.locator('input[disabled], input[readonly]')
    ).first
    price_input.fill("")
    price_input.fill(str(target_price))
    price_input.press("Tab")
    page.wait_for_timeout(500)

    rate_input = modal.locator('input').filter(has_text="").nth(1)
    try:
        rate_value = rate_input.input_value(timeout=2_000)
        if rate_value and rate_value.isdigit() and int(rate_value) < DISCOUNT_RATE_PERCENT:
            print(f"  rate auto-calculated to {rate_value}%, correcting to {DISCOUNT_RATE_PERCENT}%")
            rate_input.fill(str(DISCOUNT_RATE_PERCENT))
            rate_input.press("Tab")
            page.wait_for_timeout(500)
    except PWTimeoutError:
        pass

    modal.locator('button:has-text("直前割を設定")').first.click()
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")


def remove_discount_on_card(page: Page, card: Locator) -> None:
    card.locator('button:has-text("直前割編集")').first.click()
    modal = page.locator('[class*="modal"], [role="dialog"]').last
    modal.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    modal.locator('button:has-text("直前割を終了する")').first.click()

    for label in ("終了する", "OK", "はい"):
        confirm = page.locator(f'button:has-text("{label}")')
        try:
            if confirm.count() > 0:
                confirm.last.click(timeout=3_000)
                break
        except PWTimeoutError:
            continue
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")


def run_set(page: Page, menu_filter: str | None = None) -> None:
    cards = menu_cards(page)
    print(f"found {len(cards)} menu cards")

    if menu_filter:
        print(f"set mode: TEST (menu filter='{menu_filter}')")
        for i, card in enumerate(cards):
            name = extract_menu_name(card)
            if menu_filter not in name:
                continue
            if card.locator('button:has-text("直前割作成")').count() == 0:
                print(f"[{i}] matched '{name}' but no 直前割作成 button (already set?)")
                return
            print(f"[{i}] test set on: {name}")
            try:
                set_discount_on_card(page, card)
                print("set complete: 1 menu updated (test)")
            except Exception as e:
                print(f"[{i}] ERROR: {e}")
                traceback.print_exc()
            return
        print(f"no menu matching '{menu_filter}' found")
        return

    keyword = required_keyword(datetime.now(JST))
    print(f"set mode: PRODUCTION (keyword={keyword})")
    processed = 0
    for i, card in enumerate(cards):
        try:
            ok, reason = card_is_eligible_for_set(card, keyword)
            if not ok:
                continue
            print(f"[{i}] eligible: {extract_menu_name(card)[:60]}")
            set_discount_on_card(page, card)
            processed += 1
            cards = menu_cards(page)
        except Exception as e:
            print(f"[{i}] ERROR: {e}")
            traceback.print_exc()
    print(f"set complete: {processed} menus updated")


def run_remove(page: Page, menu_filter: str | None = None) -> None:
    if menu_filter:
        print(f"remove mode: TEST (menu filter='{menu_filter}')")
        cards = menu_cards(page)
        for i, card in enumerate(cards):
            name = extract_menu_name(card)
            if menu_filter not in name:
                continue
            if card.locator('button:has-text("直前割編集")').count() == 0:
                print(f"[{i}] matched '{name}' but no 直前割編集 button (no discount set)")
                return
            print(f"[{i}] test remove on: {name}")
            try:
                remove_discount_on_card(page, card)
                print("remove complete: 1 menu updated (test)")
            except Exception as e:
                print(f"[{i}] ERROR: {e}")
                traceback.print_exc()
            return
        print(f"no menu matching '{menu_filter}' found")
        return

    print("remove mode: PRODUCTION (all menus)")
    processed = 0
    while True:
        cards = menu_cards(page)
        target = None
        for card in cards:
            if card.locator('button:has-text("直前割編集")').count() > 0:
                target = card
                break
        if target is None:
            break
        try:
            remove_discount_on_card(page, target)
            processed += 1
        except Exception as e:
            print(f"ERROR removing discount: {e}")
            traceback.print_exc()
            break
    print(f"remove complete: {processed} menus updated")


def main() -> None:
    parser = argparse.ArgumentParser(description="minimo last-minute discount automation")
    parser.add_argument("action", choices=("set", "remove"))
    parser.add_argument(
        "--menu",
        default=os.environ.get("MENU_FILTER", "").strip() or None,
        help="test mode: only process the menu whose name contains this substring",
    )
    args = parser.parse_args()

    salon_id = env("MINIMO_SALON_ID")
    password = env("MINIMO_PASSWORD")
    staff_hash = env("MINIMO_STAFF_HASH")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page = context.new_page()
        try:
            login(page, salon_id, password)
            open_menu_page(page, staff_hash)
            if args.action == "set":
                run_set(page, menu_filter=args.menu)
            else:
                run_remove(page, menu_filter=args.menu)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
