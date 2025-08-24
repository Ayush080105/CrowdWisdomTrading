from crewai.tools import tool
from playwright.async_api import async_playwright
import asyncio
from datetime import datetime
import re

@tool
async def scrape_mcp_site(url: str):
    """
    Dynamic scraper using Playwright. Detects product cards and prices without hardcoding selectors.

    Args:
        url: URL of the prediction market page

    Returns:
        list of dict: site, product_name, price, timestamp
    """
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle")
        except Exception as e:
            print(f"Error navigating to {url}: {e}")
            await browser.close()
            return results

        # Detect possible product containers dynamically
        possible_containers = await page.query_selector_all("div, li, article, section")
        seen_texts = set()

        for container in possible_containers:
            text = await container.inner_text()
            text = text.strip()
            if not text or text.lower().find("blocked") != -1 or text.lower().find("access") != -1:
                continue
            if text in seen_texts:
                continue  # avoid duplicates
            seen_texts.add(text)

            # Try to extract a price/odds number
            matches = re.findall(r"\d+\.?\d*", text)
            for match in matches:
                price = float(match)
                # Filter unrealistic numbers (like page headers or placeholders)
                if price < 0.01 or price > 2.0:  # adjust based on typical market ranges
                    continue

                product_name = " ".join(text.splitlines()).strip()[:100]  # first 100 chars
                results.append({
                    "site": url,
                    "product_name": product_name,
                    "price": price,
                    "timestamp": datetime.utcnow().isoformat()
                })
                break  # only take first valid price per container

        await browser.close()

    return results
