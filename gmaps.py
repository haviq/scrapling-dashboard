from urllib.parse import quote


def scrape_gmaps_leads(keyword, limit=50):
    from playwright.sync_api import sync_playwright

    results, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        ).new_page()
        page.goto('https://www.google.com/maps/search/' + quote(keyword),
                  wait_until='domcontentloaded')
        try:
            page.wait_for_selector('div[role="feed"]', timeout=15000)
        except Exception:
            pass

        for _ in range(30):
            if len(results) >= limit:
                break
            for a in page.query_selector_all('a[href*="/maps/place/"]'):
                if len(results) >= limit:
                    break
                href = (a.get_attribute('href') or '').split('&')[0]
                if not href or href in seen:
                    continue
                seen.add(href)
                name = (a.get_attribute('aria-label') or a.inner_text() or '').strip()
                name = name.split('\n')[0][:120]
                item = {'name': name, 'url': href}
                try:
                    card = a.evaluate_handle('el => el.closest("div[jsaction]")')
                    el = card.as_element()
                    if el:
                        r = el.query_selector('span[role="img"][aria-label]')
                        if r:
                            label = r.get_attribute('aria-label') or ''
                            parts = label.replace(',', '.').split()
                            item['rating'] = next((x for x in parts if x[0].isdigit()), '')
                            item['reviews'] = next((x.strip('()') for x in parts if x.startswith('(')), '')
                except Exception:
                    pass
                results.append(item)
            try:
                feed = page.query_selector('div[role="feed"]')
                if feed:
                    feed.evaluate('el => el.scrollBy(0, el.scrollHeight)')
                else:
                    break
                page.wait_for_timeout(2000)
            except Exception:
                break
        browser.close()
    return results
