# 🍓 XNXX Pi Scraper — v3.0

A self-hosted, private media server and scraper built to run on a **Raspberry Pi Zero W2** (or any Linux machine). Powered by Flask, it provides a clean web UI to browse, search, stream, and download videos — with full multi-user authentication, per-user data isolation, and an admin panel.

> ⚠️ **Personal Use Only.** This tool is intended for private, self-hosted use on your own network. Respect the terms of service of any site you access.

---

## 📋 Table of Contents

- [Features](#-features)
- [Requirements](#-requirements)
- [Installation](#-installation)
- [Running the Server](#-running-the-server)
- [First Login](#-first-login)
- [Using the App](#-using-the-app)
- [Debug Logging](#-debug-logging)
- [Admin Panel](#-admin-panel)
- [Scrape Debugger](#-scrape-debugger)
- [Project Structure](#-project-structure)
- [Configuration](#-configuration)
- [Troubleshooting](#-troubleshooting)

---

## ✨ Features

- 🔐 **Multi-user login** with session authentication
- 👤 **Per-user data isolation** — favorites, watch history, search history, and downloads are private per account
- 👑 **Admin panel** — create/delete users, reset passwords, view any user's data, monitor all active downloads
- 🔍 **Search & Browse** — search by keyword or browse 70+ categories
- 📺 **In-browser streaming** — plays MP4 (high/low quality) or HLS streams directly in the browser
- ⬇️ **Background downloads** — download videos to the Pi with live progress tracking; cancellable at any time
- ★ **Favorites** — star any video; bulk-download all favorites in one click
- 🕐 **Watch history** — automatic, configurable, clearable
- 🖼️ **Thumbnail proxy** — routes thumbnails through the Pi to avoid mixed-content/CORS issues
- 🗃️ **HTML caching** — configurable cache duration reduces redundant network requests
- 🔧 **Scrape debugger** — admin-only tool to inspect exactly what the scraper sees for any URL
- 📱 **Responsive UI** — works on phone, tablet, and desktop
- 📝 **File logging** — opt-in debug log via `--debug` flag

---

## 🖥️ Requirements

| Component | Minimum |
|-----------|---------|
| Hardware  | Raspberry Pi Zero W2 (or any Pi / Linux box) |
| OS        | Raspberry Pi OS Bullseye (or any Debian/Ubuntu-based distro) |
| Python    | 3.9+ |
| RAM       | 512 MB+ |
| Storage   | SD card with enough space for your downloads |

---

## 📦 Installation

### One-line install (recommended)

Clone the repo and run the install script:

```bash
git clone https://github.com/AnonymousxiosdeV/xnxx-scraper-pi.git
cd xnxx-scraper-pi
chmod +x install.sh
./install.sh
```

The install script will:
1. Update `apt` and install `python3`, `pip3`, and `python3-venv` if missing
2. Create a Python virtual environment at `./venv`
3. Install all Python dependencies (`flask`, `requests`, `beautifulsoup4`, `werkzeug`)
4. Create the required data directories under `~/xnxx-scraper/`
5. Print next steps

### Manual install

If you prefer to install manually:

```bash
# Install system dependencies
sudo apt update && sudo apt install -y python3 python3-pip python3-venv

# Create and activate a virtual environment (install.sh uses ~/xnxx-venv)
python3 -m venv ~/xnxx-venv
source ~/xnxx-venv/bin/activate

# Install Python packages
pip install -r requirements.txt

# Create data directories
mkdir -p ~/xnxx-scraper/{cache,data,users}
```

---

## 🚀 Running the Server

### Normal mode (console logging only)

```bash
source ~/xnxx-venv/bin/activate
python3 xnxx_pi2.py
```

### Debug mode (verbose logging to `xnxx_pi.log`)

```bash
python3 xnxx_pi2.py --debug
```

> The `--debug` flag is **required** to enable file logging. Without it, only minimal output goes to the console.

### Custom port

```bash
python3 xnxx_pi2.py --port 8080
```

### All options

```
usage: xnxx_pi2.py [-h] [--debug] [--port PORT] [--host HOST]

optional arguments:
  --debug        Enable verbose file logging to xnxx_pi.log
  --port PORT    Port to listen on (default: 5000)
  --host HOST    Bind host (default: 0.0.0.0 — all interfaces)
```

### Access the server

Once running, open a browser and navigate to:

```
http://<your-pi-hostname>.local:5000
http://<your-pi-ip-address>:5000
```

To find your Pi's IP: `hostname -I`

---

## 🔑 First Login

A default admin account is created automatically on first run:

| Field    | Value      |
|----------|------------|
| Username | `admin`    |
| Password | `admin123` |

**Change this password immediately after first login** via ⚙️ Settings → Change Password, or via the Admin panel.

---

## 📱 Using the App

### Navigation

| Button | Page | Description |
|--------|------|-------------|
| 🏠 | Home | "Best" videos listing |
| ☰ Cats | Categories | Browse 70+ categories |
| ★ Favs | Favorites | Your starred videos |
| 🕐 Hist | History | Your watch history |
| 📁 DLs | Downloads | Local downloaded videos |
| ⚙️ | Settings | App & account settings |
| 👥 | Admin | Admin panel (admins only) |

### Searching

Type any search term in the top search bar and press 🔍. Your recent searches are saved and shown as autocomplete suggestions.

### Playing a video

Click **▶ Play** on any video card. The video streams directly in the browser. Use the **Direct** link button to open the raw video URL in a new tab if needed.

### Downloading a video

Click **⬇** on any video card or the Download button on the player page. Downloads run in the background — you can navigate away and the download continues. Monitor progress on the **📁 DLs** page.

### Favorites

Click the **☆** star icon on any video card to add it to your favorites. Click again to remove. Use **⬇ Download All** on the Favorites page to queue all favorites for download at once.

### Streaming local downloads

On the Downloads page, click **▶** next to any downloaded file to stream it locally from the Pi with full seek support.

---

## 📝 Debug Logging

File logging is **disabled by default** to reduce I/O on the SD card.

To enable it, start the server with the `--debug` flag:

```bash
python3 xnxx_pi2.py --debug
```

Logs are written to `xnxx_pi.log` in the project directory. The log includes:

- All fetch attempts and cache hits/misses
- Per-strategy scraper results (how many videos each strategy found)
- Download start, progress, completion, and errors
- Login/logout events
- User management actions (create, delete, password reset)
- Settings changes

To tail the log in real time:

```bash
tail -f xnxx_pi.log
```

---

## 👑 Admin Panel

Access the admin panel at `/admin` (admin accounts only).

### What admins can do

- **View all users** — see join date, favorite count, history count, download count, and storage used per user
- **Add users** — create new regular or admin accounts
- **Delete users** — remove accounts (data files are preserved by default)
- **Reset passwords** — set a new password for any user inline
- **View any user's data** — browse their favorites, history, and downloads via "admin view" mode (a banner shows when viewing another user's data)
- **Monitor all active downloads** — see every in-progress download across all users, with the ability to cancel any of them
- **Access the scrape debugger** — diagnose scraping issues for any URL

---

## 🔍 Scrape Debugger

Admin-only tool to diagnose why a URL may be returning zero results.

Access it at Admin panel. The debugger shows:

- Whether the page was fetched successfully and HTML length
- Whether the HTML passed validity checks (blocks Cloudflare challenges, age gates, etc.)
- Hit counts for each of the 5 scraping strategies
- All CSS class names found on the page (useful for identifying structural changes)
- All video-pattern anchor hrefs detected
- The first 5 parsed video results
- The first 3000 characters of raw HTML

---

## 📁 Project Structure

```
xnxx-pi-scraper/
├── xnxx_pi2.py          # Main application
├── install.sh           # One-time setup script
├── requirements.txt     # Python dependencies
├── .gitignore
├── xnxx_pi.log          # Debug log (created when --debug is active; gitignored)
└── ~/xnxx-scraper/      # Data root (outside the repo)
    ├── cache/           # Cached HTML pages
    ├── data/
    │   ├── settings.json
    │   └── users.json
    └── users/
        └── <username>/
            ├── favorites.json
            ├── history.json
            ├── searches.json
            └── downloads/
                ├── *.mp4
                └── favorites/
                    └── *.mp4
```

---

## ⚙️ Configuration

All settings are accessible via the ⚙️ Settings page in the UI and persisted to `~/xnxx-scraper/data/settings.json`.

| Setting | Default | Description |
|---------|---------|-------------|
| `quality` | `high` | Default stream/download quality: `high` (HD MP4), `low` (SD MP4), or `hls` |
| `autoplay` | `true` | Auto-start video playback when opening the player |
| `grid_size` | `medium` | Video grid density: `small`, `medium`, or `large` |
| `cache_hours` | `6` | How long to cache scraped HTML pages (1–168 hours) |
| `history_enabled` | `true` | Whether to record watch history |
| `max_history` | `200` | Maximum number of history entries to keep |

---

## 🛠️ Troubleshooting

### Zero results / scraper returning no videos

1. Open the **Scrape Debugger** (`/admin/debug_scrape`) and test the URL.
2. Check the strategy hit counts — if all are 0, the site's HTML structure may have changed.
3. Check `html_length` — if it's very small or the "Valid" field shows NO, the request was blocked (Cloudflare, age gate, or rate limiting).
4. Try with `&force=1` to bypass the cache.
5. Start the server with `--debug` and check `xnxx_pi.log` for detailed fetch/parse logs.

### Downloads stalling or failing

- Check that the Pi has enough free disk space: `df -h ~/xnxx-scraper/`
- Check `xnxx_pi.log` (with `--debug`) for the specific error on the download thread.
- The download uses a fresh session per scrape but a persistent session for the actual file transfer. If downloads fail consistently, try clearing the cache (`/clear_cache`) and retrying.

### Can't access the server from another device

- Confirm the server is bound to `0.0.0.0` (the default): `python3 xnxx_pi2.py`
- Check your Pi's firewall: `sudo ufw status` — port 5000 should be allowed.
- Find the Pi's IP with `hostname -I` and try `http://<ip>:5000` directly.

### "SyntaxError: keyword argument repeated: active"

This is a known Python error that can appear if the code is modified and a local variable named `active` is introduced in the `downloads()` route, clashing with the `active='dls'` keyword argument passed to `render_page()`. The fix is to rename any local variable `active` in that route to `active_dls`.

### Forgot admin password

If locked out, delete `~/xnxx-scraper/data/users.json` and restart the server. The default `admin` / `admin123` account will be recreated automatically.

---

## 📜 License

This project is for personal, private use only. No license is granted for redistribution or commercial use.
