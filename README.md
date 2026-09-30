# ↗ Reachflow — WhatsApp Outreach for Mac

A sleek, fully automated bulk messaging tool for **WhatsApp Desktop on macOS**. It uses native macOS Accessibility and AppleScript to send messages directly through the WhatsApp Desktop app — no browser hacks, no WhatsApp Web, no API keys.

![Platform](https://img.shields.io/badge/Platform-macOS-blue?logo=apple)
![Python](https://img.shields.io/badge/Python-3.10%2B-yellow?logo=python)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

| Feature | Description |
|---|---|
| **Bulk Messaging** | Send the same message to multiple phone numbers automatically |
| **Duplicate Prevention** | Built-in sent log prevents the same message from being sent to the same number twice |
| **Lead Profile** | Choose the area/market and country before adding leads |
| **Smart Number Parsing** | Local numbers use the selected country code; numbers beginning with `+` stay unchanged |
| **Lead Auto-Import** | Upload or paste CSV, TSV, JSON, Markdown, or exported text and auto-detect lead fields |
| **Image Attachments** | Attach an image to the shared message, to each custom recipient, or set a default image — the text is sent as the image's caption, in one message |
| **Bulk Import** | Paste multiple numbers (comma, semicolon, or newline separated) and import all at once |
| **Adjustable Delay** | Set 3–20 second delay between messages to avoid rate limiting |
| **Queue Batches** | Choose the maximum messages per run; remaining leads stay locked until app restart |
| **Cooldown Window** | Triggers after 10 messages or 10 minutes, whichever comes first, then pauses for 10 minutes |
| **Restart Queue** | Releases the next batch through the visible Restart Queue button |
| **Persistent State** | Numbers and message text are saved between sessions |
| **Activity Log** | Real-time log of all send activity, skips, and errors |
| **Modern UI** | Dark-themed interface built with CustomTkinter |

---

## 🖥️ Requirements

- **macOS** (tested on macOS 13+)
- **Python 3.10+**
- **WhatsApp Desktop** (installed and logged in)
- **Accessibility Permission** granted to your Terminal app (see [Permissions](#-permissions))

---

## 🚀 Quick Start

### Option 1 — One-Line Setup

```bash
chmod +x install_and_run.sh
./install_and_run.sh
```

This installs dependencies and launches the app automatically.

### Option 2 — Manual Setup

```bash
# Install dependencies
pip3 install customtkinter pillow

# Run the app
python3 app.py
```

### Option 3 — Using `uv`

```bash
uv add customtkinter pillow
uv run app.py
```

---

## 📖 Usage

### 1. Set the lead profile

- Enter the lead area, city, or market label
- Choose the country that local phone numbers belong to
- The app displays the selected country code and applies it automatically

### 2. Import a lead file

- Upload or paste a CSV, TSV, JSON, Markdown export, or plain-text lead list
- Click **Detect & import**
- The importer detects phone, business/name, message, and location columns
- Rows without a usable phone number are skipped and shown in the import summary
- If a message column is found, the app switches to custom-message mode automatically

### 3. Add Numbers

- Type a number in the input field and press **Add** (or hit Enter)
- Or paste multiple numbers into the bulk import box and click **Import All**
- Supported formats: `+923001234567`, `03001234567`, `0300-123-4567`

### 4. Write Your Message

Type your message in the message box. Character count updates in real time.

### 4b. Add an image (optional)

Images work in **both** message options and on **both** channels (WhatsApp Desktop and Chrome). The
image and your text are sent as **one WhatsApp message** — the text becomes the image's caption.

| Option | How to add the image |
|---|---|
| **Same message for all** | Click **🖼 Add image** under the message box. Everyone gets the same image + caption. |
| **Custom message per number** | Click **🖼 Image for this number** before pressing **Add** (or ✎ edit a recipient to change it). |
| **Custom — default image** | Click **🖼 Set default image** below the list. Used for every recipient that has no image of their own. |
| **Bulk import (custom)** | `number \| message \| /path/to/image.jpg` — the image part is optional. Messages containing `\|` still work. |
| **Lead file import** | Add a column named `Image`, `Photo`, `Picture`, or `Attachment`. Relative paths are resolved from the lead file's folder. |

Notes:

- A recipient can have an image with **no text** (image only).
- **PNG and JPG** are pasted as-is. **GIF, WebP, HEIC** are converted to a static PNG (animation is not kept).
- WhatsApp limits captions to **1024 characters** when an image is attached. Sending is blocked with a clear message if a caption is longer.
- Every image in the batch is checked **before** sending starts, so a missing file never fails halfway through a run.
- Duplicate prevention includes the image: the same text with a *different* image (or no image) counts as a different message.

### 5. Configure Delay

Use the slider to set the delay between messages (3–20 seconds). Higher values are safer to avoid WhatsApp rate limiting.

### 6. Send

Click **▶ Start Sending**. The app will:

1. Copy the message to clipboard
2. Open each chat via the `whatsapp://` URL scheme
3. Paste and send using AppleScript automation

With an image attached, step 3 becomes: paste the image (WhatsApp opens its attachment preview), paste the
message into the preview's caption box, then press Enter — so image and text arrive together.

The **Maximum messages per run** setting limits the active queue batch. For example,
with 40 leads and a limit of 10, only the first 10 are processed. The remaining
30 stay locked until **↻ Restart Queue** is clicked. The next batch then starts
from lead 11. The queue position is saved when a batch completes.

The sender pauses for 10 minutes whenever either 10 successful messages have been
sent or 10 minutes have elapsed since the last cooldown, whichever comes first.

If **Maximum messages per run** is left empty, the active queue contains all
available leads.

You can click **⏹ Stop** at any time to halt the process.

---

## 🛡️ Duplicate Prevention

The app maintains a persistent **sent log** (`~/.wa_sender_sent_log.json`) that tracks which message was sent to which number. Immediately before every recipient is sent, it checks the latest history: if that exact message was already successfully sent to that number, the recipient is skipped.

| Scenario | Result |
|---|---|
| `"Hello"` → `+923001234567` (first time) | ✅ Sent |
| `"Hello"` → `+923001234567` (again) | ⏭ Skipped — duplicate |
| `"Hi there"` → `+923001234567` | ✅ Sent — different message |
| `"Hello"` → `+923009876543` | ✅ Sent — different number |

- Skipped messages are logged in the Activity Log
- Final summary separates actual sends from unregistered numbers: `Done — X sent, Y not on WhatsApp, Z skipped (duplicate), W failed.`
- Use the **Clear Sent History** button to reset the log and allow re-sending

### Numbers not on WhatsApp

If WhatsApp reports that a number is not registered, the tool records it as a
completed contact outcome: it is added to the sent history for that exact
message so it will not be tried again, but it is **not** counted as a sent
message. Each detected number is appended to `~/not_on_whatsapp.csv` for easy
lead-list cleanup.

---

## 🔐 Permissions

The app requires **macOS Accessibility permission** to automate WhatsApp Desktop.

### How to Grant Permission

1. Open **System Settings** → **Privacy & Security** → **Accessibility**
2. Click the **+** button
3. Add your **Terminal** app (e.g., Terminal.app, iTerm2, or your IDE's terminal)
4. Make sure the toggle is **enabled**
5. **Restart the app** after granting permission

> ⚠️ The app will show a warning banner if Accessibility permission is missing.

---

## 📁 File Structure

```
wa_mac/
├── app.py                 # Main application
├── install_and_run.sh     # One-line setup script
└── README.md              # This file
```

### Persistent Files (in home directory)

| File | Purpose |
|---|---|
| `~/.wa_sender_mac.json` | Saved numbers, message text, and image paths |
| `~/.wa_sender_sent_log.json` | Sent message log for duplicate prevention |

---

## 🔧 Troubleshooting

| Issue | Solution |
|---|---|
| **"WhatsApp is not running"** | Open WhatsApp Desktop and log in before starting |
| **"Accessibility permission may be missing"** | Grant permission in System Settings (see [Permissions](#-permissions)) |
| **Messages not sending** | Increase the delay — WhatsApp may rate-limit fast sends |
| **Wrong country code** | Set the lead country before adding local numbers, or enter the full international number with `+` |
| **Segfault on launch (exit code 139)** | Try running directly with `python3 app.py` instead of `uv run` |
| **Image not sent / "Image not found"** | The app stores the image's *path*. If you move or rename the file, pick it again |
| **Image pasted but caption missing** | Increase the delay and keep WhatsApp in the foreground; the preview needs ~2s to open |
| **Duplicate not detected** | Even a small whitespace difference creates a different message hash — keep messages consistent |

---

## ⚠️ Disclaimer

This tool is intended for **personal and legitimate use only**. Bulk messaging may violate WhatsApp's Terms of Service. Use responsibly and at your own risk. The authors are not responsible for any account restrictions or bans that may result from misuse.

---

## 📄 License

MIT License — free to use and modify.
