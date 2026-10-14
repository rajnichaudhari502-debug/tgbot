import os
import re
import glob
import time
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
playlist_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60

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
    escaped_font = font_path.replace(":", r"\:")
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
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+).*?start:\s*(\-?\d+\.\d+)", result.stderr)
    if match:
        h, m, s, start_str = match.groups()
        duration = float(h) * 3600 + float(m) * 60 + float(s)
        start = float(start_str)
        return start, start + duration
    return 0.0, float('inf')

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

# ==========================================
# COMMAND & MENU HANDLERS
# ==========================================

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎛 Open Control Menu", callback_data="main_menu")],
        [InlineKeyboardButton("🔍 Search Channels", callback_data="prompt_search")]
    ])
    await message.reply_text(
        "🎬 **JIO Stream Ripper Bot**\n\n"
        "• `/s <channel>` - Search channels instantly\n"
        "• `/menu` - Open interactive control dashboard",
        reply_markup=keyboard
    )

@app.on_message(filters.command("menu"))
async def menu_handler(client: Client, message: Message):
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
    await callback_query.answer()
    try:
        await callback_query.message.edit_text(
            "🔍 **Channel Search**\nPlease send your search query using the command format:\n`/s <channel_name>` (e.g., `/s pogo` or `/s sports`)"
        )
    except Exception:
        pass

@app.on_callback_query(filters.regex(r"^bot_stats$"))
async def callback_bot_stats(client: Client, callback_query: CallbackQuery):
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
    if user_id in active_processes:
        try:
            active_processes[user_id].terminate()
            del active_processes[user_id]
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

@app.on_message(filters.command("s"))
async def search_handler(client: Client, message: Message):
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

    keyboard = []
    for num, data in list(matched.items())[:12]:
        keyboard.append([InlineKeyboardButton(f"📺 {num}. {data['name']}", callback_data=f"chan_{num}")])
    keyboard.append([InlineKeyboardButton("« Back to Menu", callback_data="main_menu")])

    await status_msg.edit_text(
        f"✅ Found **{len(matched)}** channels matching `{query}`.\nSelect a channel below to record:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@app.on_callback_query(filters.regex(r"^chan_(\d+)$"))
async def channel_click_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()
    channel_num = int(callback_query.matches[0].group(1))
    
    content = await asyncio.to_thread(fetch_fresh_playlist)
    channels = parse_playlist_blocks(content)
    
    if channel_num not in channels:
        await callback_query.answer("Channel no longer available in fresh playlist. Search again.", show_alert=True)
        return

    chan_name = channels[channel_num]["name"]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Record Normal", callback_data=f"rec_norm_{channel_num}"),
            InlineKeyboardButton("🎨 Record + Watermark", callback_data=f"rec_wm_{channel_num}")
        ],
        [InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]
    ])

    try:
        await callback_query.message.edit_text(
            f"🎯 **Selected Channel:** `{chan_name}`\n\nChoose your recording mode:",
            reply_markup=keyboard
        )
    except Exception:
        pass

@app.on_callback_query(filters.regex(r"^rec_(norm|wm)_(\d+)$"))
async def record_mode_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()
    mode = callback_query.matches[0].group(1)
    channel_num = int(callback_query.matches[0].group(2))

    content = await asyncio.to_thread(fetch_fresh_playlist)
    channels = parse_playlist_blocks(content)
    if channel_num not in channels:
        await callback_query.answer("Channel unavailable.", show_alert=True)
        return

    chan_data = channels[channel_num]
    mode_label = "Record with Watermark" if mode == "wm" else "Normal Record"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 1 Min", callback_data=f"rip_{mode}_{channel_num}_00:01:00"),
            InlineKeyboardButton("⏱ 5 Mins", callback_data=f"rip_{mode}_{channel_num}_00:05:00")
        ],
        [
            InlineKeyboardButton("⏱ 30 Mins", callback_data=f"rip_{mode}_{channel_num}_00:30:00"),
            InlineKeyboardButton("⏱ 1 Hour", callback_data=f"rip_{mode}_{channel_num}_01:00:00")
        ],
        [InlineKeyboardButton("« Back", callback_data=f"chan_{channel_num}")]
    ])

    try:
        await callback_query.message.edit_text(
            f"🎯 **Channel:** `{chan_data['name']}`\n"
            f"⚙️ **Mode:** `{mode_label}`\n\n"
            f"Select recording duration to start instantly:",
            reply_markup=keyboard
        )
    except Exception:
        pass

@app.on_callback_query(filters.regex(r"^rip_(norm|wm)_(\d+)_(\d{2}:\d{2}:\d{2})$"))
async def instant_rip_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id in active_processes:
        await callback_query.answer("⚠️ You already have an active recording running! Please wait or cancel it from the menu.", show_alert=True)
        return

    await callback_query.answer("Starting rip process...")
    mode = callback_query.matches[0].group(1)
    channel_num = int(callback_query.matches[0].group(2))
    duration_str = callback_query.matches[0].group(3)

    status_msg = callback_query.message
    try:
        await status_msg.edit_text("📥 Fetching fresh keys & starting rip...")
    except Exception:
        pass
    
    content = await asyncio.to_thread(fetch_fresh_playlist)
    if not content:
        await status_msg.edit_text("❌ Error fetching live playlist through proxy.")
        return

    fresh_channels = parse_playlist_blocks(content)
    if channel_num not in fresh_channels:
        await status_msg.edit_text(f"❌ Channel `{channel_num}` not found in fresh playlist.")
        return

    url, clean_name, drm_keys, headers = parse_m3u_block(fresh_channels[channel_num]["block"])
    if not url:
        await status_msg.edit_text("❌ Error: Could not extract stream URL.")
        return

    # Unique per-job filenames to prevent file collisions during simultaneous user rips
    job_id = f"{user_id}_{int(time.time())}"
    temp_prefix_local = f"raw_stream_{job_id}"
    output_filename = f"{clean_name}_{job_id}.mp4"
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
        "--select-audio", "all",
        "--thread-count", "16"
    ]
    command.extend(headers)
    command.extend(drm_keys)

    try:
        await status_msg.edit_text(f"⏺ **Ripping Channel {channel_num}:** `{fresh_channels[channel_num]['name']}`\n⏱ Duration: `{duration_str}`\n\n{make_progress_bar(0)}")
        
        proc = await asyncio.create_subprocess_exec(
            *command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT
        )
        active_processes[user_id] = proc

        h, m, s = map(int, duration_str.split(':'))
        total_expected_sec = h * 3600 + m * 60 + s
        start_time = time.time()
        last_update_time = 0

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode('utf-8', errors='ignore').strip()
            
            current_time = time.time()
            elapsed_sec = current_time - start_time

            pct = None
            if "%" in line_str:
                match = re.search(r'(\d+(?:\.\d+)?)%', line_str)
                if match:
                    pct = int(float(match.group(1)))

            if pct is None and total_expected_sec > 0:
                pct = int(min(99, (elapsed_sec / total_expected_sec) * 100))

            if pct is not None and (current_time - last_update_time > 3):
                try:
                    await status_msg.edit_text(
                        f"⏺ **Ripping Channel {channel_num}:** `{fresh_channels[channel_num]['name']}`\n"
                        f"⏱ Duration: `{duration_str}`\n\n"
                        f"{make_progress_bar(pct)}"
                    )
                    last_update_time = current_time
                except Exception:
                    pass

        await proc.wait()
        if user_id in active_processes:
            del active_processes[user_id]

        if proc.returncode != 0:
            await status_msg.edit_text(f"❌ Ripping failed with return code {proc.returncode}")
            return

        await status_msg.edit_text(f"✅ **Recording Complete!**\n\n{make_progress_bar(100)}")
        await asyncio.sleep(1)

        # Smart Muxing with Hindi-First Audio Sorting
        await status_msg.edit_text(f"⚙️ **Smart Muxing Tracks (Hindi Default)...**\n\n{make_progress_bar(50)}")
        
        video_files = glob.glob(f"{temp_prefix_local}*.mp4")
        audio_files = glob.glob(f"{temp_prefix_local}*.m4a")
        
        # Sort audio files so that Hindi ('hi') is placed first to make it the default track
        audio_files.sort(key=lambda f: 0 if 'hi' in f.lower() else 1)
        all_tracks = video_files + audio_files

        if not all_tracks:
            await status_msg.edit_text("❌ Error: No decrypted media tracks found.")
            return

        mux_cmd = ["ffmpeg"]
        for f in all_tracks:
            mux_cmd.extend(["-i", f])

        for i in range(len(all_tracks)):
            mux_cmd.extend(["-map", str(i)])

        mux_cmd.extend([
            "-c", "copy",
            "-movflags", "+faststart",
            "-y", output_filename
        ])

        mux_proc = await asyncio.create_subprocess_exec(*mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await mux_proc.communicate()

        await status_msg.edit_text(f"⚙️ **Muxing Complete!**\n\n{make_progress_bar(100)}")
        await asyncio.sleep(1)

        final_file = output_filename

        if mode == "wm":
            await status_msg.edit_text(f"🎨 **Applying Watermark Filter...**\n\n{make_progress_bar(50)}")
            watermarked_path = f"wm_{output_filename}"
            wm_filter = get_watermark_drawtext_filter()
            
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", output_filename,
                "-vf", wm_filter,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "copy",
                "-movflags", "+faststart",
                watermarked_path
            ]
            
            ff_proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await ff_proc.communicate()
            
            if os.path.exists(watermarked_path) and os.path.getsize(watermarked_path) > 50000:
                final_file = watermarked_path
            await status_msg.edit_text(f"🎨 **Watermar
