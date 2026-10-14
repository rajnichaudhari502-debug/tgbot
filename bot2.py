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
BOT_PASSWORD = "xcdfgh"

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
playlist_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60

authorized_users = set()
user_selected_channels = {} 
user_audio_mode = {}        # "all" or language code
user_record_mode = {}       # "norm" or "wm"
user_waiting_duration = {}  

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
    user_id = message.from_user.id
    if user_id not in authorized_users:
        await message.reply_text(
            "🔒 **Authentication Required**\n\n"
            "This bot is password protected. Please authenticate using:\n"
            "`/auth <password>`"
        )
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

@app.on_message(filters.command("auth"))
async def auth_handler(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        await message.reply_text("⚠️ Please provide the password. Example: `/auth xcdfgh`")
        return
    
    pwd = args[0].strip()
    user_id = message.from_user.id
    if pwd == BOT_PASSWORD:
        authorized_users.add(user_id)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎛 Open Control Menu", callback_data="main_menu")]])
        await message.reply_text("✅ **Authentication Successful!** You now have access to the bot.", reply_markup=keyboard)
    else:
        await message.reply_text("❌ **Incorrect Password!** Access denied.")

@app.on_message(filters.command("menu"))
async def menu_handler(client: Client, message: Message):
    if message.from_user.id not in authorized_users:
        await message.reply_text("🔒 Please authenticate first using `/auth <password>`")
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
    if callback_query.from_user.id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
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
    if callback_query.from_user.id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
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
    if callback_query.from_user.id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
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
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    if user_id in active_processes:
        try:
            active_processes[user_id].terminate()
            del active_processes[user_id]
            user_job_controls[user_id] = "cancel"
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
    action = callback_query.matches[0].group(1)

    if user_id in active_processes:
        try:
            active_processes[user_id].terminate()
            del active_processes[user_id]
        except Exception:
            pass

    user_job_controls[user_id] = action
    if action == "pause":
        await callback_query.answer("⏸️ Pausing and preparing partial file...", show_alert=True)
    else:
        await callback_query.answer("❌ Recording cancelled.", show_alert=True)

@app.on_message(filters.command("s"))
async def search_handler(client: Client, message: Message):
    if message.from_user.id not in authorized_users:
        await message.reply_text("🔒 Please authenticate first using `/auth <password>`")
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
        f"✅ Found **{len(matched)}** channels matching `{query}`.\n"
        f"Tap channels to select/deselect them, then click Proceed:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@app.on_callback_query(filters.regex(r"^toggle_chan_(\d+)$"))
async def toggle_channel_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
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
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    selected = user_selected_channels.get(user_id, set())
    if not selected:
        await callback_query.answer("⚠️ Please select at least one channel first!", show_alert=True)
        return

    # STEP 1: AUDIO TRACK SELECTION (All Audio vs Specific Tracks)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 All Audio Tracks (Recommended)", callback_data="audio_all"),
            InlineKeyboardButton("🎵 Hindi Audio Only", callback_data="audio_hi")
        ],
        [
            InlineKeyboardButton("🎵 English Audio", callback_data="audio_en"),
            InlineKeyboardButton("🎵 Tamil Audio", callback_data="audio_ta")
        ],
        [
            InlineKeyboardButton("🎵 Telugu Audio", callback_data="audio_te"),
            InlineKeyboardButton("🎵 Kannada Audio", callback_data="audio_kan")
        ],
        [InlineKeyboardButton("« Back", callback_data="prompt_search")]
    ])

    await callback_query.message.edit_text(
        f"🎯 **Selected Channels:** `{list(selected)}`\n\nSelect Audio Option:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^audio_(all|hi|en|ta|te|kan)$"))
async def audio_select_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    audio_mode = callback_query.matches[0].group(1)
    user_audio_mode[user_id] = audio_mode

    # STEP 2: RECORDING MODE (Normal vs Watermark)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Record Normal", callback_data="mode_norm"),
            InlineKeyboardButton("🎨 Record + Watermark", callback_data="mode_wm")
        ],
        [InlineKeyboardButton("« Back", callback_data="confirm_channels")]
    ])

    await callback_query.message.edit_text(
        f"⚙️ **Audio Mode:** `{audio_mode.upper()}`\n\nChoose Recording Mode:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^mode_(norm|wm)$"))
async def mode_select_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    mode = callback_query.matches[0].group(1)
    user_record_mode[user_id] = mode

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
        ]
    ])

    await callback_query.message.edit_text(
        f"⏱ **Select Recording Duration:**",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^dur_custom$"))
async def custom_duration_prompt(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    user_waiting_duration[user_id] = True

    await callback_query.answer()
    await callback_query.message.edit_text(
        "✏️ **Custom Duration**\n\n"
        "Please type the recording duration in chat.\n"
        "• Format 1: Minutes as a number (e.g. `10` for 10 minutes)\n"
        "• Format 2: Exact timestamp `HH:MM:SS` (e.g. `00:15:00`)"
    )

@app.on_message(filters.text & ~filters.command(["start", "auth", "menu", "s"]))
async def custom_duration_text_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in authorized_users or not user_waiting_duration.get(user_id):
        return

    user_waiting_duration.pop(user_id, None)
    text = message.text.strip()

    duration_str = "00:05:00"
    if text.isdigit():
        mins = int(text)
        duration_str = f"00:{mins:02d}:00"
    elif re.match(r"^\d{2}:\d{2}:\d{2}$", text):
        duration_str = text
    else:
        await message.reply_text("❌ Invalid format. Defaulting duration to `00:05:00`.")

    selected = list(user_selected_channels.get(user_id, set()))
    if not selected:
        await message.reply_text("❌ No channels selected. Please search again using `/s`")
        return

    status_msg = await message.reply_text(f"🚀 Starting rips for {len(selected)} channels...")
    
    for channel_num in selected:
        await execute_rip_process(client, message.chat.id, user_id, channel_num, duration_str, status_msg)

    user_selected_channels[user_id] = set()

@app.on_callback_query(filters.regex(r"^dur_(\d{2}:\d{2}:\d{2})$"))
async def preset_duration_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in authorized_users:
        await callback_query.answer("Authentication required!", show_alert=True)
        return

    await callback_query.answer("Starting rip process...")
    duration_str = callback_query.matches[0].group(1)

    selected = list(user_selected_channels.get(user_id, set()))
    if not selected:
        await callback_query.answer("No channels selected.", show_alert=True)
        return

    status_msg = callback_query.message
    for channel_num in selected:
        await execute_rip_process(client, callback_query.message.chat.id, user_id, channel_num, duration_str, status_msg)

    user_selected_channels[user_id] = set()

async def execute_rip_process(client, chat_id, user_id, channel_num, duration_str, status_msg):
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
        await status_msg.edit_text(f"❌ Error: Could not extract stream URL for channel {channel_num}.")
        return

    audio_selection = user_audio_mode.get(user_id, "all")
    record_mode = user_record_mode.get(user_id, "norm")

    job_id = f"{user_id}_{int(time.time())}"
    temp_prefix_local = f"raw_stream_{job_id}"
    output_filename = f"{clean_name}_{job_id}.mkv" # Match mkv container support for all audio tracks like fake.py
    thumb_path_local = f"thumb_{job_id}.jpg"

    # EXACT WORKING COMMAND FROM fake.py (Including --use-shaka-packager)[cite: 1]
    command = [
        "N_m3u8DL-RE",
        url,
        "--save-name", temp_prefix_local,
        "--use-shaka-packager",
        "--live-real-time-merge", 
        "--live-record-limit", duration_str,
        "--custom-proxy", DOWNLOAD_PROXY,
        "--select-video", "best",
        "--select-audio", audio_selection,
        "--thread-count", "16"
    ]
    command.extend(headers)
    command.extend(drm_keys)

    user_job_controls[user_id] = None

    try:
        control_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸️ Pause & Send", callback_data="job_control_pause"),
                InlineKeyboardButton("❌ Cancel", callback_data="job_control_cancel")
            ]
        ])

        await status_msg.edit_text(
            f"⏺ **Ripping Channel {channel_num}:** `{fresh_channels[channel_num]['name']}`\n"
            f"⏱ Target Duration: `{duration_str}`\n\n{make_progress_bar(0)}",
            reply_markup=control_markup
        )
        
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
            if user_job_controls.get(user_id) in ["pause", "cancel"]:
                break

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
                        f"⏱ Target Duration: `{duration_str}`\n\n{make_progress_bar(pct)}",
                        reply_markup=control_markup
                    )
                    last_update_time = current_time
                except Exception:
                    pass

        await proc.wait()
        if user_id in active_processes:
            del active_processes[user_id]

        action_taken = user_job_controls.pop(user_id, None)
        if action_taken == "cancel":
            await status_msg.edit_text("❌ Recording cancelled by user.")
            for f in glob.glob(f"{temp_prefix_local}*") + [output_filename, thumb_path_local]:
                try:
                    os.remove(f)
                except Exception:
                    pass
            return

        # ==========================================
        # SMART MUXING EXACTLY MATCHING fake.py
        # ==========================================
        video_files = glob.glob(f"{temp_prefix_local}*.mp4") + glob.glob(f"{temp_prefix_local}*.ts")
        audio_files = glob.glob(f"{temp_prefix_local}*.m4a") + glob.glob(f"{temp_prefix_local}*.aac")
        all_files = video_files + audio_files

        if not all_files:
            # Fallback broad glob
            all_raw = glob.glob(f"{temp_prefix_local}*")
            all_files = [f for f in all_raw if not f.endswith('.jpg') and f != output_filename and f != f"wm_{output_filename}"]

        if not all_files:
            await status_msg.edit_text("❌ Error: No decrypted tracks found.")
            return

        await status_msg.edit_text(f"⚙️ **Smart Muxing & Syncing Tracks (fake.py engine)...**\n\n{make_progress_bar(50)}")

        max_start = 0.0
        min_end = float('inf')

        for f in all_files:
            start, end = await asyncio.to_thread(get_media_info, f)
            if start > max_start: max_start = start
            if end < min_end: min_end = end

        mux_cmd = ["ffmpeg"]
        for f in all_files:
            mux_cmd.extend(["-i", f])

        for i in range(len(all_files)):
            mux_cmd.extend(["-map", str(i)])

        mux_cmd.extend([
            "-c", "copy",
            "-copyts",
            "-ss", str(max_start),
            "-to", str(min_end),
            "-avoid_negative_ts", "make_zero",
            "-y", output_filename
        ])

        mux_proc = await asyncio.create_subprocess_exec(*mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await mux_proc.communicate()

        final_file = output_filename

        if record_mode == "wm":
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
            caption=f"📺 Channel: {fresh_channels[channel_num]['name']}{suffix_label}\n⏱ Duration: {duration_str}"
        )
        await status_msg.delete()

        for f in all_files + [output_filename, f"wm_{output_filename}", thumb_path_local]:
            try:
                os.remove(f)
            except Exception:
                pass

    except Exception as e:
        if user_id in active_processes:
            del active_processes[user_id]
        await status_msg.edit_text(f"❌ Error during execution: {e}")

if __name__ == "__main__":
    print("Bot starting up with fake.py engine & full multi-audio support...", flush=True)
    app.run()
