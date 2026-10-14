import os
import re
import glob
import time
import uuid
import math
import json
import shutil
import signal
import logging
import asyncio
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from wzgram import Client, filters
from wzgram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION & PROXY SETUP
# ==========================================
API_ID = 31135256
API_HASH = "6b31bb267109963bac7ddb8dd37fe09c"
BOT_TOKEN = "8938316203:AAHTUfaP8qwgLOo3JPw8xw5mQYCibl_u_k8"
PLAYLIST_URL = "http://premiumplugx.top/jiostb/mjelo.php?view=raw"

DOWNLOAD_PROXY = "http://kunanisaitarun:nxx9ge29bp0e@103.243.119.172:59100"
DEFAULT_FONT_PATH = os.path.abspath("SNAP.TTF")

# Hardcoded authorised users (no /auth needed)
AUTHORIZED_USERS = {
    7802806467,
    8980140588,
    6905747168,
    8952935309,
}

HINDI_TAGS = {"hi", "hin", "hindi"}      # Hindi audio always goes first
AUDIO_SELECTION = "all"                  # always record every audio track

TZ = timezone(timedelta(hours=5, minutes=30))   # IST, used for caption timestamp
JOBS_DIR = os.path.abspath("jobs")              # each recording gets its own folder here
TG_LIMIT = 1_950_000_000                        # files bigger than this are split before upload
SPLIT_TARGET = 1_600_000_000                    # aim for parts of about this size
MIN_FREE_BYTES = 500 * 1024 * 1024              # auto-stop a recording if free disk drops below this
PROGRESS_INTERVAL = 8                           # seconds between progress message edits
MAX_SEARCH_RESULTS = 30

proxy_dict = {
    "scheme": "http",
    "hostname": "103.243.119.172",
    "port": 59100,
    "username": "kunanisaitarun",
    "password": "nxx9ge29bp0e"
}

print("Initializing Pyrogram Client with full proxy routing...", flush=True)
app = Client(
    "jio_bot_session_v3",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    proxy=proxy_dict
)

# job_id -> dict(user_id, chat_id, channel_num, title, proc, action, stage, start, note ...)
jobs = {}
background_tasks = set()
playlist_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60

user_selected_channels = {}
user_record_mode = {}        # "norm" or "wm"
user_waiting_duration = {}

MEDIA_EXTS = (".mp4", ".ts", ".m4a", ".aac", ".mka", ".mkv", ".ac3", ".eac3", ".m4s")
AUDIO_ONLY_EXTS = (".m4a", ".aac", ".mka", ".ac3", ".eac3")
TEMP_PREFIX = "rawstream"

LANG_NAMES = {
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi",
    "en": "English", "eng": "English",
    "ta": "Tamil", "tam": "Tamil",
    "te": "Telugu", "tel": "Telugu",
    "kn": "Kannada", "kan": "Kannada",
    "ml": "Malayalam", "mal": "Malayalam",
    "mr": "Marathi", "mar": "Marathi",
    "bn": "Bengali", "ben": "Bengali",
    "gu": "Gujarati", "guj": "Gujarati",
    "pa": "Punjabi", "pan": "Punjabi",
    "ur": "Urdu", "urd": "Urdu",
    "or": "Odia", "ori": "Odia",
    "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean",
}


# ==========================================
# HELPERS
# ==========================================
def is_authorized(user) -> bool:
    return bool(user) and user.id in AUTHORIZED_USERS


def spawn(coro):
    """Run a coroutine in the background so handlers return immediately."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)

    def _done(t):
        background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("Background task crashed: %r", t.exception())

    task.add_done_callback(_done)
    return task


async def safe_edit(msg, text, markup=None):
    if not msg:
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.debug("edit failed: %s", e)


def fmt_size(num_bytes):
    num = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def fetch_fresh_playlist():
    global playlist_cache
    current_time = time.time()
    if playlist_cache["data"] and (current_time - playlist_cache["timestamp"] < CACHE_TTL):
        return playlist_cache["data"]

    logger.info("Fetching fresh playlist via proxy using requests...")
    try:
        proxies = {'http': DOWNLOAD_PROXY, 'https': DOWNLOAD_PROXY}
        response = requests.get(PLAYLIST_URL, proxies=proxies, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        playlist_cache["data"] = response.text
        playlist_cache["timestamp"] = current_time
        return response.text
    except Exception as e:
        logger.error(f"Failed to fetch playlist through proxy: {e}")
        return playlist_cache.get("data")


def parse_playlist_blocks(content):
    if not content:
        return {}
    blocks = content.split('#EXTINF')
    channels = {}
    channel_num = 1
    for block in blocks[1:]:
        full_block = '#EXTINF' + block
        name_match = re.search(r'tvg-name="([^"]+)"', full_block)
        name = name_match.group(1).strip() if name_match else f"Channel_{channel_num}"
        channels[channel_num] = {"name": name, "block": full_block.strip()}
        channel_num += 1
    return channels


def parse_m3u_block(block_text):
    stream_url = ""
    clean_name = "Recording"
    drm_keys = []
    headers = []

    for line in block_text.split('\n'):
        line = line.strip()
        if line.startswith('http'):
            stream_url = line.split('|')[0].strip()
            break

    name_match = re.search(r'tvg-name="([^"]+)"', block_text)
    if name_match:
        clean_name = re.sub(r'[\\/*?:"<>|]', "", name_match.group(1).strip())
        clean_name = re.sub(r'\s+', "_", clean_name) or "Recording"

    key_match = re.search(r'#KODIPROP:inputstream\.adaptive\.license_key=(.*)', block_text)
    if key_match:
        for k in key_match.group(1).strip().split(','):
            if k.strip():
                drm_keys.extend(["--key", k.strip()])

    ua_match = re.search(r'#EXTVLCOPT:http-user-agent=(.*)', block_text)
    if ua_match: headers.extend(["-H", f"User-Agent: {ua_match.group(1).strip()}"])

    ref_match = re.search(r'#EXTVLCOPT:http-referrer=(.*)', block_text)
    if ref_match: headers.extend(["-H", f"Referer: {ref_match.group(1).strip()}"])

    origin_match = re.search(r'#EXTVLCOPT:http-extra-headers=Origin:\s*(.*)', block_text)
    if origin_match: headers.extend(["-H", f"Origin: {origin_match.group(1).strip()}"])

    cookie_match = re.search(r'#EXTVLCOPT:http-cookie=(.*)', block_text)
    if cookie_match: headers.extend(["-H", f"Cookie: {cookie_match.group(1).strip()}"])

    return stream_url, clean_name, drm_keys, headers


def get_watermark_drawtext_filter():
    font_path = DEFAULT_FONT_PATH
    escaped_font = font_path.replace("\\", "/").replace(":", r"\:")
    font_arg = f"fontfile='{escaped_font}':" if os.path.exists(font_path) else ""

    text = "Join Our Telegram - @Atoztoonsverse"
    fontsize = 32
    fontcolor = "white@0.70"
    x = "(w-text_w)/2"
    y = "h*0.87-text_h"

    interval = 300
    dur = 30
    enable_arg = f":enable='lt(mod(t\\,{interval})\\,{dur})'"

    return (
        f"drawtext={font_arg}text='{text}':fontcolor={fontcolor}:fontsize={fontsize}:"
        f"x={x}:y={y}{enable_arg}"
    )


def get_media_info(filepath):
    cmd = ["ffmpeg", "-hide_banner", "-i", filepath]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+).*?start:\s*(\-?\d+\.\d+)", result.stderr)
    if match:
        h, m, s, start_str = match.groups()
        duration = float(h) * 3600 + float(m) * 60 + float(s)
        start = float(start_str)
        return start, start + duration
    return 0.0, float('inf')


def probe_streams(filepath):
    """Return a list of stream dicts (index, type, lang, codec, channels, width, height)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name,channels,width,height:stream_tags=language",
        "-of", "json", filepath
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout or "{}")
        streams = []
        for s in data.get("streams", []):
            streams.append({
                "index": s.get("index"),
                "type": s.get("codec_type"),
                "lang": (s.get("tags", {}).get("language") or "").lower(),
                "codec": s.get("codec_name") or "",
                "channels": s.get("channels"),
                "width": s.get("width"),
                "height": s.get("height"),
            })
        return streams
    except Exception as e:
        logger.warning(f"ffprobe failed for {filepath}: {e}")
        return []


def filename_is_hindi(filepath, prefix):
    base = os.path.basename(filepath)
    suffix = base[len(prefix):] if base.startswith(prefix) else base
    tokens = re.split(r"[._\-\s]+", suffix.lower())
    return any(t in HINDI_TAGS for t in tokens)


def is_hindi_stream(lang, filepath, prefix):
    if lang in HINDI_TAGS:
        return True
    if not lang or lang in ("und", "unk"):
        return filename_is_hindi(filepath, prefix)
    return False


def build_mux_command(all_files, prefix, max_start, min_end, output_filename):
    """ffmpeg mux command: video first, then audio (Hindi first), then subtitles."""
    video, audio, subs = [], [], []

    for fi, f in enumerate(all_files):
        for s in probe_streams(f):
            entry = (fi, s["index"], is_hindi_stream(s["lang"], f, prefix))
            if s["type"] == "video":
                video.append(entry)
            elif s["type"] == "audio":
                audio.append(entry)
            elif s["type"] == "subtitle":
                subs.append(entry)

    cmd = ["ffmpeg", "-hide_banner"]
    for f in all_files:
        cmd.extend(["-i", f])

    if video or audio or subs:
        audio.sort(key=lambda e: 0 if e[2] else 1)  # stable sort: Hindi first, rest keep order
        for fi, si, _ in video + audio + subs:
            cmd.extend(["-map", f"{fi}:{si}"])
        for a_idx in range(len(audio)):
            cmd.extend([f"-disposition:a:{a_idx}", "default" if a_idx == 0 else "0"])
    else:
        order = sorted(
            range(len(all_files)),
            key=lambda i: 0 if filename_is_hindi(all_files[i], prefix) else 1
        )
        vids = [i for i in order if not all_files[i].lower().endswith(AUDIO_ONLY_EXTS)]
        auds = [i for i in order if i not in vids]
        for i in vids + auds:
            cmd.extend(["-map", str(i)])

    cmd.extend(["-c", "copy", "-copyts", "-ss", str(max_start)])
    if min_end != float('inf'):
        cmd.extend(["-to", str(min_end)])
    cmd.extend(["-avoid_negative_ts", "make_zero", "-y", output_filename])
    return cmd


def generate_thumbnail(video_path, thumb_path):
    try:
        cmd = ["ffmpeg", "-y", "-ss", "00:00:03", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    return None


def make_progress_bar(percent):
    percent = max(0, min(100, percent))
    filled = int(percent // 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {percent}%"


def normalize_duration(text):
    """'10' (minutes) or 'HH:MM:SS' -> 'HH:MM:SS', else None."""
    text = text.strip()
    if text.isdigit():
        mins = int(text)
        if mins <= 0:
            return None
        return f"{mins // 60:02d}:{mins % 60:02d}:00"
    if re.match(r"^\d{2}:\d{2}:\d{2}$", text):
        h, m, s = map(int, text.split(":"))
        if h * 3600 + m * 60 + s > 0:
            return text
    return None


def parse_duration(text):
    """Return (valid, duration_str). duration_str is None for unlimited recording."""
    t = text.strip().lower()
    if t in ("inf", "infinite", "unlimited", "live", "forever", "∞"):
        return True, None
    d = normalize_duration(t)
    return d is not None, d


def safe_remove(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def lang_label(lang):
    if not lang or lang in ("und", "unk"):
        return None
    return LANG_NAMES.get(lang, lang.upper())


def channel_layout(n):
    return {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(n, str(n) if n else "")


def build_caption(title, streams, actual_sec, size_bytes, planned, mode, note="", part=None):
    lines = [f"📺 **{title}**"]
    if part:
        lines.append(f"📦 Part {part[0]}/{part[1]}")
    lines.append(f"⏱ Duration: {fmt_duration(actual_sec)}" + (f"  (planned {planned})" if planned else "  (unlimited)"))
    lines.append(f"💾 Size: {fmt_size(size_bytes)}")

    vid = next((s for s in streams if s["type"] == "video"), None)
    if vid and vid.get("width") and vid.get("height"):
        lines.append(f"🎞 Video: {vid['width']}x{vid['height']} ({vid['codec'].upper()})")

    auds = [s for s in streams if s["type"] == "audio"]
    if auds:
        items = []
        for i, a in enumerate(auds, 1):
            name = lang_label(a["lang"]) or f"Track {i}"
            detail = " ".join(x for x in (a["codec"].upper(), channel_layout(a.get("channels"))) if x)
            items.append(f"{name} ({detail})" if detail else name)
        lines.append(f"🔊 Audio ({len(auds)}): " + " • ".join(items))

    lines.append(f"🎨 Watermark: {'Yes' if mode == 'wm' else 'No'}")
    lines.append(f"🕒 {datetime.now(TZ).strftime('%d %b %Y, %H:%M')} IST")
    if note:
        lines.append(f"⚠️ {note}")
    return "\n".join(lines)[:1020]


def mode_menu_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Record Normal", callback_data="mode_norm"),
            InlineKeyboardButton("🎨 Record + Watermark", callback_data="mode_wm")
        ],
        [InlineKeyboardButton("« Back", callback_data="prompt_search")]
    ])


def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Channels", callback_data="prompt_search")],
        [InlineKeyboardButton("📋 Active Recordings", callback_data="jobs_refresh")],
        [InlineKeyboardButton("📊 Bot Status & Stats", callback_data="bot_stats")],
        [InlineKeyboardButton("❌ Cancel All My Recordings", callback_data="cancel_active")]
    ])


def user_jobs(user_id):
    return {jid: j for jid, j in jobs.items() if j["user_id"] == user_id}


def render_jobs(user_id):
    mine = user_jobs(user_id)
    rows = []
    if not mine:
        text = "📋 **Active Recordings**\n\nNothing is recording right now."
    else:
        lines = ["📋 **Active Recordings**\n"]
        for jid, j in mine.items():
            dur = j["duration_str"] or "∞"
            lines.append(
                f"• `{jid}` — **{j['title']}**\n"
                f"   {j['stage']} • ⏱ {fmt_duration(time.time() - j['start'])} / {dur}"
            )
            if j["stage"] == "recording":
                rows.append([
                    InlineKeyboardButton(f"⏹ Stop & Send {jid[:4]}", callback_data=f"job_stop_{jid}"),
                    InlineKeyboardButton(f"❌ Cancel {jid[:4]}", callback_data=f"job_cancel_{jid}")
                ])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="jobs_refresh")])
    rows.append([InlineKeyboardButton("« Back to Menu", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


# ==========================================
# COMMAND & MENU HANDLERS
# ==========================================
NOT_AUTH_TEXT = "🚫 **Access Denied**\n\nYou are not authorized to use this bot."


@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎛 Open Control Menu", callback_data="main_menu")],
        [InlineKeyboardButton("🔍 Search Channels", callback_data="prompt_search")]
    ])
    await message.reply_text(
        "🎬 **JIO Stream Ripper Bot**\n\n"
        "• `/s <name>` - Search & select channels\n"
        "• `/r <num[,num2,...]> [min|HH:MM:SS|inf]` - Quick record (e.g. `/r 12 30`, `/r 12,15 inf`)\n"
        "• `/jobs` - See / stop running recordings\n"
        "• `/menu` - Control dashboard\n\n"
        "You can run as many recordings in parallel as you like.",
        reply_markup=keyboard
    )


@app.on_message(filters.command("menu"))
async def menu_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return
    await message.reply_text("🎛 **Jio Bot Control Panel**\nChoose an action below:", reply_markup=main_menu_markup())


@app.on_message(filters.command("jobs"))
async def jobs_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return
    text, markup = render_jobs(message.from_user.id)
    await message.reply_text(text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^jobs_refresh$"))
async def callback_jobs_refresh(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return
    await callback_query.answer()
    text, markup = render_jobs(callback_query.from_user.id)
    await safe_edit(callback_query.message, text, markup)


@app.on_callback_query(filters.regex(r"^main_menu$"))
async def callback_main_menu(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return
    await callback_query.answer()
    await safe_edit(callback_query.message, "🎛 **Jio Bot Control Panel**\nChoose an action below:", main_menu_markup())


@app.on_callback_query(filters.regex(r"^prompt_search$"))
async def callback_prompt_search(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return
    await callback_query.answer()
    await safe_edit(
        callback_query.message,
        "🔍 **Channel Search**\nSend your query as:\n`/s <channel_name>` (e.g., `/s pogo` or `/s sports`)\n\n"
        "Already know the channel number? Use `/r <num> <minutes>`."
    )


@app.on_callback_query(filters.regex(r"^bot_stats$"))
async def callback_bot_stats(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    await callback_query.answer("Fetching live stats...")
    content = await asyncio.to_thread(fetch_fresh_playlist)
    total_channels = len(parse_playlist_blocks(content))
    try:
        free = shutil.disk_usage(os.getcwd()).free
    except Exception:
        free = 0

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]])
    await safe_edit(
        callback_query.message,
        f"📊 **Bot Operational Status**\n\n"
        f"• **Recordings running (all users):** `{len(jobs)}`\n"
        f"• **Your recordings:** `{len(user_jobs(callback_query.from_user.id))}`\n"
        f"• **Total Playlist Channels:** `{total_channels}`\n"
        f"• **Free Disk Space:** `{fmt_size(free)}`\n"
        f"• **Proxy Status:** `Connected ✅`",
        keyboard
    )


@app.on_callback_query(filters.regex(r"^cancel_active$"))
async def callback_cancel_active(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    count = 0
    for j in user_jobs(callback_query.from_user.id).values():
        if j["stage"] == "recording" and j["action"] is None:
            j["action"] = "cancel"
            count += 1

    await callback_query.answer(f"Cancelling {count} recording(s)." if count else "No active recording found.", show_alert=True)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]])
    await safe_edit(callback_query.message, f"❌ Cancelled {count} recording(s).", keyboard)


@app.on_callback_query(filters.regex(r"^job_(stop|cancel)_([0-9a-f]{8})$"))
async def job_control_callback(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    action, job_id = callback_query.matches[0].group(1, 2)
    job = jobs.get(job_id)
    if not job or job["user_id"] != callback_query.from_user.id:
        await callback_query.answer("This recording has already finished.", show_alert=True)
        return
    if job["stage"] != "recording":
        await callback_query.answer("Recording already ended - it's being processed.", show_alert=True)
        return

    job["action"] = action
    if action == "stop":
        await callback_query.answer("⏹ Stopping and preparing what's recorded so far...", show_alert=True)
    else:
        await callback_query.answer("❌ Recording cancelled.", show_alert=True)


@app.on_message(filters.command("s"))
async def search_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return

    query = " ".join(message.command[1:]).strip().lower()
    if not query:
        await message.reply_text("⚠️ Please specify a search term. Example: `/s pogo`")
        return

    status_msg = await message.reply_text("🔍 Fetching fresh playlist via proxy & parsing channels...")
    content = await asyncio.to_thread(fetch_fresh_playlist)

    if not content:
        await status_msg.edit_text("❌ Failed to fetch playlist from remote server through proxy.")
        return

    channels = parse_playlist_blocks(content)
    matched = {num: data for num, data in channels.items() if query in data["name"].lower()}

    if not matched:
        await status_msg.edit_text(f"❌ No channels found matching `{query}`.")
        return

    user_selected_channels[message.from_user.id] = set()

    keyboard = []
    for num, data in list(matched.items())[:MAX_SEARCH_RESULTS]:
        keyboard.append([InlineKeyboardButton(f"☐ {num}. {data['name']}", callback_data=f"toggle_chan_{num}")])

    keyboard.append([InlineKeyboardButton("🚀 Proceed with Selected Channels", callback_data="confirm_channels")])
    keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="main_menu")])

    extra = f" (showing first {MAX_SEARCH_RESULTS} - refine your search)" if len(matched) > MAX_SEARCH_RESULTS else ""
    await status_msg.edit_text(
        f"✅ Found **{len(matched)}** channels matching `{query}`{extra}.\n"
        f"Tap channels to select/deselect them, then click Proceed:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@app.on_callback_query(filters.regex(r"^toggle_chan_(\d+)$"))
async def toggle_channel_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    channel_num = int(callback_query.matches[0].group(1))
    selected = user_selected_channels.setdefault(user_id, set())

    if channel_num in selected:
        selected.remove(channel_num)
        await callback_query.answer(f"Deselected channel {channel_num}")
    else:
        selected.add(channel_num)
        await callback_query.answer(f"Selected channel {channel_num}")

    content = await asyncio.to_thread(fetch_fresh_playlist)
    channels = parse_playlist_blocks(content)

    reply_markup = callback_query.message.reply_markup
    if not reply_markup:
        return

    new_keyboard = []
    for row in reply_markup.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("toggle_chan_"):
                c_num = int(btn.callback_data.split("_")[2])
                c_name = channels.get(c_num, {}).get("name", f"Channel {c_num}")
                checked = "☑" if c_num in selected else "☐"
                new_row.append(InlineKeyboardButton(f"{checked} {c_num}. {c_name}", callback_data=btn.callback_data))
            else:
                new_row.append(btn)
        new_keyboard.append(new_row)

    try:
        await callback_query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^confirm_channels$"))
async def confirm_channels_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    selected = user_selected_channels.get(user_id, set())
    if not selected:
        await callback_query.answer("⚠️ Please select at least one channel first!", show_alert=True)
        return

    await callback_query.answer()
    await safe_edit(
        callback_query.message,
        f"🎯 **Selected Channels:** `{sorted(selected)}`\n"
        f"🎵 **Audio:** All tracks (Hindi first)\n\nChoose Recording Mode:",
        mode_menu_markup()
    )


@app.on_callback_query(filters.regex(r"^mode_(norm|wm)$"))
async def mode_select_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    await callback_query.answer()
    user_record_mode[user_id] = callback_query.matches[0].group(1)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 1 Min", callback_data="dur_00:01:00"),
            InlineKeyboardButton("⏱ 5 Mins", callback_data="dur_00:05:00")
        ],
        [
            InlineKeyboardButton("⏱ 30 Mins", callback_data="dur_00:30:00"),
            InlineKeyboardButton("⏱ 1 Hour", callback_data="dur_01:00:00")
        ],
        [
            InlineKeyboardButton("♾ Until I Stop", callback_data="dur_inf"),
            InlineKeyboardButton("✏️ Custom", callback_data="dur_custom")
        ],
        [InlineKeyboardButton("« Back", callback_data="confirm_channels")]
    ])
    await safe_edit(callback_query.message, "⏱ **Select Recording Duration:**", keyboard)


@app.on_callback_query(filters.regex(r"^dur_custom$"))
async def custom_duration_prompt(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    user_waiting_duration[user_id] = True
    await callback_query.answer()
    await safe_edit(
        callback_query.message,
        "✏️ **Custom Duration**\n\n"
        "Type the recording duration in chat:\n"
        "• Minutes as a number (e.g. `10`)\n"
        "• Exact time `HH:MM:SS` (e.g. `00:15:00`)\n"
        "• `inf` for unlimited (stop it yourself with /jobs)"
    )


@app.on_message(filters.text & ~filters.command(["start", "menu", "s", "r", "jobs"]))
async def custom_duration_text_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        return
    user_id = message.from_user.id
    if not user_waiting_duration.get(user_id):
        return

    user_waiting_duration.pop(user_id, None)
    valid, duration_str = parse_duration(message.text)
    if not valid:
        duration_str = "00:05:00"
        await message.reply_text("❌ Invalid format. Defaulting duration to `00:05:00`.")

    channels = sorted(user_selected_channels.get(user_id, set()))
    if not channels:
        await message.reply_text("❌ No channels selected. Please search again using `/s`")
        return

    user_selected_channels[user_id] = set()
    await message.reply_text(
        f"🚀 Starting {len(channels)} recording(s) in parallel. Track them with /jobs"
    )
    await start_rips(client, message.chat.id, user_id, channels, duration_str)


@app.on_callback_query(filters.regex(r"^dur_(\d{2}:\d{2}:\d{2}|inf)$"))
async def preset_duration_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    channels = sorted(user_selected_channels.get(user_id, set()))
    if not channels:
        await callback_query.answer("No channels selected.", show_alert=True)
        return

    raw = callback_query.matches[0].group(1)
    duration_str = None if raw == "inf" else raw

    await callback_query.answer("Starting recordings...")
    user_selected_channels[user_id] = set()
    await safe_edit(
        callback_query.message,
        f"🚀 Starting {len(channels)} recording(s) in parallel.\nTrack or stop them with /jobs"
    )
    await start_rips(client, callback_query.message.chat.id, user_id, channels, duration_str)


@app.on_message(filters.command("r"))
async def quick_record_handler(client: Client, message: Message):
    """/r <num[,num2]> [minutes|HH:MM:SS|inf]  - record without the menus."""
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return

    args = message.command[1:]
    nums = [a for a in (args[0].split(",") if args else []) if a.strip()]
    if not nums or not all(n.strip().isdigit() for n in nums):
        await message.reply_text(
            "⚠️ Usage: `/r <channel_number[,more]> [minutes|HH:MM:SS|inf]`\n"
            "Examples: `/r 12 30` • `/r 12,15 00:45:00` • `/r 12 inf`\n"
            "Default duration is 5 minutes."
        )
        return

    channels = [int(n) for n in nums]
    valid, duration_str = parse_duration(args[1]) if len(args) > 1 else (True, "00:05:00")
    if not valid:
        await message.reply_text("❌ Invalid duration. Use minutes (`30`), `HH:MM:SS` or `inf`.")
        return

    await message.reply_text(
        f"🚀 Starting {len(channels)} recording(s) "
        f"({duration_str or 'unlimited'}). Track them with /jobs"
    )
    await start_rips(client, message.chat.id, message.from_user.id, channels, duration_str)


async def start_rips(client, chat_id, user_id, channels, duration_str):
    """Launch every recording as an independent background task (no limit)."""
    for channel_num in channels:
        spawn(execute_rip_process(client, chat_id, user_id, channel_num, duration_str))
        await asyncio.sleep(1)  # small stagger to avoid message flood limits


# ==========================================
# RECORDING ENGINE
# ==========================================
def _signal_process(proc, sig):
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), sig)
        elif sig == "kill":
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


async def stop_process(proc, graceful):
    """Stop N_m3u8DL-RE (and any child like shaka-packager). Graceful = Ctrl+C first."""
    if proc.returncode is not None:
        return
    if graceful and os.name == "posix":
        _signal_process(proc, signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
            return
        except asyncio.TimeoutError:
            pass
    _signal_process(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=8)
        return
    except asyncio.TimeoutError:
        pass
    _signal_process(proc, signal.SIGKILL if os.name == "posix" else "kill")
    await proc.wait()


async def drain_stdout(proc, tail):
    """Consume process output so the pipe never fills; keep a short tail for debugging."""
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            tail.append(chunk.decode("utf-8", errors="ignore"))
            if len(tail) > 20:
                del tail[:-20]
    except Exception:
        pass


async def split_file(path, out_dir, total_sec, size_bytes):
    """Split a too-big file into parts below Telegram's limit (cut on keyframes, no re-encode)."""
    parts = max(2, math.ceil(size_bytes / SPLIT_TARGET))
    seg_time = max(60, int(total_sec / parts))
    pattern = os.path.join(out_dir, "part_%02d.mkv")
    cmd = [
        "ffmpeg", "-y", "-i", path, "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(seg_time), "-reset_timestamps", "1", pattern
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    await proc.communicate()
    return sorted(glob.glob(os.path.join(out_dir, "part_*.mkv")))


async def execute_rip_process(client, chat_id, user_id, channel_num, duration_str):
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    job = {
        "user_id": user_id, "chat_id": chat_id, "channel_num": channel_num,
        "title": f"Channel {channel_num}", "duration_str": duration_str,
        "proc": None, "action": None, "stage": "starting", "start": time.time(), "note": ""
    }
    jobs[job_id] = job
    status_msg = None

    try:
        status_msg = await client.send_message(chat_id, f"⏳ Preparing channel `{channel_num}`...")

        content = await asyncio.to_thread(fetch_fresh_playlist)
        if not content:
            await safe_edit(status_msg, "❌ Error fetching live playlist through proxy.")
            return "error"

        fresh_channels = parse_playlist_blocks(content)
        if channel_num not in fresh_channels:
            await safe_edit(status_msg, f"❌ Channel `{channel_num}` not found in fresh playlist.")
            return "error"

        url, clean_name, drm_keys, headers = parse_m3u_block(fresh_channels[channel_num]["block"])
        if not url:
            await safe_edit(status_msg, f"❌ Could not extract stream URL for channel {channel_num}.")
            return "error"

        title = fresh_channels[channel_num]["name"]
        job["title"] = title
        record_mode = user_record_mode.get(user_id, "norm")

        stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        output_filename = os.path.join(job_dir, f"{clean_name}_{stamp}.mkv")
        watermarked_path = os.path.join(job_dir, f"wm_{clean_name}_{stamp}.mkv")
        thumb_path_local = os.path.join(job_dir, "thumb.jpg")

        command = [
            "N_m3u8DL-RE", url,
            "--save-name", TEMP_PREFIX,
            "--save-dir", job_dir,
            "--tmp-dir", job_dir,
            "--use-shaka-packager",
            "--live-real-time-merge",
        ]
        if duration_str:
            command.extend(["--live-record-limit", duration_str])
        command.extend([
            "--custom-proxy", DOWNLOAD_PROXY,
            "--select-video", "best",
            "--select-audio", AUDIO_SELECTION,
            "--thread-count", "16"
        ])
        command.extend(headers)
        command.extend(drm_keys)

        control_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Stop & Send", callback_data=f"job_stop_{job_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"job_cancel_{job_id}")
        ]])

        total_expected_sec = 0
        if duration_str:
            h, m, s = map(int, duration_str.split(':'))
            total_expected_sec = h * 3600 + m * 60 + s

        def progress_text(elapsed, size_now):
            if total_expected_sec:
                pct = int(min(99, elapsed / total_expected_sec * 100))
                body = make_progress_bar(pct)
                target = duration_str
            else:
                body = "♾ Unlimited - recording until you press Stop"
                target = "Unlimited"
            return (
                f"⏺ **Ripping Channel {channel_num}:** `{title}`\n"
                f"🆔 `{job_id}`\n"
                f"🎯 Target: `{target}`\n"
                f"⏱ Elapsed: `{fmt_duration(elapsed)}`\n"
                f"💾 Recorded: `{fmt_size(size_now)}`\n\n{body}"
            )

        await safe_edit(status_msg, progress_text(0, 0), control_markup)

        popen_kwargs = {"start_new_session": True} if os.name == "posix" else {}
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=job_dir, **popen_kwargs
        )
        job["proc"] = proc
        job["stage"] = "recording"
        job["start"] = time.time()
        tail = []
        drain_task = asyncio.create_task(drain_stdout(proc, tail))

        last_edit = 0
        while proc.returncode is None:
            if job["action"] in ("stop", "cancel"):
                break

            now = time.time()
            try:
                if shutil.disk_usage(JOBS_DIR).free < MIN_FREE_BYTES:
                    job["action"] = "stop"
                    job["note"] = "Stopped early: low disk space"
                    break
            except Exception:
                pass

            if now - last_edit >= PROGRESS_INTERVAL:
                last_edit = now
                size_now = await asyncio.to_thread(dir_size, job_dir)
                await safe_edit(status_msg, progress_text(now - job["start"], size_now), control_markup)

            await asyncio.sleep(1)

        action_taken = job["action"]
        if proc.returncode is None:
            await stop_process(proc, graceful=(action_taken != "cancel"))
        await proc.wait()
        await drain_task
        elapsed_total = time.time() - job["start"]

        if action_taken == "cancel":
            await safe_edit(status_msg, f"❌ Recording of `{title}` cancelled.")
            return "cancelled"

        # ==========================================
        # SMART MUXING (Hindi audio first)
        # ==========================================
        job["stage"] = "muxing"
        all_raw = [
            os.path.join(job_dir, f) for f in os.listdir(job_dir)
            if f.startswith(TEMP_PREFIX) and os.path.isfile(os.path.join(job_dir, f))
        ]
        all_files = [f for f in all_raw if f.lower().endswith(MEDIA_EXTS)]
        if not all_files:
            all_files = [f for f in all_raw if not f.endswith(('.jpg', '.srt', '.vtt'))]
        all_files.sort()

        if not all_files:
            logger.error("N_m3u8DL-RE output tail: %s", "".join(tail)[-1500:])
            await safe_edit(status_msg, f"❌ `{title}`: no decrypted tracks found (stream may be offline or keys expired).")
            return "error"

        await safe_edit(status_msg, f"⚙️ **Muxing `{title}` (Hindi audio first)...**\n\n{make_progress_bar(50)}")

        max_start = 0.0
        min_end = float('inf')
        for f in all_files:
            start, end = await asyncio.to_thread(get_media_info, f)
            if start > max_start:
                max_start = start
            if end < min_end:
                min_end = end

        mux_cmd = await asyncio.to_thread(
            build_mux_command, all_files, TEMP_PREFIX, max_start, min_end, output_filename
        )
        logger.info("[%s] Mux command: %s", job_id, " ".join(mux_cmd))

        mux_proc = await asyncio.create_subprocess_exec(*mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, mux_err = await mux_proc.communicate()

        if not os.path.exists(output_filename) or os.path.getsize(output_filename) < 10000:
            logger.error("[%s] Mux failed: %s", job_id, mux_err.decode("utf-8", errors="ignore")[-1500:])
            await safe_edit(status_msg, f"❌ `{title}`: muxing failed (output missing or empty).")
            return "error"

        final_file = output_filename

        if record_mode == "wm":
            await safe_edit(status_msg, f"🎨 **Applying watermark to `{title}`...**\n\n{make_progress_bar(50)}")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", output_filename, "-map", "0",
                "-vf", get_watermark_drawtext_filter(),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy", "-c:s", "copy",
                watermarked_path
            ]
            ff_proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await ff_proc.communicate()
            if os.path.exists(watermarked_path) and os.path.getsize(watermarked_path) > 50000:
                final_file = watermarked_path

        # ---- gather info for the caption ----
        streams = await asyncio.to_thread(probe_streams, final_file)
        start_abs, end_abs = await asyncio.to_thread(get_media_info, final_file)
        actual_sec = end_abs - start_abs
        if not (0 < actual_sec < float('inf')):
            actual_sec = elapsed_total
        thumb_path = await asyncio.to_thread(generate_thumbnail, final_file, thumb_path_local)

        final_size = os.path.getsize(final_file)
        upload_list = [final_file]
        if final_size > TG_LIMIT:
            await safe_edit(
                status_msg,
                f"✂️ **File is {fmt_size(final_size)} - splitting into parts for Telegram...**"
            )
            parts = await split_file(final_file, job_dir, actual_sec, final_size)
            if parts:
                upload_list = parts

        # ---- upload ----
        job["stage"] = "uploading"
        note = job["note"] or ("Stopped manually" if action_taken == "stop" else "")
        for idx, path in enumerate(upload_list, 1):
            part_info = (idx, len(upload_list)) if len(upload_list) > 1 else None
            part_label = f" (part {idx}/{len(upload_list)})" if part_info else ""
            size_p = os.path.getsize(path)

            if part_info:
                p_start, p_end = await asyncio.to_thread(get_media_info, path)
                part_sec = (p_end - p_start) if 0 < (p_end - p_start) < float('inf') else actual_sec / len(upload_list)
            else:
                part_sec = actual_sec

            caption = build_caption(
                title, streams, part_sec, size_p, duration_str, record_mode, note, part_info
            )

            last_prog = {"t": 0}

            async def upload_progress(current, total, _label=part_label):
                now = time.time()
                if current != total and now - last_prog["t"] < 6:
                    return
                last_prog["t"] = now
                pct = int(current * 100 / total) if total else 0
                await safe_edit(
                    status_msg,
                    f"⬆️ **Uploading `{title}`{_label}**\n"
                    f"{fmt_size(current)} / {fmt_size(total)}\n\n{make_progress_bar(pct)}"
                )

            await safe_edit(status_msg, f"⬆️ **Uploading `{title}`{part_label}...**\n\n{make_progress_bar(0)}")
            await client.send_video(
                chat_id=chat_id,
                video=path,
                duration=max(1, int(part_sec)),
                thumb=thumb_path if thumb_path else None,
                supports_streaming=True,
                caption=caption,
                progress=upload_progress
            )

        try:
            await status_msg.delete()
        except Exception:
            pass
        return "ok"

    except Exception as e:
        logger.exception("[%s] Rip failed", job_id)
        await safe_edit(status_msg, f"❌ Error during execution: {e}")
        return "error"

    finally:
        proc_ref = job.get("proc")
        if proc_ref is not None and proc_ref.returncode is None:
            try:
                await stop_process(proc_ref, graceful=False)
            except Exception:
                pass
        jobs.pop(job_id, None)
        safe_remove(job_dir)


if __name__ == "__main__":
    # wipe leftovers from a previous crash, then make a clean jobs folder
    safe_remove(JOBS_DIR)
    os.makedirs(JOBS_DIR, exist_ok=True)
    print("Bot starting up: unlimited parallel recordings, Hindi-first multi-audio...", flush=True)
    app.run()
