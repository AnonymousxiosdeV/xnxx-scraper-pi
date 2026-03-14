#!/usr/bin/env python3
"""
XNXX Scraper — Raspberry Pi Zero W2 / Bullseye  v3.0
Multi-User Authentication Edition
═══════════════════════════════════════════════════════
Lightweight Flask web server on port 5000.

  http://<pi-hostname>.local:5000
  http://<pi-ip-address>:5000

Usage:
    python3 xnxx_pi2.py               # normal mode (console log only)
    python3 xnxx_pi2.py --debug       # verbose logging to file + console
    python3 xnxx_pi2.py --port 8080   # custom port

Default admin credentials (CHANGE AFTER FIRST LOGIN):
    Username: admin
    Password: admin123

What's new in v3.0:
    - Login screen with session auth
    - Per-user favorites, history, searches, and downloads
    - Admin panel to add/delete/manage users
    - Admin can view any user's data via ?admin_view=<username>
    - Password reset from admin panel
"""

import re
import os
import sys
import json
import time
import hashlib
import logging
import argparse
import threading
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps

import requests
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (Flask, render_template_string, request, redirect,
                   url_for, jsonify, abort, Response, session)

# ══════════════════════════════════════════════════════════════════════════════
# CLI arguments
# ══════════════════════════════════════════════════════════════════════════════
_parser = argparse.ArgumentParser(description='XNXX Pi Scraper v3.0')
_parser.add_argument('--debug', action='store_true', help='Enable verbose file logging')
_parser.add_argument('--port', type=int, default=5000, help='Port to listen on (default 5000)')
_parser.add_argument('--host', default='0.0.0.0', help='Bind host (default 0.0.0.0)')
ARGS = _parser.parse_args()

# ══════════════════════════════════════════════════════════════════════════════
# Logging — file log only when --debug is passed
# ══════════════════════════════════════════════════════════════════════════════
_log_level    = logging.DEBUG if ARGS.debug else logging.INFO
_log_handlers = [logging.StreamHandler()]
if ARGS.debug:
    _log_handlers.append(logging.FileHandler('xnxx_pi.log', encoding='utf-8'))

logging.basicConfig(
    level=_log_level,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%H:%M:%S',
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)
if not ARGS.debug:
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
log.info(f'Starting XNXX Pi v3.0 | debug={ARGS.debug} port={ARGS.port}')

# ══════════════════════════════════════════════════════════════════════════════
# Paths & settings
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR      = os.path.expanduser('~/xnxx-scraper/')
CACHE_DIR     = os.path.join(BASE_DIR, 'cache')
DATA_DIR      = os.path.join(BASE_DIR, 'data')
USERS_DIR     = os.path.join(BASE_DIR, 'users')   # per-user subdirs live here
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
USERS_FILE    = os.path.join(DATA_DIR, 'users.json')

for _d in (CACHE_DIR, DATA_DIR, USERS_DIR):
    os.makedirs(_d, exist_ok=True)

DEFAULT_SETTINGS = {
    'cache_hours':     6,
    'quality':         'high',
    'history_enabled': True,
    'max_history':     200,
    'grid_size':       'medium',
    'autoplay':        True,
}

# ══════════════════════════════════════════════════════════════════════════════
# JSON helpers
# ══════════════════════════════════════════════════════════════════════════════
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log.error(f'load_json {path}: {e}')
    return default.copy() if isinstance(default, dict) else list(default)

def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f'save_json {path}: {e}')

SETTINGS = _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
USERS    = _load_json(USERS_FILE, {})

for k, v in DEFAULT_SETTINGS.items():
    SETTINGS.setdefault(k, v)

# ══════════════════════════════════════════════════════════════════════════════
# User account management
# ══════════════════════════════════════════════════════════════════════════════
_users_lock = threading.Lock()

def _ensure_admin():
    """Create default admin account if no users exist yet."""
    if not USERS:
        USERS['admin'] = {
            'password_hash': generate_password_hash('admin123'),
            'role':          'admin',
            'created_at':    datetime.now().isoformat(timespec='seconds'),
        }
        _save_json(USERS_FILE, USERS)
        log.info('Created default admin — username: admin  password: admin123')

_ensure_admin()

def create_user(username, password, role='user'):
    with _users_lock:
        if username in USERS:
            return False, 'Username already exists'
        USERS[username] = {
            'password_hash': generate_password_hash(password),
            'role':          role,
            'created_at':    datetime.now().isoformat(timespec='seconds'),
        }
        _save_json(USERS_FILE, USERS)
        _ensure_user_dirs(username)
    log.info(f'User created: {username} ({role})')
    return True, 'User created'

def delete_user(username):
    with _users_lock:
        if username not in USERS:
            return False, 'User not found'
        if username == 'admin' and sum(1 for u in USERS.values() if u['role'] == 'admin') <= 1:
            return False, 'Cannot delete the last admin account'
        del USERS[username]
        _save_json(USERS_FILE, USERS)
    log.info(f'User deleted: {username}')
    return True, 'User deleted'

def reset_password(username, new_password):
    with _users_lock:
        if username not in USERS:
            return False, 'User not found'
        USERS[username]['password_hash'] = generate_password_hash(new_password)
        _save_json(USERS_FILE, USERS)
    log.info(f'Password reset for: {username}')
    return True, 'Password updated'

def verify_user(username, password):
    u = USERS.get(username)
    if not u:
        return False
    return check_password_hash(u['password_hash'], password)

# ══════════════════════════════════════════════════════════════════════════════
# Per-user directory & data helpers
# ══════════════════════════════════════════════════════════════════════════════
def user_dir(username):
    return os.path.join(USERS_DIR, username)

def user_download_dir(username):
    return os.path.join(user_dir(username), 'downloads')

def user_fav_dl_dir(username):
    return os.path.join(user_download_dir(username), 'favorites')

def _ensure_user_dirs(username):
    for d in (user_dir(username), user_download_dir(username), user_fav_dl_dir(username)):
        os.makedirs(d, exist_ok=True)

def get_favorites(username):
    return _load_json(os.path.join(user_dir(username), 'favorites.json'), {})

def save_favorites(username, data):
    _save_json(os.path.join(user_dir(username), 'favorites.json'), data)

def get_history(username):
    return _load_json(os.path.join(user_dir(username), 'history.json'), {})

def save_history(username, data):
    _save_json(os.path.join(user_dir(username), 'history.json'), data)

def get_searches(username):
    return _load_json(os.path.join(user_dir(username), 'searches.json'), [])

def save_searches(username, data):
    _save_json(os.path.join(user_dir(username), 'searches.json'), data)

def user_stats(username):
    """Return quick stats dict for a user (used in admin panel)."""
    fav_count  = len(get_favorites(username))
    hist_count = len(get_history(username))
    dl_dir     = user_download_dir(username)
    dl_count   = 0
    dl_bytes   = 0
    if os.path.isdir(dl_dir):
        for root, _, files in os.walk(dl_dir):
            for fn in files:
                if fn.lower().endswith('.mp4'):
                    try:
                        dl_bytes += os.path.getsize(os.path.join(root, fn))
                        dl_count += 1
                    except OSError:
                        pass
    return {
        'favorites':   fav_count,
        'history':     hist_count,
        'downloads':   dl_count,
        'dl_size_str': _fmt_size(dl_bytes),
    }

# ══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ══════════════════════════════════════════════════════════════════════════════
def current_user():
    """Username of the logged-in user, or None."""
    return session.get('username')

def current_role():
    return session.get('role', '')

def is_admin():
    return current_role() == 'admin'

def effective_user():
    """
    The username whose data should be shown.
    Admins can pass ?admin_view=<username> to view another user's data.
    """
    if is_admin():
        av = request.args.get('admin_view', '').strip()
        if av and av in USERS:
            return av
    return current_user()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for('login'))
        if not is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════════════════════════════
# Network — persistent session with connection pooling
# ══════════════════════════════════════════════════════════════════════════════
_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

def _new_session():
    """Create a fresh requests.Session per call — avoids persistent-session fingerprinting."""
    s = requests.Session()
    s.headers.update({
        'User-Agent':      _UA,
        'Accept':          'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer':         'https://www.xnxx.com/',
    })
    s.cookies.update({'age_verified': '1', 'nv': '1', 'nv2': '1'})
    return s

# Keep a long-lived session ONLY for the thumbnail proxy (small images, low risk)
_SESSION = _new_session()
_SESSION_LOCK = threading.Lock()

def _valid_html(html):
    if not html or len(html) < 3000:
        return False
    low = html.lower()
    bad = ('cf-browser-verification', 'cf_chl_opt', 'jschl-answer',
           'enable javascript', 'age_verification', 'age-gate', 'are you 18', 'just a moment')
    return not any(t in low for t in bad)

# ══════════════════════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════════════════════
def _cache_path(url):
    return os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + '.html')

def _cache_valid(p):
    if not os.path.exists(p):
        return False
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(p))
    return age < timedelta(hours=SETTINGS.get('cache_hours', 6))

def fetch_url(url, timeout=25, retries=2):
    cp = _cache_path(url)
    if _cache_valid(cp):
        try:
            with open(cp, 'r', encoding='utf-8') as f:
                html = f.read()
            if _valid_html(html):
                log.debug(f'cache hit: {url}')
                return html
            os.remove(cp)
        except OSError:
            pass

    last_err = None
    for attempt in range(1, retries + 2):
        try:
            r = _new_session().get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            html = r.text
            if _valid_html(html):
                with open(cp, 'w', encoding='utf-8') as f:
                    f.write(html)
            return html
        except Exception as e:
            last_err = e
            log.warning(f'fetch attempt {attempt} failed for {url}: {e}')
            if attempt <= retries:
                time.sleep(1.5 * attempt)

    log.error(f'fetch_url giving up after {retries+1} attempts: {url} — {last_err}')
    return None

# ══════════════════════════════════════════════════════════════════════════════
# Title / filename / size helpers
# ══════════════════════════════════════════════════════════════════════════════
def _title_from_href(href):
    path  = href.split('xnxx.com')[-1]
    parts = path.strip('/').split('/')
    if len(parts) >= 2:
        slug  = re.sub(r'-\d+$', '', parts[1])
        title = slug.replace('-', ' ').strip().title()
        if title:
            return title
    if parts:
        return parts[0].replace('-', ' ').title()
    return 'Untitled'

def _safe_filename(title):
    t = (title or '').strip()
    if not t or t.lower() in ('untitled', 'untitled video', 'unknown'):
        return datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    f = re.sub(r'[^\w\s\-\.\(\)]', '', t)
    f = re.sub(r'\s+', '_', f.strip())
    f = re.sub(r'_+', '_', f).strip('_.-')
    return f[:180] or datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

def _fmt_size(nb):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if nb < 1024:
            return f'{nb:.1f} {unit}'
        nb /= 1024
    return f'{nb:.1f} TB'

def _disk_usage(path):
    try:
        st    = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        return total - free, total
    except Exception:
        return 0, 0

# ══════════════════════════════════════════════════════════════════════════════
# XNXX scraping — multi-strategy, resilient to HTML changes
# ══════════════════════════════════════════════════════════════════════════════
XNXX_BASE = 'https://www.xnxx.com'

# Match any XNXX video link pattern:
#   /video-abc123/some-title
#   /video-abc123/some-title.html
#   /video-abc123
LINK_RE = re.compile(r'^/video-\w+', re.I)

def _abs_link(href):
    if href.startswith('http'):
        return href
    if href.startswith('//'):
        return 'https:' + href
    return XNXX_BASE + href

def _pick_thumb(tag):
    """Extract the best thumbnail URL from an img tag."""
    if not tag:
        return ''
    for attr in ('data-src', 'data-thumb_url', 'data-original', 'src'):
        val = tag.get(attr, '')
        if val and not val.endswith('blank.gif') and not val.startswith('data:'):
            return _abs_link(val) if val.startswith('//') else val
    return ''

def _xnxx_urls(html):
    """Extract high/low/hls video stream URLs from a video page."""
    def g(pat):
        m = re.search(pat, html)
        return m.group(1) if m else None

    patterns = [
        # JS player calls
        (r"html5player\.setVideoUrlHigh\('([^']+)'\)",   'hi'),
        (r"html5player\.setVideoUrlLow\('([^']+)'\)",    'lo'),
        (r"html5player\.setVideoHLS\('([^']+)'\)",       'hls'),
        # JSON-style
        (r'"videoUrlHigh"\s*:\s*"([^"]+)"',              'hi'),
        (r'"videoUrlLow"\s*:\s*"([^"]+)"',               'lo'),
        (r'"videoHLS"\s*:\s*"([^"]+)"',                  'hls'),
        (r'setVideoUrlHigh\("([^"]+)"\)',                 'hi'),
        (r'setVideoUrlLow\("([^"]+)"\)',                  'lo'),
        (r'setVideoHLS\("([^"]+)"\)',                     'hls'),
        # data attributes / nuxt
        (r'"hd_src"\s*:\s*"([^"]+\.mp4[^"]*)"',         'hi'),
        (r'"low_src"\s*:\s*"([^"]+\.mp4[^"]*)"',        'lo'),
        (r'"hls_src"\s*:\s*"([^"]+\.m3u8[^"]*)"',       'hls'),
    ]
    hi = lo = hls = None
    for pat, key in patterns:
        m = re.search(pat, html)
        if m:
            val = m.group(1).replace('\\/', '/')
            if key == 'hi'  and not hi:  hi  = val
            if key == 'lo'  and not lo:  lo  = val
            if key == 'hls' and not hls: hls = val
    return hi, lo, hls


def _extract_videos_from_html(html, url=''):
    """
    Try every known strategy to pull video cards out of a listing page.
    Returns list of dicts: {link, thumb, title, duration}
    """
    soup = BeautifulSoup(html, 'html.parser')
    seen, videos = set(), []

    def _add(href, thumb='', title='', duration=''):
        link = _abs_link(href)
        if link in seen:
            return
        seen.add(link)
        if not title:
            title = _title_from_href(href)
        videos.append({'link': link, 'thumb': thumb,
                       'title': title, 'duration': duration})

    # ── Strategy 1: classic div.thumb-block ──────────────────────────────────
    containers = soup.find_all('div', class_='thumb-block')
    log.debug(f'Strategy 1 (thumb-block): {len(containers)} hits')
    for c in containers:
        a = c.find('a', href=LINK_RE)
        if not a:
            continue
        img      = c.find('img')
        dur_el   = c.find(class_=re.compile(r'duration|time', re.I))
        _add(a['href'], _pick_thumb(img),
             a.get('title', ''), dur_el.get_text(strip=True) if dur_el else '')

    if videos:
        log.info(f'scrape_page strategy 1: {len(videos)} videos @ {url}')
        return soup, videos

    # ── Strategy 2: any element whose class contains "thumb" ─────────────────
    containers = soup.find_all(class_=re.compile(r'\bthumb\b', re.I))
    log.debug(f'Strategy 2 (class~=thumb): {len(containers)} hits')
    for c in containers:
        a = c.find('a', href=LINK_RE)
        if not a:
            continue
        img    = c.find('img')
        dur_el = c.find(class_=re.compile(r'duration|time|length', re.I))
        _add(a['href'], _pick_thumb(img),
             a.get('title', ''), dur_el.get_text(strip=True) if dur_el else '')

    if videos:
        log.info(f'scrape_page strategy 2: {len(videos)} videos @ {url}')
        return soup, videos

    # ── Strategy 3: mozaique grid (common XNXX wrapper) ──────────────────────
    moz = soup.find(class_=re.compile(r'mozaique|video-?list|list-?video', re.I))
    if moz:
        for a in moz.find_all('a', href=LINK_RE):
            img    = a.find('img') or (a.parent.find('img') if a.parent else None)
            dur_el = a.find(class_=re.compile(r'duration|time|length', re.I))
            if not dur_el and a.parent:
                dur_el = a.parent.find(class_=re.compile(r'duration|time|length', re.I))
            _add(a['href'], _pick_thumb(img),
                 a.get('title', ''), dur_el.get_text(strip=True) if dur_el else '')
    log.debug(f'Strategy 3 (mozaique): {len(videos)} hits')

    if videos:
        log.info(f'scrape_page strategy 3: {len(videos)} videos @ {url}')
        return soup, videos

    # ── Strategy 4: all anchors matching video pattern site-wide ─────────────
    for a in soup.find_all('a', href=LINK_RE):
        img    = a.find('img')
        par    = a.parent or a
        dur_el = par.find(class_=re.compile(r'duration|time|length', re.I))
        _add(a['href'], _pick_thumb(img),
             a.get('title', ''), dur_el.get_text(strip=True) if dur_el else '')
    log.debug(f'Strategy 4 (all video anchors): {len(videos)} hits')

    if videos:
        log.info(f'scrape_page strategy 4: {len(videos)} videos @ {url}')
        return soup, videos

    # ── Strategy 5: JSON embedded in page scripts ─────────────────────────────
    # Some modern XNXX pages embed data as JSON in <script> tags
    for script in soup.find_all('script'):
        text = script.string or ''
        if 'video' not in text.lower():
            continue
        # Look for arrays of objects with url + thumb fields
        for m in re.finditer(
            r'\{[^{}]*?"(?:url|link|href)"\s*:\s*"(/video[^"]+)"[^{}]*?\}', text
        ):
            block = m.group(0)
            link_m = re.search(r'"(?:url|link|href)"\s*:\s*"(/video[^"]+)"', block)
            thumb_m = re.search(r'"(?:thumb|thumbnail|image|img)"\s*:\s*"([^"]+)"', block)
            title_m = re.search(r'"(?:title|name)"\s*:\s*"([^"]+)"', block)
            if link_m:
                _add(link_m.group(1),
                     thumb_m.group(1) if thumb_m else '',
                     title_m.group(1) if title_m else '')

    log.debug(f'Strategy 5 (JSON scripts): {len(videos)} hits')
    if videos:
        log.info(f'scrape_page strategy 5: {len(videos)} videos @ {url}')

    if not videos:
        log.warning(f'scrape_page: NO videos found @ {url}')
        # Log a snippet of the HTML to help diagnose
        log.debug(f'HTML snippet (first 2000 chars): {html[:2000]}')

    return soup, videos


def scrape_page(url):
    html = fetch_url(url)
    if not html:
        log.error(f'scrape_page: fetch returned nothing for {url}')
        return [], None
    if not _valid_html(html):
        log.warning(f'scrape_page: HTML failed validation for {url} (len={len(html)})')
        return [], None

    soup, videos = _extract_videos_from_html(html, url)

    # ── Next page ─────────────────────────────────────────────────────────────
    next_url = None
    # Try rel=next first
    cand = soup.find('a', rel='next')
    if not cand:
        cand = soup.find('a', rel=['next'])
    if not cand:
        # pagination buttons with class containing "next"
        cand = soup.find('a', class_=re.compile(r'\bnext\b', re.I))
    if not cand:
        # numbered pagination — find current page number and construct next
        m = re.search(r'/(\d+)(?:/?\s*$|\?)', url)
        if m:
            cur = int(m.group(1))
            next_candidate = re.sub(r'/\d+(/?)$', f'/{cur+1}\\1', url)
            if next_candidate != url:
                next_url = next_candidate
    if cand and cand.get('href'):
        h        = cand['href']
        next_url = _abs_link(h)

    return videos, next_url


def get_video_details(video_url):
    html = fetch_url(video_url)
    if not html:
        return 'Unavailable', None, None, None

    soup  = BeautifulSoup(html, 'html.parser')
    meta  = soup.find('meta', {'name': 'description'})
    h1    = soup.find('h1')
    title = h1.get_text(strip=True) if h1 else ''
    desc  = (meta.get('content') if meta else '') or title

    hi, lo, hls = _xnxx_urls(html)
    return desc, hi, lo, hls

# ══════════════════════════════════════════════════════════════════════════════
# History & search (per-user)
# ══════════════════════════════════════════════════════════════════════════════
_history_lock = threading.Lock()

def record_watch(username, url, title, thumb=''):
    if not SETTINGS.get('history_enabled', True):
        return
    with _history_lock:
        hist = get_history(username)
        hist[url] = {
            'link':       url,
            'title':      title,
            'thumb':      thumb or '',
            'visited_at': datetime.now().isoformat(timespec='seconds'),
        }
        max_h = SETTINGS.get('max_history', 200)
        if len(hist) > max_h:
            oldest = sorted(hist.items(), key=lambda x: x[1].get('visited_at', ''))
            for k, _ in oldest[:len(hist) - max_h]:
                del hist[k]
        save_history(username, hist)

def record_search(username, q):
    if not q:
        return
    with _history_lock:
        searches = get_searches(username)
        if q in searches:
            searches.remove(q)
        searches.insert(0, q)
        del searches[30:]
        save_searches(username, searches)

# ══════════════════════════════════════════════════════════════════════════════
# Background download tracker (per-user)
# ══════════════════════════════════════════════════════════════════════════════
DOWNLOADS = {}   # token -> {username, title, status, pct, path, speed, eta, cancel, error}
_dl_lock  = threading.Lock()

def _dl_thread(token, username, video_url, title, dest_dir):
    def upd(**kw):
        with _dl_lock:
            DOWNLOADS[token].update(kw)

    upd(status='resolving', pct=0, speed='', eta='')
    _, hi, lo, _ = get_video_details(video_url)

    q      = SETTINGS.get('quality', 'high')
    dl_url = (hi if q != 'low' else lo) or hi or lo
    if not dl_url:
        upd(status='error', error='No downloadable URL found')
        log.error(f'[{username}] dl_thread {title}: no URL')
        return

    os.makedirs(dest_dir, exist_ok=True)
    filename = _safe_filename(title) + '.mp4'
    path     = os.path.join(dest_dir, filename)
    upd(status='downloading', pct=1, path=path)
    log.info(f'[{username}] Downloading → {path}')

    try:
        with _SESSION_LOCK:
            r = _SESSION.get(dl_url, stream=True, timeout=60)
        r.raise_for_status()
        total   = int(r.headers.get('content-length', 0))
        done    = 0
        t_start = time.time()

        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=131072):
                with _dl_lock:
                    if DOWNLOADS[token].get('cancel'):
                        log.info(f'[{username}] Download cancelled: {path}')
                        break
                if chunk:
                    f.write(chunk)
                    done    += len(chunk)
                    elapsed  = time.time() - t_start
                    pct      = int(done / total * 100) if total else 0
                    speed    = done / elapsed if elapsed > 0.1 else 0
                    eta_s    = int((total - done) / speed) if (speed > 0 and total) else 0
                    upd(pct=pct,
                        speed=_fmt_size(speed) + '/s' if speed > 0 else '',
                        eta=f'{eta_s//60}m{eta_s%60:02d}s' if eta_s > 0 else '')

        with _dl_lock:
            cancelled = DOWNLOADS[token].get('cancel', False)

        if cancelled:
            try:    os.remove(path)
            except OSError: pass
            upd(status='cancelled', pct=0)
        else:
            upd(status='done', pct=100, speed='', eta='')
            log.info(f'[{username}] Download complete: {path}')

    except Exception as e:
        log.error(f'[{username}] Download error {video_url}: {e}')
        upd(status='error', error=str(e), speed='', eta='')
        try:    os.remove(path)
        except OSError: pass

# ══════════════════════════════════════════════════════════════════════════════
# XNXX categories
# ══════════════════════════════════════════════════════════════════════════════
CATEGORIES = [
    ('🎥 Amateur',       '/search/amateur/0'),
    ('🍑 Anal',          '/search/anal/0'),
    ('🌏 Asian',         '/search/asian/0'),
    ('🍩 BBW',           '/search/bbw/0'),
    ('⛓️ BDSM',          '/search/bdsm/0'),
    ('🍑 Big Ass',       '/search/big-ass/0'),
    ('🍆 Big Dick',      '/search/big-dick/0'),
    ('🍒 Big Tits',      '/search/big-tits/0'),
    ('👱 Blonde',        '/search/blonde/0'),
    ('💋 Blowjob',       '/search/blowjob/0'),
    ('🤎 Brunette',      '/search/brunette/0'),
    ('🎬 Casting',       '/search/casting/0'),
    ('💔 Cheating',      '/search/cheating/0'),
    ('🎓 College',       '/search/college/0'),
    ('📼 Compilation',   '/search/compilation/0'),
    ('🎭 Cosplay',       '/search/cosplay/0'),
    ('🍦 Creampie',      '/search/creampie/0'),
    ('💦 Cumshot',       '/search/cumshot/0'),
    ('🇨🇿 Czech',        '/search/czech/0'),
    ('😮 Deep Throat',   '/search/deep-throat/0'),
    ('✌️ Double Pen',    '/search/double-penetration/0'),
    ('🖤 Ebony',         '/search/ebony/0'),
    ('🇪🇺 European',     '/search/european/0'),
    ('😏 Facial',        '/search/facial/0'),
    ('🦶 Feet',          '/search/feet/0'),
    ('👸 Femdom',        '/search/femdom/0'),
    ('🔗 Fetish',        '/search/fetish/0'),
    ('👥 Gangbang',      '/search/gangbang/0'),
    ('🇩🇪 German',       '/search/german/0'),
    ('👴 Granny',        '/search/granny/0'),
    ('🎉 Group Sex',     '/search/group-sex/0'),
    ('✋ Handjob',       '/search/handjob/0'),
    ('🔥 Hardcore',      '/search/hardcore/0'),
    ('🎞️ HD',            '/search/hd/0'),
    ('🌸 Hentai',        '/search/hentai/0'),
    ('👁️ Hidden Cam',    '/search/hidden-cam/0'),
    ('🏠 Homemade',      '/search/homemade/0'),
    ('🇮🇳 Indian',       '/search/indian/0'),
    ('🌎 Interracial',   '/search/interracial/0'),
    ('🇮🇹 Italian',      '/search/italian/0'),
    ('🇯🇵 Japanese',     '/search/japanese/0'),
    ('🇰🇷 Korean',       '/search/korean/0'),
    ('💃 Latina',        '/search/latina/0'),
    ('♀️ Lesbian',       '/search/lesbian/0'),
    ('💆 Massage',       '/search/massage/0'),
    ('💅 Masturbation',  '/search/masturbation/0'),
    ('🧓 Mature',        '/search/mature/0'),
    ('👩 MILF',          '/search/milf/0'),
    ('💉 Nurse',         '/search/nurse/0'),
    ('🏢 Office',        '/search/office/0'),
    ('🍾 Orgy',          '/search/orgy/0'),
    ('🌲 Outdoor',       '/search/outdoor/0'),
    ('📷 POV',           '/search/pov/0'),
    ('⭐ Pornstar',      '/search/pornstar/0'),
    ('🏙️ Public',        '/search/public/0'),
    ('🔴 Redhead',       '/search/redhead/0'),
    ('🇷🇺 Russian',      '/search/russian/0'),
    ('👠 Shemale',       '/search/shemale/0'),
    ('🌺 Small Tits',    '/search/small-tits/0'),
    ('🧘 Solo',          '/search/solo/0'),
    ('💧 Squirt',        '/search/squirt/0'),
    ('🧦 Stockings',     '/search/stockings/0'),
    ('🔧 Strapon',       '/search/strapon/0'),
    ('💃 Strip',         '/search/strip/0'),
    ('🔄 Swinger',       '/search/swinger/0'),
    ('🍎 Teacher',       '/search/teacher/0'),
    ('🌱 Teen',          '/search/teen/0'),
    ('🔱 Threesome',     '/search/threesome/0'),
    ('🎮 Toys',          '/search/toys/0'),
    ('👔 Uniform',       '/search/uniform/0'),
    ('📀 Vintage',       '/search/vintage/0'),
    ('👀 Voyeur',        '/search/voyeur/0'),
    ('📹 Webcam',        '/search/webcam/0'),
]

# ══════════════════════════════════════════════════════════════════════════════
# Flask app
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = hashlib.sha256(b'xnxx-pi-v3-anonymousx-secret-key').hexdigest()

@app.template_filter('urlencode')
def _urlencode_filter(s):
    return urllib.parse.quote_plus(str(s))

# ══════════════════════════════════════════════════════════════════════════════
# Login page (standalone — no nav, no auth required)
# ══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d0d1a">
<title>AnonymousX's XNXX Scraper — Sign In</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(ellipse at top,#1c1c3e 0%,#0d0d1a 60%);color:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:#1c1c2e;border-radius:16px;padding:42px 38px;width:100%;max-width:400px;border:1px solid #2a2a45;box-shadow:0 20px 64px #000a}
.logo{font-size:30px;font-weight:900;color:#e94560;text-align:center;margin-bottom:6px;letter-spacing:-0.5px}
.logo span{color:#f0f0f0;font-weight:300}
.subtitle{text-align:center;color:#9a9ab8;font-size:13px;margin-bottom:32px}
.form-group{margin-bottom:18px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#9a9ab8;margin-bottom:7px}
input[type=text],input[type=password]{width:100%;padding:12px 15px;border-radius:9px;border:1px solid #2a2a45;background:#111128;color:#f0f0f0;font-size:15px;outline:none;transition:border-color .2s,background .2s}
input:focus{border-color:#e94560;background:#151535}
input::placeholder{color:#4a4a6a}
.btn-login{width:100%;padding:13px;border-radius:9px;border:none;background:linear-gradient(135deg,#e94560,#c0304d);color:#fff;font-size:15px;font-weight:800;cursor:pointer;margin-top:6px;transition:opacity .15s,transform .1s;letter-spacing:.02em}
.btn-login:hover{opacity:.9}
.btn-login:active{transform:scale(.98)}
.error{background:#2d1217;border:1px solid #6b2030;border-left:3px solid #e94560;padding:11px 13px;border-radius:8px;font-size:13px;margin-bottom:20px;color:#f87b8e}
.pi-badge{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:22px;font-size:12px;color:#4a4a6a}
</style>
</head>
<body>
<div class="card">
  <div class="logo">XNXX<span>Pi</span></div>
  <div class="subtitle">Private Media Server</div>
  {% if error %}
  <div class="error">⚠ {{ error }}</div>
  {% endif %}
  <form method="post" autocomplete="off">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" value="{{ username|default('') }}"
             autofocus autocomplete="username" placeholder="Enter username">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password"
             autocomplete="current-password" placeholder="Enter password">
    </div>
    <input type="hidden" name="next" value="{{ next|default('') }}">
    <button type="submit" class="btn-login">Sign In →</button>
  </form>
  <div class="pi-badge">🍓 Raspberry Pi Media Server</div>
</div>
</body>
</html>
"""

# ══════════════════════════════════════════════════════════════════════════════
# Base HTML template (all authenticated pages)
# ══════════════════════════════════════════════════════════════════════════════
_GRID_COLS = {
    'small':  'repeat(auto-fill,minmax(130px,1fr))',
    'medium': 'repeat(auto-fill,minmax(175px,1fr))',
    'large':  'repeat(auto-fill,minmax(240px,1fr))',
}

BASE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d0d1a">
<title>AnonymousX's XNXX Scraper{% if page_title %} · {{ page_title }}{% endif %}</title>
<style>
:root{
  --bg:#0d0d1a;--surface:#1c1c2e;--surface2:#16213e;--surface3:#222244;
  --accent:#e94560;--accent2:#0f3460;--accent3:#1a7a4a;--accent4:#7b2d8b;
  --text:#f0f0f0;--muted:#9a9ab8;--border:#2a2a45;
  --grid-cols:GRID_PLACEHOLDER;
  --radius:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
img{display:block;max-width:100%}

/* ── Nav ── */
nav{background:var(--surface2);padding:9px 12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:0;z-index:200;border-bottom:1px solid var(--border);box-shadow:0 2px 12px #0006}
.logo{font-weight:900;font-size:20px;color:var(--accent);letter-spacing:-0.5px;white-space:nowrap;text-decoration:none!important}
.logo span{color:var(--text);font-weight:300}
.navlinks{display:flex;gap:5px;flex-wrap:wrap}
nav form{display:flex;gap:6px;flex:1;min-width:160px}
nav input[type=text]{flex:1;padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:#fff1;color:var(--text);font-size:14px;outline:none;transition:border-color .2s,background .2s}
nav input[type=text]:focus{border-color:var(--accent);background:#fff2}
nav input::placeholder{color:var(--muted)}
.nav-user{display:flex;align-items:center;gap:7px;flex-shrink:0;margin-left:auto}
.nav-user-name{font-size:12px;color:var(--muted);white-space:nowrap}
.admin-banner{background:linear-gradient(90deg,var(--accent4)33,transparent);border-bottom:1px solid var(--accent4)55;padding:5px 12px;font-size:12px;color:#d4a0e8;display:flex;align-items:center;gap:6px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:4px;padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:700;color:#fff;text-align:center;transition:opacity .15s,transform .1s;white-space:nowrap;text-decoration:none!important;user-select:none}
.btn:hover{opacity:.88;text-decoration:none}
.btn:active{transform:scale(.96)}
.btn-red{background:var(--accent)}
.btn-blue{background:var(--accent2)}
.btn-green{background:var(--accent3)}
.btn-purple{background:var(--accent4)}
.btn-dark{background:var(--surface3)}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--muted)}
.btn-orange{background:#c05a00}
.btn-sm{padding:5px 10px;font-size:12px;border-radius:6px}
.btn-xs{padding:3px 7px;font-size:11px;border-radius:5px}
.btn[disabled]{opacity:.4;pointer-events:none}

/* ── Grid ── */
.grid{display:grid;grid-template-columns:var(--grid-cols);gap:10px;padding:12px}
.card{background:var(--surface);border-radius:var(--radius);overflow:hidden;position:relative;transition:transform .15s,box-shadow .15s;border:1px solid transparent}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 24px #0007;border-color:var(--border)}
.thumb-wrap{position:relative;width:100%;aspect-ratio:16/9;background:var(--surface3);overflow:hidden}
.thumb-wrap img{width:100%;height:100%;object-fit:cover;transition:transform .3s}
.card:hover .thumb-wrap img{transform:scale(1.04)}
.thumb-placeholder{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--muted)}
.card .duration{position:absolute;bottom:5px;right:5px;background:#000b;color:#fff;font-size:11px;font-weight:700;padding:2px 5px;border-radius:4px}
.card .watched-dot{position:absolute;top:5px;left:5px;width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 5px var(--accent)}
.star{position:absolute;top:5px;right:5px;font-size:20px;cursor:pointer;text-shadow:0 0 4px #000;background:#0007;border:none;color:#bbb;padding:3px 5px;border-radius:6px;transition:color .15s,background .15s;line-height:1}
.star:hover{background:#000a}
.star.on{color:#e94560}
.card .info{padding:8px 9px}
.card .title{font-size:12px;font-weight:600;line-height:1.4;color:var(--text);margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .actions{display:flex;gap:5px;flex-wrap:wrap}

/* ── Categories ── */
.catgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:8px;padding:12px}
.catcard{background:var(--surface);border-radius:var(--radius);padding:14px 10px;text-align:center;font-weight:700;font-size:13px;color:var(--text);cursor:pointer;border:1px solid var(--border);transition:background .15s,border-color .15s,transform .1s;text-decoration:none!important}
.catcard:hover{background:var(--surface3);border-color:var(--accent);transform:translateY(-1px)}
.catcard .cat-emoji{font-size:20px;margin-bottom:4px;display:block}

/* ── Tables ── */
.dl-table{width:100%;border-collapse:collapse}
.dl-table th,.dl-table td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);font-size:13px}
.dl-table th{background:var(--surface2);font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.dl-table tr:hover td{background:var(--surface)}
.badge{background:var(--surface2);color:var(--accent);border-radius:5px;padding:2px 7px;font-size:11px;font-weight:700}
.badge-green{background:#1a3a26;color:#5cf09c}
.badge-purple{background:#2a1a3a;color:#c080f0}
.badge-blue{background:#0f2040;color:#60a8f0}

/* ── Progress ── */
.prog-wrap{background:#fff1;border-radius:99px;height:6px;overflow:hidden}
.prog-bar{height:6px;background:linear-gradient(90deg,var(--accent),#ff6b8a);border-radius:99px;transition:width .4s ease}

/* ── Storage bar ── */
.storage-bar-wrap{background:var(--surface3);border-radius:99px;height:8px;overflow:hidden;margin:6px 0}
.storage-bar{height:8px;border-radius:99px;background:linear-gradient(90deg,var(--accent2),var(--accent))}

/* ── Pagination ── */
.pages{display:flex;gap:8px;padding:12px;flex-wrap:wrap;align-items:center;justify-content:center}

/* ── Banner / alerts ── */
.banner{padding:10px 14px;background:var(--surface);border-left:3px solid var(--accent);margin:10px 12px;border-radius:6px;font-size:13px}
.banner-ok{border-color:var(--accent3)}
.banner-warn{border-color:#f0a500}
.banner-err{border-color:var(--accent)}

/* ── Player ── */
video{width:100%;background:#000;display:block;border-radius:0 0 8px 8px}
.player-wrap{background:#000;max-width:1024px;margin:0 auto;border-radius:8px;overflow:hidden;box-shadow:0 8px 32px #0009}
.player-title{padding:12px 14px;font-weight:700;font-size:15px;background:var(--surface2);border-radius:8px 8px 0 0}
.player-actions{padding:10px 14px;display:flex;gap:8px;flex-wrap:wrap;background:var(--surface);border-radius:0 0 8px 8px}

/* ── Empty states ── */
.empty{text-align:center;padding:60px 20px;color:var(--muted);font-size:16px;line-height:1.8}
.empty .icon{font-size:48px;margin-bottom:12px}

/* ── Toasts ── */
#toast-container{position:fixed;bottom:20px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface2);color:var(--text);border-left:3px solid var(--accent);padding:10px 14px;border-radius:8px;font-size:13px;font-weight:600;box-shadow:0 4px 16px #0008;animation:slideIn .25s ease;min-width:180px;pointer-events:auto}
.toast.ok{border-color:var(--accent3)}
.toast.warn{border-color:#f0a500}
@keyframes slideIn{from{transform:translateX(60px);opacity:0}to{transform:none;opacity:1}}
@keyframes fadeOut{to{opacity:0;transform:translateX(60px)}}

/* ── Settings form ── */
.settings-form{max-width:520px;margin:16px auto;padding:0 12px}
.form-row{margin-bottom:18px}
.form-row label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.form-row select,.form-row input[type=number],.form-row input[type=text],.form-row input[type=password]{width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:14px;outline:none}
.form-row select:focus,.form-row input:focus{border-color:var(--accent)}
.toggle-wrap{display:flex;align-items:center;gap:10px}
.toggle{position:relative;width:44px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{position:absolute;inset:0;background:var(--border);border-radius:99px;transition:background .2s}
.toggle input:checked + .toggle-track{background:var(--accent3)}
.toggle-knob{position:absolute;top:3px;left:3px;width:18px;height:18px;background:#fff;border-radius:50%;transition:transform .2s}
.toggle input:checked ~ .toggle-knob{transform:translateX(20px)}
.section-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);padding:16px 12px 6px;border-top:1px solid var(--border)}
.section-title:first-child{border-top:none;padding-top:8px}

/* ── History ── */
.hist-item{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border);font-size:13px;transition:background .1s}
.hist-item:hover{background:var(--surface)}
.hist-thumb{width:64px;height:36px;object-fit:cover;border-radius:5px;background:var(--surface3);flex-shrink:0}
.hist-info{flex:1;min-width:0}
.hist-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hist-time{font-size:11px;color:var(--muted);margin-top:2px}
.hist-actions{display:flex;gap:5px;flex-shrink:0}

/* ── Admin ── */
.admin-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;padding:12px}
.user-card{background:var(--surface);border-radius:var(--radius);padding:16px;border:1px solid var(--border);transition:border-color .15s}
.user-card:hover{border-color:var(--accent4)}
.user-card-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.user-avatar{width:40px;height:40px;border-radius:50%;background:var(--accent4);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.user-card-name{font-weight:800;font-size:16px}
.user-card-role{font-size:11px;color:var(--muted)}
.user-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:12px}
.stat-box{background:var(--surface2);border-radius:7px;padding:8px;text-align:center}
.stat-box .stat-val{font-size:18px;font-weight:800;color:var(--accent)}
.stat-box .stat-lbl{font-size:10px;color:var(--muted);margin-top:2px}
.user-card-actions{display:flex;gap:6px;flex-wrap:wrap}
.add-user-form{background:var(--surface);border-radius:var(--radius);padding:20px;margin:12px;border:1px solid var(--accent4)66;max-width:460px}
.add-user-form h3{font-size:14px;font-weight:700;color:var(--accent4);margin-bottom:14px;text-transform:uppercase;letter-spacing:.05em}
.inline-fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}

/* ── Scroll to top ── */
#scrolltop{position:fixed;bottom:70px;right:16px;width:40px;height:40px;border-radius:50%;background:var(--accent);border:none;color:#fff;font-size:18px;cursor:pointer;display:none;align-items:center;justify-content:center;z-index:300;box-shadow:0 3px 10px #0005;transition:opacity .2s}
#scrolltop.show{display:flex}

/* ── Responsive ── */
@media(max-width:480px){
  nav{gap:5px;padding:7px 8px}
  .logo{font-size:16px}
  .navlinks .btn-sm{padding:4px 8px;font-size:11px}
  .grid{gap:7px;padding:8px}
  .inline-fields{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div id="toast-container"></div>

{% if admin_viewing %}
<div class="admin-banner">
  👁 Admin view: viewing data for <strong>{{ admin_viewing }}</strong>
  &nbsp;·&nbsp; <a href="{{ request.path }}" style="color:#d4a0e8">← Back to your data</a>
</div>
{% endif %}

<nav>
  <a class="logo" href="/">XNXX<span>Pi</span></a>
  <div class="navlinks">
    <a class="btn btn-dark btn-sm {% if active=='home' %}btn-red{% endif %}" href="/">🏠</a>
    <a class="btn btn-dark btn-sm {% if active=='cats' %}btn-red{% endif %}" href="/categories">☰ Cats</a>
    <a class="btn btn-dark btn-sm {% if active=='favs' %}btn-red{% endif %}" href="/favorites">★ Favs</a>
    <a class="btn btn-dark btn-sm {% if active=='hist' %}btn-red{% endif %}" href="/history">🕐 Hist</a>
    <a class="btn btn-dark btn-sm {% if active=='dls' %}btn-red{% endif %}" href="/downloads">📁 DLs</a>
    <a class="btn btn-dark btn-sm {% if active=='settings' %}btn-red{% endif %}" href="/settings">⚙️</a>
    {% if is_admin %}
    <a class="btn btn-purple btn-sm {% if active=='admin' %}btn-red{% endif %}" href="/admin">👥</a>
    {% endif %}
  </div>
  <form action="/search" method="get" autocomplete="off">
    <input type="text" name="q" placeholder="Search XNXX…"
           value="{{ query|default('') }}" list="recent-searches">
    <datalist id="recent-searches">
      {% for s in recent_searches %}<option value="{{ s|e }}">{% endfor %}
    </datalist>
    <button class="btn btn-red" type="submit">🔍</button>
  </form>
  <div class="nav-user">
    <span class="nav-user-name">👤 {{ current_user_name }}</span>
    {% if is_admin %}<span class="badge badge-purple">admin</span>{% endif %}
    <a class="btn btn-ghost btn-sm" href="/logout" title="Sign out">⏻</a>
  </div>
</nav>

{% block content %}{% endblock %}

<button id="scrolltop" onclick="window.scrollTo({top:0,behavior:'smooth'})" title="Back to top">↑</button>

<script>
function toast(msg,type=''){
  const c=document.getElementById('toast-container');
  const t=document.createElement('div');
  t.className='toast'+(type?' '+type:'');
  t.textContent=msg;
  c.appendChild(t);
  setTimeout(()=>{t.style.animation='fadeOut .3s forwards';setTimeout(()=>t.remove(),300)},3000);
}
function toggleFav(btn,encodedUrl,title,thumb){
  fetch('/fav_toggle?url='+encodedUrl+'&title='+encodeURIComponent(title)+'&thumb='+encodeURIComponent(thumb||''),{method:'POST'})
    .then(r=>r.json())
    .then(d=>{
      btn.textContent=d.is_fav?'★':'☆';
      btn.classList.toggle('on',d.is_fav);
      toast(d.is_fav?'Added to favorites':'Removed from favorites',d.is_fav?'ok':'');
    });
}
const stb=document.getElementById('scrolltop');
window.addEventListener('scroll',()=>stb.classList.toggle('show',window.scrollY>400),{passive:true});
</script>
</body>
</html>
"""

# ── Video card template ──────────────────────────────────────────────────────
CARD_HTML = """
<div class="card">
  <div class="thumb-wrap">
    {% if v.thumb %}
    <img src="/thumb?url={{ v.thumb|urlencode }}" loading="lazy" alt="">
    {% else %}
    <div class="thumb-placeholder">🎬</div>
    {% endif %}
    {% if v.duration %}<span class="duration">{{ v.duration }}</span>{% endif %}
    {% if v.link in watched %}<span class="watched-dot" title="Watched"></span>{% endif %}
  </div>
  <button class="star {{ 'on' if v.link in favorites else '' }}"
          onclick="toggleFav(this,'{{ v.link|urlencode }}','{{ v.title|e }}','{{ (v.thumb or '')|urlencode }}')"
          title="Favorite">{{ '★' if v.link in favorites else '☆' }}</button>
  <div class="info">
    <div class="title" title="{{ v.title|e }}">{{ v.title }}</div>
    <div class="actions">
      <a class="btn btn-green btn-sm" href="/play?url={{ v.link|urlencode }}{% if listing_url %}&ref_url={{ listing_url|urlencode }}{% endif %}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">▶ Play</a>
      <a class="btn btn-blue btn-sm" href="/download?url={{ v.link|urlencode }}&title={{ v.title|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">⬇</a>
    </div>
  </div>
</div>
"""

# ══════════════════════════════════════════════════════════════════════════════
# render_page helper
# ══════════════════════════════════════════════════════════════════════════════
def render_page(page_content, extra_js='', active='', **context):
    username      = current_user() or ''
    eff_username  = effective_user() or username
    admin_viewing = eff_username if eff_username != username else ''

    favorites = get_favorites(eff_username) if eff_username else {}
    watched   = get_history(eff_username)   if eff_username else {}
    searches  = get_searches(eff_username)[:10] if eff_username else []

    grid_col = _GRID_COLS.get(SETTINGS.get('grid_size', 'medium'), _GRID_COLS['medium'])
    block = f"""{{% block content %}}\n{page_content}\n{extra_js}\n{{% endblock %}}"""
    tmpl  = BASE_HTML.replace('{% block content %}{% endblock %}', block)
    tmpl  = tmpl.replace('GRID_PLACEHOLDER', grid_col)

    context.setdefault('query', '')
    context.setdefault('page_title', '')
    context.setdefault('listing_url', '')
    context['favorites']       = favorites
    context['watched']         = watched
    context['recent_searches'] = searches
    context['active']          = active
    context['current_user_name'] = username
    context['is_admin']        = is_admin()
    context['admin_viewing']   = admin_viewing
    context['request']         = request
    return render_template_string(tmpl, **context)

# ══════════════════════════════════════════════════════════════════════════════
# Thumbnail proxy
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/thumb')
@login_required
def thumb_proxy():
    url = request.args.get('url', '')
    if not url or not url.startswith('http'):
        abort(400)
    try:
        with _SESSION_LOCK:
            r = _SESSION.get(url, timeout=8, stream=True)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', 'image/jpeg')
        return Response(r.content, content_type=ct,
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        log.debug(f'thumb_proxy failed {url}: {e}')
        import base64
        gif1px = base64.b64decode('R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==')
        return Response(gif1px, content_type='image/gif',
                        headers={'Cache-Control': 'public, max-age=60'})

# ══════════════════════════════════════════════════════════════════════════════
# Auth routes
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect('/')
    error    = ''
    username = ''
    next_url = request.args.get('next', '/')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        next_url = request.form.get('next', '/') or '/'
        if verify_user(username, password):
            session.clear()
            session['username'] = username
            session['role']     = USERS[username]['role']
            session.permanent   = True
            log.info(f'Login: {username}')
            # Safety: only allow local redirects
            if not next_url.startswith('/'):
                next_url = '/'
            return redirect(next_url)
        else:
            error = 'Invalid username or password'
            log.warning(f'Failed login for: {username}')
    return render_template_string(LOGIN_HTML, error=error,
                                  username=username, next=next_url)

@app.route('/logout')
def logout():
    user = current_user()
    session.clear()
    log.info(f'Logout: {user}')
    return redirect(url_for('login'))

# ══════════════════════════════════════════════════════════════════════════════
# Home / Best
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    page_url           = request.args.get('page_url', XNXX_BASE + '/best/')
    videos, next_url   = scrape_page(page_url)
    page_content = """
<div style="padding:10px 12px 4px;font-size:13px;color:var(--muted);display:flex;justify-content:space-between">
  <span>🏆 Best Videos &nbsp;·&nbsp; <strong>{{ videos|length }}</strong> results</span>
</div>
<div class="grid">
  {% for v in videos %}""" + CARD_HTML + """{% endfor %}
</div>
<div class="pages">
  {% if next_url %}
  <a class="btn btn-blue" href="/?page_url={{ next_url|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">Next Page ▶</a>
  {% endif %}
</div>
"""
    return render_page(page_content, active='home', videos=videos,
                       next_url=next_url, listing_url=page_url, page_title='Best')

# ══════════════════════════════════════════════════════════════════════════════
# Search
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/search')
@login_required
def search():
    q        = request.args.get('q', '').strip()
    page_url = request.args.get('page_url', '')
    if not q:
        return redirect('/')
    record_search(current_user(), q)
    if not page_url:
        # XNXX search requires trailing page number: /search/QUERY/0
        page_url = XNXX_BASE + '/search/' + urllib.parse.quote_plus(q) + '/0'
    videos, next_url = scrape_page(page_url)
    page_content = """
<div style="padding:10px 12px 4px;font-size:13px;color:var(--muted)">
  🔍 <strong>{{ query }}</strong> &nbsp;·&nbsp; {{ videos|length }} results
</div>
<div class="grid">
  {% for v in videos %}""" + CARD_HTML + """{% endfor %}
</div>
<div class="pages">
  {% if next_url %}
  <a class="btn btn-blue" href="/search?q={{ query|urlencode }}&page_url={{ next_url|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">Next Page ▶</a>
  {% endif %}
</div>
"""
    return render_page(page_content, active='home', videos=videos,
                       next_url=next_url, query=q, listing_url=page_url, page_title=f'Search: {q}')

# ══════════════════════════════════════════════════════════════════════════════
# Categories
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/categories')
@login_required
def categories():
    page_content = """
<div style="padding:10px 12px 4px;font-size:13px;color:var(--muted)">
  ☰ <strong>{{ cats|length }}</strong> categories
</div>
<div class="catgrid">
  {% for name, path in cats %}
  {% set parts = name.split(' ', 1) %}
  <a class="catcard" href="/browse?page_url={{ (base + path)|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">
    <span class="cat-emoji">{{ parts[0] }}</span>
    {{ parts[1] if parts|length > 1 else parts[0] }}
  </a>
  {% endfor %}
</div>
"""
    return render_page(page_content, active='cats', cats=CATEGORIES,
                       base=XNXX_BASE, page_title='Categories')

# ══════════════════════════════════════════════════════════════════════════════
# Browse category
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/browse')
@login_required
def browse():
    page_url         = request.args.get('page_url', XNXX_BASE + '/best/')
    videos, next_url = scrape_page(page_url)
    label            = _title_from_href(page_url.replace(XNXX_BASE, ''))
    page_content = """
<div style="padding:10px 12px 4px;font-size:13px;color:var(--muted)">
  📂 <strong>{{ label }}</strong> &nbsp;·&nbsp; {{ videos|length }} videos
</div>
<div class="grid">
  {% for v in videos %}""" + CARD_HTML + """{% endfor %}
</div>
<div class="pages">
  {% if next_url %}
  <a class="btn btn-blue" href="/browse?page_url={{ next_url|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">Next Page ▶</a>
  {% endif %}
</div>
"""
    return render_page(page_content, active='cats', videos=videos,
                       next_url=next_url, label=label, listing_url=page_url, page_title=label)

# ══════════════════════════════════════════════════════════════════════════════
# Play
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/play')
@login_required
def play():
    video_url = request.args.get('url', '')
    if not video_url:
        return redirect('/')

    desc, hi, lo, hls = get_video_details(video_url)
    q = SETTINGS.get('quality', 'high')
    if q == 'hls':
        play_url = hls or hi or lo
    elif q == 'low':
        play_url = lo or hi or hls
    else:
        play_url = hi or lo or hls

    vtype  = 'application/x-mpegURL' if (play_url == hls and hls) else 'video/mp4'
    title  = _title_from_href(video_url.replace(XNXX_BASE, ''))
    euser  = effective_user()
    record_watch(euser, video_url, title)
    autoplay_attr = 'autoplay' if SETTINGS.get('autoplay', True) else ''

    # ── Similar videos from the referring listing page (cached — fast) ────────
    ref_url = request.args.get('ref_url', '')
    similar = []
    if ref_url:
        try:
            log.debug(f'play: fetching similar videos from ref_url={ref_url}')
            sim_videos, _ = scrape_page(ref_url)
            # Exclude the video currently playing
            similar = [v for v in sim_videos if v.get('link') != video_url][:16]
            log.info(f'play: {len(similar)} similar videos from {ref_url}')
        except Exception as e:
            log.warning(f'play: failed to load similar videos: {e}')

    page_content = """
<div style="padding:10px 14px;max-width:1024px;margin:0 auto">
  <div class="player-title">{{ title }}</div>
  <div class="player-wrap">
    {% if play_url %}
    <video controls """ + autoplay_attr + """ playsinline style="max-height:70vh"
           onkeydown="if(event.code==='Space'){event.preventDefault();this.paused?this.play():this.pause()}">
      <source src="{{ play_url }}" type="{{ vtype }}">
      Your browser does not support HTML5 video.
    </video>
    {% else %}
    <div class="banner banner-warn" style="margin:0;border-radius:0">
      ⚠ No playable URL found. The site may be blocking the request — try again shortly.
    </div>
    {% endif %}
    <div class="player-actions">
      <a class="btn btn-blue btn-sm"
         href="/download?url={{ video_url|urlencode }}&title={{ title|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">⬇ Download</a>
      <button class="btn btn-dark btn-sm" onclick="history.back()">◀ Back</button>
      {% if play_url %}
      <a class="btn btn-ghost btn-sm" href="{{ play_url }}" target="_blank">🔗 Direct</a>
      {% endif %}
    </div>
  </div>
  {% if desc %}
  <div style="padding:10px 0;font-size:13px;color:var(--muted);max-width:700px">{{ desc[:400] }}</div>
  {% endif %}
</div>

{% if similar %}
<div style="padding:4px 14px 8px;max-width:1024px;margin:0 auto">
  <div style="display:flex;align-items:center;gap:8px;padding:10px 0 6px;border-top:1px solid var(--border)">
    <span style="font-size:13px;font-weight:700;color:var(--muted)">▶▶ More like this</span>
    <span style="font-size:12px;color:var(--border)">{{ similar|length }} videos</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px">
    {% for v in similar %}
    <div class="card">
      <div class="thumb-wrap">
        {% if v.thumb %}
        <img src="/thumb?url={{ v.thumb|urlencode }}" loading="lazy" alt="">
        {% else %}
        <div class="thumb-placeholder">🎬</div>
        {% endif %}
        {% if v.duration %}<span class="duration">{{ v.duration }}</span>{% endif %}
        {% if v.link in watched %}<span class="watched-dot" title="Watched"></span>{% endif %}
      </div>
      <button class="star {{ 'on' if v.link in favorites else '' }}"
              onclick="toggleFav(this,'{{ v.link|urlencode }}','{{ v.title|e }}','{{ (v.thumb or '')|urlencode }}')"
              title="Favorite">{{ '★' if v.link in favorites else '☆' }}</button>
      <div class="info">
        <div class="title" title="{{ v.title|e }}">{{ v.title }}</div>
        <div class="actions">
          <a class="btn btn-green btn-sm"
             href="/play?url={{ v.link|urlencode }}&ref_url={{ ref_url|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">▶ Play</a>
          <a class="btn btn-blue btn-sm"
             href="/download?url={{ v.link|urlencode }}&title={{ v.title|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">⬇</a>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}
"""
    return render_page(page_content, title=title, play_url=play_url,
                       vtype=vtype, desc=desc, video_url=video_url,
                       similar=similar, ref_url=ref_url,
                       page_title=title, active='')

# ══════════════════════════════════════════════════════════════════════════════
# Download
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/download')
@login_required
def download_start():
    video_url = request.args.get('url', '')
    title     = request.args.get('title', '') or _title_from_href(
                    video_url.replace(XNXX_BASE, ''))
    dest      = request.args.get('dest', 'downloads')
    euser     = effective_user()
    dest_dir  = user_fav_dl_dir(euser) if dest == 'favorites' else user_download_dir(euser)

    token = hashlib.md5((euser + video_url + title).encode()).hexdigest()[:12]
    with _dl_lock:
        existing = DOWNLOADS.get(token, {})
        if existing.get('status') not in ('done', 'error', 'cancelled', None, ''):
            pass  # already running
        else:
            DOWNLOADS[token] = {
                'username': euser, 'title': title, 'status': 'queued',
                'pct': 0, 'path': '', 'error': '', 'speed': '', 'eta': '', 'cancel': False
            }
            threading.Thread(target=_dl_thread,
                             args=(token, euser, video_url, title, dest_dir),
                             daemon=True).start()

    page_content = """
<div style="padding:14px;max-width:600px;margin:0 auto">
<div class="banner banner-ok">
  ⬇ Download started: <strong>{{ title }}</strong><br>
  <span style="font-size:12px;color:var(--muted)">You can leave this page — download continues in background.</span>
</div>
<div style="margin-top:16px;background:var(--surface);border-radius:10px;padding:14px">
  <div class="prog-wrap"><div class="prog-bar" id="bar" style="width:0%"></div></div>
  <div id="status" style="margin-top:8px;font-size:13px;color:var(--muted)">Resolving URL…</div>
  <div id="speed-eta" style="font-size:12px;color:var(--muted);margin-top:3px"></div>
</div>
<div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap">
  <a class="btn btn-blue btn-sm" href="/downloads">📁 View Downloads</a>
  <button class="btn btn-dark btn-sm" onclick="history.back()">◀ Back</button>
  <button class="btn btn-ghost btn-sm" id="cancel-btn" onclick="cancelDl('{{ token }}')">✕ Cancel</button>
</div>
</div>
<script>
function cancelDl(token){
  fetch('/dl_cancel?token='+token,{method:'POST'}).then(()=>{
    document.getElementById('cancel-btn').disabled=true;
    document.getElementById('status').textContent='Cancelling…';
  });
}
(function poll(){
  fetch('/dl_status?token={{ token }}').then(r=>r.json()).then(d=>{
    document.getElementById('bar').style.width=d.pct+'%';
    const se=document.getElementById('speed-eta');
    if(d.status==='done'){
      document.getElementById('status').textContent='✓ Done: '+(d.path||'').split('/').pop();
      document.getElementById('bar').style.background='var(--accent3)';
      se.textContent='';
    }else if(d.status==='error'){
      document.getElementById('status').textContent='✗ Error: '+d.error;
      document.getElementById('bar').style.background='#c00';
      se.textContent='';
    }else if(d.status==='cancelled'){
      document.getElementById('status').textContent='Cancelled';
      se.textContent='';
    }else{
      document.getElementById('status').textContent=d.status+' '+d.pct+'%';
      se.textContent=(d.speed?d.speed+' · ':'')+(d.eta?'ETA '+d.eta:'');
      setTimeout(poll,1500);
    }
  }).catch(()=>setTimeout(poll,3000));
})();
</script>
"""
    return render_page(page_content, title=title, token=token,
                       page_title='Downloading', active='dls')

@app.route('/dl_status')
@login_required
def dl_status():
    token = request.args.get('token', '')
    with _dl_lock:
        info = dict(DOWNLOADS.get(token, {
            'status': 'unknown', 'pct': 0, 'path': '',
            'error': '', 'title': '', 'speed': '', 'eta': ''
        }))
    return jsonify(info)

@app.route('/dl_cancel', methods=['POST'])
@login_required
def dl_cancel():
    token = request.args.get('token', '')
    with _dl_lock:
        if token in DOWNLOADS:
            # Only owner or admin can cancel
            dl = DOWNLOADS[token]
            if dl.get('username') == current_user() or is_admin():
                DOWNLOADS[token]['cancel'] = True
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════════════════════════
# Favorites
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/favorites')
@login_required
def favorites():
    euser = effective_user()
    favs  = list(get_favorites(euser).values())
    page_content = """
<div style="padding:10px 12px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
  <span style="font-size:13px;color:var(--muted)">★ <strong>{{ favs|length }}</strong> favorites
    {% if admin_viewing %} &nbsp;·&nbsp; <span class="badge badge-purple">{{ admin_viewing }}</span>{% endif %}
  </span>
  <div style="display:flex;gap:6px">
    {% if favs %}
    <a class="btn btn-blue btn-sm" href="/download_all_favs{% if admin_viewing %}?admin_view={{ admin_viewing|urlencode }}{% endif %}">⬇ Download All</a>
    <a class="btn btn-ghost btn-sm" href="/clear_favs{% if admin_viewing %}?admin_view={{ admin_viewing|urlencode }}{% endif %}"
       onclick="return confirm('Remove ALL favorites?')">🗑 Clear All</a>
    {% endif %}
  </div>
</div>
{% if not favs %}
<div class="empty"><div class="icon">☆</div>No favorites yet.<br>Tap ☆ on any video.</div>
{% else %}
<div class="grid">
  {% for v in favs %}""" + CARD_HTML + """{% endfor %}
</div>
{% endif %}
"""
    return render_page(page_content, active='favs', videos=favs,
                       favs=favs, page_title='Favorites')

@app.route('/fav_toggle', methods=['POST'])
@login_required
def fav_toggle():
    euser = effective_user()
    url   = request.args.get('url', '')
    title = request.args.get('title', '') or _title_from_href(url.replace(XNXX_BASE, ''))
    thumb = request.args.get('thumb', '')
    favs  = get_favorites(euser)
    if url in favs:
        del favs[url]
        is_fav = False
    else:
        favs[url] = {'link': url, 'title': title, 'thumb': thumb}
        is_fav = True
    save_favorites(euser, favs)
    return jsonify({'is_fav': is_fav})

@app.route('/clear_favs')
@login_required
def clear_favs():
    euser = effective_user()
    save_favorites(euser, {})
    av = request.args.get('admin_view', '')
    return redirect(url_for('favorites') + (f'?admin_view={av}' if av else ''))

@app.route('/download_all_favs')
@login_required
def download_all_favs():
    euser  = effective_user()
    favs   = get_favorites(euser)
    queued = 0
    for url, info in favs.items():
        title = info.get('title', '') or _title_from_href(url.replace(XNXX_BASE, ''))
        token = hashlib.md5((euser + url + title).encode()).hexdigest()[:12]
        with _dl_lock:
            if DOWNLOADS.get(token, {}).get('status') not in ('queued', 'resolving', 'downloading'):
                DOWNLOADS[token] = {
                    'username': euser, 'title': title, 'status': 'queued',
                    'pct': 0, 'path': '', 'error': '', 'speed': '', 'eta': '', 'cancel': False
                }
                threading.Thread(target=_dl_thread,
                                 args=(token, euser, url, title, user_fav_dl_dir(euser)),
                                 daemon=True).start()
                queued += 1
    return redirect(url_for('downloads') + f'?msg=Queued+{queued}+downloads')

# ══════════════════════════════════════════════════════════════════════════════
# Watch history
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/history')
@login_required
def history():
    euser = effective_user()
    hist  = sorted(get_history(euser).values(),
                   key=lambda x: x.get('visited_at', ''), reverse=True)
    page_content = """
<div style="padding:10px 12px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
  <span style="font-size:13px;color:var(--muted)">🕐 <strong>{{ hist|length }}</strong> watched
    {% if admin_viewing %} &nbsp;·&nbsp; <span class="badge badge-purple">{{ admin_viewing }}</span>{% endif %}
  </span>
  <div style="display:flex;gap:6px">
    {% if hist %}
    <a class="btn btn-ghost btn-sm" href="/clear_history{% if admin_viewing %}?admin_view={{ admin_viewing|urlencode }}{% endif %}"
       onclick="return confirm('Clear all watch history?')">🗑 Clear History</a>
    {% endif %}
    {% if not settings.history_enabled %}
    <span class="badge">History disabled in settings</span>
    {% endif %}
  </div>
</div>
{% if not hist %}
<div class="empty"><div class="icon">🕐</div>No history yet.<br>Videos you play will appear here.</div>
{% else %}
{% for v in hist %}
<div class="hist-item">
  {% if v.thumb %}
  <img class="hist-thumb" src="/thumb?url={{ v.thumb|urlencode }}" loading="lazy" alt="">
  {% else %}
  <div class="hist-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px">🎬</div>
  {% endif %}
  <div class="hist-info">
    <div class="hist-title">{{ v.title }}</div>
    <div class="hist-time">{{ v.visited_at }}</div>
  </div>
  <div class="hist-actions">
    <a class="btn btn-green btn-sm" href="/play?url={{ v.link|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">▶</a>
    <a class="btn btn-blue btn-sm" href="/download?url={{ v.link|urlencode }}&title={{ v.title|urlencode }}{% if admin_viewing %}&admin_view={{ admin_viewing|urlencode }}{% endif %}">⬇</a>
  </div>
</div>
{% endfor %}
{% endif %}
"""
    return render_page(page_content, active='hist', hist=hist,
                       settings=SETTINGS, page_title='History')

@app.route('/clear_history')
@login_required
def clear_history():
    euser = effective_user()
    save_history(euser, {})
    av = request.args.get('admin_view', '')
    return redirect(url_for('history') + (f'?admin_view={av}' if av else ''))

# ══════════════════════════════════════════════════════════════════════════════
# Downloads library
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/downloads')
@login_required
def downloads():
    euser   = effective_user()
    msg     = request.args.get('msg', '')
    dl_dir  = user_download_dir(euser)
    files   = []

    if os.path.isdir(dl_dir):
        for root, _, fnames in os.walk(dl_dir):
            rel       = os.path.relpath(root, dl_dir)
            subfolder = '' if rel == '.' else rel
            for fn in fnames:
                if not fn.lower().endswith('.mp4'):
                    continue
                path = os.path.join(root, fn)
                try:
                    st = os.stat(path)
                    files.append({
                        'path':      path,
                        'name':      os.path.splitext(fn)[0],
                        'size_str':  _fmt_size(st.st_size),
                        'size':      st.st_size,
                        'mtime':     st.st_mtime,
                        'date_str':  datetime.fromtimestamp(st.st_mtime).strftime('%b %d %Y  %H:%M'),
                        'subfolder': subfolder,
                        'relpath':   os.path.relpath(path, dl_dir),
                    })
                except OSError:
                    pass

    files.sort(key=lambda f: f['mtime'], reverse=True)
    total_bytes = sum(f['size'] for f in files)
    total_mb    = total_bytes / (1024 * 1024)
    used, disk_total = _disk_usage(dl_dir if os.path.isdir(dl_dir) else BASE_DIR)
    disk_pct    = int(used / disk_total * 100) if disk_total else 0

    with _dl_lock:
        active_dls = [(t, dict(d)) for t, d in DOWNLOADS.items()
                      if d.get('username') == euser
                      and d['status'] not in ('done', 'error', 'cancelled')]

    import json as _json
    active_tokens_js = _json.dumps([t for t, _ in active_dls])
    av_param = f'&admin_view={euser}' if euser != current_user() else ''

    page_content = """
{% if msg %}<div class="banner banner-ok">{{ msg }}</div>{% endif %}
<div style="padding:10px 12px">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px">
    <span style="font-size:13px;color:var(--muted)">
      📁 <strong>{{ files|length }}</strong> videos &nbsp;·&nbsp; {{ '%.1f'|format(total_mb) }} MB
      {% if admin_viewing %}&nbsp;·&nbsp;<span class="badge badge-purple">{{ admin_viewing }}</span>{% endif %}
      {% if active_dls %}&nbsp;·&nbsp;<strong style="color:var(--accent)">{{ active_dls|length }} downloading…</strong>{% endif %}
    </span>
    <div style="display:flex;gap:6px">
      <a class="btn btn-dark btn-sm" href="/downloads{{ av_qs }}">↺ Refresh</a>
      <a class="btn btn-ghost btn-sm" href="/clear_cache">🗑 Clear Cache</a>
    </div>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:4px">
    Disk: {{ used_str }} used / {{ total_str }} total ({{ disk_pct }}%)
  </div>
  <div class="storage-bar-wrap"><div class="storage-bar" style="width:{{ disk_pct }}%"></div></div>
</div>

{% if active_dls %}
<div style="padding:0 12px 10px">
  {% for token, d in active_dls %}
  <div style="background:var(--surface);border-radius:10px;padding:10px 12px;margin-bottom:8px;font-size:13px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
      <span style="font-weight:600">⬇ {{ d.title[:60] }}</span>
      <button class="btn btn-ghost btn-xs" onclick="cancelDl('{{ token }}')">✕</button>
    </div>
    <div class="prog-wrap"><div class="prog-bar" id="bar_{{ token }}" style="width:{{ d.pct }}%"></div></div>
    <div style="display:flex;justify-content:space-between;margin-top:4px">
      <span id="st_{{ token }}" style="font-size:12px;color:var(--muted)">{{ d.status }} {{ d.pct }}%</span>
      <span id="se_{{ token }}" style="font-size:12px;color:var(--muted)">{{ (d.speed or '')+(' · ETA '+d.eta if d.eta else '') }}</span>
    </div>
  </div>
  {% endfor %}
</div>
<script>
function cancelDl(token){fetch('/dl_cancel?token='+token,{method:'POST'});}
(function poll(){
  {{ active_tokens|safe }}.forEach(token=>{
    fetch('/dl_status?token='+token).then(r=>r.json()).then(d=>{
      const b=document.getElementById('bar_'+token);
      const s=document.getElementById('st_'+token);
      const se=document.getElementById('se_'+token);
      if(b)b.style.width=d.pct+'%';
      if(s)s.textContent=d.status+' '+d.pct+'%';
      if(se)se.textContent=(d.speed?d.speed+' · ':'')+('ETA '+d.eta||'');
    });
  });
  setTimeout(poll,2000);
})();
</script>
{% endif %}

{% if not files %}
<div class="empty"><div class="icon">📂</div>No downloads yet.<br>Tap ⬇ on any video.</div>
{% else %}
<table class="dl-table">
<thead><tr><th>Filename</th><th>Size</th><th>Date</th><th>Folder</th><th>Actions</th></tr></thead>
<tbody>
{% for f in files %}
<tr>
  <td style="max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
    <span title="{{ f.name }}">{{ f.name[:60] }}{% if f.name|length > 60 %}…{% endif %}</span>
  </td>
  <td style="white-space:nowrap">{{ f.size_str }}</td>
  <td style="white-space:nowrap;color:var(--muted)">{{ f.date_str }}</td>
  <td>{% if f.subfolder %}<span class="badge">{{ f.subfolder }}</span>{% else %}<span style="color:var(--border)">—</span>{% endif %}</td>
  <td style="white-space:nowrap">
    <a class="btn btn-green btn-sm" href="/stream?relpath={{ f.relpath|urlencode }}{{ av_qs }}">▶</a>
    <a class="btn btn-red btn-sm" href="/delete?relpath={{ f.relpath|urlencode }}{{ av_qs }}"
       onclick="return confirm('Delete?')">🗑</a>
  </td>
</tr>
{% endfor %}
</tbody>
</table>
{% endif %}
"""
    av_qs = f'?admin_view={euser}' if euser != current_user() else ''
    return render_page(page_content, active='dls', files=files,
                       total_mb=total_mb, active_dls=active_dls, msg=msg,
                       disk_pct=disk_pct, used_str=_fmt_size(used),
                       total_str=_fmt_size(disk_total),
                       active_tokens=active_tokens_js,
                       av_qs=av_qs, page_title='Downloads')

# ── Stream local file ─────────────────────────────────────────────────────────
@app.route('/stream')
@login_required
def stream():
    euser   = effective_user()
    relpath = request.args.get('relpath', '')
    dl_dir  = user_download_dir(euser)
    path    = os.path.normpath(os.path.join(dl_dir, relpath))
    if not os.path.realpath(path).startswith(os.path.realpath(dl_dir)):
        abort(403)
    if not os.path.isfile(path):
        abort(404)

    name  = os.path.splitext(os.path.basename(path))[0]
    title = name.replace('_', ' ')
    size  = os.path.getsize(path)

    range_header = request.headers.get('Range')
    if range_header:
        m      = re.match(r'bytes=(\d+)-(\d*)', range_header)
        start  = int(m.group(1)) if m else 0
        end    = int(m.group(2)) if m and m.group(2) else size - 1
        end    = min(end, size - 1)
        length = end - start + 1

        def generate():
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(131072, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = Response(generate(), 206, mimetype='video/mp4', direct_passthrough=True)
        resp.headers['Content-Range']  = f'bytes {start}-{end}/{size}'
        resp.headers['Accept-Ranges']  = 'bytes'
        resp.headers['Content-Length'] = str(length)
        return resp

    av_qs         = f'?admin_view={euser}' if euser != current_user() else ''
    autoplay_attr = 'autoplay' if SETTINGS.get('autoplay', True) else ''
    page_content = """
<div style="padding:10px 14px;max-width:1024px;margin:0 auto">
  <div class="player-title">{{ title }}</div>
  <div class="player-wrap">
    <video controls """ + autoplay_attr + """ playsinline style="max-height:72vh">
      <source src="/stream?relpath={{ relpath|urlencode }}{{ av_qs }}" type="video/mp4">
    </video>
    <div class="player-actions">
      <a class="btn btn-dark btn-sm" href="/downloads{{ av_qs }}">◀ Downloads</a>
      <a class="btn btn-red btn-sm" href="/delete?relpath={{ relpath|urlencode }}{{ av_qs }}"
         onclick="return confirm('Delete this video?')">🗑 Delete</a>
    </div>
  </div>
</div>
"""
    return render_page(page_content, title=title, relpath=relpath,
                       av_qs=av_qs, page_title=title, active='dls')

# ── Delete ────────────────────────────────────────────────────────────────────
@app.route('/delete')
@login_required
def delete():
    euser   = effective_user()
    relpath = request.args.get('relpath', '')
    dl_dir  = user_download_dir(euser)
    path    = os.path.normpath(os.path.join(dl_dir, relpath))
    if not os.path.realpath(path).startswith(os.path.realpath(dl_dir)):
        abort(403)
    try:
        os.remove(path)
        log.info(f'[{euser}] Deleted: {path}')
    except Exception as e:
        log.error(f'Delete failed {path}: {e}')
    av    = request.args.get('admin_view', '')
    qs    = f'?admin_view={av}' if av else ''
    return redirect(url_for('downloads') + qs)

# ── Clear cache ───────────────────────────────────────────────────────────────
@app.route('/clear_cache')
@login_required
def clear_cache():
    n = 0
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith('.html'):
            try:
                os.remove(os.path.join(CACHE_DIR, fn))
                n += 1
            except OSError:
                pass
    log.info(f'Cache cleared: {n} files by {current_user()}')
    return redirect(url_for('downloads') + f'?msg=Cleared+{n}+cached+pages')

# ══════════════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    msg = ''
    change_pw_msg = ''

    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'change_password':
            old_pw  = request.form.get('old_password', '')
            new_pw  = request.form.get('new_password', '').strip()
            confirm = request.form.get('confirm_password', '').strip()
            uname   = current_user()
            if not verify_user(uname, old_pw):
                change_pw_msg = '✗ Current password is incorrect'
            elif len(new_pw) < 4:
                change_pw_msg = '✗ New password must be at least 4 characters'
            elif new_pw != confirm:
                change_pw_msg = '✗ New passwords do not match'
            else:
                ok, change_pw_msg = reset_password(uname, new_pw)
                if ok:
                    change_pw_msg = '✓ Password changed successfully!'

        else:  # save settings
            f = request.form
            SETTINGS['cache_hours']     = max(1, min(168, int(f.get('cache_hours', 6))))
            SETTINGS['quality']         = f.get('quality', 'high')
            SETTINGS['history_enabled'] = 'history_enabled' in f
            SETTINGS['max_history']     = max(10, min(1000, int(f.get('max_history', 200))))
            SETTINGS['grid_size']       = f.get('grid_size', 'medium')
            SETTINGS['autoplay']        = 'autoplay' in f
            _save_json(SETTINGS_FILE, SETTINGS)
            msg = '✓ Settings saved!'
            log.info(f'Settings updated by {current_user()}: {SETTINGS}')

    page_content = """
{% if msg %}<div class="banner banner-ok" style="margin:10px 12px">{{ msg }}</div>{% endif %}
<form method="post" class="settings-form">
  <input type="hidden" name="action" value="settings">
  <div class="section-title">📺 Playback</div>
  <div class="form-row">
    <label>Default Quality</label>
    <select name="quality">
      <option value="high" {{ 'selected' if settings.quality=='high' }}>High (MP4 HD)</option>
      <option value="low"  {{ 'selected' if settings.quality=='low'  }}>Low (MP4 SD)</option>
      <option value="hls"  {{ 'selected' if settings.quality=='hls'  }}>HLS Stream</option>
    </select>
  </div>
  <div class="form-row">
    <label>Autoplay</label>
    <div class="toggle-wrap">
      <label class="toggle">
        <input type="checkbox" name="autoplay" {{ 'checked' if settings.autoplay }}>
        <div class="toggle-track"></div><div class="toggle-knob"></div>
      </label>
      <span style="font-size:13px;color:var(--muted)">Start playing immediately</span>
    </div>
  </div>
  <div class="section-title">🗂️ Interface</div>
  <div class="form-row">
    <label>Grid Size</label>
    <select name="grid_size">
      <option value="small"  {{ 'selected' if settings.grid_size=='small'  }}>Small</option>
      <option value="medium" {{ 'selected' if settings.grid_size=='medium' }}>Medium (default)</option>
      <option value="large"  {{ 'selected' if settings.grid_size=='large'  }}>Large</option>
    </select>
  </div>
  <div class="section-title">🗃️ Cache & History</div>
  <div class="form-row">
    <label>Cache Duration (hours)</label>
    <input type="number" name="cache_hours" value="{{ settings.cache_hours }}" min="1" max="168">
  </div>
  <div class="form-row">
    <label>Watch History</label>
    <div class="toggle-wrap">
      <label class="toggle">
        <input type="checkbox" name="history_enabled" {{ 'checked' if settings.history_enabled }}>
        <div class="toggle-track"></div><div class="toggle-knob"></div>
      </label>
      <span style="font-size:13px;color:var(--muted)">Track videos you watch</span>
    </div>
  </div>
  <div class="form-row">
    <label>Max History Items</label>
    <input type="number" name="max_history" value="{{ settings.max_history }}" min="10" max="1000">
  </div>
  <div style="margin-top:20px;display:flex;gap:8px">
    <button type="submit" class="btn btn-red">💾 Save Settings</button>
    <a class="btn btn-ghost" href="/clear_history" onclick="return confirm('Clear all your watch history?')">🗑 Clear History</a>
    <a class="btn btn-ghost" href="/clear_cache">🗑 Clear Cache</a>
  </div>
</form>

<div style="max-width:520px;margin:24px auto 0;padding:0 12px">
  <div class="section-title" style="padding-top:0;border-top:1px solid var(--border)">🔑 Change Password</div>
  {% if change_pw_msg %}
  <div class="banner {{ 'banner-ok' if change_pw_msg.startswith('✓') else 'banner-err' }}" style="margin:8px 0">
    {{ change_pw_msg }}
  </div>
  {% endif %}
  <form method="post" style="margin-top:10px">
    <input type="hidden" name="action" value="change_password">
    <div class="form-row">
      <label>Current Password</label>
      <input type="password" name="old_password" autocomplete="current-password">
    </div>
    <div class="form-row">
      <label>New Password</label>
      <input type="password" name="new_password" autocomplete="new-password">
    </div>
    <div class="form-row">
      <label>Confirm New Password</label>
      <input type="password" name="confirm_password" autocomplete="new-password">
    </div>
    <button type="submit" class="btn btn-orange">🔑 Update Password</button>
  </form>
</div>
"""
    return render_page(page_content, active='settings', settings=SETTINGS,
                       msg=msg, change_pw_msg=change_pw_msg, page_title='Settings')

# ══════════════════════════════════════════════════════════════════════════════
# Admin panel
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin')
@admin_required
def admin():
    msg = request.args.get('msg', '')
    err = request.args.get('err', '')

    # Build user list with stats
    user_list = []
    for uname, udata in USERS.items():
        stats = user_stats(uname)
        user_list.append({
            'username':   uname,
            'role':       udata.get('role', 'user'),
            'created_at': udata.get('created_at', ''),
            **stats,
        })
    user_list.sort(key=lambda u: (u['role'] != 'admin', u['username']))

    # Active downloads across ALL users (admin sees everything)
    with _dl_lock:
        all_active = [(t, dict(d)) for t, d in DOWNLOADS.items()
                      if d['status'] not in ('done', 'error', 'cancelled')]

    page_content = """
{% if msg %}<div class="banner banner-ok">✓ {{ msg }}</div>{% endif %}
{% if err %}<div class="banner banner-err">✗ {{ err }}</div>{% endif %}

<div style="padding:10px 12px 0;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
  <span style="font-size:13px;color:var(--muted)">
    👥 <strong>{{ user_list|length }}</strong> users
    {% if all_active %}&nbsp;·&nbsp;<strong style="color:var(--accent)">{{ all_active|length }} active download(s)</strong>{% endif %}
  </span>
</div>

<!-- Add user form -->
<div class="add-user-form">
  <h3>➕ Add New User</h3>
  <form method="post" action="/admin/add_user" autocomplete="off">
    <div class="inline-fields">
      <div>
        <div style="font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;margin-bottom:5px">Username</div>
        <input type="text" name="username" class="form-row" required
               style="width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:14px;outline:none"
               placeholder="username" autocomplete="off">
      </div>
      <div>
        <div style="font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;margin-bottom:5px">Password</div>
        <input type="password" name="password" required
               style="width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:14px;outline:none"
               placeholder="password" autocomplete="new-password">
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:12px;margin-top:10px">
      <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);cursor:pointer">
        <input type="checkbox" name="make_admin" style="accent-color:var(--accent4)">
        Make admin
      </label>
      <button type="submit" class="btn btn-purple btn-sm">Create User</button>
    </div>
  </form>
</div>

<!-- User cards -->
<div class="admin-grid">
  {% for u in user_list %}
  <div class="user-card">
    <div class="user-card-header">
      <div class="user-avatar">{{ '👑' if u.role == 'admin' else '👤' }}</div>
      <div>
        <div class="user-card-name">{{ u.username }}</div>
        <div class="user-card-role">
          {% if u.role == 'admin' %}<span class="badge badge-purple">admin</span>
          {% else %}<span class="badge badge-blue">user</span>{% endif %}
          &nbsp; joined {{ u.created_at[:10] if u.created_at else '—' }}
        </div>
      </div>
    </div>
    <div class="user-stats">
      <div class="stat-box">
        <div class="stat-val">{{ u.favorites }}</div>
        <div class="stat-lbl">Favorites</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{{ u.history }}</div>
        <div class="stat-lbl">History</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{{ u.downloads }}</div>
        <div class="stat-lbl">Downloads</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
      📦 {{ u.dl_size_str }} of video files
    </div>
    <div class="user-card-actions">
      <a class="btn btn-green btn-sm" href="/favorites?admin_view={{ u.username|urlencode }}">★ Favs</a>
      <a class="btn btn-blue btn-sm" href="/history?admin_view={{ u.username|urlencode }}">🕐 Hist</a>
      <a class="btn btn-dark btn-sm" href="/downloads?admin_view={{ u.username|urlencode }}">📁 DLs</a>
      <button class="btn btn-orange btn-sm"
              onclick="showResetPw('{{ u.username }}')">🔑</button>
      {% if u.username != current_user_name %}
      <button class="btn btn-red btn-sm"
              onclick="confirmDelete('{{ u.username }}')">🗑</button>
      {% endif %}
    </div>
    <!-- Inline reset password form (hidden by default) -->
    <div id="pw_{{ u.username }}" style="display:none;margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
      <form method="post" action="/admin/reset_password/{{ u.username }}" autocomplete="off">
        <div style="font-size:11px;color:var(--muted);margin-bottom:5px;font-weight:700;text-transform:uppercase">New Password for {{ u.username }}</div>
        <div style="display:flex;gap:6px">
          <input type="password" name="new_password" placeholder="New password" required
                 style="flex:1;padding:8px 10px;border-radius:7px;border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:13px;outline:none">
          <button type="submit" class="btn btn-orange btn-sm">Set</button>
          <button type="button" class="btn btn-ghost btn-sm"
                  onclick="document.getElementById('pw_{{ u.username }}').style.display='none'">✕</button>
        </div>
      </form>
    </div>
  </div>
  {% endfor %}
</div>

{% if all_active %}
<div style="padding:12px">
  <div class="section-title" style="padding-top:0;border-top:none;margin-bottom:8px">⬇ All Active Downloads</div>
  {% for token, d in all_active %}
  <div style="background:var(--surface);border-radius:9px;padding:10px 12px;margin-bottom:7px;font-size:13px;display:flex;align-items:center;gap:10px">
    <span class="badge badge-blue">{{ d.username }}</span>
    <span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ d.title[:50] }}</span>
    <span style="color:var(--muted);font-size:12px">{{ d.status }} {{ d.pct }}%</span>
    <button class="btn btn-ghost btn-xs" onclick="fetch('/dl_cancel?token={{ token }}',{method:'POST'})">✕</button>
  </div>
  {% endfor %}
</div>
{% endif %}

<script>
function showResetPw(username){
  document.getElementById('pw_'+username).style.display='block';
}
function confirmDelete(username){
  if(confirm('Delete user "'+username+'" and ALL their data? This cannot be undone.')){
    window.location='/admin/delete_user/'+encodeURIComponent(username);
  }
}
</script>
"""
    return render_page(page_content, active='admin', user_list=user_list,
                       all_active=all_active, msg=msg, err=err,
                       page_title='Admin Panel')

@app.route('/admin/add_user', methods=['POST'])
@admin_required
def admin_add_user():
    username   = request.form.get('username', '').strip().lower()
    password   = request.form.get('password', '')
    make_admin = 'make_admin' in request.form

    if not username or not password:
        return redirect(url_for('admin') + '?err=Username+and+password+required')
    if not re.match(r'^[a-z0-9_-]{2,32}$', username):
        return redirect(url_for('admin') + '?err=Username+must+be+2-32+chars+(a-z+0-9+-+_)')

    role  = 'admin' if make_admin else 'user'
    ok, msg = create_user(username, password, role)
    if ok:
        return redirect(url_for('admin') + f'?msg=User+{username}+created+({role})')
    return redirect(url_for('admin') + f'?err={urllib.parse.quote_plus(msg)}')

@app.route('/admin/delete_user/<username>')
@admin_required
def admin_delete_user(username):
    ok, msg = delete_user(username)
    if ok:
        # Also remove their data directory? (optional — commented to preserve files)
        # import shutil; shutil.rmtree(user_dir(username), ignore_errors=True)
        return redirect(url_for('admin') + f'?msg=User+{username}+deleted')
    return redirect(url_for('admin') + f'?err={urllib.parse.quote_plus(msg)}')

@app.route('/admin/reset_password/<username>', methods=['POST'])
@admin_required
def admin_reset_password(username):
    new_pw = request.form.get('new_password', '').strip()
    if not new_pw or len(new_pw) < 4:
        return redirect(url_for('admin') + '?err=Password+must+be+at+least+4+characters')
    ok, msg = reset_password(username, new_pw)
    if ok:
        return redirect(url_for('admin') + f'?msg=Password+reset+for+{username}')
    return redirect(url_for('admin') + f'?err={urllib.parse.quote_plus(msg)}')

# ══════════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/searches')
@login_required
def api_searches():
    return jsonify(get_searches(current_user())[:15])

# ══════════════════════════════════════════════════════════════════════════════
# Admin: debug scrape — shows exactly what the scraper sees for any URL
# Access: /admin/debug_scrape?url=https://www.xnxx.com/search/amateur/0
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/debug_scrape')
@admin_required
def admin_debug_scrape():
    target = request.args.get('url', XNXX_BASE + '/search/amateur/0')
    force  = request.args.get('force', '') == '1'

    # Optionally bust cache
    if force:
        cp = _cache_path(target)
        try:    os.remove(cp)
        except OSError: pass

    html = fetch_url(target)
    result = {
        'url':          target,
        'fetched':      bool(html),
        'html_length':  len(html) if html else 0,
        'valid':        _valid_html(html) if html else False,
        'videos':       [],
        'strategies':   {},
        'html_snippet': (html[:3000] if html else ''),
        'all_classes':  [],
        'video_links_found': [],
    }

    if html:
        soup = BeautifulSoup(html, 'html.parser')

        # Count containers per strategy
        result['strategies'] = {
            'thumb-block':    len(soup.find_all('div', class_='thumb-block')),
            'class~=thumb':   len(soup.find_all(class_=re.compile(r'\bthumb\b', re.I))),
            'mozaique':       len(soup.find_all(class_=re.compile(r'mozaique|video-?list', re.I))),
            'all_video_links':len(soup.find_all('a', href=LINK_RE)),
        }

        # Collect all unique CSS classes on the page (first 60)
        all_cls = set()
        for tag in soup.find_all(True):
            for c in (tag.get('class') or []):
                all_cls.add(c)
        result['all_classes'] = sorted(all_cls)[:60]

        # Collect all video-like hrefs
        result['video_links_found'] = [
            a['href'] for a in soup.find_all('a', href=LINK_RE)
        ][:20]

        _, videos = _extract_videos_from_html(html, target)
        result['videos'] = videos[:5]  # show first 5

    page_content = """
<div style="padding:14px;max-width:900px;margin:0 auto">
  <h2 style="margin-bottom:12px;font-size:16px">🔍 Scrape Debugger</h2>

  <form method="get" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
    <input type="text" name="url" value="{{ result.url }}"
           style="flex:1;min-width:260px;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:13px;outline:none">
    <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:var(--muted)">
      <input type="checkbox" name="force" value="1"> Bust cache
    </label>
    <button type="submit" class="btn btn-red btn-sm">Test URL</button>
  </form>

  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin-bottom:14px">
    <div class="stat-box"><div class="stat-val" style="font-size:14px">{{ 'YES' if result.fetched else 'NO' }}</div><div class="stat-lbl">Fetched</div></div>
    <div class="stat-box"><div class="stat-val" style="font-size:14px">{{ result.html_length }}</div><div class="stat-lbl">HTML bytes</div></div>
    <div class="stat-box"><div class="stat-val" style="font-size:14px">{{ 'YES' if result.valid else 'NO' }}</div><div class="stat-lbl">Valid HTML</div></div>
    <div class="stat-box"><div class="stat-val" style="font-size:14px">{{ result.videos|length }}</div><div class="stat-lbl">Videos found</div></div>
  </div>

  <div style="margin-bottom:14px">
    <div class="section-title" style="padding:0 0 6px;border:none;font-size:11px">Strategy hits</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      {% for k,v in result.strategies.items() %}
      <div style="background:var(--surface);border-radius:7px;padding:6px 12px;font-size:13px">
        <strong style="color:{{ 'var(--accent3)' if v > 0 else 'var(--muted)' }}">{{ v }}</strong>
        <span style="color:var(--muted);margin-left:5px">{{ k }}</span>
      </div>
      {% endfor %}
    </div>
  </div>

  {% if result.video_links_found %}
  <div style="margin-bottom:14px">
    <div class="section-title" style="padding:0 0 6px;border:none;font-size:11px">Video hrefs detected (first 20)</div>
    <div style="background:var(--surface);border-radius:8px;padding:10px 12px;font-size:12px;font-family:monospace;line-height:1.7;max-height:200px;overflow-y:auto">
      {% for link in result.video_links_found %}<div>{{ link }}</div>{% endfor %}
    </div>
  </div>
  {% endif %}

  {% if result.videos %}
  <div style="margin-bottom:14px">
    <div class="section-title" style="padding:0 0 6px;border:none;font-size:11px">First {{ result.videos|length }} parsed video(s)</div>
    {% for v in result.videos %}
    <div style="background:var(--surface);border-radius:8px;padding:10px 12px;margin-bottom:6px;font-size:13px">
      <div><strong>{{ v.title }}</strong></div>
      <div style="color:var(--muted);font-size:11px;word-break:break-all">{{ v.link }}</div>
      <div style="color:var(--muted);font-size:11px;word-break:break-all">thumb: {{ v.thumb or '—' }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div>
    <div class="section-title" style="padding:0 0 6px;border:none;font-size:11px">CSS classes on page (first 60)</div>
    <div style="display:flex;flex-wrap:wrap;gap:4px">
      {% for c in result.all_classes %}
      <span style="background:var(--surface2);border-radius:4px;padding:2px 6px;font-size:11px;font-family:monospace;color:var(--muted)">{{ c }}</span>
      {% endfor %}
    </div>
  </div>

  {% if result.html_snippet %}
  <details style="margin-top:14px">
    <summary style="cursor:pointer;font-size:13px;color:var(--muted);padding:6px 0">▶ HTML snippet (first 3000 chars)</summary>
    <pre style="background:var(--surface);border-radius:8px;padding:12px;font-size:11px;overflow-x:auto;margin-top:6px;white-space:pre-wrap;word-break:break-all;max-height:400px;overflow-y:auto">{{ result.html_snippet|e }}</pre>
  </details>
  {% endif %}
</div>
"""
    return render_page(page_content, active='admin', result=result,
                       page_title='Scrape Debugger')


# ══════════════════════════════════════════════════════════════════════════════
# 403 handler
# ══════════════════════════════════════════════════════════════════════════════
@app.errorhandler(403)
def forbidden(e):
    page_content = """
<div class="empty">
  <div class="icon">🚫</div>
  <strong>Access Denied</strong><br>
  You don't have permission to view this page.<br><br>
  <a class="btn btn-dark" href="/">← Go Home</a>
</div>
"""
    return render_page(page_content, page_title='403 Forbidden', active=''), 403

# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
            _s.connect(('8.8.8.8', 80))
            ip = _s.getsockname()[0]
    except Exception:
        ip = '0.0.0.0'

    print('═' * 58)
    print("  AnonymousX's XNXX Scraper  v3.0  (Multi-User)")
    print(f'  Local:   http://localhost:{ARGS.port}')
    print(f'  Network: http://{ip}:{ARGS.port}')
    print(f'  Debug logging: {"ON → xnxx_pi.log" if ARGS.debug else "OFF (use --debug)"}')
    print(f'  Users: {len(USERS)} registered')
    print('  Default admin: admin / admin123  (change after login!)')
    print('  Ctrl+C to stop')
    print('═' * 58)

    app.run(host=ARGS.host, port=ARGS.port, threaded=True, debug=False)
