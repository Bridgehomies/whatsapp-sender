import customtkinter as ctk
import threading
import subprocess
import time
import json
import re
import os
import hashlib
import csv
import io
import zlib
import uuid
import tempfile
from pathlib import Path
from tkinter import filedialog
from datetime import datetime
import random

try:
    from PIL import Image
except Exception:          # Pillow is optional for previews only
    Image = None

# ── Theme ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

WHATSAPP_GREEN = "#35d07f"
DARK_GREEN     = "#1eaa63"
GREEN_DIM      = "#123b2a"
BG_DARK        = "#09110f"
BG_CARD        = "#10201b"
BG_INPUT       = "#0b1713"
BG_ROW         = "#142820"
TEXT_PRIMARY   = "#f2fbf6"
TEXT_MUTED     = "#8ca99b"
BORDER         = "#214336"
DANGER         = "#ff6b6b"
ACCENT_BLUE    = "#7dd3fc"

COUNTRY_CODES = {
    "Pakistan (+92)": "+92",
    "United States (+1)": "+1",
    "United Kingdom (+44)": "+44",
    "United Arab Emirates (+971)": "+971",
    "Saudi Arabia (+966)": "+966",
    "Qatar (+974)": "+974",
    "Oman (+968)": "+968",
    "Australia (+61)": "+61",
    "New Zealand (+64)": "+64",
    "Bangladesh (+880)": "+880",
    "India (+91)": "+91",
    "Canada (+1)": "+1",
    "South Africa (+27)": "+27",
    "Other / already international": "",
}

SAVE_FILE     = os.path.join(os.path.expanduser("~"), ".wa_sender_mac.json")
SENT_LOG_FILE = os.path.join(os.path.expanduser("~"), ".wa_sender_sent_log.json")
NOT_ON_WHATSAPP_FILE = os.path.join(os.path.expanduser("~"), "not_on_whatsapp.csv")

IMAGE_EXTS    = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")
CAPTION_LIMIT = 1024      # WhatsApp caption length limit


# ── Helpers ────────────────────────────────────────────────────────────────────
def load_saved():
    try:
        with open(SAVE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_data(numbers, message, recipients, msg_mode, mode="app", min_delay=13,
              country_code="+92", lead_area="", max_messages=10, queue_offset=0,
              queue_signature="", image="", default_image=""):
    try:
        with open(SAVE_FILE, "w") as f:
            json.dump({
                "numbers": numbers, "message": message,
                "recipients": recipients, "msg_mode": msg_mode,
                "mode": mode, "min_delay": min_delay,
                "country_code": country_code, "lead_area": lead_area,
                "max_messages": max_messages, "queue_offset": queue_offset,
                "queue_signature": queue_signature,
                "image": image, "default_image": default_image
            }, f)
    except Exception:
        pass


def _image_fingerprint(path: str) -> str:
    """Fingerprint an image by its content, so renaming a file does not matter."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "missing"


def _msg_hash(message: str, image_path: str = "") -> str:
    """Return a stable fingerprint for the actual outbound message (text + image).

    Text-only messages hash exactly as before, so existing sent logs stay valid.
    """
    base = message.strip()
    if image_path:
        base += "\n\x00image:" + _image_fingerprint(image_path)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def load_sent_log() -> dict:
    try:
        with open(SENT_LOG_FILE) as f:
            log = json.load(f)
        # A malformed or older log should never prevent the sender from starting.
        return log if isinstance(log, dict) else {}
    except Exception:
        return {}


def save_sent_log(log: dict):
    try:
        # Write then replace so an interrupted save cannot leave a corrupt history.
        temp_file = SENT_LOG_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(log, f)
        os.replace(temp_file, SENT_LOG_FILE)
    except Exception:
        pass


def was_already_sent(message: str, number: str, log: dict | None = None,
                     image_path: str = "") -> bool:
    if log is None:
        log = load_sent_log()
    h = _msg_hash(message, image_path)
    return number in log.get(h, [])


def record_sent(message: str, number: str, log: dict | None = None,
                image_path: str = ""):
    """Record only confirmed sends; callers may pass their in-memory history."""
    if log is None:
        log = load_sent_log()
    h = _msg_hash(message, image_path)
    sent_list = log.setdefault(h, [])
    if number not in sent_list:
        sent_list.append(number)
    save_sent_log(log)


def is_not_on_whatsapp_result(result: str) -> bool:
    """Recognise WhatsApp's invalid/unregistered-number notices."""
    text = (result or "").lower()
    notices = (
        "not on whatsapp",
        "isn't on whatsapp",
        "is not on whatsapp",
        "phone number shared via url is invalid",
        "invalid phone number",
    )
    return any(notice in text for notice in notices)


def record_not_on_whatsapp(number: str, message: str, image_path: str = ""):
    """Append an unregistered number to a simple, reusable CSV export."""
    new_file = not os.path.exists(NOT_ON_WHATSAPP_FILE)
    try:
        with open(NOT_ON_WHATSAPP_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["Number", "Status", "Detected at", "Message fingerprint"])
            writer.writerow([
                number,
                "Not found on WhatsApp",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                _msg_hash(message, image_path),
            ])
    except Exception:
        pass


# Valid national-number lengths (digits after the country code, no trunk 0).
# Used to tell "923234217100" (already has +92) from "3234217100" (local).
_NATIONAL_LEN = {
    "+92": (9, 10), "+1": (10, 10), "+44": (9, 10), "+971": (8, 9),
    "+966": (8, 9), "+974": (8, 8), "+968": (8, 8), "+61": (9, 9),
    "+64": (8, 10), "+880": (10, 10), "+91": (10, 10), "+27": (9, 9),
}


def clean_number(raw: str, country_code: str = "+92") -> str:
    """Normalise a phone number to +<countrycode><number>.

    Only attaches country_code when the number does not already carry one:
      +923234217100   -> +923234217100   (unchanged)
      923234217100    -> +923234217100   (code present, just add '+')
      00923234217100  -> +923234217100
      +9203234217100  -> +923234217100   (stray trunk 0 removed)
      03234217100     -> +923234217100
      3234217100      -> +923234217100
    """
    raw = (raw or "").strip()
    # Spreadsheet exports store numbers as floats: "923468502307.0" or 9.23e+11
    if re.fullmatch(r"\+?\d+(\.\d+)?[eE][+-]?\d+", raw):
        try:
            raw = str(int(float(raw)))
        except ValueError:
            pass
    raw = re.sub(r"\.0+$", "", raw)
    cleaned = re.sub(r"[^\d+]", "", raw)
    # keep only a leading '+', drop any stray ones
    has_plus = cleaned.startswith("+")
    digits = cleaned.replace("+", "")
    if not digits:
        return ""

    # Explicit international prefix: +... or 00...
    if has_plus or digits.startswith("00"):
        if not has_plus:
            digits = digits[2:]
        # Remove a trunk '0' wrongly left after the country code (+92 0323...)
        if country_code:
            cc = country_code.lstrip("+")
            if digits.startswith(cc + "0"):
                digits = cc + digits[len(cc) + 1:]
        return "+" + digits

    if not country_code:
        return digits

    cc = country_code.lstrip("+")
    lo, hi = _NATIONAL_LEN.get(country_code, (7, 12))

    # Number already begins with the country code (no '+'): 92323...
    if digits.startswith(cc):
        rest = digits[len(cc):].lstrip("0") if digits[len(cc):].startswith("0") else digits[len(cc):]
        if lo <= len(rest) <= hi:
            return "+" + cc + rest

    # Local number: drop trunk '0' and attach the country code
    if digits.startswith("0"):
        digits = digits[1:]
    return country_code + digits


def _split_row(line: str) -> list[str]:
    """Split a CSV, TSV, or markdown row without retaining table padding."""
    if line.lstrip().startswith("|"):
        return [x.strip() for x in line.strip().strip("|").split("|")]
    return next(csv.reader([line]))


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _find_column(headers: list[str], aliases: set[str]) -> str | None:
    normalised = {_normalise_key(h): h for h in headers}
    for alias in aliases:
        if _normalise_key(alias) in normalised:
            return normalised[_normalise_key(alias)]
    for h in headers:
        key = _normalise_key(h)
        if any(_normalise_key(alias) in key for alias in aliases):
            return h
    return None


def detect_lead_data(raw: str, filename: str = "") -> tuple[list[dict], dict]:
    """Detect common lead fields from CSV, TSV, JSON, markdown, or pasted exports."""
    text = raw.replace("\r\n", "\n").strip()
    rows: list[dict] = []
    headers: list[str] = []

    # JSON exports: either a list of records or {"leads": [...]}
    if Path(filename).suffix.lower() == ".json" or text.startswith(("[", "{")):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                payload = payload.get("leads") or payload.get("data") or []
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                headers = list(payload[0].keys())
                rows = [{str(k): "" if v is None else str(v) for k, v in item.items()}
                        for item in payload]
        except Exception:
            pass

    # Markdown table: locate a header followed by a separator row.
    if not rows:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for idx in range(len(lines) - 1):
            if lines[idx].startswith("|") and re.match(r"^\|?\s*:?-{2,}", lines[idx + 1]):
                headers = _split_row(lines[idx])
                for line in lines[idx + 2:]:
                    if line.startswith("|"):
                        values = _split_row(line)
                        if len(values) == len(headers):
                            rows.append(dict(zip(headers, values)))
                break

    # CSV/TSV and other delimited text.
    if (not rows and not headers and
            not re.search(r"Row\s+\d+:\s*Business=", text, re.IGNORECASE)):
        try:
            sample = text[:4096]
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            headers = reader.fieldnames or []
            rows = [{str(k): (v or "") for k, v in row.items() if k is not None} for row in reader]
        except Exception:
            pass

    # Markdown/code exports like: Row 1: Business='City Cafe', ... Phone='...'
    if not rows:
        pattern = re.compile(
            r"Row\s+\d+:\s*Business=['\"](.*?)['\"],\s*"
            r"Category=['\"](.*?)['\"],\s*City=['\"](.*?)['\"],\s*"
            r"Phone=['\"](.*?)['\"]",
            re.IGNORECASE)
        for match in pattern.finditer(text):
            rows.append({"Business Name": match.group(1), "Type/Category": match.group(2),
                         "City": match.group(3), "Phone": match.group(4)})
        if rows:
            headers = list(rows[0].keys())

    # Generic label-style text fallback: Phone: ..., Business: ...
    if not rows:
        for block in re.split(r"\n\s*\n", text):
            phone = re.search(r"(?:phone|mobile|whatsapp)\s*[:|]\s*([^\n]+)", block, re.I)
            if phone:
                name = re.search(r"(?:business|company|name)\s*[:|]\s*([^\n]+)", block, re.I)
                rows.append({"Business Name": name.group(1).strip() if name else "",
                             "Phone": phone.group(1).strip()})
        headers = list(rows[0].keys()) if rows else []

    phone_col = _find_column(headers, {"phone", "phone number", "mobile", "mobile number",
                                        "whatsapp", "whatsapp number", "contact"})
    name_col = _find_column(headers, {"name", "business", "business name", "company",
                                      "company name", "lead name"})
    message_col = _find_column(headers, {"message", "whatsapp message", "outreach message",
                                         "custom message", "text"})
    area_col = _find_column(headers, {"city", "area", "location", "state", "address"})
    country_col = _find_column(headers, {"country", "country name", "nation"})
    image_col = _find_column(headers, {"image", "image path", "image file", "photo",
                                       "picture", "attachment"})
    detected = {"phone": phone_col, "name": name_col, "message": message_col, "area": area_col,
                "country": country_col, "image": image_col,
                "rows": len(rows), "columns": headers}
    return rows, detected


def set_clipboard(text: str) -> bool:
    try:
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                               capture_output=True, timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


# ── Outbound preparation (text or image + caption) ─────────────────────────────
TEXT_SEND_BLOCK = '''
keystroke "v" using command down
delay 0.8
key code 36
delay 0.5
'''


def _as_str(value: str) -> str:
    """Escape a value for use inside an AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def prepare_outbound(message: str, image_path: str = "") -> tuple[str, list[str], str]:
    """Build the paste-and-send AppleScript block for one message.

    Returns (applescript_block, temp_files_to_delete, error). ``error`` is empty
    on success.

    * Text only  -> the message goes to the clipboard, block pastes + Enter.
    * Image      -> the image is pasted first (WhatsApp opens its attachment
      preview with the caption box focused), then the message text is pasted
      into that caption box and Enter sends both as ONE message.
    """
    if not image_path:
        if not set_clipboard(message):
            return "", [], "Could not set clipboard"
        return TEXT_SEND_BLOCK, [], ""

    if not os.path.isfile(image_path):
        return "", [], f"Image not found: {image_path}"

    temps: list[str] = []
    ext = Path(image_path).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        source, clip_class = image_path, "JPEG picture"
    elif ext == ".png":
        source, clip_class = image_path, "\u00abclass PNGf\u00bb"
    else:
        # gif / webp / heic etc: convert to a static PNG with macOS's built-in sips
        source = os.path.join(tempfile.gettempdir(), f"wa_img_{uuid.uuid4().hex}.png")
        temps.append(source)
        try:
            conv = subprocess.run(
                ["sips", "-s", "format", "png", image_path, "--out", source],
                capture_output=True, text=True, timeout=20)
            if conv.returncode != 0 or not os.path.isfile(source):
                return "", temps, "Could not convert image to PNG"
        except Exception as exc:
            return "", temps, f"Could not convert image: {exc}"
        clip_class = "\u00abclass PNGf\u00bb"

    block = f'''
tell current application to set the clipboard to (read (POSIX file "{_as_str(source)}") as {clip_class})
delay 0.5
keystroke "v" using command down
delay 2.5
'''
    caption = message.strip()
    if caption:
        caption_file = os.path.join(tempfile.gettempdir(), f"wa_caption_{uuid.uuid4().hex}.txt")
        temps.append(caption_file)
        try:
            with open(caption_file, "w", encoding="utf-8") as f:
                f.write(caption)
        except Exception as exc:
            return "", temps, f"Could not write caption: {exc}"
        block += f'''
tell current application to set the clipboard to (read (POSIX file "{_as_str(caption_file)}") as \u00abclass utf8\u00bb)
delay 0.4
keystroke "v" using command down
delay 0.8
'''
    block += '''
key code 36
delay 1.5
'''
    return block, temps, ""


def _cleanup(paths: list[str]):
    for path in paths:
        try:
            os.remove(path)
        except Exception:
            pass


# ── Send via WhatsApp Desktop App ──────────────────────────────────────────────
def send_via_whatsapp_desktop(number: str, message: str, image_path: str = "") -> tuple[bool, str]:
    send_block, temps, err = prepare_outbound(message, image_path)
    if err:
        _cleanup(temps)
        return False, err

    try:
        clean_num = re.sub(r"\D", "", number)
        url = f"whatsapp://send?phone={clean_num}"
        open_result = subprocess.run(["open", url], capture_output=True, timeout=10)
        if open_result.returncode != 0:
            return False, "Could not open WhatsApp URL scheme"

        script = f'''
delay 3.5

tell application "WhatsApp"
    activate
end tell

delay 2.0

tell application "System Events"
    tell process "WhatsApp"
        set uiText to (name of every static text) as text
        if uiText contains "not on WhatsApp" or uiText contains "isn't on WhatsApp" or uiText contains "Phone number shared via url is invalid" then
            return "NOT_ON_WHATSAPP"
        end if
{send_block}
    end tell
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=45 if image_path else 20
            )
            if result.returncode == 0:
                if "NOT_ON_WHATSAPP" in result.stdout:
                    return False, "Not on WhatsApp"
                return True, "Sent"
            else:
                err = result.stderr.strip()
                return False, err or "AppleScript error"
        except subprocess.TimeoutExpired:
            return False, "Timed out"
        except Exception as e:
            return False, str(e)
    finally:
        _cleanup(temps)


# ── Send via Google Chrome (WhatsApp Web) ──────────────────────────────────────
def _chrome_wa_tab_exists() -> bool:
    """Return True if Chrome already has a WhatsApp Web tab open."""
    check_script = '''
tell application "Google Chrome"
    if (count of windows) is 0 then return "none"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t starts with "https://web.whatsapp.com" then return "found"
        end repeat
    end repeat
    return "none"
end tell
'''
    try:
        r = subprocess.run(["osascript", "-e", check_script],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() == "found"
    except Exception:
        return False


def send_via_chrome(number: str, message: str, image_path: str = "") -> tuple[bool, str]:
    """
    Navigate WhatsApp Web to the target contact using JS history navigation
    (window.location.assign) so the SPA re-renders the chat pane WITHOUT a
    full page reload.  A full reload only happens on the very first call when
    no WA Web tab exists yet.
    """
    send_block, temps, err = prepare_outbound(message, image_path)
    if err:
        _cleanup(temps)
        return False, err

    clean_num = re.sub(r"\D", "", number)
    wa_url    = f"https://web.whatsapp.com/send?phone={clean_num}"

    scpt_path = "/tmp/wa_chrome_send.applescript"

    # ── JS injected into the existing WA Web tab ──────────────────────────────
    # 1. navigate() uses history.pushState-style SPA navigation (no reload).
    # 2. poll() waits for the message-input box to appear, then focuses it.
    # 3. On success the Python side pastes & sends via AppleScript keystrokes.
    js_navigate_and_focus = f"""
(function() {{
    var targetUrl = '{wa_url}';

    // SPA navigation — no page reload
    window.location.assign(targetUrl);

    var attempts = 0;
    var maxAttempts = 40;   // up to 20 s

    function tryFocus() {{
        var selectors = [
            'div[contenteditable="true"][data-tab="10"]',
            'footer div[contenteditable="true"]',
            'div[contenteditable="true"][data-tab="1"]',
            'div[contenteditable="true"]'
        ];
        for (var s of selectors) {{
            var el = document.querySelector(s);
            if (el) {{
                el.focus();
                el.click();
                var range = document.createRange();
                var sel   = window.getSelection();
                range.selectNodeContents(el);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
                return true;
            }}
        }}
        return false;
    }}

    function poll() {{
        if (tryFocus()) return;
        attempts++;
        if (attempts < maxAttempts) setTimeout(poll, 500);
    }}

    // Give the SPA router ~400 ms to start rendering before polling
    setTimeout(poll, 400);
}})();
"""

    wa_tab_open = _chrome_wa_tab_exists()
    not_on_whatsapp_check = """(function() {
        var text = (document.body && document.body.innerText || '').toLowerCase();
        var notices = [
            'phone number shared via url is invalid',
            'not on whatsapp',
            \"isn't on whatsapp\",
            'invalid phone number'
        ];
        return notices.some(function(notice) { return text.indexOf(notice) !== -1; })
            ? 'NOT_ON_WHATSAPP' : 'READY';
    })();""".replace(chr(10), ' ').replace('"', '\\"')

    if wa_tab_open:
        # ── Happy path: WA Web is already loaded — inject JS, no tab change ──
        scpt_code = f'''
tell application "Google Chrome"
    activate
    -- Find the WhatsApp Web tab and make it front
    set waTab to missing value
    set waWin to missing value
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t starts with "https://web.whatsapp.com" then
                set waTab to t
                set waWin to w
                exit repeat
            end if
        end repeat
        if waTab is not missing value then exit repeat
    end repeat

    if waTab is missing value then
        -- Fallback: open fresh (should not happen after the check above)
        set URL of active tab of front window to "{wa_url}"
        set waTab to active tab of front window
        delay 7
        tell active tab of front window
            execute javascript "{js_navigate_and_focus.replace(chr(10), ' ').replace('"', '\\"')}"
        end tell
    else
        set index of waWin to 1
        set active tab index of waWin to tab index of waTab
        -- Inject navigation + focus JS into the existing tab (no reload)
        tell waTab
            execute javascript "{js_navigate_and_focus.replace(chr(10), ' ').replace('"', '\\"')}"
        end tell
    end if
end tell

-- Wait for SPA to render the chat input (~3 s is enough for navigate)
delay 3.5

tell application "Google Chrome"
    tell waTab
        set pageState to execute javascript "{not_on_whatsapp_check}"
    end tell
end tell
if pageState is "NOT_ON_WHATSAPP" then return "NOT_ON_WHATSAPP"

tell application "System Events"
    tell process "Google Chrome"
{send_block}
    end tell
end tell

return "SENT"
'''
    else:
        # ── Cold start: no WA Web tab open yet — load it once ─────────────────
        scpt_code = f'''
tell application "Google Chrome"
    activate
    if (count of windows) is 0 then make new window
    set URL of active tab of front window to "{wa_url}"
    delay 8
    set pageState to execute javascript "{not_on_whatsapp_check}"
end tell

if pageState is "NOT_ON_WHATSAPP" then return "NOT_ON_WHATSAPP"

tell application "System Events"
    tell process "Google Chrome"
{send_block}
    end tell
end tell

return "SENT"
'''

    try:
        with open(scpt_path, "w") as f:
            f.write(scpt_code)

        result = subprocess.run(
            ["osascript", scpt_path],
            capture_output=True, text=True, timeout=80 if image_path else 60
        )

        if result.returncode == 0:
            if "NOT_ON_WHATSAPP" in result.stdout:
                return False, "Not on WhatsApp"
            return True, "Sent"

        err = result.stderr.strip()

        recoverable = [
            "timed out",
            "Connection refused",
            "chrome not running",
            "-600",
            "-1712",
        ]
        for pat in recoverable:
            if pat.lower() in err.lower():
                return False, f"Recoverable: {err}"

        return False, err or "Unknown AppleScript error"

    except subprocess.TimeoutExpired:
        return False, "Recoverable: process timed out"
    except Exception as e:
        return False, str(e)
    finally:
        _cleanup([scpt_path] + temps)


# ── System checks ──────────────────────────────────────────────────────────────
def check_whatsapp_running() -> bool:
    result = subprocess.run(["pgrep", "-x", "WhatsApp"], capture_output=True, text=True)
    return result.returncode == 0


def check_chrome_installed() -> bool:
    result = subprocess.run(
        ["osascript", "-e", 'return POSIX path of (path to application "Google Chrome")'],
        capture_output=True, text=True
    )
    return result.returncode == 0


def check_accessibility_permission() -> bool:
    script = '''
tell application "System Events"
    return name of first process whose frontmost is true
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.returncode == 0


# ── Image picker widget ────────────────────────────────────────────────────────
class ImagePicker(ctk.CTkFrame):
    """One 'attach an image' row: thumbnail, choose button, file name, remove button."""

    def __init__(self, parent, label="🖼  Add image", empty_text="No image — text only",
                 on_change=None):
        super().__init__(parent, fg_color="transparent")
        self.path = ""
        self.on_change = on_change
        self.empty_text = empty_text
        self._thumb = None
        self.thumb_lbl = None
        self.grid_columnconfigure(2, weight=1)

        self.pick_btn = ctk.CTkButton(
            self, text=label, width=170, height=36, corner_radius=9,
            fg_color=BG_INPUT, hover_color=BORDER, border_width=1, border_color=BORDER,
            text_color=TEXT_PRIMARY, font=("SF Pro Text", 12), command=self.choose)
        self.pick_btn.grid(row=0, column=1, padx=(0, 10))

        self.name_lbl = ctk.CTkLabel(self, text=empty_text, anchor="w",
                                     font=("SF Pro Text", 11), text_color=TEXT_MUTED)
        self.name_lbl.grid(row=0, column=2, sticky="ew")

        self.remove_btn = ctk.CTkButton(
            self, text="✕", width=28, height=26, corner_radius=6,
            fg_color="transparent", hover_color="#3a1a1a", text_color=DANGER,
            font=("SF Pro Text", 11), command=self.clear)
        self.remove_btn.grid(row=0, column=3, padx=(6, 0))
        self._render()

    def choose(self):
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Images", " ".join(f"*{e}" for e in IMAGE_EXTS)), ("All files", "*.*")])
        if path:
            self.set(path)

    def clear(self):
        self.set("")

    def set(self, path: str, notify: bool = True):
        self.path = path or ""
        self._render()
        if notify and self.on_change:
            self.on_change()

    def _render(self):
        # Rebuild the thumbnail label each time — avoids Tk "image doesn't exist" errors.
        if self.thumb_lbl is not None:
            self.thumb_lbl.destroy()
        img = None
        exists = bool(self.path) and os.path.isfile(self.path)
        if exists and Image is not None:
            try:
                im = Image.open(self.path)
                im.thumbnail((120, 120))
                im = im.convert("RGBA")
                scale = 46 / max(im.size)
                size = (max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale)))
                img = ctk.CTkImage(light_image=im, dark_image=im, size=size)
            except Exception:
                img = None
        self._thumb = img
        self.thumb_lbl = ctk.CTkLabel(
            self, text="" if img else "🖼", image=img, width=46, height=46,
            fg_color=BG_INPUT, corner_radius=8, text_color=TEXT_MUTED,
            font=("SF Pro Text", 18))
        self.thumb_lbl.grid(row=0, column=0, padx=(0, 10))

        if not self.path:
            self.name_lbl.configure(text=self.empty_text, text_color=TEXT_MUTED)
            self.remove_btn.grid_remove()
        elif not exists:
            self.name_lbl.configure(text=f"⚠  File not found — {Path(self.path).name}",
                                    text_color=DANGER)
            self.remove_btn.grid()
        else:
            kb = max(1, os.path.getsize(self.path) // 1024)
            self.name_lbl.configure(text=f"{Path(self.path).name}  ·  {kb} KB",
                                    text_color=WHATSAPP_GREEN)
            self.remove_btn.grid()


# ── App ────────────────────────────────────────────────────────────────────────
class WASenderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp Bulk Sender  •  Mac")
        self.geometry("960x980")
        self.minsize(820, 760)
        self.configure(fg_color=BG_DARK)

        self.numbers: list[str] = []       # "same message" mode
        self.recipients: list[dict] = []   # "custom message" mode: [{"number","message"}]
        self.msg_mode = ctk.StringVar(value="same")   # "same" or "custom"
        self.editing_idx = None   # index into self.recipients currently being edited, or None
        self.sending = False
        self.stop_flag = False
        self.sent_log: dict = load_sent_log()
        self.send_mode = ctk.StringVar(value="app")   # "app" or "chrome"
        self.country_var = ctk.StringVar(value="Pakistan (+92)")
        self.lead_area_var = ctk.StringVar(value="")
        self.max_messages_var = ctk.IntVar(value=10)
        self.batch_locked = False
        self.queue_offset = 0
        self.queue_signature = ""

        self._build_ui()

        saved = load_saved()
        if saved.get("numbers"):
            self.numbers = saved["numbers"]
        if saved.get("recipients"):
            self.recipients = saved["recipients"]
        if saved.get("msg_mode") in ("same", "custom"):
            self.msg_mode.set(saved["msg_mode"])
        if saved.get("message"):
            self.msg_box.insert("0.0", saved["message"])
        if saved.get("image"):
            self.same_picker.set(saved["image"], notify=False)
        if saved.get("default_image"):
            self.cdefault_picker.set(saved["default_image"], notify=False)
        if saved.get("mode") in ("app", "chrome"):
            self.send_mode.set(saved["mode"])
            self._on_mode_change()
        if saved.get("country_code"):
            for label, code in COUNTRY_CODES.items():
                if code == saved["country_code"]:
                    self.country_var.set(label)
                    break
        if saved.get("lead_area"):
            self.lead_area_var.set(saved["lead_area"])
        if saved.get("min_delay"):
            self.delay_var.set(max(13, int(saved["min_delay"])))
            self.delay_lbl.configure(text=f"{self.delay_var.get()}s")
        if saved.get("max_messages"):
            self.max_messages_var.set(max(1, int(saved["max_messages"])))
            self.max_messages_entry.delete(0, "end")
            self.max_messages_entry.insert(0, str(self.max_messages_var.get()))
        if saved.get("queue_offset"):
            self.queue_offset = max(0, int(saved["queue_offset"]))
        self.queue_signature = saved.get("queue_signature") or self._current_queue_signature()
        self._set_msg_mode(self.msg_mode.get())

        self.update_send_btn()
        self.after(500, self._run_startup_checks)

    # ── Startup checks ──────────────────────────────────────────────────────────
    def _run_startup_checks(self):
        mode = self.send_mode.get()

        if mode == "app":
            if not check_whatsapp_running():
                self._log("⚠  WhatsApp Desktop is not running. Please open it first.", "#f39c12")
            else:
                self._log("✓  WhatsApp Desktop detected.", WHATSAPP_GREEN)
        else:
            if not check_chrome_installed():
                self._log("⚠  Google Chrome not found. Please install it.", "#e74c3c")
            else:
                self._log("✓  Google Chrome detected.", WHATSAPP_GREEN)
            self._log("ℹ  Chrome mode: make sure you are logged in to WhatsApp Web (web.whatsapp.com).", TEXT_MUTED)

        if not check_accessibility_permission():
            self._log("⚠  Accessibility permission may be missing. See instructions below.", "#f39c12")
            self._show_permission_banner()
        else:
            self._log("✓  Accessibility permission OK.", WHATSAPP_GREEN)

    def _show_permission_banner(self):
        banner = ctk.CTkFrame(self.outer, fg_color="#2d1f00", corner_radius=12,
                               border_width=1, border_color="#f39c12")
        banner.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 0))
        banner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(banner,
            text="⚠  Accessibility Permission Required",
            font=("SF Pro Display", 13, "bold"), text_color="#f39c12"
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 2))
        ctk.CTkLabel(banner,
            text="Go to:  System Settings → Privacy & Security → Accessibility\n"
                 "→ Add your Terminal app (or Python) and enable it.\n"
                 "Then restart this app.",
            font=("SF Pro Text", 12), text_color="#d4a843",
            justify="left"
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

    # ── UI ──────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.outer = ctk.CTkScrollableFrame(self, fg_color=BG_DARK, corner_radius=0)
        self.outer.grid(row=0, column=0, sticky="nsew")
        self.outer.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(self.outer, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=34, pady=(30, 12))
        hdr.grid_columnconfigure(1, weight=1)

        logo = ctk.CTkFrame(hdr, width=54, height=54, corner_radius=18, fg_color=WHATSAPP_GREEN)
        logo.grid(row=0, column=0, padx=(0, 14))
        logo.grid_propagate(False)
        ctk.CTkLabel(logo, text="↗", font=("SF Pro Display", 26, "bold"),
                     text_color="#062014").place(relx=.5, rely=.5, anchor="center")

        tf = ctk.CTkFrame(hdr, fg_color="transparent")
        tf.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(tf, text="Reachflow",
                     font=("SF Pro Display", 25, "bold"),
                     text_color=TEXT_PRIMARY).pack(anchor="w")
        ctk.CTkLabel(tf, text="A focused workspace for sending thoughtful WhatsApp outreach",
                     font=("SF Pro Text", 12),
                     text_color=TEXT_MUTED).pack(anchor="w")

        ctk.CTkLabel(hdr, text="MAC WORKSPACE  •  READY",
                     font=("SF Pro Text", 10, "bold"), text_color=WHATSAPP_GREEN).grid(
                         row=0, column=2, sticky="ne", padx=(12, 0), pady=4)

        self._card(self.outer, row=1, title="01  •  Choose your channel", builder=self._mode_section)
        self._card(self.outer, row=2, title="02  •  Build your lead list", builder=self._numbers_section)
        self._card(self.outer, row=3, title="03  •  Import leads automatically", builder=self._import_section)
        self._card(self.outer, row=4, title="04  •  Sending guardrails", builder=self._settings_section)
        self._action_section(self.outer, row=5)
        self._log_section(self.outer, row=6)

    def _card(self, parent, row, title, builder):
        f = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16,
                          border_width=1, border_color=BORDER)
        f.grid(row=row, column=0, sticky="ew", padx=28, pady=10)
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text=title, font=("SF Pro Display", 13, "bold"),
                     text_color=TEXT_PRIMARY).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 6))
        ctk.CTkFrame(f, height=1, fg_color=BORDER, corner_radius=0).grid(row=1, column=0, sticky="ew", padx=20)
        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 16))
        inner.grid_columnconfigure(0, weight=1)
        builder(inner)

    # ── Mode section ────────────────────────────────────────────────────────────
    def _mode_section(self, p):
        p.grid_columnconfigure(0, weight=1)
        p.grid_columnconfigure(1, weight=1)

        self.btn_app = ctk.CTkButton(
            p,
            text="📲  WhatsApp Desktop",
            height=56,
            corner_radius=12,
            fg_color=WHATSAPP_GREEN,
            hover_color=DARK_GREEN,
            text_color="#fff",
            font=("SF Pro Display", 13, "bold"),
            command=lambda: self._set_mode("app")
        )
        self.btn_app.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.btn_chrome = ctk.CTkButton(
            p,
            text="🌐  Google Chrome",
            height=56,
            corner_radius=12,
            fg_color=BG_INPUT,
            hover_color=BORDER,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=("SF Pro Display", 13, "bold"),
            command=lambda: self._set_mode("chrome")
        )
        self.btn_chrome.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.mode_info = ctk.CTkLabel(
            p,
            text="Using WhatsApp Desktop app via URL scheme + AppleScript.",
            font=("SF Pro Text", 11),
            text_color=TEXT_MUTED,
            wraplength=560,
            justify="left"
        )
        self.mode_info.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _set_mode(self, mode: str):
        self.send_mode.set(mode)
        self._on_mode_change()
        self._save()

    def _on_mode_change(self):
        mode = self.send_mode.get()
        if mode == "app":
            self.btn_app.configure(
                fg_color=WHATSAPP_GREEN,
                hover_color=DARK_GREEN,
                text_color="#fff",
                border_width=0
            )
            self.btn_chrome.configure(
                fg_color=BG_INPUT,
                hover_color=BORDER,
                text_color=TEXT_MUTED,
                border_width=1,
                border_color=BORDER
            )
            self.mode_info.configure(
                text="📲  Using WhatsApp Desktop app via whatsapp:// URL scheme + AppleScript.\n"
                     "     WhatsApp must be open and logged in."
            )
        else:
            self.btn_chrome.configure(
                fg_color=WHATSAPP_GREEN,
                hover_color=DARK_GREEN,
                text_color="#fff",
                border_width=0
            )
            self.btn_app.configure(
                fg_color=BG_INPUT,
                hover_color=BORDER,
                text_color=TEXT_MUTED,
                border_width=1,
                border_color=BORDER
            )
            self.mode_info.configure(
                text="🌐  Using Google Chrome with WhatsApp Web (web.whatsapp.com).\n"
                     "     Chrome must be open and you must be logged in to WhatsApp Web."
            )

    # ── Lead import ────────────────────────────────────────────────────────────
    def _import_section(self, p):
        p.grid_columnconfigure(0, weight=1)
        p.grid_columnconfigure(1, weight=0)

        intro = ctk.CTkFrame(p, fg_color="transparent")
        intro.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(intro, text="Drop in a CSV, TSV, JSON, Markdown export, or paste the text below.",
                     font=("SF Pro Text", 12), text_color=TEXT_PRIMARY).pack(anchor="w")
        ctk.CTkLabel(intro, text="Reachflow looks for phone, name/company, message, and location columns automatically.",
                     font=("SF Pro Text", 11), text_color=TEXT_MUTED).pack(anchor="w", pady=(3, 0))

        self.import_box = ctk.CTkTextbox(
            p, height=92, fg_color=BG_INPUT, border_color=BORDER,
            text_color=TEXT_PRIMARY, corner_radius=10, font=("Menlo", 11), border_width=1)
        self.import_box.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        self.import_box.insert("0.0", "Paste your lead file content here, or use Upload file →")
        self.import_box.bind("<FocusIn>", lambda e: self._clear_import_hint())

        actions = ctk.CTkFrame(p, fg_color="transparent")
        actions.grid(row=1, column=1, sticky="ns", pady=(4, 0))
        ctk.CTkButton(actions, text="Upload file", width=118, height=42,
                      fg_color=BG_INPUT, hover_color=BORDER, border_width=1,
                      border_color=BORDER, text_color=TEXT_PRIMARY, corner_radius=10,
                      command=self._upload_leads).pack(fill="x", pady=(0, 8))
        ctk.CTkButton(actions, text="Detect & import", width=118, height=42,
                      fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
                      text_color="#062014", corner_radius=10,
                      font=("SF Pro Text", 12, "bold"), command=self._detect_and_import).pack(fill="x")

        self.import_status = ctk.CTkLabel(p, text="Waiting for a lead file", anchor="w",
                                          font=("SF Pro Text", 11), text_color=TEXT_MUTED)
        self.import_status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _resolve_image(self, raw: str) -> str:
        """Turn an image cell into an absolute path (relative to the lead file if needed)."""
        raw = raw.strip().strip("'\"")
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base = getattr(self, "import_filename", "")
            if base:
                path = Path(base).parent / path
        return str(path)

    def _clear_import_hint(self):
        if self.import_box.get("0.0", "end").strip().startswith("Paste your lead file"):
            self.import_box.delete("0.0", "end")

    def _upload_leads(self):
        path = filedialog.askopenfilename(
            title="Choose a lead file",
            filetypes=[("Lead files", "*.csv *.tsv *.txt *.md *.markdown *.json"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
            self.import_box.delete("0.0", "end")
            self.import_box.insert("0.0", content)
            self.import_filename = path
            self.import_status.configure(text=f"Loaded {Path(path).name} — click Detect & import.",
                                         text_color=ACCENT_BLUE)
        except Exception as exc:
            self.import_status.configure(text=f"Could not read file: {exc}", text_color=DANGER)

    def _detect_and_import(self):
        raw = self.import_box.get("0.0", "end").strip()
        if not raw or raw.startswith("Paste your lead file"):
            self.import_status.configure(text="Paste or upload a lead file first.", text_color=DANGER)
            return
        rows, detected = detect_lead_data(raw, getattr(self, "import_filename", ""))
        phone_col = detected.get("phone")
        if not phone_col:
            self.import_status.configure(text="I could not detect a phone column. Try a column named Phone or WhatsApp.",
                                          text_color=DANGER)
            return

        # If the file has an explicit country column, use it before normalizing
        # local phone numbers. Otherwise the country selected in Lead Profile wins.
        detected_country_value = ""
        if detected.get("country"):
            for row in rows:
                value = str(row.get(detected["country"], "")).strip()
                if value:
                    detected_country_value = value
                    for label in COUNTRY_CODES:
                        if value.lower() in label.lower().split(" (")[0]:
                            self.country_var.set(label)
                            break
                    break
        code = self._selected_country_code()
        imported = skipped = custom_count = missing_images = 0
        detected_area_value = ""
        for row in rows:
            if detected.get("area") and not detected_area_value:
                detected_area_value = str(row.get(detected["area"], "")).strip()
            if detected.get("country") and not detected_country_value:
                detected_country_value = str(row.get(detected["country"], "")).strip()
            raw_phone = str(row.get(phone_col, "")).strip()
            if not raw_phone or not re.search(r"\d", raw_phone):
                skipped += 1
                continue
            number = clean_number(raw_phone, code)
            if len(re.sub(r"\D", "", number)) < 7:
                skipped += 1
                continue
            message = ""
            if detected.get("message"):
                message = str(row.get(detected["message"], "")).strip()
            image = ""
            if detected.get("image"):
                image = self._resolve_image(str(row.get(detected["image"], "")).strip())
            if message or image:
                if not any(r.get("number") == number for r in self.recipients):
                    rec = {"number": number, "message": message}
                    if image:
                        rec["image"] = image
                        if not os.path.isfile(image):
                            missing_images += 1
                    self.recipients.append(rec)
                    custom_count += 1
                    imported += 1
            elif number not in self.numbers:
                self.numbers.append(number)
                imported += 1

        if detected_area_value and not self.lead_area_var.get().strip():
            self.lead_area_var.set(detected_area_value)
        if custom_count and self.msg_mode.get() != "custom":
            self._set_msg_mode("custom")
        self._refresh_list_same()
        self._refresh_list_custom()
        self._save()
        self.update_send_btn()
        area = detected.get("area") or "not detected"
        country_note = detected_country_value or self.country_var.get().split(" (")[0]
        self.import_status.configure(
            text=f"Detected {len(rows)} rows • phone: {phone_col} • area: {area} • country: {country_note} • imported: {imported} • skipped: {skipped}",
            text_color=WHATSAPP_GREEN if imported else DANGER)
        self._log(f"Imported {imported} lead(s) from detected {phone_col} column; skipped {skipped}.",
                  WHATSAPP_GREEN if imported else DANGER)
        if detected.get("image"):
            self._log(f"🖼  Image column detected: {detected['image']}.", ACCENT_BLUE)
        if missing_images:
            self._log(f"⚠  {missing_images} image file(s) from the import were not found on disk. "
                      f"Sending is blocked until they are fixed.", "#f39c12")

    # ── Recipients section ───────────────────────────────────────────────────
    def _numbers_section(self, p):
        p.grid_columnconfigure(0, weight=1)

        profile = ctk.CTkFrame(p, fg_color=GREEN_DIM, corner_radius=14,
                               border_width=1, border_color="#255b43")
        profile.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        profile.grid_columnconfigure(0, weight=1)
        profile.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(profile, text="LEAD PROFILE",
                     font=("SF Pro Text", 10, "bold"), text_color=WHATSAPP_GREEN
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(profile,
                     text="Tell us where these leads are from. Local numbers will use this country code.",
                     font=("SF Pro Text", 11), text_color=TEXT_MUTED
                     ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 10))

        self.lead_area_entry = ctk.CTkEntry(
            profile, textvariable=self.lead_area_var,
            placeholder_text="Area / city / market (e.g. Lahore, GCC clinics)",
            fg_color=BG_INPUT, border_color="#285b45", text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED, height=40, corner_radius=9,
            font=("SF Pro Text", 12))
        self.lead_area_entry.grid(row=2, column=0, sticky="ew", padx=(14, 8), pady=(0, 14))

        self.country_menu = ctk.CTkOptionMenu(
            profile, variable=self.country_var, values=list(COUNTRY_CODES.keys()),
            height=40, corner_radius=9, fg_color=BG_INPUT,
            button_color="#285b45", button_hover_color=WHATSAPP_GREEN,
            dropdown_fg_color=BG_CARD, dropdown_hover_color=GREEN_DIM,
            text_color=TEXT_PRIMARY, font=("SF Pro Text", 12),
            command=lambda _: self._country_changed())
        self.country_menu.grid(row=2, column=1, sticky="ew", padx=(8, 14), pady=(0, 14))

        toggle_wrap = ctk.CTkFrame(p, fg_color=BG_INPUT, corner_radius=12)
        toggle_wrap.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        toggle_wrap.grid_columnconfigure(0, weight=1)
        toggle_wrap.grid_columnconfigure(1, weight=1)

        self.btn_same = ctk.CTkButton(
            toggle_wrap, text="Same message for all", height=38, corner_radius=9,
            font=("SF Pro Text", 12, "bold"), border_width=0,
            command=lambda: self._set_msg_mode("same"))
        self.btn_same.grid(row=0, column=0, sticky="ew", padx=3, pady=3)

        self.btn_custom = ctk.CTkButton(
            toggle_wrap, text="Custom message per number", height=38, corner_radius=9,
            font=("SF Pro Text", 12, "bold"), border_width=0,
            command=lambda: self._set_msg_mode("custom"))
        self.btn_custom.grid(row=0, column=1, sticky="ew", padx=3, pady=3)

        # ── Image attachment strips (right under the toggle, so they are always visible)
        self.attach_same = ctk.CTkFrame(p, fg_color=GREEN_DIM, corner_radius=12,
                                        border_width=1, border_color="#255b43")
        self.attach_same.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.attach_same.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.attach_same, text="🖼  IMAGE  ·  optional",
                     font=("SF Pro Text", 10, "bold"), text_color=WHATSAPP_GREEN
                     ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        ctk.CTkLabel(self.attach_same,
                     text="Sent together with your message — the text becomes the image caption.",
                     font=("SF Pro Text", 11), text_color=TEXT_MUTED
                     ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 6))
        self.same_picker = ImagePicker(self.attach_same, label="🖼  Add image",
                                       on_change=self._on_msg_key)
        self.same_picker.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))

        self.attach_custom = ctk.CTkFrame(p, fg_color=GREEN_DIM, corner_radius=12,
                                          border_width=1, border_color="#255b43")
        self.attach_custom.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.attach_custom.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.attach_custom, text="🖼  DEFAULT IMAGE  ·  optional",
                     font=("SF Pro Text", 10, "bold"), text_color=WHATSAPP_GREEN
                     ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        ctk.CTkLabel(self.attach_custom,
                     text="Used for every recipient without their own image. "
                          "You can also give each number its own image below.",
                     font=("SF Pro Text", 11), text_color=TEXT_MUTED, wraplength=560, justify="left"
                     ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 6))
        self.cdefault_picker = ImagePicker(
            self.attach_custom, label="🖼  Set default image",
            empty_text="No default — text only unless a recipient has its own image",
            on_change=self._on_default_image_change)
        self.cdefault_picker.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))

        # ── "Same message" frame: numbers list + one shared message box ──────
        self.same_frame = ctk.CTkFrame(p, fg_color="transparent")
        self.same_frame.grid(row=3, column=0, sticky="ew")
        self.same_frame.grid_columnconfigure(0, weight=1)
        sf = self.same_frame

        row0 = ctk.CTkFrame(sf, fg_color="transparent")
        row0.grid(row=0, column=0, sticky="ew")
        row0.grid_columnconfigure(0, weight=1)

        self.num_entry = ctk.CTkEntry(
            row0, placeholder_text="+92300 123 4567  or  0300-123-4567",
            fg_color=BG_INPUT, border_color=BORDER, text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED, height=42, corner_radius=10,
            font=("Menlo", 13))
        self.num_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.num_entry.bind("<Return>", lambda e: self._add_number_same())

        ctk.CTkButton(row0, text="Add", width=72, height=42,
                      fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
                      text_color="#fff", corner_radius=10,
                      font=("SF Pro Text", 13, "bold"),
                      command=self._add_number_same).grid(row=0, column=1)

        bulk_row = ctk.CTkFrame(sf, fg_color="transparent")
        bulk_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        bulk_row.grid_columnconfigure(0, weight=1)

        self.bulk_box = ctk.CTkTextbox(
            bulk_row, height=68, fg_color=BG_INPUT, border_color=BORDER,
            text_color=TEXT_PRIMARY, corner_radius=10, font=("Menlo", 12),
            border_width=1)
        self.bulk_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.bulk_box.insert("0.0", "Paste multiple numbers here (one per line or comma-separated)")
        self.bulk_box.bind("<FocusIn>", lambda e: self._clear_hint())

        ctk.CTkButton(bulk_row, text="Import\nAll", width=72, height=68,
                      fg_color=BG_INPUT, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT_PRIMARY, corner_radius=10,
                      font=("SF Pro Text", 11),
                      command=self._bulk_import_same).grid(row=0, column=1)

        ctk.CTkLabel(sf, text="RECIPIENTS", font=("SF Pro Text", 10, "bold"),
                     text_color=TEXT_MUTED).grid(row=2, column=0, sticky="w", pady=(16, 6))

        self.num_scroll = ctk.CTkScrollableFrame(
            sf, height=110, fg_color=BG_INPUT,
            corner_radius=10, border_width=1, border_color=BORDER)
        self.num_scroll.grid(row=3, column=0, sticky="ew")
        self.num_scroll.grid_columnconfigure(0, weight=1)

        btm = ctk.CTkFrame(sf, fg_color="transparent")
        btm.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        btm.grid_columnconfigure(0, weight=1)

        self.count_lbl = ctk.CTkLabel(btm, text="0 recipients",
                                       font=("SF Pro Text", 11), text_color=TEXT_MUTED)
        self.count_lbl.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(btm, text="Clear All", width=80, height=26,
                      fg_color="transparent", hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT_MUTED, corner_radius=8,
                      font=("SF Pro Text", 11),
                      command=self._clear_numbers_same).grid(row=0, column=1)

        ctk.CTkLabel(sf, text="MESSAGE  ·  sent to everyone above", font=("SF Pro Text", 10, "bold"),
                     text_color=TEXT_MUTED
                     ).grid(row=5, column=0, sticky="w", pady=(18, 6))

        self.msg_box = ctk.CTkTextbox(
            sf, height=110, fg_color=BG_INPUT, border_color=BORDER,
            text_color=TEXT_PRIMARY, corner_radius=10,
            font=("SF Pro Text", 13), border_width=1, wrap="word")
        self.msg_box.grid(row=6, column=0, sticky="ew")
        self.msg_box.bind("<KeyRelease>", self._on_msg_key)

        self.char_lbl = ctk.CTkLabel(sf, text="0 characters",
                                      font=("SF Pro Text", 11), text_color=TEXT_MUTED)
        self.char_lbl.grid(row=7, column=0, sticky="e", pady=(4, 0))

        # ── "Custom message" frame: number + its own message, one at a time ──
        self.custom_frame = ctk.CTkFrame(p, fg_color="transparent")
        self.custom_frame.grid(row=3, column=0, sticky="ew")
        self.custom_frame.grid_columnconfigure(0, weight=1)
        cf = self.custom_frame

        self.edit_banner = ctk.CTkLabel(
            cf, text="✎  Editing a recipient below — change the fields and press Update, or Cancel.",
            font=("SF Pro Text", 11, "bold"), text_color=WHATSAPP_GREEN,
            fg_color=GREEN_DIM, corner_radius=8, anchor="w")
        self.edit_banner.grid(row=0, column=0, sticky="ew", pady=(0, 8), ipady=6, ipadx=10)
        self.edit_banner.grid_remove()

        crow0 = ctk.CTkFrame(cf, fg_color="transparent")
        crow0.grid(row=1, column=0, sticky="ew")
        crow0.grid_columnconfigure(0, weight=0)
        crow0.grid_columnconfigure(1, weight=1)

        self.cnum_entry = ctk.CTkEntry(
            crow0, placeholder_text="+92300 123 4567",
            fg_color=BG_INPUT, border_color=BORDER, text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED, height=42, corner_radius=10,
            font=("Menlo", 13), width=190)
        self.cnum_entry.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.cmsg_entry = ctk.CTkEntry(
            crow0, placeholder_text="Message for this number",
            fg_color=BG_INPUT, border_color=BORDER, text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED, height=42, corner_radius=10,
            font=("SF Pro Text", 13))
        self.cmsg_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.cmsg_entry.bind("<Return>", lambda e: self._add_number_custom())

        self.cadd_btn = ctk.CTkButton(
            crow0, text="Add", width=72, height=42,
            fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
            text_color="#fff", corner_radius=10,
            font=("SF Pro Text", 13, "bold"),
            command=self._add_number_custom)
        self.cadd_btn.grid(row=0, column=2)

        self.ccancel_btn = ctk.CTkButton(
            crow0, text="Cancel", width=64, height=42,
            fg_color="transparent", hover_color=BORDER,
            border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, corner_radius=10,
            font=("SF Pro Text", 12),
            command=self._cancel_edit_custom)
        self.ccancel_btn.grid(row=0, column=3, padx=(8, 0))
        self.ccancel_btn.grid_remove()

        crow1 = ctk.CTkFrame(cf, fg_color="transparent")
        crow1.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        crow1.grid_columnconfigure(0, weight=1)
        self.cimg_picker = ImagePicker(
            crow1, label="🖼  Image for this number",
            empty_text="Optional — attach an image to this recipient")
        self.cimg_picker.grid(row=0, column=0, sticky="ew")

        cbulk_row = ctk.CTkFrame(cf, fg_color="transparent")
        cbulk_row.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        cbulk_row.grid_columnconfigure(0, weight=1)

        self.cbulk_box = ctk.CTkTextbox(
            cbulk_row, height=90, fg_color=BG_INPUT, border_color=BORDER,
            text_color=TEXT_PRIMARY, corner_radius=10, font=("Menlo", 12),
            border_width=1)
        self.cbulk_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.cbulk_box.insert("0.0", "One per line:  number | message   (optional image:  number | message | /path/to/image.jpg)")
        self.cbulk_box.bind("<FocusIn>", lambda e: self._clear_hint_custom())

        ctk.CTkButton(cbulk_row, text="Import\nAll", width=72, height=90,
                      fg_color=BG_INPUT, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT_PRIMARY, corner_radius=10,
                      font=("SF Pro Text", 11),
                      command=self._bulk_import_custom).grid(row=0, column=1)

        ctk.CTkLabel(cf, text="RECIPIENTS  ·  click ✎ to edit a message", font=("SF Pro Text", 10, "bold"),
                     text_color=TEXT_MUTED).grid(row=4, column=0, sticky="w", pady=(16, 6))

        self.cnum_scroll = ctk.CTkScrollableFrame(
            cf, height=190, fg_color=BG_INPUT,
            corner_radius=10, border_width=1, border_color=BORDER)
        self.cnum_scroll.grid(row=5, column=0, sticky="ew")
        self.cnum_scroll.grid_columnconfigure(0, weight=1)

        cbtm = ctk.CTkFrame(cf, fg_color="transparent")
        cbtm.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        cbtm.grid_columnconfigure(0, weight=1)

        self.ccount_lbl = ctk.CTkLabel(cbtm, text="0 recipients",
                                        font=("SF Pro Text", 11), text_color=TEXT_MUTED)
        self.ccount_lbl.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(cbtm, text="Clear All", width=80, height=26,
                      fg_color="transparent", hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT_MUTED, corner_radius=8,
                      font=("SF Pro Text", 11),
                      command=self._clear_numbers_custom).grid(row=0, column=1)

    def _set_msg_mode(self, mode: str):
        self._cancel_edit_custom()
        self.msg_mode.set(mode)
        if mode == "same":
            self.same_frame.grid()
            self.attach_same.grid()
            self.custom_frame.grid_remove()
            self.attach_custom.grid_remove()
            self.btn_same.configure(fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
                                     text_color="#fff")
            self.btn_custom.configure(fg_color="transparent", hover_color=BORDER,
                                       text_color=TEXT_MUTED)
        else:
            self.custom_frame.grid()
            self.attach_custom.grid()
            self.same_frame.grid_remove()
            self.attach_same.grid_remove()
            self.btn_custom.configure(fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
                                       text_color="#fff")
            self.btn_same.configure(fg_color="transparent", hover_color=BORDER,
                                     text_color=TEXT_MUTED)
        self.update_send_btn()
        self._save()

    # ── Settings section ────────────────────────────────────────────────────────
    def _settings_section(self, p):
        p.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(p, text="Minimum delay between messages",
                     font=("SF Pro Text", 12), text_color=TEXT_PRIMARY
                     ).grid(row=0, column=0, sticky="w", padx=(0, 16))

        self.delay_var = ctk.IntVar(value=13)
        sl = ctk.CTkSlider(p, from_=13, to=60, variable=self.delay_var,
                            number_of_steps=47,
                            button_color=WHATSAPP_GREEN,
                            button_hover_color=DARK_GREEN,
                            progress_color=WHATSAPP_GREEN)
        sl.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        sl.bind("<ButtonRelease-1>", lambda e: self.delay_lbl.configure(text=f"{self.delay_var.get()}s"))

        self.delay_lbl = ctk.CTkLabel(p, text="13s", font=("Menlo", 13, "bold"),
                                       text_color=WHATSAPP_GREEN, width=36)
        self.delay_lbl.grid(row=0, column=2)

        ctk.CTkLabel(p, text="Maximum messages per run",
                     font=("SF Pro Text", 12), text_color=TEXT_PRIMARY
                     ).grid(row=1, column=0, sticky="w", padx=(0, 16), pady=(12, 0))

        self.max_messages_entry = ctk.CTkEntry(
            p, width=92, height=38, corner_radius=9, fg_color=BG_INPUT,
            border_color=BORDER, text_color=TEXT_PRIMARY, font=("Menlo", 13),
            justify="center")
        self.max_messages_entry.insert(0, "10")
        self.max_messages_entry.grid(row=1, column=1, sticky="w", pady=(12, 0))
        self.max_messages_entry.bind("<FocusOut>", lambda e: self._save())

        ctk.CTkLabel(p, text="contacts",
                     font=("SF Pro Text", 12), text_color=TEXT_MUTED
                     ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(12, 0))

        ctk.CTkLabel(p,
            text="ℹ  This app uses macOS Accessibility to control WhatsApp — fully automated, no clicking needed.\n"
                 "   For Chrome mode, ensure you are already logged in to web.whatsapp.com before sending.\n"
                 "   13s is a hard floor — the first message waits exactly this long, and every message\n"
                 "   after that waits a random amount at or above it.\n"
                 "   🛡  Cooldown trigger: after 10 messages or 10 minutes, whichever comes first; pause is 10 minutes.\n"
                 "   ⏸  Queue protection: only the maximum batch is sent; use Restart Queue for the next batch.",
            font=("SF Pro Text", 11), text_color=TEXT_MUTED,
            wraplength=520, justify="left"
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # ── Action section ──────────────────────────────────────────────────────────
    def _action_section(self, parent, row):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, sticky="ew", padx=28, pady=(4, 8))
        f.grid_columnconfigure(0, weight=1)

        self.send_btn = ctk.CTkButton(
            f, text="▶  Start Sending",
            height=52, corner_radius=14,
            fg_color=WHATSAPP_GREEN, hover_color=DARK_GREEN,
            text_color="#fff", font=("SF Pro Display", 15, "bold"),
            command=self._start_sending
        )
        self.send_btn.grid(row=0, column=0, sticky="ew")

        self.stop_btn = ctk.CTkButton(
            f, text="⏹  Stop", height=52, corner_radius=14, width=110,
            fg_color="#c0392b", hover_color="#96281b",
            text_color="#fff", font=("SF Pro Display", 15, "bold"),
            command=self._stop, state="disabled"
        )
        self.stop_btn.grid(row=0, column=1, padx=(10, 0))

        self.restart_btn = ctk.CTkButton(
            f, text="↻  Restart Queue", height=52, corner_radius=14, width=150,
            fg_color=BG_INPUT, hover_color=BORDER,
            border_width=1, border_color=BORDER,
            text_color=TEXT_PRIMARY, font=("SF Pro Display", 13, "bold"),
            command=self._restart_queue
        )
        self.restart_btn.grid(row=0, column=2, padx=(10, 0))

        self.progress = ctk.CTkProgressBar(
            f, height=6, corner_radius=3,
            progress_color=WHATSAPP_GREEN, fg_color=BG_CARD
        )
        self.progress.set(0)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.status_lbl = ctk.CTkLabel(f, text="",
                                        font=("SF Pro Text", 12), text_color=TEXT_MUTED)
        self.status_lbl.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # ── Log section ─────────────────────────────────────────────────────────────
    def _log_section(self, parent, row):
        f = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16,
                          border_width=1, border_color=BORDER)
        f.grid(row=row, column=0, sticky="ew", padx=28, pady=(0, 28))
        f.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(f, text="📋  Activity Log", font=("SF Pro Display", 13, "bold"),
                     text_color=TEXT_PRIMARY).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 6))
        ctk.CTkFrame(f, height=1, fg_color=BORDER, corner_radius=0).grid(row=1, column=0, sticky="ew", padx=20)

        self.log_box = ctk.CTkTextbox(
            f, height=160, fg_color="transparent", border_width=0,
            text_color=TEXT_MUTED, font=("Menlo", 11), state="disabled"
        )
        self.log_box.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 4))

        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="e", padx=20, pady=(0, 12))

        ctk.CTkButton(btn_row, text="Clear Sent History", width=120, height=26,
                      fg_color="transparent", hover_color="#3a1a1a",
                      border_width=1, border_color="#e74c3c",
                      text_color="#e74c3c", corner_radius=8,
                      font=("SF Pro Text", 11),
                      command=self._clear_sent_log
                      ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(btn_row, text="Clear Log", width=80, height=26,
                      fg_color="transparent", hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT_MUTED, corner_radius=8,
                      font=("SF Pro Text", 11),
                      command=self._clear_log
                      ).pack(side="left")

    # ── Events ──────────────────────────────────────────────────────────────────
    def _selected_country_code(self) -> str:
        return COUNTRY_CODES.get(self.country_var.get(), "+92")

    def _country_changed(self):
        code = self._selected_country_code()
        self._log(f"Lead country set to {self.country_var.get()}.")
        self._save()
        # Keep the placeholder honest when the user switches market.
        hint = f"Local number — country code {code}" if code else "Enter full number with +country code"
        if hasattr(self, "num_entry"):
            self.num_entry.configure(placeholder_text=hint)
        if hasattr(self, "cnum_entry"):
            self.cnum_entry.configure(placeholder_text=hint)

    def _clear_hint(self):
        if self.bulk_box.get("0.0", "end").strip().startswith("Paste multiple"):
            self.bulk_box.delete("0.0", "end")

    def _clear_hint_custom(self):
        if self.cbulk_box.get("0.0", "end").strip().startswith("One per line"):
            self.cbulk_box.delete("0.0", "end")

    # ── Same-message mode: numbers only ─────────────────────────────────────
    def _add_number_same(self):
        raw = self.num_entry.get().strip()
        if not raw:
            return
        num = clean_number(raw, self._selected_country_code())
        if len(re.sub(r"\D", "", num)) < 7:
            self.num_entry.configure(border_color="#e74c3c")
            self.after(800, lambda: self.num_entry.configure(border_color=BORDER))
            return
        if num not in self.numbers:
            self.numbers.append(num)
            self._refresh_list_same()
            self._save()
        self.num_entry.delete(0, "end")
        self.update_send_btn()

    def _bulk_import_same(self):
        raw = self.bulk_box.get("0.0", "end")
        parts = re.split(r"[\n,;]+", raw)
        added = 0
        for p in parts:
            num = clean_number(p, self._selected_country_code())
            if len(re.sub(r"\D", "", num)) >= 7 and num not in self.numbers:
                self.numbers.append(num)
                added += 1
        self.bulk_box.delete("0.0", "end")
        self._refresh_list_same()
        self._save()
        self.update_send_btn()
        self._log(f"Imported {added} number(s).")

    def _remove_number_same(self, num):
        if num in self.numbers:
            self.numbers.remove(num)
            self._refresh_list_same()
            self._save()
            self.update_send_btn()

    def _clear_numbers_same(self):
        self.numbers.clear()
        self._refresh_list_same()
        self._save()
        self.update_send_btn()

    def _refresh_list_same(self):
        for w in self.num_scroll.winfo_children():
            w.destroy()
        self.num_scroll.grid_columnconfigure(0, weight=1)

        if not self.numbers:
            ctk.CTkLabel(self.num_scroll, text="No numbers added yet",
                         font=("SF Pro Text", 12), text_color=TEXT_MUTED
                         ).grid(row=0, column=0, pady=16)
        else:
            for i, num in enumerate(self.numbers):
                rf = ctk.CTkFrame(self.num_scroll, corner_radius=8, fg_color=BG_ROW)
                rf.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
                rf.grid_columnconfigure(0, weight=1)

                ctk.CTkLabel(rf, text=num, font=("Menlo", 12, "bold"),
                             text_color=TEXT_PRIMARY
                             ).grid(row=0, column=0, sticky="w", padx=(10, 4), pady=8)

                ctk.CTkButton(rf, text="✕", width=28, height=26,
                              fg_color="transparent", hover_color="#3a1a1a",
                              text_color=DANGER, corner_radius=6,
                              font=("SF Pro Text", 11),
                              command=lambda n=num: self._remove_number_same(n)
                              ).grid(row=0, column=1, padx=(0, 6))

        n = len(self.numbers)
        self.count_lbl.configure(text=f"{n} recipient{'s' if n != 1 else ''}")

    def _on_msg_key(self, event=None):
        txt = self.msg_box.get("0.0", "end").strip()
        if self.same_picker.path:
            over = len(txt) > CAPTION_LIMIT
            self.char_lbl.configure(
                text=f"{len(txt)} / {CAPTION_LIMIT} characters (image caption limit)",
                text_color=DANGER if over else TEXT_MUTED)
        else:
            self.char_lbl.configure(text=f"{len(txt)} characters", text_color=TEXT_MUTED)
        self.update_send_btn()
        self._save()

    def _on_default_image_change(self):
        self._refresh_list_custom()
        self.update_send_btn()
        self._save()

    def _effective_image(self, rec: dict) -> str:
        """A recipient's own image, falling back to the default image."""
        return rec.get("image") or self.cdefault_picker.path

    # ── Custom-message mode: number + its own message ───────────────────────
    def _add_number_custom(self):
        raw = self.cnum_entry.get().strip()
        msg = self.cmsg_entry.get().strip()
        img = self.cimg_picker.path
        if not raw or (not msg and not (img or self.cdefault_picker.path)):
            return
        num = clean_number(raw, self._selected_country_code())
        if len(re.sub(r"\D", "", num)) < 7:
            self.cnum_entry.configure(border_color="#e74c3c")
            self.after(800, lambda: self.cnum_entry.configure(border_color=BORDER))
            return

        rec = {"number": num, "message": msg}
        if img:
            rec["image"] = img

        if self.editing_idx is not None:
            self.recipients[self.editing_idx] = rec
            self._log(f"✎  Updated {num}.")
            self._cancel_edit_custom()
        else:
            self.recipients.append(rec)
            self.cnum_entry.delete(0, "end")
            self.cmsg_entry.delete(0, "end")
            self.cimg_picker.set("", notify=False)

        self._refresh_list_custom()
        self._save()
        self.update_send_btn()

    def _start_edit_custom(self, idx):
        if not (0 <= idx < len(self.recipients)):
            return
        r = self.recipients[idx]
        self.editing_idx = idx
        self.cnum_entry.delete(0, "end")
        self.cnum_entry.insert(0, r["number"])
        self.cmsg_entry.delete(0, "end")
        self.cmsg_entry.insert(0, r["message"])
        self.cimg_picker.set(r.get("image", ""), notify=False)
        self.cmsg_entry.focus_set()
        self.cadd_btn.configure(text="Update")
        self.ccancel_btn.grid()
        self.edit_banner.grid()
        self._refresh_list_custom()

    def _cancel_edit_custom(self):
        self.editing_idx = None
        if hasattr(self, "cnum_entry"):
            self.cnum_entry.delete(0, "end")
            self.cmsg_entry.delete(0, "end")
            self.cimg_picker.set("", notify=False)
            self.cadd_btn.configure(text="Add")
            self.ccancel_btn.grid_remove()
            self.edit_banner.grid_remove()
            self._refresh_list_custom()

    def _bulk_import_custom(self):
        self._cancel_edit_custom()
        raw = self.cbulk_box.get("0.0", "end")
        added = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"\s*\|\s*", line, maxsplit=1)
            if len(parts) != 2:
                continue
            num = clean_number(parts[0], self._selected_country_code())
            msg = parts[1].strip()
            image = ""
            # Optional trailing "| /path/to/image.jpg" (message text may still contain pipes)
            m = re.match(r"^(.*)\|\s*([^|]+\.(?:png|jpe?g|gif|webp|heic))\s*$", msg,
                         re.IGNORECASE | re.DOTALL)
            if m:
                msg = m.group(1).strip()
                image = self._resolve_image(m.group(2))
            if len(re.sub(r"\D", "", num)) >= 7 and (msg or image):
                rec = {"number": num, "message": msg}
                if image:
                    rec["image"] = image
                self.recipients.append(rec)
                added += 1
        self.cbulk_box.delete("0.0", "end")
        self._refresh_list_custom()
        self._save()
        self.update_send_btn()
        self._log(f"Imported {added} recipient(s).")

    def _remove_number_custom(self, idx):
        if 0 <= idx < len(self.recipients):
            self.recipients.pop(idx)
            if self.editing_idx == idx:
                self._cancel_edit_custom()
            self._refresh_list_custom()
            self._save()
            self.update_send_btn()

    def _clear_numbers_custom(self):
        self._cancel_edit_custom()
        self.recipients.clear()
        self._refresh_list_custom()
        self._save()
        self.update_send_btn()

    def _refresh_list_custom(self):
        for w in self.cnum_scroll.winfo_children():
            w.destroy()
        self.cnum_scroll.grid_columnconfigure(0, weight=1)

        if not self.recipients:
            ctk.CTkLabel(self.cnum_scroll, text="No recipients added yet",
                         font=("SF Pro Text", 12), text_color=TEXT_MUTED
                         ).grid(row=0, column=0, pady=16)
        else:
            for i, r in enumerate(self.recipients):
                being_edited = (i == self.editing_idx)

                rf = ctk.CTkFrame(
                    self.cnum_scroll, corner_radius=8,
                    fg_color=GREEN_DIM if being_edited else BG_ROW,
                    border_width=1, border_color=WHATSAPP_GREEN if being_edited else BG_ROW)
                rf.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
                rf.grid_columnconfigure(1, weight=1)

                ctk.CTkLabel(rf, text=r["number"], font=("Menlo", 12, "bold"),
                             text_color=TEXT_PRIMARY, width=132, anchor="w"
                             ).grid(row=0, column=0, sticky="w", padx=(10, 4), pady=8)

                msg_text = r["message"] or "(image only)"
                preview = msg_text if len(msg_text) <= 60 else msg_text[:57] + "…"
                ctk.CTkLabel(rf, text=preview, font=("SF Pro Text", 11),
                             text_color=TEXT_MUTED, anchor="w"
                             ).grid(row=0, column=1, sticky="ew", padx=4)

                eff_img = self._effective_image(r)
                if eff_img:
                    own = bool(r.get("image"))
                    missing = not os.path.isfile(eff_img)
                    tag = f"🖼 {Path(eff_img).name}" if own else "🖼 default"
                    if len(tag) > 22:
                        tag = tag[:21] + "…"
                    ctk.CTkLabel(rf, text=("⚠ " + tag) if missing else tag,
                                 font=("SF Pro Text", 10),
                                 text_color=DANGER if missing else ACCENT_BLUE
                                 ).grid(row=0, column=2, padx=(4, 4))

                ctk.CTkButton(rf, text="✎", width=28, height=26,
                              fg_color="transparent", hover_color=BORDER,
                              text_color=WHATSAPP_GREEN, corner_radius=6,
                              font=("SF Pro Text", 12),
                              command=lambda idx=i: self._start_edit_custom(idx)
                              ).grid(row=0, column=3, padx=(4, 2))

                ctk.CTkButton(rf, text="✕", width=28, height=26,
                              fg_color="transparent", hover_color="#3a1a1a",
                              text_color=DANGER, corner_radius=6,
                              font=("SF Pro Text", 11),
                              command=lambda idx=i: self._remove_number_custom(idx)
                              ).grid(row=0, column=4, padx=(0, 6))

        n = len(self.recipients)
        self.ccount_lbl.configure(text=f"{n} recipient{'s' if n != 1 else ''}")

    def update_send_btn(self):
        if getattr(self, "batch_locked", False):
            self.send_btn.configure(state="disabled")
            return
        if self.msg_mode.get() == "same":
            msg = self.msg_box.get("0.0", "end").strip()
            ok = (len(self.numbers) > 0 and (len(msg) > 0 or bool(self.same_picker.path))
                  and not self.sending)
        else:
            ok = len(self.recipients) > 0 and not self.sending
        self.send_btn.configure(state="normal" if ok else "disabled")

    def _log(self, text: str, color: str = TEXT_MUTED):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {text}\n")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("0.0", "end")
        self.log_box.configure(state="disabled")

    def _clear_sent_log(self):
        save_sent_log({})
        self.sent_log = {}
        self._log("🗑  Sent-message history cleared — duplicates will no longer be blocked.", "#f39c12")

    def _save(self):
        current_signature = self._current_queue_signature()
        if (self.queue_signature and current_signature != self.queue_signature
                and not self.sending):
            # A changed lead list is a new queue. Do not let an older completed
            # queue offset block a newly uploaded short list.
            self.queue_offset = 0
            self.batch_locked = False
        self.queue_signature = current_signature
        save_data(
            self.numbers,
            self.msg_box.get("0.0", "end").strip(),
            self.recipients,
            self.msg_mode.get(),
            self.send_mode.get(),
            self.delay_var.get(),
            self._selected_country_code(),
            self.lead_area_var.get().strip(),
            self._max_messages(),
            self.queue_offset,
            self.queue_signature,
            self.same_picker.path,
            self.cdefault_picker.path
        )

    def _current_queue_signature(self) -> str:
        if self.msg_mode.get() == "same":
            value = f"same|{self.same_picker.path}|" + "|".join(self.numbers)
        else:
            value = f"custom|{self.cdefault_picker.path}|" + "|".join(
                f"{r.get('number', '')}:{r.get('message', '')}:{r.get('image', '')}"
                for r in self.recipients
            )
        return f"{zlib.adler32(value.encode('utf-8')):08x}"

    def _max_messages(self) -> int:
        raw = getattr(self, "max_messages_entry", None)
        raw_value = raw.get().strip() if raw else ""
        if not raw_value:
            if self.msg_mode.get() == "same":
                return max(1, len(self.numbers))
            return max(1, len(self.recipients))
        try:
            value = int(raw_value)
        except (ValueError, AttributeError):
            value = 10
        return max(1, min(value, 10000))

    # ── Send ────────────────────────────────────────────────────────────────────
    def _start_sending(self):
        if self.sending:
            return
        if self.batch_locked:
            self._log("⏸  Queue is paused after the selected batch. Restart the app to release the remaining leads.", "#f39c12")
            return

        if self.msg_mode.get() == "same":
            msg = self.msg_box.get("0.0", "end").strip()
            img = self.same_picker.path
            if (not msg and not img) or not self.numbers:
                return
            pairs = [(n, msg, img) for n in self.numbers]
        else:
            if not self.recipients:
                return
            pairs = [(r["number"], r["message"], self._effective_image(r))
                     for r in self.recipients]

        max_messages = self._max_messages()
        available = pairs[self.queue_offset:]
        if not available:
            self._log("✓  Queue is empty. Add more leads or restart the queue after updating your list.", WHATSAPP_GREEN)
            self.status_lbl.configure(text="Queue complete — no remaining leads.")
            return
        batch = available[:max_messages]
        remaining = max(0, len(available) - len(batch))

        # Check every image in this batch up front, so nothing fails halfway through a run.
        for num, message, image in batch:
            if image and not os.path.isfile(image):
                self._log(f"✗  Image not found for {num}: {image}. Fix or remove it, then start again.",
                          "#e74c3c")
                self.status_lbl.configure(text="Sending blocked — an image file is missing.")
                return
            if image and len(message) > CAPTION_LIMIT:
                self._log(f"✗  Message for {num} is {len(message)} characters; WhatsApp allows "
                          f"{CAPTION_LIMIT} when an image is attached. Shorten it or remove the image.",
                          "#e74c3c")
                self.status_lbl.configure(text="Sending blocked — caption too long for an image.")
                return
        mode = self.send_mode.get()
        self._log(f"▶  Mode: {'WhatsApp Desktop' if mode == 'app' else 'Google Chrome (WhatsApp Web)'}")
        self._log(f"📦  Queue batch: contacts {self.queue_offset + 1}–{self.queue_offset + len(batch)}; {remaining} lead(s) remain locked until restart.", ACCENT_BLUE)

        self.sending = True
        self.stop_flag = False
        self.send_btn.configure(state="disabled", text="⏳  Sending...")
        self.stop_btn.configure(state="normal")
        self.restart_btn.configure(state="disabled")
        self.progress.set(0)

        threading.Thread(
            target=self._worker,
            args=(batch, mode, len(pairs), remaining),
            daemon=True
        ).start()

    def _stop(self):
        self.stop_flag = True
        self._log("⏹  Stop requested…")

    def _restart_queue(self):
        if self.sending:
            return
        self.batch_locked = False
        self.status_lbl.configure(text=f"Queue restarted — next batch begins at contact {self.queue_offset + 1}.")
        self._log(f"↻  Queue restarted. Ready to send the next {self._max_messages()} lead(s).", WHATSAPP_GREEN)
        self.update_send_btn()

    # Hard floor — never send faster than this, no matter what the UI allows.
    MIN_DELAY = 13

    def _worker(self, recipients, mode, queue_total, queue_remaining):
        min_delay    = max(self.MIN_DELAY, self.delay_var.get())
        total        = len(recipients)
        skipped      = 0
        sent_ok      = 0
        not_on_whatsapp = 0
        failed       = 0

        # A cooldown is triggered by either threshold, whichever is reached first.
        COOLDOWN_AFTER_MESSAGES = 10
        COOLDOWN_AFTER_SECONDS = 10 * 60
        COOLDOWN_DURATION_SECONDS = 10 * 60
        since_last_cooldown = 0
        cooldown_started_at = time.monotonic()

        self.sent_log = load_sent_log()
        self._log(
            f"Starting — {total} queued recipients, min delay {min_delay}s, mode={mode}.\n"
            f"   🛡  Cooldown after 10 messages or 10 minutes, whichever comes first."
        )

        # First message always waits the fixed floor before anything is sent.
        self.status_lbl.configure(text=f"Warming up ({self.MIN_DELAY}s)…")
        for r in range(self.MIN_DELAY, 0, -1):
            if self.stop_flag:
                break
            self.status_lbl.configure(text=f"Starting in {r}s…")
            time.sleep(1)

        for i, (num, message, image) in enumerate(recipients):
            if self.stop_flag:
                self._log("⏹  Stopped.")
                break

            # Refresh history immediately before this recipient. This catches a
            # matching message sent by an earlier run (or another open instance)
            # after this batch was initially loaded.
            self.sent_log = load_sent_log()
            if was_already_sent(message, num, self.sent_log, image):
                self._log(f"⏭  Skipped {num} — this exact message was already sent.")
                self.progress.set((i + 1) / total)
                skipped += 1
                continue

            self.status_lbl.configure(text=f"Sending to {num}  ({i+1}/{total})")
            self._log(f"→  {num}  ({i+1}/{total})" + (f"  🖼 {Path(image).name}" if image else ""))

            # ── Retry logic (up to 2 attempts) ──
            ok, msg_result = False, "Not attempted"
            for attempt in range(2):
                if mode == "app":
                    ok, msg_result = send_via_whatsapp_desktop(num, message, image)
                else:
                    ok, msg_result = send_via_chrome(num, message, image)

                if ok:
                    break

                is_recoverable = "recoverable" in msg_result.lower()
                if is_recoverable and attempt == 0:
                    self._log(f"   ↻  Attempt {attempt+1} failed ({msg_result}), retrying in 4s…", "#f39c12")
                    time.sleep(4)
                else:
                    break

            if ok:
                self._log(f"   ✓  Sent to {num}")
                record_sent(message, num, self.sent_log, image)
                sent_ok             += 1
                since_last_cooldown += 1
            elif is_not_on_whatsapp_result(msg_result):
                # This is a completed contact outcome, not a failed message. It
                # is added to the normal history so the same message is skipped
                # next time, and to a separate CSV for lead-list cleanup.
                record_sent(message, num, self.sent_log, image)
                record_not_on_whatsapp(num, message, image)
                not_on_whatsapp += 1
                self._log(f"   ⏭  {num} is not on WhatsApp — saved to not_on_whatsapp.csv.", "#f39c12")
            else:
                self._log(f"   ✗  Failed: {msg_result}", "#e74c3c")
                failed += 1

            self.progress.set((i + 1) / total)

            # ── Cooldown check: 10 sends OR 10 minutes, whichever comes first ─
            cooldown_due = (
                since_last_cooldown >= COOLDOWN_AFTER_MESSAGES
                or time.monotonic() - cooldown_started_at >= COOLDOWN_AFTER_SECONDS
            )
            if cooldown_due and i < total - 1 and not self.stop_flag:
                cooldown_secs = COOLDOWN_DURATION_SECONDS
                mins, secs = divmod(cooldown_secs, 60)
                self._log(
                    f"🛡  Cooldown — pausing {mins}m {secs}s after "
                    f"{since_last_cooldown} messages / 10-minute window.",
                    "#f39c12"
                )
                for r in range(cooldown_secs, 0, -1):
                    if self.stop_flag:
                        break
                    m, s = divmod(r, 60)
                    self.status_lbl.configure(
                        text=f"🛡  Anti-ban pause — resuming in {m}:{s:02d}…  ({i+2}/{total})"
                    )
                    time.sleep(1)

                # Reset the message and time windows after the pause.
                since_last_cooldown = 0
                cooldown_started_at = time.monotonic()
                if not self.stop_flag:
                    self._log(
                        "✓  Cooldown done — continuing the current queue batch.",
                        WHATSAPP_GREEN
                    )

            # ── Normal inter-message delay ───────────────────────────────────
            elif i < total - 1 and not self.stop_flag:
                delay = random.randint(min_delay, min_delay + 15)   # never below the floor
                self._log(f"   ⏱  Next in {delay}s…", TEXT_MUTED)
                for r in range(delay, 0, -1):
                    if self.stop_flag:
                        break
                    self.status_lbl.configure(text=f"Next in {r}s…  ({i+2}/{total})")
                    time.sleep(1)

        completed_naturally = not self.stop_flag
        self.sending = False
        self.after(0, self._done, total, sent_ok, skipped, not_on_whatsapp, failed,
                   queue_total, queue_remaining, completed_naturally)

    def _done(self, total, sent_ok=0, skipped=0, not_on_whatsapp=0, failed=0,
              queue_total=0, queue_remaining=0, completed_naturally=True):
        self.send_btn.configure(state="normal", text="▶  Start Sending")
        self.stop_btn.configure(state="disabled")
        self.restart_btn.configure(state="normal")
        summary = (
            f"Done — {sent_ok} sent, {not_on_whatsapp} not on WhatsApp, "
            f"{skipped} skipped (duplicate), {failed} failed."
        )
        self.status_lbl.configure(text=summary)
        self._log(f"✓  {summary}")
        if completed_naturally and total:
            self.queue_offset += total
            self._save()
        if queue_remaining and completed_naturally:
            self.batch_locked = True
            self.status_lbl.configure(
                text=f"{summary}  Queue paused — restart the app to release {queue_remaining} remaining lead(s)."
            )
            self._log(
                f"⏸  Batch complete. {queue_remaining} lead(s) remain queued and will not send until restart.",
                "#f39c12"
            )
        self.update_send_btn()


if __name__ == "__main__":
    app = WASenderApp()
    app.mainloop()