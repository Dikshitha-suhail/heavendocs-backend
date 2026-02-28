Content is user-generated and unverified.
# ╔══════════════════════════════════════════════════════════════════════╗
# ║         HEAVENDOCS — KARNATAKA PROPERTY VERIFICATION BACKEND        ║
# ║   Portals: Kaveri | Bhoomi | BBMP | RERA | eCourt | eMutation      ║
# ╚══════════════════════════════════════════════════════════════════════╝

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import anthropic, asyncio, json, os, httpx, base64, re
from datetime import datetime

# ── Playwright for govt portal scraping ──
from playwright.async_api import async_playwright

app = FastAPI(title="HeavenDocs API", version="1.0.0")

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],  # Production mein apna domain dalo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ── Config ──────────────────────────────────────────────────────────────
CLAUDE_API_KEY  = os.getenv("CLAUDE_API_KEY", "")
TWOCAPTCHA_KEY  = os.getenv("TWOCAPTCHA_KEY", "")  # 2captcha.com API key
claude_client   = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ════════════════════════════════════════════════════════════════════════
# CAPTCHA SOLVER — 2captcha.com use karta hai
# ════════════════════════════════════════════════════════════════════════
async def solve_captcha_image(img_bytes: bytes) -> str:
    """Image captcha solve karo via 2captcha"""
    b64 = base64.b64encode(img_bytes).decode()
    async with httpx.AsyncClient(timeout=120) as c:
        # Submit
        r = await c.post("http://2captcha.com/in.php", data={
            "key": TWOCAPTCHA_KEY, "method": "base64", "body": b64
        })
        if "OK|" not in r.text:
            raise Exception(f"2captcha submit failed: {r.text}")
        cap_id = r.text.split("|")[1]

        # Poll for result (max 60 seconds)
        for _ in range(20):
            await asyncio.sleep(3)
            r2 = await c.get(
                f"http://2captcha.com/res.php?key={TWOCAPTCHA_KEY}&action=get&id={cap_id}"
            )
            if "OK|" in r2.text:
                return r2.text.split("|")[1]
            if "ERROR" in r2.text and "NOT_READY" not in r2.text:
                raise Exception(f"2captcha error: {r2.text}")
    raise Exception("Captcha solve timeout")

# ════════════════════════════════════════════════════════════════════════
# 1. KAVERI ONLINE — Encumbrance Certificate (EC)
# ════════════════════════════════════════════════════════════════════════
async def scrape_kaveri_ec(
    district: str, property_no: str,
    from_year: int = 1990, to_year: int = 2025
) -> dict:
    """Kaveri Online se EC fetch karo"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(
                "https://kaverionline.karnataka.gov.in/ViewDoc/EC",
                wait_until="networkidle"
            )

            # Select district
            await page.select_option("select#DistrictId", label=district)
            await page.wait_for_timeout(1000)

            # Fill property number
            await page.fill("input#PropertyNo", property_no)
            await page.fill("input#FromYear", str(from_year))
            await page.fill("input#ToYear",   str(to_year))

            # Solve CAPTCHA
            cap_el = page.locator("#CaptchaImage")
            if await cap_el.count() > 0:
                cap_img = await cap_el.screenshot()
                cap_text = await solve_captcha_image(cap_img)
                await page.fill("input#CaptchaText", cap_text)

            await page.click("button#btnSearch")
            await page.wait_for_selector("#tblResults, .no-data", timeout=20000)

            # Parse results
            entries = []
            rows = await page.query_selector_all("#tblResults tbody tr")
            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) >= 4:
                    texts = [await c.inner_text() for c in cols]
                    entries.append({
                        "date":   texts[0].strip(),
                        "type":   texts[1].strip(),
                        "party":  texts[2].strip(),
                        "amount": texts[3].strip()
                    })

            screenshot = await page.screenshot(full_page=True)
            return {
                "source":       "Kaveri Online",
                "portal_url":   "kaverionline.karnataka.gov.in",
                "verified":     True,
                "property_no":  property_no,
                "district":     district,
                "period":       f"{from_year}–{to_year}",
                "entries":      entries,
                "is_clear":     len(entries) == 0,
                "total_entries": len(entries),
                "screenshot_b64": base64.b64encode(screenshot).decode(),
                "fetched_at":   datetime.now().isoformat()
            }

        except Exception as e:
            return {
                "source": "Kaveri Online", "verified": False,
                "error": str(e), "fetched_at": datetime.now().isoformat()
            }
        finally:
            await browser.close()

# ════════════════════════════════════════════════════════════════════════
# 2. BHOOMI — RTC / Pahani + Mutation Register
# ════════════════════════════════════════════════════════════════════════
BHOOMI_DISTRICTS = {
    "Bengaluru Urban":"572","Bengaluru Rural":"573","Mysuru":"574",
    "Tumakuru":"575","Mangaluru":"576","Belagavi":"577",
    "Shivamogga":"578","Ballari":"579","Davangere":"580",
    "Hassan":"581","Mandya":"582","Kalaburagi":"583",
    "Raichur":"584","Hubballi-Dharwad":"585","Vijayapura":"586",
    "Bidar":"587","Dharwad":"588","Udupi":"589",
    "Chitradurga":"590","Chikkamagaluru":"591","Kodagu":"592",
    "Gadag":"593","Haveri":"594","Koppal":"595",
    "Yadgir":"596","Ramanagara":"597","Chikkaballapur":"598",
    "Bagalkot":"599","Chamarajanagar":"600","Dakshina Kannada":"601",
    "Uttara Kannada":"602"
}

async def scrape_bhoomi_rtc(
    district: str, taluk: str, hobli: str,
    village: str, survey_no: str
) -> dict:
    """Bhoomi se RTC aur Mutation fetch karo"""
    dist_id = BHOOMI_DISTRICTS.get(district)
    if not dist_id:
        return {"source": "Bhoomi", "verified": False,
                "error": f"District '{district}' not mapped"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(
                "https://landrecords.karnataka.gov.in/service6/",
                wait_until="networkidle"
            )

            # District select
            await page.select_option("select#Districts", dist_id)
            await page.wait_for_timeout(1500)

            # Taluk
            taluk_options = await page.query_selector_all("select#Taluks option")
            taluk_id = None
            for opt in taluk_options:
                txt = await opt.inner_text()
                if taluk.lower() in txt.lower():
                    taluk_id = await opt.get_attribute("value")
                    break
            if taluk_id:
                await page.select_option("select#Taluks", taluk_id)
                await page.wait_for_timeout(1500)

            # Hobli
            hobli_options = await page.query_selector_all("select#Hoblis option")
            hobli_id = None
            for opt in hobli_options:
                txt = await opt.inner_text()
                if hobli.lower() in txt.lower():
                    hobli_id = await opt.get_attribute("value")
                    break
            if hobli_id:
                await page.select_option("select#Hoblis", hobli_id)
                await page.wait_for_timeout(1500)

            # Village
            village_options = await page.query_selector_all("select#Villages option")
            village_id = None
            for opt in village_options:
                txt = await opt.inner_text()
                if village.lower() in txt.lower():
                    village_id = await opt.get_attribute("value")
                    break
            if village_id:
                await page.select_option("select#Villages", village_id)
                await page.wait_for_timeout(1000)

            # Survey number
            await page.fill("input#SurveyNo", survey_no)

            # Captcha
            cap_el = page.locator("#captchaImage, .captcha-img")
            if await cap_el.count() > 0:
                cap_img = await cap_el.screenshot()
                cap_text = await solve_captcha_image(cap_img)
                await page.fill("input#captchaText, input.captcha-input", cap_text)

            await page.click("button#btnView, input[type=submit]")
            await page.wait_for_timeout(3000)

            # Extract owner info
            content = await page.content()
            screenshot = await page.screenshot(full_page=True)

            # Parse owner table
            owners = []
            owner_rows = await page.query_selector_all("table.owner-table tr, #ownerDetails tr")
            for row in owner_rows[1:]:
                cols = await row.query_selector_all("td")
                if len(cols) >= 2:
                    texts = [await c.inner_text() for c in cols]
                    owners.append({
                        "name":  texts[0].strip(),
                        "share": texts[1].strip() if len(texts) > 1 else "",
                        "how_acquired": texts[2].strip() if len(texts) > 2 else ""
                    })

            # Parse encumbrances
            encumbrances = []
            enc_rows = await page.query_selector_all("table.encumbrance-table tr, #encumbranceDetails tr")
            for row in enc_rows[1:]:
                cols = await row.query_selector_all("td")
                if cols:
                    texts = [await c.inner_text() for c in cols]
                    encumbrances.append({
                        "type": texts[0].strip(),
                        "detail": texts[1].strip() if len(texts) > 1 else ""
                    })

            return {
                "source":        "Bhoomi",
                "portal_url":    "landrecords.karnataka.gov.in",
                "verified":      True,
                "survey_number": survey_no,
                "district":      district,
                "taluk":         taluk,
                "village":       village,
                "owners":        owners,
                "encumbrances":  encumbrances,
                "is_encumbrance_free": len(encumbrances) == 0,
                "screenshot_b64": base64.b64encode(screenshot).decode(),
                "fetched_at":    datetime.now().isoformat()
            }

        except Exception as e:
            return {
                "source": "Bhoomi", "verified": False,
                "error": str(e), "fetched_at": datetime.now().isoformat()
            }
        finally:
            await browser.close()

# ════════════════════════════════════════════════════════════════════════
# 3a. BBMP — Property Tax + Khata Certificate (BENGALURU CITY ONLY)
# Portal: bbmptax.karnataka.gov.in
# Scope:  Sirf BBMP limits ke andar wali properties
# Extracts: Owner, PID, property address, tax paid status,
#           annual tax, arrears, khata number, ward
# ════════════════════════════════════════════════════════════════════════
BBMP_ZONES = {
    "East": "1", "West": "2", "South": "3", "Mahadevapura": "4",
    "Bommanahalli": "5", "Yelahanka": "6", "RR Nagar": "7", "Dasarahalli": "8"
}

def _safe_text(val: str) -> str:
    return val.strip() if val else "Not found"

async def _extract_label_value(page, label_text: str) -> str:
    """Helper: label ke baad wala td text extract karo"""
    try:
        el = await page.query_selector(
            f"td:has-text('{label_text}') + td, "
            f"th:has-text('{label_text}') + td, "
            f"label:has-text('{label_text}') + span, "
            f"label:has-text('{label_text}') + div"
        )
        if el:
            return _safe_text(await el.inner_text())
    except:
        pass
    return "Not found"

async def scrape_bbmp_property_tax(
    pid_no: str = "", khata_no: str = "", ward_no: str = "", zone: str = "East"
) -> dict:
    """
    BBMP Property Tax portal — bbmptax.karnataka.gov.in
    Scope: Sirf Bengaluru BBMP limits
    Search by: PID Number ya Khata Number
    Extracts: Owner, PID, address, ward, property tax paid/pending, arrears, A/B Khata
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(45000)

        result = {
            "source": "BBMP Property Tax",
            "portal_url": "bbmptax.karnataka.gov.in",
            "scope": "Bengaluru BBMP limits only",
            "verified": False,
            "pid_number": pid_no or "Not provided",
            "khata_number": khata_no or "Not provided",
            "ward": ward_no, "zone": zone,
            "owner_name": "Not found",
            "property_address": "Not found",
            "property_type": "Not found",
            "site_area_sqft": "Not found",
            "built_up_area": "Not found",
            "annual_tax": "Not found",
            "tax_paid_upto": "Not found",
            "tax_arrears": "Not found",
            "total_due": "Not found",
            "khata_type": "Not found",
            "is_tax_clear": False,
            "has_arrears": False,
            "property_found": False,
            "screenshot_b64": "",
            "fetched_at": datetime.now().isoformat()
        }
        try:
            await page.goto("https://bbmptax.karnataka.gov.in/",
                            wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(2000)

            search_val = pid_no if pid_no else khata_no
            search_input = page.locator(
                "input#txtPID, input[name*='PID'], input[placeholder*='PID'], "
                "input#txtSearch, input[name*='search']"
            )
            if await search_input.count() > 0:
                await search_input.first.fill(search_val)

            cap_el = page.locator("img[id*='aptcha']")
            if await cap_el.count() > 0:
                cap_bytes = await cap_el.first.screenshot()
                cap_text = await solve_captcha_image(cap_bytes)
                cap_inp = page.locator("input[id*='aptcha']")
                if await cap_inp.count() > 0:
                    await cap_inp.first.fill(cap_text)

            btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Search')")
            if await btn.count() > 0:
                await btn.first.click()
            await page.wait_for_timeout(4000)

            body_text = await page.inner_text("body")
            screenshot = await page.screenshot(full_page=True)
            result["screenshot_b64"] = base64.b64encode(screenshot).decode()

            if not any(k in body_text.lower() for k in ["no record","not found","no data","invalid"]):
                result["property_found"] = True
                result["verified"] = True

                first_link = page.locator("table tbody tr:first-child td a")
                if await first_link.count() > 0:
                    await first_link.first.click()
                    await page.wait_for_timeout(3000)
                    body_text = await page.inner_text("body")
                    screenshot = await page.screenshot(full_page=True)
                    result["screenshot_b64"] = base64.b64encode(screenshot).decode()

                for lbl in ["Owner Name","Property Owner","Name"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["owner_name"] = v; break
                for lbl in ["Property Address","Address","Door No"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["property_address"] = v; break
                for lbl in ["Property Type","Usage","Usage Type"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["property_type"] = v; break
                for lbl in ["Site Area","Plot Area","Total Area"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["site_area_sqft"] = v; break
                for lbl in ["Built Up Area","Plinth Area"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["built_up_area"] = v; break
                for lbl in ["Annual Tax","Property Tax","Current Year Tax"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["annual_tax"] = v; break
                for lbl in ["Paid Upto","Tax Paid Till","Last Paid Year"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found":
                        result["tax_paid_upto"] = v
                        result["is_tax_clear"] = "2024" in v or "2025" in v
                        break
                for lbl in ["Arrears","Tax Arrears","Pending","Outstanding"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found":
                        result["tax_arrears"] = v
                        result["has_arrears"] = bool(v) and v not in ["0","0.00","Not found"]
                        break
                for lbl in ["Total Due","Total Payable","Amount Due"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["total_due"] = v; break

                if "A-Khata" in body_text or "A Khata" in body_text:
                    result["khata_type"] = "A-Khata (Legal — Revenue site)"
                elif "B-Khata" in body_text or "B Khata" in body_text:
                    result["khata_type"] = "B-Khata (Unauthorized layout — risky!)"
            else:
                result["verified"] = True
                result["property_found"] = False

        except Exception as e:
            result["error"] = str(e)
        finally:
            await context.close()
            await browser.close()
        return result


# ════════════════════════════════════════════════════════════════════════
# 3b. e-AASTHI — e-Khata for ALL KARNATAKA (Non-BBMP areas)
# Portal: eaasthi.karnataka.gov.in
# Scope:  All Karnataka districts — Town Panchayats, CMC, TMC, City Corps
# Note:   Does NOT cover BBMP area — use scrape_bbmp_property_tax for Bengaluru
# Extracts: e-Khata number, owner, ULB, boundaries, area, tax status
# ════════════════════════════════════════════════════════════════════════
async def scrape_eaasthi_ekhata(
    district: str, ulb_name: str = "", khata_no: str = "",
    owner_name_search: str = ""
) -> dict:
    """
    e-Aasthi Karnataka — eaasthi.karnataka.gov.in
    Poora Karnataka except BBMP area ke liye e-Khata records
    ULB = Urban Local Body (Town Panchayat / CMC / TMC / City Corporation)
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(45000)

        result = {
            "source": "e-Aasthi Karnataka",
            "portal_url": "eaasthi.karnataka.gov.in",
            "scope": "All Karnataka — Non-BBMP areas (Town Panchayat, CMC, TMC etc.)",
            "verified": False,
            "search_district": district,
            "search_ulb": ulb_name,
            "search_khata_no": khata_no,
            "ekhata_number": "Not found",
            "owner_name": "Not found",
            "co_owner_name": "Not found",
            "property_address": "Not found",
            "ward_no": "Not found",
            "ward_name": "Not found",
            "survey_number": "Not found",
            "site_area": "Not found",
            "built_up_area": "Not found",
            "usage_type": "Not found",
            "property_type": "Not found",
            "ulb_name": "Not found",
            "ulb_type": "Not found",
            "boundaries": {"north":"Not found","south":"Not found","east":"Not found","west":"Not found"},
            "tax_status": "Not found",
            "annual_tax": "Not found",
            "tax_arrears": "Not found",
            "is_tax_clear": False,
            "khata_found": False,
            "ekhata_issued": False,
            "screenshot_b64": "",
            "fetched_at": datetime.now().isoformat()
        }

        try:
            await page.goto("https://eaasthi.karnataka.gov.in/",
                            wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(2000)

            # Navigate to property search
            citizen_link = page.locator(
                "a:has-text('Citizen'), a:has-text('Property Search'), "
                "a:has-text('Search Property')"
            )
            if await citizen_link.count() > 0:
                await citizen_link.first.click()
                await page.wait_for_timeout(2000)

            # District
            dist_sel = page.locator("select[id*='istrict'], select[name*='istrict']")
            if await dist_sel.count() > 0:
                opts = await dist_sel.first.query_selector_all("option")
                for opt in opts:
                    txt = await opt.inner_text()
                    if district.lower() in txt.lower():
                        val = await opt.get_attribute("value")
                        await dist_sel.first.select_option(val)
                        break
                await page.wait_for_timeout(1500)

            # ULB
            if ulb_name:
                ulb_sel = page.locator(
                    "select[id*='ulb'], select[name*='ulb'], "
                    "select[id*='ULB'], select[name*='ULB'], "
                    "select[id*='body'], select[id*='Body']"
                )
                if await ulb_sel.count() > 0:
                    opts = await ulb_sel.first.query_selector_all("option")
                    for opt in opts:
                        txt = await opt.inner_text()
                        if ulb_name.lower() in txt.lower():
                            val = await opt.get_attribute("value")
                            await ulb_sel.first.select_option(val)
                            break
                    await page.wait_for_timeout(1500)

            # Khata number input
            if khata_no:
                khata_inp = page.locator(
                    "input[id*='hata'], input[name*='hata'], input[placeholder*='Khata']"
                )
                if await khata_inp.count() > 0:
                    await khata_inp.first.fill(khata_no)
            elif owner_name_search:
                owner_inp = page.locator(
                    "input[id*='wner'], input[name*='wner'], input[placeholder*='wner']"
                )
                if await owner_inp.count() > 0:
                    await owner_inp.first.fill(owner_name_search)

            # Captcha
            cap_el = page.locator("img[id*='aptcha'], img[src*='captcha']")
            if await cap_el.count() > 0:
                cap_bytes = await cap_el.first.screenshot()
                cap_text = await solve_captcha_image(cap_bytes)
                cap_inp = page.locator("input[id*='aptcha'], input[name*='aptcha']")
                if await cap_inp.count() > 0:
                    await cap_inp.first.fill(cap_text)

            btn = page.locator("button[type='submit'], input[type='submit'], button:has-text('Search')")
            if await btn.count() > 0:
                await btn.first.click()
            await page.wait_for_timeout(4000)

            body_text = await page.inner_text("body")
            screenshot = await page.screenshot(full_page=True)
            result["screenshot_b64"] = base64.b64encode(screenshot).decode()

            if not any(k in body_text.lower() for k in ["no record","not found","no data"]):
                result["khata_found"] = True
                result["verified"] = True

                first_link = page.locator("table tbody tr:first-child td a, .property-list tr:first-child a")
                if await first_link.count() > 0:
                    await first_link.first.click()
                    await page.wait_for_timeout(3000)
                    body_text = await page.inner_text("body")
                    screenshot = await page.screenshot(full_page=True)
                    result["screenshot_b64"] = base64.b64encode(screenshot).decode()

                for lbl in ["e-Khata No","Khata Number","eKhata No","Khata No"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found":
                        result["ekhata_number"] = v
                        result["ekhata_issued"] = True; break
                for lbl in ["Owner Name","Property Owner","Name of Owner"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["owner_name"] = v; break
                for lbl in ["Co-Owner","Joint Owner","Co Owner"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["co_owner_name"] = v; break
                for lbl in ["Property Address","Address","House No","Door No"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["property_address"] = v; break
                for lbl in ["Ward No","Ward Number"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["ward_no"] = v; break
                for lbl in ["Ward Name"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["ward_name"] = v; break
                for lbl in ["Survey No","Survey Number"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["survey_number"] = v; break
                for lbl in ["Site Area","Plot Area","Total Area","Land Area"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["site_area"] = v; break
                for lbl in ["Built Up Area","Building Area","Plinth Area"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["built_up_area"] = v; break
                for lbl in ["Usage","Usage Type","Nature of Usage"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["usage_type"] = v; break
                for lbl in ["Property Type","Type of Property"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["property_type"] = v; break
                for lbl in ["ULB Name","Local Body","Municipality"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["ulb_name"] = v; break
                for lbl in ["ULB Type","Body Type"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["ulb_type"] = v; break
                for direction in ["North","South","East","West"]:
                    v = await _extract_label_value(page, direction)
                    if v != "Not found": result["boundaries"][direction.lower()] = v
                for lbl in ["Tax Status","Payment Status"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found":
                        result["tax_status"] = v
                        result["is_tax_clear"] = "paid" in v.lower() or "clear" in v.lower()
                        break
                for lbl in ["Annual Tax","Property Tax"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["annual_tax"] = v; break
                for lbl in ["Arrears","Tax Arrears","Pending Tax"]:
                    v = await _extract_label_value(page, lbl)
                    if v != "Not found": result["tax_arrears"] = v; break
            else:
                result["verified"] = True
                result["khata_found"] = False

        except Exception as e:
            result["error"] = str(e)
        finally:
            await context.close()
            await browser.close()
        return result


# ════════════════════════════════════════════════════════════════════════
# 4. RERA KARNATAKA — Project / Developer Check
# ════════════════════════════════════════════════════════════════════════
async def scrape_rera_karnataka(
    project_name: str = "", promoter_name: str = ""
) -> dict:
    """RERA Karnataka se project check karo"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(
                "https://rera.karnataka.gov.in/viewAllProjects",
                wait_until="networkidle"
            )

            if project_name:
                proj_input = page.locator(
                    "input[placeholder*='project'], input#projectName, input[name*='project']"
                )
                if await proj_input.count() > 0:
                    await proj_input.first.fill(project_name)

            if promoter_name:
                prom_input = page.locator(
                    "input[placeholder*='promoter'], input#promoterName, input[name*='promoter']"
                )
                if await prom_input.count() > 0:
                    await prom_input.first.fill(promoter_name)

            # Search button
            search_btn = page.locator("button:has-text('Search'), input[type=submit], button[type=submit]")
            if await search_btn.count() > 0:
                await search_btn.first.click()
            await page.wait_for_timeout(3000)

            screenshot = await page.screenshot(full_page=True)

            # Extract results
            projects = []
            rows = await page.query_selector_all("table tbody tr, .project-row")
            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) >= 3:
                    texts = [await c.inner_text() for c in cols]
                    projects.append({
                        "rera_no":   texts[0].strip(),
                        "project":   texts[1].strip() if len(texts) > 1 else "",
                        "promoter":  texts[2].strip() if len(texts) > 2 else "",
                        "status":    texts[3].strip() if len(texts) > 3 else "",
                        "district":  texts[4].strip() if len(texts) > 4 else ""
                    })

            return {
                "source":     "RERA Karnataka",
                "portal_url": "rera.karnataka.gov.in",
                "verified":   True,
                "projects":   projects,
                "registered": len(projects) > 0,
                "total_found": len(projects),
                "screenshot_b64": base64.b64encode(screenshot).decode(),
                "fetched_at": datetime.now().isoformat()
            }

        except Exception as e:
            return {
                "source": "RERA Karnataka", "verified": False,
                "error": str(e), "fetched_at": datetime.now().isoformat()
            }
        finally:
            await browser.close()

# ════════════════════════════════════════════════════════════════════════
# 5. eCOURTS KARNATAKA — Litigation / Court Case Check
# ════════════════════════════════════════════════════════════════════════
async def scrape_ecourts(
    party_name: str = "", case_no: str = ""
) -> dict:
    """Karnataka eCourts se case check karo"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(
                "https://services.ecourts.gov.in/ecourtindiaStates/",
                wait_until="networkidle"
            )

            # Karnataka select
            state_sel = page.locator("select#sess_state_code, select[name*='state']")
            if await state_sel.count() > 0:
                await state_sel.first.select_option(label="Karnataka")
                await page.wait_for_timeout(1000)

            # Party name search
            if party_name:
                party_input = page.locator("input#petPartyName, input[name*='party']")
                if await party_input.count() > 0:
                    await party_input.first.fill(party_name)
                await page.click("button:has-text('Go'), input[type=submit]")
                await page.wait_for_timeout(3000)

            screenshot = await page.screenshot(full_page=True)
            content = await page.content()

            # Parse cases
            cases = []
            rows = await page.query_selector_all("table.case_details tr, .case-row")
            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) >= 3:
                    texts = [await c.inner_text() for c in cols]
                    cases.append({
                        "case_no":    texts[0].strip(),
                        "parties":    texts[1].strip() if len(texts) > 1 else "",
                        "status":     texts[2].strip() if len(texts) > 2 else "",
                        "next_date":  texts[3].strip() if len(texts) > 3 else ""
                    })

            return {
                "source":       "Karnataka eCourts",
                "portal_url":   "services.ecourts.gov.in",
                "verified":     True,
                "cases":        cases,
                "litigation":   len(cases) > 0,
                "total_cases":  len(cases),
                "screenshot_b64": base64.b64encode(screenshot).decode(),
                "fetched_at":   datetime.now().isoformat()
            }

        except Exception as e:
            return {
                "source": "Karnataka eCourts", "verified": False,
                "error": str(e), "fetched_at": datetime.now().isoformat()
            }
        finally:
            await browser.close()

# ════════════════════════════════════════════════════════════════════════
# 6. KAVERI ONLINE — Mutation Register
# ════════════════════════════════════════════════════════════════════════
async def scrape_mutation_register(
    district: str, property_no: str
) -> dict:
    """Kaveri Online se Mutation Register check karo"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(
                "https://kaverionline.karnataka.gov.in/ViewDoc/MutationRegister",
                wait_until="networkidle"
            )

            await page.select_option("select#DistrictId", label=district)
            await page.wait_for_timeout(1000)
            await page.fill("input#PropertyNo", property_no)

            cap_el = page.locator("#CaptchaImage")
            if await cap_el.count() > 0:
                cap_img = await cap_el.screenshot()
                cap_text = await solve_captcha_image(cap_img)
                await page.fill("input#CaptchaText", cap_text)

            await page.click("button#btnSearch")
            await page.wait_for_timeout(3000)

            screenshot = await page.screenshot(full_page=True)

            mutations = []
            rows = await page.query_selector_all("#tblResults tbody tr")
            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) >= 3:
                    texts = [await c.inner_text() for c in cols]
                    mutations.append({
                        "mutation_no": texts[0].strip(),
                        "date":        texts[1].strip() if len(texts) > 1 else "",
                        "from_party":  texts[2].strip() if len(texts) > 2 else "",
                        "to_party":    texts[3].strip() if len(texts) > 3 else "",
                        "type":        texts[4].strip() if len(texts) > 4 else ""
                    })

            return {
                "source":         "Kaveri Online — Mutation",
                "portal_url":     "kaverionline.karnataka.gov.in",
                "verified":       True,
                "property_no":    property_no,
                "mutations":      mutations,
                "total_mutations": len(mutations),
                "screenshot_b64": base64.b64encode(screenshot).decode(),
                "fetched_at":     datetime.now().isoformat()
            }

        except Exception as e:
            return {
                "source": "Mutation Register", "verified": False,
                "error": str(e), "fetched_at": datetime.now().isoformat()
            }
        finally:
            await browser.close()

# ════════════════════════════════════════════════════════════════════════
# 7. CLAUDE AI — Final Report Generation
# ════════════════════════════════════════════════════════════════════════
async def generate_ai_report(
    govt_data: dict, client_info: dict, uploaded_doc_text: str = ""
) -> dict:
    """Sab govt data + documents se final legal report banao"""

    system_prompt = """You are a senior Karnataka property law expert with 25+ years of experience.
You have just received verified data from multiple Karnataka government portals.
Generate a comprehensive, accurate legal opinion report.
Always respond in valid JSON only. No markdown. No extra text."""

    user_prompt = f"""
CLIENT INFORMATION:
{json.dumps(client_info, indent=2)}

GOVERNMENT PORTAL VERIFIED DATA:
{json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'screenshot_b64'} for k, v in govt_data.items()}, indent=2)}

UPLOADED DOCUMENT TEXT (if any):
{uploaded_doc_text[:3000] if uploaded_doc_text else "No documents uploaded by client"}

Based on ALL the above verified government data, generate this JSON:
{{
  "overall_risk": "LOW or MEDIUM or HIGH or CRITICAL",
  "risk_score": 0-100,
  "property_summary": {{
    "owner": "", "survey_number": "", "district": "",
    "taluk": "", "village": "", "area": "",
    "registration_number": "", "registration_date": "",
    "consideration_amount": ""
  }},
  "portal_summary": [
    {{
      "portal": "Portal name",
      "status": "CLEAR or WARNING or ISSUE or NOT_VERIFIED",
      "key_finding": "One line finding"
    }}
  ],
  "findings": [
    {{
      "type": "ok or warn or bad or info",
      "title": "Finding title",
      "detail": "Detailed explanation",
      "source": "Which portal this came from"
    }}
  ],
  "ownership_chain": [
    {{"year": "", "owner": "", "transaction": "", "is_current": false}}
  ],
  "encumbrances": [
    {{"type": "", "party": "", "amount": "", "date": "", "status": "Active or Closed"}}
  ],
  "litigation_status": {{
    "has_cases": false,
    "cases_count": 0,
    "details": ""
  }},
  "rera_status": {{
    "applicable": false,
    "registered": false,
    "details": ""
  }},
  "title_chain_complete": true,
  "title_chain_years": 0,
  "legal_opinion": "Comprehensive 4-5 sentence legal opinion in simple language that any buyer can understand. Mention key findings from government portals. Give clear buying recommendation.",
  "action_items": [
    "Specific action 1 — which document to check",
    "Specific action 2 — which person to contact"
  ],
  "red_flags": ["Critical issue 1 if any"],
  "buyer_recommendation": "BUY SAFELY or PROCEED WITH CAUTION or DO NOT BUY WITHOUT LEGAL CLEARANCE or DO NOT BUY"
}}
"""

    response = claude_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()

    return json.loads(raw)

# ════════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

class VerificationRequest(BaseModel):
    # Client info
    client_name: str
    client_phone: Optional[str] = ""
    district: str
    property_type: Optional[str] = ""
    concerns: Optional[str] = ""
    # Kaveri EC
    property_no: Optional[str] = ""
    # Bhoomi RTC
    taluk: Optional[str] = ""
    hobli: Optional[str] = ""
    village: Optional[str] = ""
    survey_no: Optional[str] = ""
    # BBMP — Property Tax (Bengaluru city only)
    bbmp_pid_no: Optional[str] = ""     # PID Number (preferred)
    bbmp_khata_no: Optional[str] = ""   # Khata Number (fallback)
    bbmp_ward_no: Optional[str] = ""
    bbmp_zone: Optional[str] = "East"
    # e-Aasthi — e-Khata (All Karnataka non-BBMP areas)
    eaasthi_khata_no: Optional[str] = ""
    eaasthi_ulb_name: Optional[str] = ""   # Town Panchayat / CMC / TMC name
    eaasthi_owner_name: Optional[str] = ""
    # RERA
    project_name: Optional[str] = ""
    promoter_name: Optional[str] = ""
    # eCourts
    party_name: Optional[str] = ""
    # Document text (from frontend OCR if any)
    doc_text: Optional[str] = ""
    # Which portals to check
    check_kaveri_ec: bool = True
    check_bhoomi: bool = True
    check_bbmp: bool = True        # BBMP Property Tax
    check_eaasthi: bool = True     # e-Aasthi e-Khata (non-BBMP)
    check_rera: bool = True
    check_ecourt: bool = True
    check_mutation: bool = True

@app.post("/api/verify")
async def verify_property(req: VerificationRequest):
    """
    Main endpoint — sab portals check karo aur AI report do
    """
    if not CLAUDE_API_KEY:
        raise HTTPException(500, "CLAUDE_API_KEY not configured")

    govt_data = {}
    tasks = []

    # Run all portal checks in parallel for speed
    async def run_kaveri_ec():
        if req.check_kaveri_ec and req.property_no and req.district:
            govt_data["kaveri_ec"] = await scrape_kaveri_ec(
                req.district, req.property_no
            )

    async def run_bhoomi():
        if req.check_bhoomi and req.survey_no and req.district:
            govt_data["bhoomi_rtc"] = await scrape_bhoomi_rtc(
                req.district, req.taluk or "", req.hobli or "",
                req.village or "", req.survey_no
            )

    async def run_bbmp():
        # BBMP Property Tax — Bengaluru city only
        if req.check_bbmp and (req.bbmp_pid_no or req.bbmp_khata_no):
            govt_data["bbmp_property_tax"] = await scrape_bbmp_property_tax(
                pid_no=req.bbmp_pid_no or "",
                khata_no=req.bbmp_khata_no or "",
                ward_no=req.bbmp_ward_no or "",
                zone=req.bbmp_zone or "East"
            )

    async def run_eaasthi():
        # e-Aasthi e-Khata — All Karnataka non-BBMP areas
        if req.check_eaasthi and req.district and (req.eaasthi_khata_no or req.eaasthi_owner_name):
            govt_data["eaasthi_ekhata"] = await scrape_eaasthi_ekhata(
                district=req.district,
                ulb_name=req.eaasthi_ulb_name or "",
                khata_no=req.eaasthi_khata_no or "",
                owner_name_search=req.eaasthi_owner_name or ""
            )

    async def run_rera():
        if req.check_rera and (req.project_name or req.promoter_name):
            govt_data["rera"] = await scrape_rera_karnataka(
                req.project_name or "", req.promoter_name or ""
            )

    async def run_ecourt():
        if req.check_ecourt and req.party_name:
            govt_data["ecourt"] = await scrape_ecourts(req.party_name)

    async def run_mutation():
        if req.check_mutation and req.property_no and req.district:
            govt_data["mutation"] = await scrape_mutation_register(
                req.district, req.property_no
            )

    # Run all in parallel
    await asyncio.gather(
        run_kaveri_ec(), run_bhoomi(), run_bbmp(), run_eaasthi(),
        run_rera(), run_ecourt(), run_mutation(),
        return_exceptions=True
    )

    # Generate AI report from all collected data
    client_info = {
        "name": req.client_name,
        "district": req.district,
        "property_type": req.property_type,
        "concerns": req.concerns,
        "property_no": req.property_no,
        "survey_no": req.survey_no,
        "khata_no": req.khata_no
    }

    ai_report = await generate_ai_report(govt_data, client_info, req.doc_text)

    return {
        "success": True,
        "report": ai_report,
        "govt_data_raw": {
            k: {kk: vv for kk, vv in v.items() if kk != "screenshot_b64"}
            for k, v in govt_data.items()
        },
        "screenshots": {
            k: v.get("screenshot_b64", "")
            for k, v in govt_data.items()
        },
        "portals_checked": list(govt_data.keys()),
        "generated_at": datetime.now().isoformat()
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "HeavenDocs API v1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
