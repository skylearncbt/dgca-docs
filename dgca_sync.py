#!/usr/bin/env python3
import os, sys, json, hashlib, re, logging, argparse, time
from pathlib import Path
from datetime import datetime
import requests, schedule, git
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

REPO_DIR  = Path(__file__).parent
DOCS_DIR  = REPO_DIR / "docs"
HASH_FILE = REPO_DIR / ".doc_hashes.json"
LOG_FILE  = REPO_DIR / "sync.log"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

DGCA_PAGES = {
    "CAR_Series_A_Airworthiness"        : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/1/0/viewDynamicRulesReq",
    "CAR_Series_B_Aerodromes"           : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/2/0/viewDynamicRulesReq",
    "CAR_Series_C_Air_Transport"        : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/3/0/viewDynamicRulesReq",
    "CAR_Series_D_Met_Services"         : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/4/0/viewDynamicRulesReq",
    "CAR_Series_E_Flight_Crew_Training" : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/5/0/viewDynamicRulesReq",
    "CAR_Series_F_Design_Standards"     : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/6/0/viewDynamicRulesReq",
    "CAR_Series_H_Aircraft_Operations"  : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/7/0/viewDynamicRulesReq",
    "CAR_Series_M_Maintenance"          : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=civilAviationRequirements/8/0/viewDynamicRulesReq",
    "CAARPs"                            : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=caarps/0/0/viewDynamicRulesReq",
    "Air_Safety_Circulars"              : "https://www.dgca.gov.in/digigov-portal/?dynamicPage=airSafetyCircular/0/0/viewDynamicRulesReq",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

def load_hashes():
    return json.loads(HASH_FILE.read_text()) if HASH_FILE.exists() else {}

def save_hashes(h):
    HASH_FILE.write_text(json.dumps(h, indent=2))

def content_hash(data):
    return hashlib.md5(data).hexdigest()

def safe_filename(raw, suffix=".pdf"):
    name = re.sub(r'[^\w\s\-.]', '_', raw).strip().replace(" ", "_")
    name = re.sub(r'_+', '_', name)[:80]
    if not name.endswith(suffix):
        name += suffix
    return name

def html_to_pdf(page, url):
    page.goto(url, wait_until="networkidle", timeout=60000)
    try:
        page.wait_for_selector("table, .content, main, article", timeout=8000)
    except:
        pass
    return page.pdf(
        format="A4", print_background=True,
        margin={"top":"15mm","bottom":"15mm","left":"12mm","right":"12mm"},
        display_header_footer=True,
        header_template='<div style="font-size:8px;width:100%;text-align:center;color:#555">DGCA India — BAC-Marigold Sync</div>',
        footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#555">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'
    )

def scrape_series(series_name, index_url, hashes):
    series_dir = DOCS_DIR / series_name
    series_dir.mkdir(parents=True, exist_ok=True)
    new_count = updated_count = 0
    log.info(f"\n  ── {series_name}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 Chrome/120")
        page = context.new_page()
        page.goto(index_url, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector("a", timeout=10000)
        except:
            browser.close()
            return hashes

        links = page.eval_on_selector_all("a", """els => els
            .filter(a => a.href && (
                a.href.includes('Upload') ||
                a.href.toLowerCase().includes('.pdf') ||
                a.href.includes('attach') ||
                a.href.includes('iframeAttachView')
            ))
            .map(a => ({
                href: a.href,
                text: (a.innerText||a.textContent||'').trim().substring(0,100)
            }))
        """)

        seen = set()
        unique = [l for l in links if l['href'] not in seen and not seen.add(l['href'])]
        log.info(f"    Found {len(unique)} links")

        session = requests.Session()
        for c in context.cookies():
            session.cookies.set(c['name'], c['value'])

        for lnk in unique:
            href  = lnk['href']
            label = lnk['text'] or href.split('=')[-1][:50]
            fname = safe_filename(label)
            dest  = series_dir / fname
            hkey  = f"{series_name}/{fname}"

            try:
                r = session.get(href, timeout=30)
                if r.status_code != 200:
                    continue
                data = r.content
                if not data[:4] == b'%PDF':
                    p2 = context.new_page()
                    data = html_to_pdf(p2, href)
                    p2.close()

                new_hash = content_hash(data)
                if hkey in hashes and hashes[hkey] == new_hash:
                    continue

                is_update = dest.exists()
                dest.write_bytes(data)
                hashes[hkey] = new_hash
                log.info(f"    {'✏ UPDATED' if is_update else '✚ NEW'}: {fname} ({len(data)//1024}KB)")
                if is_update: updated_count += 1
                else: new_count += 1

            except Exception as e:
                log.warning(f"    ✗ {fname}: {e}")

        browser.close()

    log.info(f"    → {new_count} new, {updated_count} updated")
    return hashes

def git_push(changed):
    try:
        repo = git.Repo(REPO_DIR)
        repo.git.add(A=True)
        if not repo.is_dirty(untracked_files=True):
            log.info("Git: nothing to commit")
            return
        ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"Auto-sync {ts}: {len(changed)} file(s) updated"
        repo.index.commit(msg)
        origin = repo.remote("origin")
        if GITHUB_TOKEN and "https://" in origin.url and "@" not in origin.url:
            origin.set_url(origin.url.replace("https://", f"https://{GITHUB_TOKEN}@"))
        origin.push()
        log.info(f"Git pushed: {msg}")
    except Exception as e:
        log.error(f"Git error: {e}")

def run_sync():
    log.info("="*60)
    log.info(f"DGCA SYNC  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("="*60)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    hashes = load_hashes()
    old    = dict(hashes)
    for series, url in DGCA_PAGES.items():
        try:
            hashes = scrape_series(series, url, hashes)
        except Exception as e:
            log.error(f"ERROR {series}: {e}")
    changed = [k for k in hashes if k not in old or old[k] != hashes[k]]
    save_hashes(hashes)
    log.info(f"\nChanges: {len(changed)}")
    if changed:
        git_push(changed)
    else:
        log.info("No changes — repo up to date")
    log.info("DONE\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", action="store_true")
    args = parser.parse_args()
    if args.schedule:
        run_sync()
        schedule.every().day.at("02:00").do(run_sync)
        log.info("Scheduler active — Ctrl+C to stop")
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_sync()
