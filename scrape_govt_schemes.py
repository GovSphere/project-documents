
import time
import random
import csv

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, StaleElementReferenceException,
    ElementClickInterceptedException, ElementNotInteractableException
)

URL = "https://www.myscheme.gov.in"
START_URL = URL + "/search"

FIRST_PAGE = 1
LAST_PAGE = 453  # as per your paginator

# ----------------------------
# Selenium setup
# ----------------------------
opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--window-size=1920,1080")
opts.add_argument("--disable-gpu")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
# (Optional) make headless look more “real”
opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

driver = webdriver.Chrome(options=opts)
wait = WebDriverWait(driver, 30)

driver.get(START_URL)

# ----------------------------
# Helpers
# ----------------------------
def scheme_details(soup, required_details):
    items = []
    details = soup.select(f"#{required_details} ol li") + soup.select(f"#{required_details} ul li")
    for li in details:
        items.append(li.get_text(strip=True))
    return items

def dismiss_modal_best_effort():
    """Close any popup/modal if it appears."""
    for text in ("Ok", "OK", "Close", "I Agree", "Accept"):
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[normalize-space()='{text}']"))
            )
            btn.click()
            time.sleep(0.3)
            return
        except Exception:
            pass

def safe_click(el):
    """Scroll into view and click; fallback to JS click."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.15)
    try:
        el.click()
    except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
        driver.execute_script("arguments[0].click();", el)

def get_pager_ul():
    return wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//ul[contains(@class,'list-none') and contains(@class,'flex') and "
        "contains(@class,'flex-wrap') and contains(@class,'items-center') and "
        "contains(@class,'justify-center')]"
    )))

def first_result_el():
    """An element that becomes stale when results change."""
    return wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, "h2[id] > a[href^='/schemes/']")
    ))

def wait_page_change(old_first):
    """Wait until results change after pagination click."""
    try:
        wait.until(EC.staleness_of(old_first))
    except TimeoutException:
        time.sleep(0.8)

def click_page_number_if_visible(n: int) -> bool:
    """Click page number <li> if visible in current pagination window."""
    ul = get_pager_ul()
    try:
        li = ul.find_element(By.XPATH, f".//li[normalize-space()='{n}']")
        safe_click(li)
        return True
    except Exception:
        return False

def click_next_arrow() -> bool:
    """Click right arrow (next page)."""
    ul = get_pager_ul()
    try:
        next_li = ul.find_element(By.XPATH, ".//li[.//*[name()='svg']][last()]")
        safe_click(next_li)
        return True
    except Exception:
        return False

# ----------------------------
# Scrape loop
# ----------------------------
with open("schemes_list.csv", "w", newline="", encoding="utf-8") as csvfile:
    csvwriter = csv.writer(csvfile)
    # If you prefer joined strings in CSV, set join_lists=True
    join_lists = True
    csvwriter.writerow(["Department/Ministry", "Scheme Name", "Scheme Link",
                        "Benefits", "Eligibility Criteria", "Documents Required"])

    dismiss_modal_best_effort()
    first_result_el()  # confirm first page loaded

    main_window = driver.current_window_handle

    # Iterate pages
    for page in range(FIRST_PAGE, LAST_PAGE + 1):
        if page > 1:
            # paginate to requested page from current results page
            dismiss_modal_best_effort()
            old_first = first_result_el()

            # Try fast path (direct page number if visible), else Next
            if not click_page_number_if_visible(page):
                if not click_next_arrow():
                    raise RuntimeError("Could not find/click Next arrow in pagination.")

            wait_page_change(old_first)
            dismiss_modal_best_effort()
            first_result_el()  # ensure new page loaded

        # Parse the current search page results
        page_html = driver.page_source
        soup = BeautifulSoup(page_html, "html.parser")

        # Collect cards (ministry, scheme name, URL, id) from the listing
        cards = []
        for a in soup.select("h2[id] > a[href^='/schemes/']"):
            title_h2 = a.find_parent("h2")
            h2_id = title_h2.get("id", "")
            href = a.get("href", "")
            scheme_url = URL + href
            scheme_name = (a.find("span") or a).get_text(strip=True)

            # Ministry/Department text near the card
            ministry_h2 = title_h2.find_next("h2", attrs={"role": "button"})
            ministry = ministry_h2.get_text(strip=True) if ministry_h2 else ""

            print(h2_id," | ",scheme_url)
            cards.append((ministry, scheme_name, scheme_url))

        # Visit each scheme in a new tab, scrape details, close tab
        for ministry, scheme_name, scheme_url in cards:
            try:
                # open and switch
                driver.execute_script("window.open(arguments[0], '_blank');", scheme_url)
                wait.until(lambda d: len(d.window_handles) > 1)
                new_tab = [h for h in driver.window_handles if h != main_window][-1]
                driver.switch_to.window(new_tab)
                dismiss_modal_best_effort()

                # Wait for a reliable element on detail page
                # Try benefits or eligibility sections; fallback to main content
                try:
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#benefits, #eligibility, main")))
                except TimeoutException:
                    pass

                detail_html = driver.page_source
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                eligibility_list = scheme_details(detail_soup, "eligibility")
                benefits_list = scheme_details(detail_soup, "benefits")
                documents_list = scheme_details(detail_soup, "documents-required")

                # Optionally join lists for CSV readability
                def fmt(lst):
                    return " | ".join(lst) if join_lists else lst

                csvwriter.writerow([
                    ministry,
                    scheme_name,
                    scheme_url,
                    fmt(benefits_list),
                    fmt(eligibility_list),
                    fmt(documents_list)
                ])

            finally:
                # close tab and return to main
                try:
                    driver.close()
                except Exception:
                    pass
                driver.switch_to.window(main_window)

            time.sleep(random.uniform(0.25, 0.7))

        time.sleep(random.uniform(0.4, 1.0))

driver.quit()
print("Done. Saved: haryana_schemes_overview.csv")
