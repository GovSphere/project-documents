
import csv
import time
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException


URL = "https://www.myscheme.gov.in"
START_URL = URL + "/search"
FIRST_PAGE, LAST_PAGE = 1, 4

# ---------- Tuning knobs ----------
MAX_WORKERS = 16          # increase to 24 on ubuntu runners if stable
REQ_TIMEOUT = 20
REQ_RETRIES = 3
POLITE_JITTER = (0.02, 0.08)  # tiny jitter per request thread to reduce burst
# ---------------------------------


def clean_text(s: str) -> str:
    """Remove troublesome invisible unicode that can break console/logs."""
    if not s:
        return ""
    return (s.replace("\u200d", "")
             .replace("\u200c", "")
             .replace("\ufeff", "")
             .strip())


def scheme_details_from_html(html: str, section_id: str):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(f"#{section_id} ol li") + soup.select(f"#{section_id} ul li")
    return [clean_text(li.get_text(strip=True)) for li in items]


def fetch_scheme_detail(session: requests.Session, row):
    """
    row = (ministry, scheme_name, scheme_url)
    Returns: (ministry, scheme_name, scheme_url, benefits, eligibility, documents)
    """
    ministry, scheme_name, scheme_url = row

    for attempt in range(1, REQ_RETRIES + 1):
        try:
            time.sleep(random.uniform(*POLITE_JITTER))
            r = session.get(scheme_url, timeout=REQ_TIMEOUT)
            r.raise_for_status()
            html = r.text

            benefits = scheme_details_from_html(html, "benefits")
            eligibility = scheme_details_from_html(html, "eligibility")
            documents = scheme_details_from_html(html, "documents-required")

            return (
                ministry, scheme_name, scheme_url,
                " | ".join(benefits),
                " | ".join(eligibility),
                " | ".join(documents)
            )
        except Exception:
            if attempt == REQ_RETRIES:
                # Return empty details if failed after retries
                return (ministry, scheme_name, scheme_url, "", "", "")
            time.sleep(0.6 * attempt)


def build_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--log-level=3")

    # Make page loads faster by not waiting for all assets
    opts.page_load_strategy = "eager"

    # Disable images / fonts / css for speed (Chrome preferences)
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.fonts": 2,
        "profile.managed_default_content_settings.cookies": 1,
        "profile.default_content_setting_values.notifications": 2,
    }
    opts.add_experimental_option("prefs", prefs)
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    driver = webdriver.Chrome(options=opts)

    # Block analytics & heavy third-party
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
            "https://plausible.io/*",
            "https://www.google-analytics.com/*",
            "https://www.googletagmanager.com/*",
            "https://connect.facebook.net/*",
        ]})
    except Exception:
        pass

    return driver


def dismiss_modal_best_effort(driver):
    for text in ("Ok", "OK", "Close", "I Agree", "Accept"):
        try:
            WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[normalize-space()='{text}']"))
            ).click()
            time.sleep(0.15)
            return
        except Exception:
            pass


def safe_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def get_pager_ul(driver, wait):
    return wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//ul[contains(@class,'list-none') and contains(@class,'flex') and "
        "contains(@class,'flex-wrap') and contains(@class,'items-center') and "
        "contains(@class,'justify-center')]"
    )))


def first_result_el(wait):
    # IMPORTANT: use real selector (not &gt;)
    return wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, "h2[id] > a[href^='/schemes/']")
    ))


def wait_page_change(wait, old_first):
    try:
        wait.until(EC.staleness_of(old_first))
    except TimeoutException:
        time.sleep(0.4)


def click_next_arrow(driver, wait):
    ul = get_pager_ul(driver, wait)
    # right arrow is last <li> that contains svg
    next_li = ul.find_element(By.XPATH, ".//li[.//*[name()='svg']][last()]")
    safe_click(driver, next_li)


def scrape_listing_links():
    """
    Selenium phase: iterate 453 pages and collect scheme listing rows
    Returns list of tuples: (ministry, scheme_name, scheme_url)
    """
    driver = build_driver()
    wait = WebDriverWait(driver, 25)

    driver.get(START_URL)
    dismiss_modal_best_effort(driver)

    # Ensure first results loaded
    first_result_el(wait)

    all_rows = []
    seen_urls = set()

    for page in range(FIRST_PAGE, LAST_PAGE + 1):
        if page > 1:
            dismiss_modal_best_effort(driver)
            old_first = first_result_el(wait)
            click_next_arrow(driver, wait)
            wait_page_change(wait, old_first)
            dismiss_modal_best_effort(driver)
            first_result_el(wait)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        for a in soup.select("h2[id] > a[href^='/schemes/']"):
            title_h2 = a.find_parent("h2")
            href = a.get("href", "")
            scheme_url = URL + href
            if scheme_url in seen_urls:
                continue
            seen_urls.add(scheme_url)

            scheme_name = clean_text((a.find("span") or a).get_text(strip=True))

            ministry_h2 = title_h2.find_next("h2", attrs={"role": "button"})
            ministry = clean_text(ministry_h2.get_text(strip=True) if ministry_h2 else "")

            all_rows.append((ministry, scheme_name, scheme_url))

        # tiny pause only between listing pages (not per scheme)
        time.sleep(random.uniform(0.05, 0.15))

        if page % 25 == 0:
            print(f"[Listing] Collected {len(all_rows)} scheme links upto page {page}/{LAST_PAGE}")

    driver.quit()
    return all_rows


def scrape_details_fast(rows, output_csv="schemes_list.csv"):
    """
    Requests phase: concurrently fetch details for every scheme URL.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with requests.Session() as session:
        session.headers.update(headers)

        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "Department/Ministry", "Scheme Name", "Scheme Link",
                "Benefits", "Eligibility Criteria", "Documents Required"
            ])

            futures = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                for row in rows:
                    futures.append(ex.submit(fetch_scheme_detail, session, row))

                done = 0
                total = len(futures)

                for fut in as_completed(futures):
                    result = fut.result()
                    w.writerow(result)
                    done += 1
                    if done % 200 == 0:
                        print(f"[Details] {done}/{total} done")

    print(f"✅ Done. Saved: {output_csv}")


if __name__ == "__main__":
    start = time.time()
    rows = scrape_listing_links()
    mid = time.time()

    print(f"Collected {len(rows)} unique scheme URLs in {mid - start:.1f}s")

    scrape_details_fast(rows, output_csv="schemes_list.csv")

    end = time.time()
    print(f"Total time: {(end - start)/60:.2f} minutes")