# post_if_due.py
import os, io, json, base64, logging, tempfile
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import base64
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- config from env / secrets ---
GCP_SA_JSON = os.environ.get("GCP_SA_JSON")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
SHEET_ID = os.environ.get("SHEET_ID")
TIKTOK_STORAGE = os.environ.get("TIKTOK_STORAGE")
CAPTION = os.environ.get("CAPTION", "✨ Made with AI | #SoraCreations #AI")
TIME_ZONE = os.environ.get("TIME_ZONE", "America/New_York")
TOLERANCE_SECONDS = int(os.environ.get("TOLERANCE_SECONDS", "150"))  # 2.5 minutes

if not (GCP_SA_JSON and DRIVE_FOLDER_ID and SHEET_ID and TIKTOK_STORAGE):
    logging.error("Missing one of required env vars: GCP_SA_JSON, DRIVE_FOLDER_ID, SHEET_ID, TIKTOK_STORAGE")
    raise SystemExit(1)

sa_info = json.loads(GCP_SA_JSON)
scopes = ['https://www.googleapis.com/auth/drive.readonly',
          'https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
sheets = build('sheets', 'v4', credentials=creds, cache_discovery=False)
tz = pytz.timezone(TIME_ZONE)

# --- helpers ---
def list_videos():
    q = f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'video' and trashed=false"
    resp = drive.files().list(q=q, fields="files(id,name,modifiedTime)", pageSize=1000).execute()
    files = resp.get("files", [])
    files_sorted = sorted(files, key=lambda x: x['name'])
    return files_sorted

def download_file(file_id, file_name):
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_"+file_name)
    tmp.write(fh.read())
    tmp.close()
    return tmp.name

def read_schedule_sheet():
    # expects Schedule tab with header in row1: Day,Time 1,Time 2,Time 3,Caption
    r = sheets.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Schedule!A2:E8").execute()
    vals = r.get("values", [])
    schedule = {}
    caption_map = {}
    for row in vals:
        if not row: continue
        day = row[0].strip().lower()
        times = []
        if len(row) > 1 and row[1]: times.append(row[1].strip())
        if len(row) > 2 and row[2]: times.append(row[2].strip())
        if len(row) > 3 and row[3]: times.append(row[3].strip())
        caption = row[4] if len(row) > 4 and row[4] else CAPTION
        schedule[day] = times
        caption_map[day] = caption
    return schedule, caption_map

def read_posted_log():
    r = sheets.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Posted!A2:A10000").execute()
    vals = r.get("values", [])
    return set(v[0] for v in vals if v)

def log_posted(filename, file_id, slot):
    now_iso = datetime.now(pytz.utc).isoformat()
    row = [[filename, file_id, now_iso, slot]]
    sheets.spreadsheets().values().append(spreadsheetId=SHEET_ID, range="Posted!A2", valueInputOption="RAW", body={"values": row}).execute()

def is_time_to_post(schedule):
    now = datetime.now(tz)
    weekday = now.strftime("%A").lower()
    today_times = schedule.get(weekday, [])
    for t in today_times:
        try:
            hh, mm = map(int, t.split(":"))
        except:
            continue
        scheduled = tz.localize(datetime(now.year, now.month, now.day, hh, mm))
        delta = abs((now - scheduled).total_seconds())
        if delta <= TOLERANCE_SECONDS:
            return True, t
    return False, None

def upload_to_tiktok(video_path, caption_text):
import base64  # Add this import at the top if not there (after other imports)
from cryptography.fernet import Fernet  # Add this too (after other imports)

# ... (in the function, replace the write block with:)
encrypted_path = "tiktok_storage.enc"
passphrase_b64 = os.environ.get("ENCRYPTED_PASSPHRASE")
storage_path = "/tmp/tiktok_storage.json"
if passphrase_b64:
    try:
        key = base64.urlsafe_b64decode(passphrase_b64)
        f = Fernet(key)
        with open(encrypted_path, "rb") as enc_file:
            encrypted = enc_file.read()
        decrypted = f.decrypt(encrypted).decode()
        with open(storage_path, "w", encoding="utf-8") as out:
            out.write(decrypted)
        logging.info("Decrypted storage state successfully.")
    except Exception as e:
        logging.error(f"Decryption failed: {e}")
        return False, "decryption_error"
else:
    logging.error("No ENCRYPTED_PASSPHRASE secret found.")
    return False, "no_passphrase"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_path)
        page = context.new_page()
        page.goto("https://www.tiktok.com/upload", timeout=60000)
        try:
            page.wait_for_selector('input[type="file"]', timeout=30000)
        except PlaywrightTimeout:
            logging.error("Upload input not found.")
            context.close(); browser.close()
            return False, "no_input"
        try:
            page.set_input_files('input[type="file"]', video_path)
        except Exception as e:
            logging.exception("set_input_files error")
            context.close(); browser.close()
            return False, str(e)
        # try caption
        caption_selectors = ['textarea[placeholder*="Describe your video"]', 'div[contenteditable="true"]', 'textarea']
        filled = False
        for sel in caption_selectors:
            try:
                page.wait_for_selector(sel, timeout=8000)
                page.fill(sel, caption_text)
                filled = True
                break
            except:
                continue
        # click post
        clicked = False
        try:
            page.wait_for_timeout(2000)
            buttons = page.query_selector_all('button')
            for b in buttons:
                try:
                    txt = b.inner_text().strip().lower()
                    if 'post' in txt or 'publish' in txt:
                        b.click()
                        clicked = True
                        break
                except:
                    continue
        except:
            pass
        if not clicked:
            logging.error("Post button not found.")
            context.close(); browser.close()
            return False, "no_post_button"
        # wait a bit
        page.wait_for_timeout(8000)
        context.close(); browser.close()
        return True, "ok"

# --- Main run ---
if __name__ == "__main__":
    schedule, caption_map = read_schedule_sheet()
    due, slot = is_time_to_post(schedule)
    if not due:
        logging.info("Not a scheduled slot right now. Exiting.")
        exit(0)
    logging.info(f"Time to post for slot {slot}")
    posted = read_posted_log()
    files = list_videos()
    next_file = None
    for f in files:
        if f['name'] not in posted:
            next_file = f
            break
    if not next_file:
        logging.info("No unposted files found. Exiting.")
        exit(0)
    logging.info("Next file: %s", next_file['name'])
    try:
        local_path = download_file(next_file['id'], next_file['name'])
    except Exception as e:
        logging.exception("Download failed")
        exit(1)
    caption_text = caption_map.get(datetime.now(tz).strftime("%A").lower(), CAPTION)
    success, info = upload_to_tiktok(local_path, caption_text)
    if success:
        logging.info("Posted successfully. Recording in sheet.")
        log_posted(next_file['name'], next_file['id'], slot)
    else:
        logging.error("Posting failed: %s", info)
