import os
import re
import glob
import time
import shutil
import json
import logging
import asyncio
import subprocess
import requests
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

# Language tags/tokens treated as Hindi
HINDI_TAGS = {"hi", "hin", "hindi"}

# Always record every audio track
AUDIO_SELECTION = "all"

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

active_processes = {}
user_job_controls = {}
running_users = set()
playlist_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60

user_selected_channels = {}
user_record_mode = {}       # "norm" or "wm"
user_waiting_duration = {}

MEDIA_EXTS = (".mp4", ".ts", ".m4a", ".aac", ".mka", ".mkv", ".ac3", ".eac3", ".m4s")


# ==========================================
# HELPERS
# ==========================================
def is_authorized(user) -> bool:
    return bool(user) and user.id in AUTHORIZED_USERS


def fetch_fresh_playlist():
    global playlist_cache
    current_time = time.time()
    if playlist_cache["data"] and (current_time - playlist_cache["timestamp"] < CACHE_TTL):
        return playlist_cache["data"]

    logger.info("Fetching fresh playlist via proxy using requests...")
    try:
        proxies = {
            'http': DOWNLOAD_PROXY,
            'https': DOWNLOAD_PROXY,
        }
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
        channels[channel_num] = {
            "name": name,
            "block": full_block.strip()
        }
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
    """Return a list of {index, type, lang} for every stream in a file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=index,codec_type:stream_tags=language",
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
                "lang": (s.get("tags", {}).get("language") or "").lower()
            })
        return streams
    except Exception as e:
        logger.warning(f"ffprobe failed for {filepath}: {e}")
        return []


def filename_is_hindi(filepath, prefix):
    """Check the part of the filename after the save-name prefix for a Hindi tag."""
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
    """Build ffmpeg mux cmd: video first, then audio (Hindi first), then subs."""
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
        audio.sort(key=lambda e: 0 if e[2] else 1)  # stable: Hindi first, rest keep order
        for fi, si, _ in video + audio + subs:
            cmd.extend(["-map", f"{fi}:{si}"])
        for a_idx in range(len(audio)):
            cmd.extend([f"-disposition:a:{a_idx}", "default" if a_idx == 0 else "0"])
    else:
        # ffprobe unavailable - fall back to file level ordering (Hindi files first among audio)
        order = sorted(
            range(len(all_files)),
            key=lambda i: 0 if filename_is_hindi(all_files[i], prefix) else 1
        )
        vids = [i for i in order if not all_files[i].lower().endswith((".m4a", ".aac", ".mka", ".ac3", ".eac3"))]
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
        cmd = [
            "ffmpeg", "-y",
            "-ss", "00:00:03",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            thumb_path
        ]
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
    """Return HH:MM:SS from '10' (minutes) or 'HH:MM:SS', else None."""
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


def safe_remove(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def mode_menu_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Record Normal", callback_data="mode_norm"),
            InlineKeyboardButton("🎨 Record + Watermark", callback_data="mode_wm")
        ],
        [InlineKeyboardButton("« Back", callback_data="prompt_search")]
    ])


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
        "• `/s <channel>` - Search & select multiple channels\n"
        "• `/menu` - Open interactive control dashboard",
        reply_markup=keyboard
    )


@app.on_message(filters.command("menu"))
async def menu_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        await message.reply_text(NOT_AUTH_TEXT)
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Channels", callback_data="prompt_search")],
        [InlineKeyboardButton("📊 Bot Status & Stats", callback_data="bot_stats")],
        [InlineKeyboardButton("❌ Cancel Active Rips", callback_data="cancel_active")]
    ])
    await message.reply_text(
        "🎛 **Jio Bot Control Panel**\nChoose an action below:",
        reply_markup=keyboard
    )


@app.on_callback_query(filters.regex(r"^main_menu$"))
async def callback_main_menu(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    await callback_query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Channels", callback_data="prompt_search")],
        [InlineKeyboardButton("📊 Bot Status & Stats", callback_data="bot_stats")],
        [InlineKeyboardButton("❌ Cancel Active Rips", callback_data="cancel_active")]
    ])
    try:
        await callback_query.message.edit_text(
            "🎛 **Jio Bot Control Panel**\nChoose an action below:",
            reply_markup=keyboard
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^prompt_search$"))
async def callback_prompt_search(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    await callback_query.answer()
    try:
        await callback_query.message.edit_text(
            "🔍 **Channel Search**\nPlease send your search query using the command format:\n`/s <channel_name>` (e.g., `/s pogo` or `/s sports`)"
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^bot_stats$"))
async def callback_bot_stats(client: Client, callback_query: CallbackQuery):
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    await callback_query.answer("Fetching live stats...")
    content = await asyncio.to_thread(fetch_fresh_playlist)
    total_channels = len(parse_playlist_blocks(content))
    active_jobs = len(active_processes)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]])
    try:
        await callback_query.message.edit_text(
            f"📊 **Bot Operational Status**\n\n"
            f"• **Active Rips Running:** `{active_jobs}`\n"
            f"• **Total Playlist Channels:** `{total_channels}`\n"
            f"• **Proxy Status:** `Connected ✅`",
            reply_markup=keyboard
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^cancel_active$"))
async def callback_cancel_active(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    proc = active_processes.get(user_id)
    if proc:
        try:
            user_job_controls[user_id] = "cancel"
            proc.terminate()
            await callback_query.answer("Active rip process terminated.", show_alert=True)
        except Exception:
            await callback_query.answer("Failed to terminate process.", show_alert=True)
    else:
        await callback_query.answer("No active rip found for you.", show_alert=True)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]])
    try:
        await callback_query.message.edit_text("❌ Active download processes cleared.", reply_markup=keyboard)
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^job_control_(pause|cancel)$"))
async def job_control_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    action = callback_query.matches[0].group(1)
    user_job_controls[user_id] = action

    proc = active_processes.get(user_id)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass

    if action == "pause":
        await callback_query.answer("⏸️ Pausing and preparing partial file...", show_alert=True)
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
    for num, data in list(matched.items())[:12]:
        keyboard.append([InlineKeyboardButton(f"☐ {num}. {data['name']}", callback_data=f"toggle_chan_{num}")])

    keyboard.append([InlineKeyboardButton("🚀 Proceed with Selected Channels", callback_data="confirm_channels")])
    keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="main_menu")])

    await status_msg.edit_text(
        f"✅ Found **{len(matched)}** channels matching `{query}`"
        f"{' (showing first 12)' if len(matched) > 12 else ''}.\n"
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
    if user_id not in user_selected_channels:
        user_selected_channels[user_id] = set()

    if channel_num in user_selected_channels[user_id]:
        user_selected_channels[user_id].remove(channel_num)
        await callback_query.answer(f"Deselected channel {channel_num}")
    else:
        user_selected_channels[user_id].add(channel_num)
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
                checked = "☑" if c_num in user_selected_channels[user_id] else "☐"
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
    # Audio track menu removed: all audio tracks are always recorded (Hindi is placed first when muxing)
    try:
        await callback_query.message.edit_text(
            f"🎯 **Selected Channels:** `{sorted(selected)}`\n"
            f"🎵 **Audio:** All tracks (Hindi first)\n\nChoose Recording Mode:",
            reply_markup=mode_menu_markup()
        )
    except Exception:
        pass


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
            InlineKeyboardButton("⏱ Custom Duration", callback_data="dur_custom")
        ],
        [InlineKeyboardButton("« Back", callback_data="confirm_channels")]
    ])

    await callback_query.message.edit_text(
        "⏱ **Select Recording Duration:**",
        reply_markup=keyboard
    )


@app.on_callback_query(filters.regex(r"^dur_custom$"))
async def custom_duration_prompt(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    user_waiting_duration[user_id] = True

    await callback_query.answer()
    await callback_query.message.edit_text(
        "✏️ **Custom Duration**\n\n"
        "Please type the recording duration in chat.\n"
        "• Format 1: Minutes as a number (e.g. `10` for 10 minutes)\n"
        "• Format 2: Exact timestamp `HH:MM:SS` (e.g. `00:15:00`)"
    )


@app.on_message(filters.text & ~filters.command(["start", "menu", "s"]))
async def custom_duration_text_handler(client: Client, message: Message):
    if not is_authorized(message.from_user):
        return
    user_id = message.from_user.id
    if not user_waiting_duration.get(user_id):
        return

    user_waiting_duration.pop(user_id, None)

    duration_str = normalize_duration(message.text)
    if not duration_str:
        duration_str = "00:05:00"
        await message.reply_text("❌ Invalid format. Defaulting duration to `00:05:00`.")

    await start_rips(client, message.chat.id, user_id, duration_str)


@app.on_callback_query(filters.regex(r"^dur_(\d{2}:\d{2}:\d{2})$"))
async def preset_duration_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_authorized(callback_query.from_user):
        await callback_query.answer("Not authorized!", show_alert=True)
        return

    if not user_selected_channels.get(user_id):
        await callback_query.answer("No channels selected.", show_alert=True)
        return
    if user_id in running_users:
        await callback_query.answer("A recording is already running for you.", show_alert=True)
        return

    await callback_query.answer("Starting rip process...")
    duration_str = callback_query.matches[0].group(1)

    try:
        await callback_query.message.edit_text("🚀 Starting rips...")
    except Exception:
        pass

    await start_rips(client, callback_query.message.chat.id, user_id, duration_str)


async def start_rips(client, chat_id, user_id, duration_str):
    selected = sorted(user_selected_channels.get(user_id, set()))
    if not selected:
        await client.send_message(chat_id, "❌ No channels selected. Please search again using `/s`")
        return
    if user_id in running_users:
        await client.send_message(chat_id, "⚠️ A recording is already running. Cancel it first.")
        return

    user_selected_channels[user_id] = set()
    running_users.add(user_id)
    try:
        for channel_num in selected:
            result = await execute_rip_process(client, chat_id, user_id, channel_num, duration_str)
            if result == "cancelled":
                break
    finally:
        running_users.discard(user_id)


async def drain_stdout(proc, tail):
    """Consume process output so the pipe never fills up; keep a short tail for debugging."""
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


async def execute_rip_process(client, chat_id, user_id, channel_num, duration_str):
    status_msg = await client.send_message(chat_id, f"⏳ Preparing channel `{channel_num}`...")

    content = await asyncio.to_thread(fetch_fresh_playlist)
    if not content:
        await status_msg.edit_text("❌ Error fetching live playlist through proxy.")
        return "error"

    fresh_channels = parse_playlist_blocks(content)
    if channel_num not in fresh_channels:
        await status_msg.edit_text(f"❌ Channel `{channel_num}` not found in fresh playlist.")
        return "error"

    url, clean_name, drm_keys, headers = parse_m3u_block(fresh_channels[channel_num]["block"])
    if not url:
        await status_msg.edit_text(f"❌ Error: Could not extract stream URL for channel {channel_num}.")
        return "error"

    channel_title = fresh_channels[channel_num]["name"]
    record_mode = user_record_mode.get(user_id, "norm")

    job_id = f"{user_id}_{int(time.time())}"
    temp_prefix_local = f"raw_stream_{job_id}"
    output_filename = f"{clean_name}_{job_id}.mkv"
    watermarked_path = f"wm_{output_filename}"
    thumb_path_local = f"thumb_{job_id}.jpg"

    command = [
        "N_m3u8DL-RE",
        url,
        "--save-name", temp_prefix_local,
        "--use-shaka-packager",
        "--live-real-time-merge",
        "--live-record-limit", duration_str,
        "--custom-proxy", DOWNLOAD_PROXY,
        "--select-video", "best",
        "--select-audio", AUDIO_SELECTION,   # always all audio tracks
        "--thread-count", "16"
    ]
    command.extend(headers)
    command.extend(drm_keys)

    user_job_controls[user_id] = None
    result_status = "error"

    try:
        control_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸️ Pause & Send", callback_data="job_control_pause"),
                InlineKeyboardButton("❌ Cancel", callback_data="job_control_cancel")
            ]
        ])

        def progress_text(pct):
            return (
                f"⏺ **Ripping Channel {channel_num}:** `{channel_title}`\n"
                f"⏱ Target Duration: `{duration_str}`\n\n{make_progress_bar(pct)}"
            )

        await status_msg.edit_text(progress_text(0), reply_markup=control_markup)

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        active_processes[user_id] = proc
        tail = []
        drain_task = asyncio.create_task(drain_stdout(proc, tail))

        h, m, s = map(int, duration_str.split(':'))
        total_expected_sec = h * 3600 + m * 60 + s
        start_time = time.time()
        last_pct = -1

        while proc.returncode is None:
            if user_job_controls.get(user_id) in ("pause", "cancel"):
                break

            elapsed = time.time() - start_time
            pct = int(min(99, (elapsed / total_expected_sec) * 100)) if total_expected_sec > 0 else 0
            if pct != last_pct:
                try:
                    await status_msg.edit_text(progress_text(pct), reply_markup=control_markup)
                    last_pct = pct
                except Exception:
                    pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

        if proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        await proc.wait()
        await drain_task
        active_processes.pop(user_id, None)

        action_taken = user_job_controls.pop(user_id, None)
        if action_taken == "cancel":
            await status_msg.edit_text("❌ Recording cancelled by user.")
            result_status = "cancelled"
            return result_status

        # ==========================================
        # SMART MUXING (Hindi audio first)
        # ==========================================
        all_raw = [f for f in glob.glob(f"{temp_prefix_local}*") if os.path.isfile(f)]
        all_files = [f for f in all_raw if f.lower().endswith(MEDIA_EXTS)]
        if not all_files:
            all_files = [f for f in all_raw if not f.endswith(('.jpg', '.srt', '.vtt'))]
        all_files.sort()

        if not all_files:
            logger.error("N_m3u8DL-RE output tail: %s", "".join(tail)[-1500:])
            await status_msg.edit_text("❌ Error: No decrypted tracks found.")
            return "error"

        await status_msg.edit_text(f"⚙️ **Smart Muxing & Syncing Tracks (Hindi audio first)...**\n\n{make_progress_bar(50)}")

        max_start = 0.0
        min_end = float('inf')
        for f in all_files:
            start, end = await asyncio.to_thread(get_media_info, f)
            if start > max_start:
                max_start = start
            if end < min_end:
                min_end = end

        mux_cmd = await asyncio.to_thread(
            build_mux_command, all_files, temp_prefix_local, max_start, min_end, output_filename
        )
        logger.info("Mux command: %s", " ".join(mux_cmd))

        mux_proc = await asyncio.create_subprocess_exec(
            *mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, mux_err = await mux_proc.communicate()

        if not os.path.exists(output_filename) or os.path.getsize(output_filename) < 10000:
            logger.error("Mux failed: %s", mux_err.decode("utf-8", errors="ignore")[-1500:])
            await status_msg.edit_text("❌ Error: Muxing failed (output file missing or empty).")
            return "error"

        final_file = output_filename

        if record_mode == "wm":
            await status_msg.edit_text(f"🎨 **Applying Watermark Filter...**\n\n{make_progress_bar(50)}")
            wm_filter = get_watermark_drawtext_filter()

            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", output_filename,
                "-map", "0",
                "-vf", wm_filter,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "copy",
                "-c:s", "copy",
                watermarked_path
            ]

            ff_proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await ff_proc.communicate()

            if os.path.exists(watermarked_path) and os.path.getsize(watermarked_path) > 50000:
                final_file = watermarked_path

        thumb_path = await asyncio.to_thread(generate_thumbnail, final_file, thumb_path_local)

        try:
            start_abs, final_duration_abs = await asyncio.to_thread(get_media_info, final_file)
            video_duration_sec = int(final_duration_abs - start_abs)
            if video_duration_sec <= 0:
                video_duration_sec = 60
        except Exception:
            video_duration_sec = 60

        suffix_label = " (Paused & Sent)" if action_taken == "pause" else ""
        await status_msg.edit_text(f"⬆️ **Uploading to Telegram{suffix_label}...**\n\n{make_progress_bar(50)}")

        await client.send_video(
            chat_id=chat_id,
            video=final_file,
            duration=video_duration_sec,
            thumb=thumb_path if thumb_path else None,
            supports_streaming=True,
            caption=f"📺 Channel: {channel_title}{suffix_label}\n⏱ Duration: {duration_str}"
        )
        try:
            await status_msg.delete()
        except Exception:
            pass

        result_status = "ok"
        return result_status

    except Exception as e:
        logger.exception("Rip failed")
        try:
            await status_msg.edit_text(f"❌ Error during execution: {e}")
        except Exception:
            pass
        return "error"

    finally:
        active_processes.pop(user_id, None)
        user_job_controls.pop(user_id, None)
        for f in glob.glob(f"{temp_prefix_local}*") + [output_filename, watermarked_path, thumb_path_local]:
            safe_remove(f)


if __name__ == "__main__":
    print("Bot starting up with multi-audio (Hindi first) support...", flush=True)
    app.run()
