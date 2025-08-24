import datetime
import asyncio
import re
from playwright.async_api import async_playwright
from playwright.async_api import Error as PlaywrightError
from crewai.tools import tool

# ---------------- Config ----------------
NAV_TIMEOUT = 45000           # ms per goto/reload
SEL_TIMEOUT = 8000            # ms per selector wait
RETRIES = 2                   # navigation retries per detail page
CONCURRENCY = 2               # keep low until stable, then 3-5
MARKET_WATCHDOG_SEC = 60      # hard cap per market (sec)
HYDRATION_RETRIES = 3         # short loops to await dynamic content

# ---------------- Regex helpers ----------------
PRICE_RE_CENTS  = re.compile(r"(\d+(?:\.\d+)?)c|\b(\d+(?:\.\d+)?)¢", flags=re.I)
PRICE_RE_DOLLAR = re.compile(r"\$?\s*(\d+(?:\.\d+)?)")
PERCENT_RE      = re.compile(r"\b(\d+(?:\.\d+)?)%\b", flags=re.I)

def parse_price_token_to_float(tok: str):
    if not tok:
        return None
    t = tok.strip()
    # cents variants 30c / 30¢
    if t.endswith("c") or t.endswith("¢"):
        try:
            v = float(re.sub(r"[^\d\.]", "", t))
            return v / 100.0
        except:
            return None
    # percent 30%
    if t.endswith("%"):
        try:
            v = float(t[:-1])
            return v / 100.0
        except:
            return None
    # dollars or plain number: $0.30, 0.30, 30 (assume cents if >1)
    try:
        v = float(t.replace("$", ""))
        return v if v <= 1.0 else v / 100.0
    except:
        return None

def parse_yes_no_from_text_block(text: str):
    """
    Parse 'Yes 30¢  No 72¢' or variants from a block of text.
    Returns dict with optional 'Yes' and 'No' floats in [0,1].
    """
    out = {}
    if not text:
        return out
    t = " ".join(text.split())

    # Capture token immediately following 'Yes'/'No'
    m_yes = re.search(r"\byes\b[^0-9$%c¢]*([$\d][\d\.\$%c¢]*)", t, flags=re.I)
    m_no  = re.search(r"\bno\b[^0-9$%c¢]*([$\d][\d\.\$%c¢]*)", t, flags=re.I)

    if m_yes:
        v = parse_price_token_to_float(m_yes.group(1))
        if v is not None:
            out["Yes"] = v
    if m_no:
        v = parse_price_token_to_float(m_no.group(1))
        if v is not None:
            out["No"] = v

    # If we didn’t catch tokens adjacent to labels, try generic nearest tokens
    if "Yes" not in out:
        # first percent/dollar/cents token in text
        m = PERCENT_RE.search(t) or PRICE_RE_CENTS.search(t) or PRICE_RE_DOLLAR.search(t)
        if m:
            tok = m.group(0)
            v = parse_price_token_to_float(tok)
            if v is not None:
                out["Yes"] = v
    if "No" not in out:
        # try to find a second token; crude but practical
        m_all = list(PERCENT_RE.finditer(t)) or list(PRICE_RE_CENTS.finditer(t)) or list(PRICE_RE_DOLLAR.finditer(t))
        if len(m_all) >= 2:
            tok = m_all[1].group(0)
            v = parse_price_token_to_float(tok)
            if v is not None:
                out["No"] = v

    return out

def slug_to_title(slug: str) -> str:
    s = slug.rsplit("/", 1)[-1]
    s = s.replace("-", " ").replace("_", " ").strip()
    return s.title() if s else slug

async def get_absolute_href(site: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if site == "polymarket":
        return f"https://polymarket.com{href}"
    if site == "kalshi":
        return f"https://kalshi.com{href}"
    return href

async def speed_block_route(route):
    # Block heavy resources only; allow scripts and styles
    rtype = route.request.resource_type
    if rtype in {"image", "media", "font"}:
        return await route.abort()
    return await route.continue_()

# ---------------- Detail-page Yes/No extraction ----------------
async def extract_yes_no_on_detail(page):
    """
    Try multiple UI patterns on detail page to get Yes/No quickly.
    """
    # 1) Try a container that likely holds the two trade buttons
    try:
        panel = await page.query_selector("section:has-text('Yes'):has-text('No'), div:has-text('Yes'):has-text('No')")
        if panel:
            t = (await panel.inner_text()) or ""
            res = parse_yes_no_from_text_block(t)
            if "Yes" in res or "No" in res:
                return res
    except PlaywrightError:
        pass

    # 2) Try outcome-like blocks and parse each
    try:
        blocks = await page.query_selector_all(
            "button[data-testid^='outcome-button'], div[data-testid='outcome'], [data-testid*='outcome'], [class*='outcome']"
        )
        got = {}
        for b in blocks or []:
            try:
                bt = (await b.inner_text()) or ""
            except PlaywrightError:
                continue
            res = parse_yes_no_from_text_block(bt)
            got.update(res)
        if "Yes" in got or "No" in got:
            return got
    except PlaywrightError:
        pass

    # 3) Fallback: whole body text
    try:
        body_text = (await page.inner_text("body")) or ""
        res = parse_yes_no_from_text_block(body_text)
        return res
    except PlaywrightError:
        return {}

# ---------------- Main tool ----------------
@tool
async def scrape_mcp_site(url: str, max_items: int = 10):
    """
    Scrapes Polymarket or Kalshi listing pages and returns JSON list where each item has:
      - site
      - product_name
      - url
      - outcomes: for Yes/No markets, includes {"Yes": float, "No": float}; for non-binary, may be {}
      - timestamp (UTC ISO)

    Key behavior:
      - Extract Yes/No directly from listing cards when present (fast path).
      - If missing/partial, open detail page and extract there.
      - Bounded time via watchdog; robust error handling and cleanup.
    """

    results = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # Open the listing URL
        page = await browser.new_page()
        try:
            await page.route("**/*", speed_block_route)
        except Exception:
            pass

        try:
            await page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        except Exception as e:
            print(f"❌ Failed to load listing: {url} ({e})")
            try:
                await browser.close()
            except Exception:
                pass
            return results

        # Small hydration scrolls
        for _ in range(2):
            try:
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                pass
            try:
                await page.wait_for_timeout(400)
            except Exception:
                pass

        # Determine site and selector for cards
        if "polymarket" in url:
            site = "polymarket"
            card_selector = "a[href*='/event/'], [data-testid='event-card'] a[href]"
        elif "kalshi" in url:
            site = "kalshi"
            # Kalshi listing pages often use anchors to market URLs:
            card_selector = "a[href*='/markets/'], a[href*='/market/'], [data-testid='market-card'] a[href]"
        else:
            try:
                await browser.close()
            except Exception:
                pass
            raise ValueError("Unsupported site")

        # Gather cards and extract quick Yes/No from each card
        try:
            await page.wait_for_selector(card_selector, state="attached", timeout=SEL_TIMEOUT)
            anchors = await page.query_selector_all(card_selector)
        except Exception:
            anchors = []

        seen = set()
        items = []
        for a in anchors:
            try:
                href = await a.get_attribute("href")
            except PlaywrightError:
                continue
            abs_href = await get_absolute_href(site, href or "")
            if not abs_href or abs_href in seen:
                continue
            seen.add(abs_href)

            # Grab card text to parse title and quick Yes/No
            card_text = ""
            try:
                card_text = (await a.inner_text()) or ""
            except PlaywrightError:
                pass

            # Title: first non-empty line, else slug
            title = slug_to_title(abs_href)
            if card_text:
                for ln in card_text.splitlines():
                    ln = ln.strip()
                    if ln:
                        title = ln
                        break

            quick_yn = parse_yes_no_from_text_block(card_text)

            items.append({
                "site": site,
                "url": abs_href,
                "title": title,
                "quick_yes_no": quick_yn
            })

            if len(items) >= max_items:
                break

        # If listing yielded nothing, treat the given URL as a single market
        if not items:
            items = [{"site": site, "url": url, "title": slug_to_title(url), "quick_yes_no": {}}]

        async def scrape_one(item):
            await sem.acquire()
            market_page = None
            try:
                site = item["site"]
                abs_href = item["url"]
                title = item["title"]
                quick = item.get("quick_yes_no") or {}

                # If grid already has both Yes and No, short-circuit
                if "Yes" in quick and "No" in quick:
                    return {
                        "site": site,
                        "product_name": title or "N/A",
                        "url": abs_href,
                        "outcomes": {"Yes": quick["Yes"], "No": quick["No"]},
                        "timestamp": datetime.datetime.utcnow().isoformat()
                    }

                # Navigate to detail page (to fill missing side)
                market_page = await browser.new_page()
                try:
                    await market_page.route("**/*", speed_block_route)
                except Exception:
                    pass

                nav_ok = False
                for attempt in range(RETRIES + 1):
                    try:
                        await market_page.goto(abs_href, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                        try:
                            await market_page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        # Dismiss consent if present
                        try:
                            consent = await market_page.query_selector("button:has-text('Accept'), button:has-text('I Agree')")
                            if consent:
                                await consent.click()
                                await asyncio.sleep(0.2)
                        except Exception:
                            pass
                        # Nudge hydration
                        for _ in range(2):
                            try:
                                await market_page.evaluate("window.scrollBy(0, 600)")
                            except Exception:
                                pass
                            try:
                                await market_page.wait_for_timeout(250)
                            except Exception:
                                pass
                        nav_ok = True
                        break
                    except Exception as nav_err:
                        if attempt < RETRIES:
                            try:
                                await market_page.reload(timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                            except Exception:
                                pass
                            await asyncio.sleep(0.6 * (attempt + 1))
                        else:
                            print(f"⚠️ Navigation failed for {abs_href}: {nav_err}")

                outcomes = dict(quick)  # start from quick values if any

                if nav_ok:
                    try:
                        detail_yn = await extract_yes_no_on_detail(market_page)
                        # Merge, preferring detail values if present
                        for k, v in detail_yn.items():
                            outcomes[k] = v
                    except PlaywrightError as pe:
                        print(f"⚠️ Detail extractor PlaywrightError for {abs_href}: {pe}")
                    except Exception as ex:
                        print(f"⚠️ Detail extractor error for {abs_href}: {ex}")

                return {
                    "site": site,
                    "product_name": title or "N/A",
                    "url": abs_href,
                    "outcomes": outcomes,
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }

            except asyncio.CancelledError:
                return {
                    "site": item.get("site", ""),
                    "product_name": item.get("title", "N/A"),
                    "url": item.get("url", ""),
                    "outcomes": {},
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }
            except Exception as e:
                print(f"❌ scrape_one error for {item.get('url')}: {e}")
                return None
            finally:
                try:
                    if market_page:
                        await market_page.close()
                except Exception:
                    pass
                try:
                    sem.release()
                except Exception:
                    pass

        async def timed(item):
            try:
                return await asyncio.wait_for(scrape_one(item), timeout=MARKET_WATCHDOG_SEC)
            except asyncio.TimeoutError:
                print(f"⏰ Watchdog timeout for {item.get('url')}")
                return {
                    "site": item.get("site", ""),
                    "product_name": item.get("title", "N/A"),
                    "url": item.get("url", ""),
                    "outcomes": item.get("quick_yes_no", {}) or {},
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }
            except Exception as e:
                print(f"❌ timed() error for {item.get('url')}: {e}")
                return None

        tasks = [timed(it) for it in items]
        done = await asyncio.gather(*tasks, return_exceptions=True)

        for res in done:
            if isinstance(res, Exception):
                print(f"⚠️ task exception: {res}")
            elif res is not None:
                results.append(res)

        try:
            await browser.close()
        except Exception:
            pass

    return results
