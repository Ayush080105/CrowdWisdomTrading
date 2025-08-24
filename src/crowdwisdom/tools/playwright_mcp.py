from playwright.sync_api import sync_playwright

class MCPPlaywright:
    """
    Minimal MCPPlaywright wrapper for scraping pages.
    """

    def __init__(self, headless=True):
        self.headless = headless
        self.playwright = None
        self.browser = None

    def new_page(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        page = self.browser.new_page()
        return page

    def close_page(self, page):
        page.close()

    def close_browser(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
