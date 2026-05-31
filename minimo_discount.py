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


def _react_modal_overlay(page: Page) -> Locator:
    return page.locator(
        ".ReactModal__Overlay--after-open, "
        ".ReactModal__Overlay, "
        "[class*='a_modal_overlay']"
    )


def _is_overlay_visible(page: Page) -> bool:
    overlay = _react_modal_overlay(page)
    try:
        return overlay.count() > 0 and overlay.first.is_visible(timeout=1_000)
    except Exception:
        return False


def _purge_overlay_via_js(page: Page) -> bool:
    """Last-resort: remove ReactModalPortal nodes from the DOM directly.
    Used when the modal has no findable dismiss button."""
    try:
        removed = page.evaluate(
            """() => {
                let n = 0;
                document.querySelectorAll(
                    '.ReactModalPortal, .ReactModal__Overlay, [class*=\"a_modal_overlay\"]'
                ).forEach(el => { el.remove(); n++; });
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                document.body.style.position = '';
                return n;
            }"""
        )
        if removed:
            print(f"  force-removed {removed} overlay element(s) via JS")
            return True
    except Exception as e:
        print(f"  JS overlay removal error: {e}")
    return False


def _dismiss_startup_modal(page: Page) -> None:
    """The minimo menu page sometimes lifts a ReactModal overlay on top of
    everything (announcement / TOS / onboarding). Try common dismiss buttons,
    then Escape, and finally rip the overlay out of the DOM."""
    if not _is_overlay_visible(page):
        return

    print("startup modal detected; attempting dismissal")
    page.screenshot(path="startup-modal.png", full_page=True)

    candidates = [
        'button[aria-label="閉じる"]',
        'button[aria-label*="閉じる"]',
        'button[aria-label*="close" i]',
        'button:has-text("閉じる")',
        'button:has-text("OK")',
        'button:has-text("はい")',
        'button:has-text("了解")',
        'button:has-text("確認")',
        'button:has-text("スキップ")',
        'button:has-text("あとで")',
        'button:has-text("今はしない")',
        'button:has-text("×")',
        '.ReactModal__Content button[class*="close"]',
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=2_000)
                page.wait_for_timeout(500)
                if not _is_overlay_visible(page):
                    print(f"  dismissed via selector: {sel}")
                    return
        except Exception:
            continue

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass

    if not _is_overlay_visible(page):
        print("  dismissed via Escape")
        return

    print("  standard dismissal failed; force-removing overlay from DOM")
    _purge_overlay_via_js(page)
    page.wait_for_timeout(300)

    if _is_overlay_visible(page):
        print("  WARNING: overlay still present after force-removal")


def open_menu_page(page: Page, staff_hash: str) -> None:
    page.goto(MENU_URL_TEMPLATE.format(staff_hash=staff_hash), wait_until="networkidle")
    page.wait_for_timeout(2000)
    print(f"menu page url: {page.url}")
    page.screenshot(path="menu-page.png", full_page=True)
    _dismiss_startup_modal(page)


def menu_cards(page: Page) -> list[Locator]:
    """Find menu rows. Anchor on 直前割 buttons (uniquely present on menu rows)
    and walk up to the smallest ancestor that also contains '通常料金' (which
    every menu card displays). Falls back to class-based selectors."""
    buttons = page.locator('button:has-text("直前割作成"), button:has-text("直前割編集")')
    n = buttons.count()
    if n > 0:
        print(f"menu_cards: anchor on 直前割 buttons ({n} matches)")
        return [
            buttons.nth(i).locator(
                "xpath=ancestor::*[contains(., '通常料金')][1]"
            ).first
            for i in range(n)
        ]

    for sel in ('[class*="menu-item"]', '[class*="MenuItem"]', '[class*="menu-card"]'):
        loc = page.locator(sel)
        if loc.count() > 0:
            print(f"menu_cards: fallback selector '{sel}' ({loc.count()} matches)")
            return [loc.nth(i) for i in range(loc.count())]
    print("menu_cards: no candidates matched")
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


def _modal(page: Page) -> Locator:
    modal = page.locator('[role="dialog"], [class*="modal"], [class*="Modal"]').filter(
        has_text="直前割"
    ).last
    modal.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    return modal


def _input_after_label(modal: Locator, label_text: str) -> Locator:
    """Return the input element that follows the given label text inside the modal."""
    return modal.locator(
        f'xpath=.//*[contains(normalize-space(.), "{label_text}")]'
        f'/following::input[1]'
    ).first


def _wait_enabled(page: Page, locator: Locator, timeout_ms: int) -> bool:
    poll = 200
    for _ in range(max(1, timeout_ms // poll)):
        try:
            if locator.is_enabled(timeout=500):
                return True
        except PWTimeoutError:
            pass
        page.wait_for_timeout(poll)
    return False


def _modal_target_price(modal: Locator) -> int | None:
    """Compute target discount price from prices shown in the modal's menu summary."""
    try:
        text = modal.inner_text(timeout=2_000)
    except PWTimeoutError:
        return None
    matches = re.findall(r"[¥￥]\s*([\d,]+)", text)
    prices: list[int] = []
    for m in matches:
        try:
            prices.append(int(m.replace(",", "")))
        except ValueError:
            pass
    if not prices:
        return None
    base = min(prices)
    return math.floor(base * (1 - DISCOUNT_RATE_PERCENT / 100))


def _try_close_modal(page: Page) -> None:
    """Best-effort: close any open dialog so the next menu's click isn't blocked.
    Includes the ReactModal overlay used by minimo for all modals."""
    if not _is_overlay_visible(page):
        return
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    if not _is_overlay_visible(page):
        return
    for sel in (
        '.ReactModal__Content button[aria-label*="閉じる"]',
        '.ReactModal__Content button[aria-label*="close" i]',
        '.ReactModal__Content button:has-text("×")',
        '[role="dialog"] button[aria-label*="閉じる"]',
        '[role="dialog"] button:has-text("×")',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=1_000)
                page.wait_for_timeout(300)
                if not _is_overlay_visible(page):
                    return
        except Exception:
            continue


def _fill_input(page: Page, inp: Locator, value: str) -> None:
    inp.click()
    inp.fill("")
    inp.fill(value)
    inp.press("Tab")
    page.wait_for_timeout(400)


def set_discount_on_card(page: Page, card: Locator) -> None:
    _dismiss_startup_modal(page)
    print(f"  applying {DISCOUNT_RATE_PERCENT}% discount on: {extract_menu_name(card)[:60]}")

    card.locator('button:has-text("直前割作成")').first.click()
    modal = _modal(page)

    rate_input = _input_after_label(modal, "割引率")
    price_input = _input_after_label(modal, "直前割価格")
    submit = modal.locator('button:has-text("直前割を設定")').first
    submit.wait_for(state="visible", timeout=5_000)

    _fill_input(page, rate_input, str(DISCOUNT_RATE_PERCENT))

    # Some modals don't auto-fill price from the rate change (likely React state
    # not seeing fill() as a real edit), so submit stays disabled. Fall back to
    # filling the price ourselves.
    if not _wait_enabled(page, submit, timeout_ms=3_000):
        target_price = _modal_target_price(modal)
        try:
            current_price = price_input.input_value(timeout=1_000)
            current_rate = rate_input.input_value(timeout=1_000)
        except PWTimeoutError:
            current_price, current_rate = "?", "?"
        print(f"  submit disabled (rate='{current_rate}', price='{current_price}'); "
              f"filling price={target_price} as fallback")
        if target_price is None:
            page.screenshot(path="submit-disabled.png", full_page=True)
            raise RuntimeError("could not derive target price for fallback")
        _fill_input(page, price_input, str(target_price))
        # Re-fill rate too in case it was cleared by price input.
        _fill_input(page, rate_input, str(DISCOUNT_RATE_PERCENT))
        if not _wait_enabled(page, submit, timeout_ms=5_000):
            page.screenshot(path="submit-disabled.png", full_page=True)
            raise RuntimeError("「直前割を設定」 button stayed disabled — check inputs")

    submit.click()
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")


def remove_discount_on_card(page: Page, card: Locator) -> None:
    _dismiss_startup_modal(page)
    print(f"  removing discount on: {extract_menu_name(card)[:60]}")
    card.locator('button:has-text("直前割編集")').first.click()
    modal = _modal(page)
    modal.get_by_text("直前割を終了する", exact=True).first.click()
    page.wait_for_timeout(800)

    for label in ("終了する", "OK", "はい"):
        confirm = page.get_by_role("button", name=label, exact=True)
        try:
            if confirm.count() > 0 and confirm.first.is_visible(timeout=500):
                confirm.first.click(timeout=2_000)
                break
        except PWTimeoutError:
            continue
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")


def dump_card_names(cards: list[Locator], limit: int = 5) -> None:
    for i, card in enumerate(cards[:limit]):
        name = extract_menu_name(card)
        print(f"  card[{i}] name={name[:80]!r}")


def run_set(page: Page, menu_filter: str | None = None) -> None:
    cards = menu_cards(page)
    print(f"found {len(cards)} menu cards")
    dump_card_names(cards)

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
                _try_close_modal(page)
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
            _try_close_modal(page)
            cards = menu_cards(page)
    print(f"set complete: {processed} menus updated")


def run_remove(page: Page, menu_filter: str | None = None) -> None:
    if menu_filter:
        print(f"remove mode: TEST (menu filter='{menu_filter}')")
        cards = menu_cards(page)
        print(f"found {len(cards)} menu cards")
        dump_card_names(cards)
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
                _try_close_modal(page)
            return
        print(f"no menu matching '{menu_filter}' found")
        return

    print("remove mode: PRODUCTION (all menus)")
    processed = 0
    consecutive_errors = 0
    while consecutive_errors < 3:
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
            consecutive_errors = 0
        except Exception as e:
            print(f"ERROR removing discount: {e}")
            traceback.print_exc()
            _try_close_modal(page)
            consecutive_errors += 1
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
        except Exception:
            try:
                page.screenshot(path="error.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
