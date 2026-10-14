import os
import sys
import glob
import json
import time
import shlex
import re
import shutil
import asyncio
import subprocess
import uuid
from wzgram import Client, filters
from wzgram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from wzgram.enums import ParseMode
from wzgram.errors import MessageNotModified

# ----------------- Configuration ----------------- #
API_ID = 31135256
API_HASH = "6b31bb267109963bac7ddb8dd37fe09c"
BOT_TOKEN = "8961987205:AAG2_jPzG2nOkcGUOdl2oO5NDcdCXi9HENU"
DEFAULT_ALLOWED_USERS = [7802806467, 6905747168, 8824203404]

MAX_TASKS_PER_USER = 1  # Kept strictly to 1 on GitHub Actions to prevent disk overflow

# Google Drive Remote & Path
RCLONE_REMOTE = "dpe:StreamRecordings"
# Resolves to /home/runner/.config/rclone/rclone.conf on GitHub Actions
RCLONE_CONFIG_PATH = os.path.expanduser("~/.config/rclone/rclone.conf")

USERS_FILE = "allowed_users.json"
RULES_FILE = "rules.json"

# File placed in the root of the repo
DEFAULT_FONT_PATH = os.path.abspath("SNAP.TTF")

# Strict Single-Job Lock to maximize 4-core allocation per transcode
ENCODE_SEMAPHORE = asyncio.Semaphore(1)

app = Client(
    "stream_recorder_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4,
    max_concurrent_transmissions=1
)

active_sessions = {}
pending_links = {}
pending_videos = {}
naming_sessions = {}

DEFAULT_VLC_HEADERS = (
    "User-Agent: VLC/3.0.20 LibVLC/3.0.20\r\n"
    "Accept: */*\r\n"
    "Range: bytes=0-\r\n"
    "Icy-MetaData: 1\r\n"
)

RESOLUTIONS_MAP = {
    "1080p": (1920, 1080, 2200, 23, "128k"),
    "720p":  (1280, 720,  1200, 24, "96k"),
    "576p":  (1024, 576,  850,  24.5, "96k"),
    "480p":  (854,  480,  650,  25, "64k"),
    "360p":  (640,  360,  400,  26, "48k"),
    "240p":  (426,  240,  220,  28, "32k"),
}

# ----------------- Helper Functions ----------------- #

def get_user_task_count(user_id: int) -> int:
    return sum(1 for s in active_sessions.values() if s.get("user_id") == user_id)

def load_allowed_users() -> set:
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_ALLOWED_USERS, f, indent=4)
        return set(DEFAULT_ALLOWED_USERS)
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set(DEFAULT_ALLOWED_USERS)

def save_allowed_users(users: set):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(users)), f, indent=4)

def auth_filter(_, __, message: Message):
    allowed = load_allowed_users()
    return message.from_user and message.from_user.id in allowed

is_auth = filters.create(auth_filter)

# ----------------- Hardcoded Watermark Filter (5 Min / 30 Sec) ----------------- #

def get_watermark_drawtext_filter():
    font_path = DEFAULT_FONT_PATH
    escaped_font = font_path.replace(":", r"\:")
    font_arg = f"fontfile='{escaped_font}':" if os.path.exists(font_path) else ""

    text = "Join Our Telegram - @Atoztoonsverse"
    fontsize = 32
    fontcolor = "white@0.70"
    x = "(w-text_w)/2"
    y = "h*0.87-text_h"

    # Hardcoded: 30 seconds every 5 minutes (300 seconds)
    interval = 300
    dur = 30
    enable_arg = f":enable='lt(mod(t\\,{interval})\\,{dur})'"

    return (
        f"drawtext={font_arg}text='{text}':fontcolor={fontcolor}:fontsize={fontsize}:"
        f"x={x}:y={y}{enable_arg}"
    )

def load_rules():
    if not os.path.exists(RULES_FILE):
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4)
        return {}
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def find_matching_rule(url: str):
    rules = load_rules()
    url_lower = url.lower()
    for pattern, config in rules.items():
        if pattern.lower() in url_lower:
            return pattern, config
    return None, None

def sanitize_stream_url(url: str) -> str:
    if "bdix.spidy.online" in url.lower() and url.startswith("https://"):
        return url.replace("https://", "http://", 1)
    return url

def needs_forced_hls(url: str) -> bool:
    u = url.lower()
    clean = u.split("?")[0]
    if clean.endswith(".m3u8"):
        return True
    return any(clean.endswith(ext) for ext in [".php", ".ashx", ".aspx", ".jsp"]) or ("id=" in u) or ("channel=" in u)

def probe_stream_metadata(url_or_file, custom_headers=None):
    safe_url = sanitize_stream_url(url_or_file) if isinstance(url_or_file, str) else url_or_file
    cmd = ["ffprobe", "-v", "error"]
    headers = custom_headers if custom_headers else DEFAULT_VLC_HEADERS

    if safe_url.startswith("http://") or safe_url.startswith("https://"):
        cmd.extend([
            "-headers", headers,
            "-analyzeduration", "2000000",
            "-probesize", "1000000",
            "-read_intervals", "%+3"
        ])
        if "bdix.spidy.online" in safe_url.lower() or needs_forced_hls(safe_url):
            cmd.extend(["-f", "hls"])

    cmd.extend([
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        safe_url
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout)

        duration = 0
        bit_rate = 0
        if "format" in data:
            if "duration" in data["format"]:
                try:
                    duration = int(float(data["format"]["duration"]))
                except Exception:
                    pass
            if "bit_rate" in data["format"]:
                try:
                    bit_rate = int(data["format"]["bit_rate"])
                except Exception:
                    pass

        width, height = 0, 0
        audio_tracks = []

        for st in data.get("streams", []):
            if st.get("codec_type") == "video" and width == 0:
                width = int(st.get("width", 0))
                height = int(st.get("height", 0))
                if duration == 0 and "duration" in st:
                    try:
                        duration = int(float(st["duration"]))
                    except Exception:
                        pass
            elif st.get("codec_type") == "audio":
                tags = st.get("tags", {})
                lang = tags.get("language") or tags.get("title") or tags.get("comment") or f"Track {len(audio_tracks)+1}"
                codec = st.get("codec_name", "aac")
                audio_tracks.append(f"{lang.upper()} ({codec})")

        return duration, width, height, audio_tracks, bit_rate
    except Exception:
        return 0, 1920, 1080, ["Default Track"], 0

def generate_thumbnail(video_path: str, thumb_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-ss", "00:00:03",
        "-i", video_path,
        "-vframes", "1",
        "-vf", "scale=320:-1",
        "-q:v", "3",
        thumb_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 100:
            return thumb_path
    except Exception:
        pass
    return None

def ensure_mp4_format(input_path: str, output_path: str) -> str:
    if input_path.endswith(".mp4") and os.path.exists(input_path) and os.path.getsize(input_path) > 50000:
        return input_path

    # Step 1: Remux stream copy (Instant)
    remux_cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-i", input_path,
        "-map", "0:v:0?",
        "-map", "0:a?",
        "-c", "copy",
        "-bsf:a:?", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(remux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(output_path) and os.path.getsize(output_path) > 50000:
        return output_path

    # Step 2: Fallback transcode using all 4 cores
    transcode_cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "4",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(transcode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(output_path) and os.path.getsize(output_path) > 50000:
        return output_path

    return input_path

def make_progress_bar(percentage: int) -> str:
    filled = int(percentage / 10)
    return "▓" * filled + "░" * (10 - filled)

def read_file_tail(path: str, max_bytes: int = 65536) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read(max_bytes).decode("utf-8", errors="ignore")
    except OSError:
        return ""

def sanitize_requested_name(name: str) -> str:
    raw = (name or "").strip()
    raw = re.sub(r'^[\\/]+', '', raw)
    if raw.lower().endswith(".mp4"):
        raw = raw[:-4]
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip()
    return clean or f"Recording_{int(time.time())}"

async def run_ffmpeg_with_progress(cmd: list, total_duration_sec: int, status_msg: Message, task_title: str) -> tuple[bool, str]:
    cmd_with_prog = cmd + ["-progress", "pipe:1"]

    proc = await asyncio.create_subprocess_exec(
        *cmd_with_prog,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    last_update_time = [0.0]
    last_pct = [-1]
    stderr_tail = bytearray()
    max_stderr_bytes = 65536

    async def read_stderr():
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            stderr_tail.extend(chunk)
            if len(stderr_tail) > max_stderr_bytes:
                del stderr_tail[:-max_stderr_bytes]

    stderr_task = asyncio.create_task(read_stderr())

    current_data = {}
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore").strip()
        if "=" in decoded:
            k, v = decoded.split("=", 1)
            current_data[k.strip()] = v.strip()

        if decoded.startswith("progress="):
            now = time.time()
            out_ms_str = current_data.get("out_time_ms", "0")
            speed = current_data.get("speed", "N/A")
            fps = current_data.get("fps", "0")

            try:
                out_sec = int(out_ms_str) / 1_000_000
            except Exception:
                out_sec = 0

            pct = int((out_sec / total_duration_sec) * 100) if total_duration_sec > 0 else 0
            pct = min(100, max(0, pct))

            if (now - last_update_time[0] >= 6.0 and pct != last_pct[0]) or decoded == "progress=end":
                last_update_time[0] = now
                last_pct[0] = pct
                bar = make_progress_bar(pct)
                progress_text = (
                    f"⚙️ **{task_title}**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Progress: `[{bar}]` **{pct}%**\n"
                    f"⏱ Time: `{int(out_sec)//60}m {int(out_sec)%60}s` / `{total_duration_sec//60}m {total_duration_sec%60}s`\n"
                    f"⚡ Speed: `{speed}` | FPS: `{fps}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Encoding on 4 vCPUs..."
                )
                try:
                    await status_msg.edit_text(progress_text)
                except MessageNotModified:
                    pass
                except Exception:
                    pass

    await proc.wait()
    await stderr_task
    full_stderr = stderr_tail.decode("utf-8", errors="ignore")
    return (proc.returncode == 0), full_stderr

# ----------------- Rclone Google Drive Uploader ----------------- #

async def upload_via_rclone(filepath: str, status_msg: Message) -> str:
    filename = os.path.basename(filepath)
    try:
        await status_msg.edit_text(f"☁️ **Uploading to Google Drive (dpe:StreamRecordings)...**\nFile: `{filename}`")
    except Exception:
        pass

    cmd = ["rclone", "copy", filepath, RCLONE_REMOTE, "--config", RCLONE_CONFIG_PATH]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise Exception(f"Rclone copy failed: {stderr.decode('utf-8', errors='ignore')}")

    dest_path = f"{RCLONE_REMOTE}/{filename}"
    link_cmd = ["rclone", "link", dest_path, "--config", RCLONE_CONFIG_PATH]
    link_proc = await asyncio.create_subprocess_exec(*link_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    link_stdout, _ = await link_proc.communicate()

    gdrive_link = link_stdout.decode("utf-8").strip()
    return gdrive_link if gdrive_link else f"https://drive.google.com/drive/folders/1d1szKY3GqKvYIKO05LWrR1KLgHYcpXx"

# ----------------- Video File Processing (Full 4-Core Speed) ----------------- #

async def process_video_file_async(input_path: str, output_path: str, status_msg: Message, task_title: str, resolution_tag: str = None, apply_wm: bool = True) -> tuple:
    dur, in_w, in_h, _, in_bitrate = await asyncio.to_thread(probe_stream_metadata, input_path)

    filters_list = []
    video_args = []
    audio_args = ["-c:a", "aac", "-b:a", "128k"]

    if resolution_tag and resolution_tag in RESOLUTIONS_MAP:
        target_w, target_h, max_v_kbps, target_crf, a_br = RESOLUTIONS_MAP[resolution_tag]
        filters_list.append(f"scale='min({target_w},iw)':-2")

        if in_bitrate > 0:
            in_v_kbps = int((in_bitrate / 1000) * 0.85)
            effective_max_kbps = min(max_v_kbps, int(in_v_kbps * (target_h / max(in_h, 720))))
            effective_max_kbps = max(180, effective_max_kbps)
        else:
            effective_max_kbps = max_v_kbps

        video_args.extend([
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-threads", "4",
            "-crf", str(target_crf),
            "-maxrate", f"{effective_max_kbps}k",
            "-bufsize", f"{effective_max_kbps * 2}k"
        ])
        audio_args = ["-c:a", "aac", "-b:a", a_br]
    else:
        video_args.extend([
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-threads", "4",
            "-crf", "23"
        ])

    if apply_wm:
        filters_list.append(get_watermark_drawtext_filter())

    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-avoid_negative_ts", "make_zero",
        "-i", input_path
    ]

    if filters_list:
        cmd.extend(["-vf", ",".join(filters_list)])

    cmd.extend(["-map", "0:v:0", "-map", "0:a?"])
    cmd.extend(video_args)
    cmd.extend(audio_args)
    cmd.extend(["-movflags", "+faststart", output_path])

    success, stderr_log = await run_ffmpeg_with_progress(cmd, dur, status_msg, task_title)
    file_valid = os.path.exists(output_path) and os.path.getsize(output_path) > 50000
    return (success and file_valid), stderr_log

# ----------------- Keyboard Layouts ----------------- #

def get_video_actions_keyboard(task_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎨 Watermark Only", callback_data=f"vact_wmonly_{task_id}"),
            InlineKeyboardButton("📐 Change Resolution", callback_data=f"vact_resmenu_{task_id}")
        ],
        [
            InlineKeyboardButton("✨ Resolution + Watermark", callback_data=f"vact_reswmmenu_{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"vact_cancel_{task_id}")
        ]
    ])

def get_resolution_selection_keyboard(task_id: str, mode: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1080p (FHD)", callback_data=f"vres_{mode}_1080p_{task_id}"),
            InlineKeyboardButton("720p (HD)", callback_data=f"vres_{mode}_720p_{task_id}"),
            InlineKeyboardButton("576p (PAL)", callback_data=f"vres_{mode}_576p_{task_id}")
        ],
        [
            InlineKeyboardButton("480p (SD)", callback_data=f"vres_{mode}_480p_{task_id}"),
            InlineKeyboardButton("360p", callback_data=f"vres_{mode}_360p_{task_id}"),
            InlineKeyboardButton("240p", callback_data=f"vres_{mode}_240p_{task_id}")
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"vact_back_{task_id}")
        ]
    ])

# ----------------- Upload Handling ----------------- #

async def upload_mp4_video(client: Client, chat_id: int, filepath: str, caption_name: str = None):
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    filename = os.path.basename(filepath)

    progress_msg = await client.send_message(
        chat_id=chat_id,
        text=f"⬆️ Uploading video: `{filename}` ({file_size_mb:.2f} MB)..."
    )

    last_logged = [0.0]
    last_pct = [-1]

    async def upload_progress(current, total_bytes):
        if total_bytes == 0:
            return
        now = time.time()
        pct = int((current / total_bytes) * 100)

        if (now - last_logged[0] >= 5.0) and (pct != last_pct[0]):
            last_logged[0] = now
            last_pct[0] = pct
            bar = make_progress_bar(pct)
            try:
                await progress_msg.edit_text(
                    f"⬆️ **Uploading video:** `{filename}`\n"
                    f"Progress: `[{bar}]` **{pct}%**\n"
                    f"Uploaded: `{current // (1024*1024)}MB / {total_bytes // (1024*1024)}MB`"
                )
            except MessageNotModified:
                pass
            except Exception:
                pass

    thumb_path = None
    try:
        duration, width, height, _, _ = await asyncio.to_thread(probe_stream_metadata, filepath)

        thumb_file = filepath.rsplit(".", 1)[0] + "_thumb.jpg"
        thumb_path = await asyncio.to_thread(generate_thumbnail, filepath, thumb_file)

        safe_width = int(width) if (width and width > 0) else 1280
        safe_height = int(height) if (height and height > 0) else 720
        safe_duration = int(duration) if (duration and duration > 0) else 0

        title = caption_name if caption_name is not None else os.path.splitext(filename)[0]
        final_caption = re.sub(r'^[\\/]+', '', title.strip())

        await client.send_video(
            chat_id=chat_id,
            video=filepath,
            caption=final_caption,
            parse_mode=ParseMode.DISABLED,
            duration=safe_duration,
            width=safe_width,
            height=safe_height,
            thumb=thumb_path,
            supports_streaming=True,
            progress=upload_progress
        )
        await progress_msg.delete()
    except Exception as e:
        await progress_msg.edit_text(f"❌ Upload error on `{filename}`: {e}")
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass

def get_action_keyboard(task_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-Upload", callback_data=f"reup_{task_id}"),
            InlineKeyboardButton("🗑️ Delete Local Files", callback_data=f"del_{task_id}")
        ]
    ])

# ----------------- Stream Monitoring ----------------- #

async def progress_monitor(task_id: str, status_msg: Message, output_file: str, log_file: str, start_time: float, audio_meta: list, video_res: str, engine_label: str, title: str):
    while task_id in active_sessions:
        await asyncio.sleep(8)
        if task_id not in active_sessions:
            break

        session = active_sessions.get(task_id)
        if not session:
            break

        proc = session["proc"]

        if proc.poll() is not None:
            if os.path.exists(output_file) and os.path.getsize(output_file) > 100000:
                await status_msg.edit_text("⚠️ **Stream ended. Auto-finalizing recorded video...**")
                await finalize_and_upload(app, task_id, status_msg)
                return
            else:
                err_clean = "Process ended or connection closed."
                if os.path.exists(log_file):
                    try:
                        err_clean = read_file_tail(log_file).strip()
                    except Exception:
                        pass

                if "log_handle" in session and not session["log_handle"].closed:
                    session["log_handle"].close()
                active_sessions.pop(task_id, None)

                await status_msg.edit_text(
                    "❌ **Recording Stopped With Error:**\n\n"
                    f"```text\n{err_clean or 'Unknown stream error'}\n```"
                )
                return

        total_bytes = os.path.getsize(output_file) if os.path.exists(output_file) else 0
        mb_size = total_bytes / (1024 * 1024)
        elapsed_sec = int(time.time() - start_time)
        hrs = elapsed_sec // 3600
        mins = (elapsed_sec % 3600) // 60
        secs = elapsed_sec % 60
        time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        audios_str = " | ".join([f"`{a}`" for a in audio_meta]) if audio_meta else "`Auto-Mapped`"

        text = (
            "⏺️ **Recording Live Stream**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 **File:** `{title}.mp4`\n"
            f"⚙️ **Engine:** `{engine_label}`\n"
            f"⏱ **Elapsed:** `{time_str}`\n"
            f"📁 **Size:** `{mb_size:.2f} MB`\n"
            f"🖥 **Resolution:** `{video_res}`\n"
            f"🔊 **Audio:** {audios_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Click **Stop Recording** when ready to finish."
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Stop Recording", callback_data=f"stop_{task_id}")]
        ])

        try:
            await status_msg.edit_text(text, reply_markup=keyboard)
        except MessageNotModified:
            pass
        except Exception:
            pass

# ----------------- Stream Finalize & Post-Process ----------------- #

async def finalize_and_upload(client: Client, task_id: str, status_message: Message):
    session = active_sessions.pop(task_id, None)
    if not session:
        return

    monitor_task = session.get("monitor_task")
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()

    proc: subprocess.Popen = session["proc"]
    log_handle = session["log_handle"]
    raw_output: str = session["raw_output"]
    log_file: str = session["log_file"]
    folder: str = session["folder"]
    chat_id: int = session["chat_id"]
    enable_wm: bool = session.get("enable_wm", False)
    custom_title: str = session.get("custom_title", "recording_master")

    await status_message.edit_text("⏳ **Finalizing recorded stream...**")

    try:
        proc.terminate()
        await asyncio.to_thread(proc.wait, timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            if not log_handle.closed:
                log_handle.close()
        except Exception:
            pass

    source_file = None
    if os.path.exists(raw_output) and os.path.getsize(raw_output) > 50000:
        source_file = raw_output
    else:
        candidates = [
            f for f in glob.glob(os.path.join(folder, "*"))
            if os.path.isfile(f) and not f.endswith(".log") and not f.endswith(".jpg") and os.path.getsize(f) > 50000
        ]
        if candidates:
            source_file = candidates[0]

    if not source_file:
        err_msg = ""
        if os.path.exists(log_file):
            try:
                err_msg = read_file_tail(log_file).strip()
            except Exception:
                pass

        await status_message.edit_text(
            "❌ **No video captured.**\n\n"
            f"**Engine Log:**\n```text\n{err_msg or 'Empty stream'}\n```"
        )
        if os.path.exists(folder):
            shutil.rmtree(folder, ignore_errors=True)
        return

    clean_target_path = os.path.join(folder, f"{custom_title}.mp4")

    if ENCODE_SEMAPHORE.locked():
        try:
            await status_message.edit_text(f"⏳ **Queued:** Waiting for encoder before processing `{custom_title}`...")
        except Exception:
            pass

    async with ENCODE_SEMAPHORE:
        await status_message.edit_text("🔄 **Packaging stream into clean MP4...**")
        clean_mp4 = await asyncio.to_thread(ensure_mp4_format, source_file, clean_target_path)

        if not os.path.exists(clean_mp4) or os.path.getsize(clean_mp4) < 50000:
            await status_message.edit_text("❌ **Error:** Failed to package video.")
            return

        # Free disk space immediately (Delete source capture)
        if source_file != clean_mp4 and os.path.exists(source_file):
            try:
                os.remove(source_file)
            except Exception:
                pass

        target_video_file = clean_mp4

        if enable_wm:
            watermarked_path = os.path.join(folder, f"{custom_title}.watermarking.mp4")
            try:
                if os.path.exists(watermarked_path):
                    os.remove(watermarked_path)
            except OSError:
                pass
            success, err = await process_video_file_async(clean_mp4, watermarked_path, status_message, f"Watermarking {custom_title}", None, True)
            if success:
                os.replace(watermarked_path, clean_mp4)
                target_video_file = clean_mp4
            else:
                try:
                    if os.path.exists(watermarked_path):
                        os.remove(watermarked_path)
                except OSError:
                    pass
                err_snip = "\n".join(err.strip().splitlines()[-6:]) if err else "Transcode error"
                await status_message.edit_text(f"⚠️ **Watermark step failed. Uploading clean recording instead:**\n`{err_snip}`")
                await asyncio.sleep(3)

    final_size_bytes = os.path.getsize(target_video_file)
    max_tg_bytes = 1950 * 1024 * 1024

    if final_size_bytes > max_tg_bytes:
        try:
            drive_link = await upload_via_rclone(target_video_file, status_message)
            size_gb = final_size_bytes / (1024 * 1024 * 1024)
            await status_message.edit_text(
                f"✅ **Uploaded to Google Drive (> 2GB)!**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 **File:** `{os.path.basename(target_video_file)}`\n"
                f"📦 **Size:** `{size_gb:.2f} GB`\n"
                f"🔗 **Link:** {drive_link}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                reply_markup=get_action_keyboard(task_id)
            )
        except Exception as e:
            await status_message.edit_text(
                f"❌ **Rclone GDrive Upload Failed:** `{e}`\n"
                "Please verify your `rclone.conf` secret.",
                reply_markup=get_action_keyboard(task_id)
            )
    else:
        await upload_mp4_video(client, chat_id, target_video_file, caption_name=custom_title)
        await status_message.edit_text(
            "✅ **Video uploaded successfully to Telegram!**",
            reply_markup=get_action_keyboard(task_id)
        )

# ----------------- Stream Execution ----------------- #

async def execute_recording(client: Client, user_id: int, chat_id: int, url: str, status_msg: Message, enable_wm: bool, custom_title: str):
    task_id = str(uuid.uuid4())[:8]
    final_url = sanitize_stream_url(url)
    is_hls = needs_forced_hls(final_url)

    matched_pattern, rule_config = find_matching_rule(final_url)
    engine = rule_config.get("engine", "ffmpeg").lower() if rule_config else "ffmpeg"
    rule_headers = rule_config.get("headers", DEFAULT_VLC_HEADERS) if (rule_config and engine == "ffmpeg") else DEFAULT_VLC_HEADERS

    _, w, h, audio_tracks, _ = await asyncio.to_thread(probe_stream_metadata, final_url, rule_headers)
    video_res = f"{w}x{h}" if w > 0 else "Auto Detect"

    session_dir = f"rec_session_{user_id}_{task_id}"
    os.makedirs(session_dir, exist_ok=True)
    raw_output = os.path.join(session_dir, f"{custom_title}_raw.ts")
    log_file = os.path.join(session_dir, "engine.log")

    if engine == "streamlink":
        engine_label = f"Streamlink ({matched_pattern})"
        extra_args = shlex.split(rule_config.get("streamlink_args", ""))
        cmd = ["streamlink", "--loglevel", "info"] + extra_args + ["-o", raw_output, f"hls://{final_url}", "best"]
    else:
        engine_label = f"FFmpeg ({'HLS Auto' if is_hls else 'Direct Stream'})"
        cmd = ["ffmpeg", "-y"]

        if rule_config:
            if rule_headers:
                cmd.extend(["-headers", rule_headers])
            if rule_config.get("input_args"):
                cmd.extend(shlex.split(rule_config["input_args"]))
            cmd.extend(["-i", final_url])
            if rule_config.get("output_args"):
                cmd.extend(shlex.split(rule_config["output_args"]))
            cmd.append(raw_output)
        else:
            if is_hls:
                cmd.extend(["-f", "hls"])

            cmd.extend([
                "-headers", DEFAULT_VLC_HEADERS,
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000",
                "-fflags", "+genpts+discardcorrupt",
                "-i", final_url,
                "-map", "0:v?",
                "-map", "0:a?",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                raw_output
            ])

    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT
        )

        start_time = time.time()
        monitor_task = asyncio.create_task(
            progress_monitor(task_id, status_msg, raw_output, log_file, start_time, audio_tracks, video_res, engine_label, custom_title)
        )

        active_sessions[task_id] = {
            "task_id": task_id,
            "user_id": user_id,
            "proc": proc,
            "log_handle": log_handle,
            "raw_output": raw_output,
            "log_file": log_file,
            "folder": session_dir,
            "chat_id": chat_id,
            "status_msg_id": status_msg.id,
            "monitor_task": monitor_task,
            "enable_wm": enable_wm,
            "custom_title": custom_title,
            "start_time": start_time
        }

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to start recorder: {e}")
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

# ----------------- Admin Commands (/view & /cleanup) ----------------- #

@app.on_message(filters.command("view") & is_auth)
async def view_tasks_handler(client: Client, message: Message):
    try:
        free_out = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.splitlines()
        mem_parts = free_out[1].split()
        total_ram, used_ram = int(mem_parts[1]), int(mem_parts[2])
        ram_str = f"{used_ram}MB / {total_ram}MB ({(used_ram/total_ram)*100:.1f}%)"
    except Exception:
        ram_str = "N/A"

    try:
        df_out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()
        disk_parts = df_out[1].split()
        disk_str = f"{disk_parts[2]} used / {disk_parts[1]} total ({disk_parts[4]})"
    except Exception:
        disk_str = "N/A"

    tasks_text = []
    if active_sessions:
        for tid, s in active_sessions.items():
            elapsed = int(time.time() - s.get("start_time", time.time()))
            hrs, mins, secs = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            raw_file = s.get("raw_output", "")
            size_mb = os.path.getsize(raw_file) / (1024 * 1024) if os.path.exists(raw_file) else 0.0
            tasks_text.append(
                f"• `[{tid}]` User: `{s.get('user_id')}`\n"
                f"  🏷 **File:** `{s.get('custom_title')}.mp4`\n"
                f"  ⏱ **Elapsed:** `{hrs:02d}:{mins:02d}:{secs:02d}` | 📁 **Size:** `{size_mb:.2f} MB`"
            )

    tasks_summary = "\n".join(tasks_text) if tasks_text else "No active recording tasks running."

    msg = (
        f"🖥 **Runner Resource Monitor**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 **RAM Usage:** `{ram_str}`\n"
        f"💾 **Disk Space:** `{disk_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ **Active Tasks ({len(active_sessions)}):**\n"
        f"{tasks_summary}"
    )
    await message.reply_text(msg)

@app.on_message(filters.command("cleanup") & is_auth)
async def cleanup_files_handler(client: Client, message: Message):
    status = await message.reply_text("🧹 **Scanning & cleaning session files...**")

    cleaned_bytes = 0
    deleted_folders = 0
    active_folders = {s.get("folder") for s in active_sessions.values() if s.get("folder")}

    for folder in glob.glob("rec_session_*") + glob.glob("file_session_*"):
        if folder in active_folders:
            continue
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp):
                        cleaned_bytes += os.path.getsize(fp)
            shutil.rmtree(folder, ignore_errors=True)
            deleted_folders += 1
        except Exception:
            pass

    for f in glob.glob("*.ts") + glob.glob("test.*"):
        try:
            cleaned_bytes += os.path.getsize(f)
            os.remove(f)
        except Exception:
            pass

    cleaned_mb = cleaned_bytes / (1024 * 1024)
    await status.edit_text(
        f"✅ **Disk Cleanup Complete!**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗑 **Folders Removed:** `{deleted_folders}`\n"
        f"📦 **Reclaimed Space:** `{cleaned_mb:.2f} MB`"
    )

# ----------------- User Commands ----------------- #

@app.on_message(~is_auth & filters.private)
async def unauthorized_handler(client: Client, message: Message):
    await message.reply_text("⛔ **Access Denied.** Contact an authorized admin.")

@app.on_message(filters.command("start") & is_auth)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "🎬 **GitHub Runner Stream Recorder**\n\n"
        "• Send any stream link to record (with or without watermark)\n"
        "• Send video files to watermark, resize, or both\n"
        "• Watermark: 30s every 5 mins (Hardcoded)\n"
        "• Transcoding runs on 4 vCPUs (`veryfast`)\n"
        "• Files > 1950 MB upload to Google Drive (`dpe:StreamRecordings`)\n"
        "• Files ≤ 1950 MB upload directly to Telegram\n"
        "• `/view` - Runner CPU/RAM/Disk stats\n"
        "• `/cleanup` - Free up runner disk space"
    )

@app.on_message(filters.command("adduser") & is_auth)
async def adduser_handler(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply_text("⚠️ **Usage:** `/adduser <user_id>`")
        return
    try:
        new_uid = int(parts[1].strip())
    except ValueError:
        await message.reply_text("❌ Must be a numeric ID.")
        return
    users = load_allowed_users()
    users.add(new_uid)
    save_allowed_users(users)
    await message.reply_text(f"✅ User `{new_uid}` authorized.")

@app.on_message(filters.command("users") & is_auth)
async def listusers_handler(client: Client, message: Message):
    users = load_allowed_users()
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(list(users))])
    await message.reply_text(f"👥 **Authorized Users:**\n\n{users_list}")

# ----------------- Stream Link Handling (2 Buttons) ----------------- #

@app.on_message(filters.regex(r"^https?://") & filters.private & is_auth)
async def link_received_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if get_user_task_count(user_id) >= MAX_TASKS_PER_USER:
        await message.reply_text(f"⚠️ A task is already in progress! Please wait for it to finish to protect disk capacity.")
        return

    url = message.text.strip()
    session_key = str(uuid.uuid4())[:8]
    pending_links[session_key] = {"url": url, "user_id": user_id}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏺️ Record", callback_data=f"opt_rec_{session_key}"),
            InlineKeyboardButton("🎨 Record + Watermark", callback_data=f"opt_wm_{session_key}")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"opt_cancel_{session_key}")
        ]
    ])

    await message.reply_text(
        "🔗 **Stream Link Received!**\n\n"
        f"URL: `{url[:60]}...`\n\n"
        "Choose an action:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^opt_(rec|wm|cancel)_([a-zA-Z0-9]+)$"))
async def link_action_callback(client: Client, callback_query: CallbackQuery):
    action = callback_query.matches[0].group(1)
    session_key = callback_query.matches[0].group(2)
    user_id = callback_query.from_user.id

    data = pending_links.pop(session_key, None)
    if not data or data.get("user_id") != user_id:
        await callback_query.answer("⚠️ Session expired.", show_alert=True)
        return

    if action == "cancel":
        await callback_query.answer("Cancelled.")
        await callback_query.message.edit_text("❌ Recording cancelled.")
        return

    naming_sessions[user_id] = {
        "type": "stream",
        "url": data["url"],
        "enable_wm": (action == "wm"),
        "status_msg_id": callback_query.message.id,
        "chat_id": callback_query.message.chat.id
    }

    name_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Use Default Name", callback_data=f"name_def_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"name_cancel_{user_id}")]
    ])

    await callback_query.message.edit_text(
        "📝 **Enter File Name:**\n\n"
        "Send your desired filename as text.\n\n"
        "Or tap **Use Default Name**:",
        reply_markup=name_kb
    )

@app.on_callback_query(filters.regex(r"^name_(def|cancel)_(\d+)$"))
async def naming_callback_choice(client: Client, callback_query: CallbackQuery):
    choice = callback_query.matches[0].group(1)
    target_user_id = int(callback_query.matches[0].group(2))
    
    if callback_query.from_user.id != target_user_id:
        await callback_query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    session = naming_sessions.pop(target_user_id, None)
    if not session:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    if choice == "cancel":
        await callback_query.message.edit_text("❌ Task cancelled.")
        return

    stype = session.get("type", "stream")

    if stype == "stream":
        url = session["url"]
        enable_wm = session["enable_wm"]
        default_name = f"Recording_{int(time.time())}"
        status_msg = await callback_query.message.edit_text(f"🔍 **Analyzing stream format for `{default_name}`...**")
        await execute_recording(client, target_user_id, session["chat_id"], url, status_msg, enable_wm, default_name)
    
    elif stype == "video":
        default_name = f"Video_{int(time.time())}"
        await callback_query.message.edit_text(f"⚙️ **Starting task for `{default_name}`...**")
        await execute_video_processing(
            client, 
            session["chat_id"], 
            callback_query.message, 
            session["work_dir"], 
            session["file_path"], 
            session.get("resolution_tag"), 
            session.get("apply_wm", False),
            default_name,
            session["task_id"]
        )

@app.on_message(filters.text & filters.private & is_auth)
async def custom_name_text_reply_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in naming_sessions:
        session = naming_sessions.pop(user_id, None)
        clean_name = sanitize_requested_name(message.text)
        stype = session.get("type", "stream")

        if stype == "stream":
            status_msg = await message.reply_text(f"🔍 **Analyzing stream format for `{clean_name}`...**")
            await execute_recording(client, user_id, message.chat.id, session["url"], status_msg, session["enable_wm"], clean_name)
        elif stype == "video":
            status_msg = await message.reply_text(f"⚙️ **Starting task for `{clean_name}`...**")
            await execute_video_processing(
                client, 
                session["chat_id"], 
                status_msg, 
                session["work_dir"], 
                session["file_path"], 
                session.get("resolution_tag"), 
                session.get("apply_wm", False),
                clean_name,
                session["task_id"]
            )

# ----------------- Video File Processing Flow ----------------- #

@app.on_message((filters.video | filters.document) & filters.private & is_auth)
async def incoming_video_file_handler(client: Client, message: Message):
    doc = message.document or message.video
    if not doc:
        return

    # Check for rclone drop
    if getattr(doc, "file_name", "") == "rclone.conf":
        os.makedirs(os.path.dirname(RCLONE_CONFIG_PATH), exist_ok=True)
        status = await message.reply_text("📥 **Saving Rclone config...**")
        await message.download(file_name=RCLONE_CONFIG_PATH)
        await status.edit_text("✅ Rclone configuration updated successfully!")
        return

    filename = getattr(doc, "file_name", "input_video.mp4")
    if not filename.lower().endswith((".mp4", ".mkv", ".ts", ".mov", ".m4v")):
        return

    user_id = message.from_user.id
    if get_user_task_count(user_id) >= MAX_TASKS_PER_USER:
        await message.reply_text("⚠️ Runner is busy processing another task. Please wait.")
        return

    task_id = str(uuid.uuid4())[:8]
    work_dir = f"file_session_{user_id}_{task_id}"
    os.makedirs(work_dir, exist_ok=True)

    status_msg = await message.reply_text("📥 **Downloading video for processing...**")

    last_pct = [-1]
    async def dl_progress(current, total):
        pct = int((current / total) * 100) if total > 0 else 0
        if pct - last_pct[0] >= 15:
            last_pct[0] = pct
            bar = make_progress_bar(pct)
            try:
                await status_msg.edit_text(
                    f"📥 **Downloading video:** `[{bar}]` **{pct}%**\n"
                    f"Size: `{current // (1024*1024)}MB / {total // (1024*1024)}MB`"
                )
            except MessageNotModified:
                pass
            except Exception:
                pass

    try:
        downloaded_file = await message.download(file_name=os.path.join(work_dir, "input_video"), progress=dl_progress)
        dur, w, h, _, _ = await asyncio.to_thread(probe_stream_metadata, downloaded_file)
        file_mb = os.path.getsize(downloaded_file) / (1024 * 1024)

        pending_videos[task_id] = {
            "task_id": task_id,
            "user_id": user_id,
            "work_dir": work_dir,
            "file_path": downloaded_file,
            "status_msg_id": status_msg.id,
            "chat_id": message.chat.id
        }

        info_text = (
            f"🎬 **Video Downloaded!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 **Size:** `{file_mb:.2f} MB`\n"
            f"🖥 **Resolution:** `{w}x{h}`\n"
            f"⏱ **Duration:** `{dur//60}m {dur%60}s`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Choose an action below:"
        )

        await status_msg.edit_text(info_text, reply_markup=get_video_actions_keyboard(task_id))

    except Exception as e:
        await status_msg.edit_text(f"❌ Error downloading video: {e}")
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

@app.on_callback_query(filters.regex(r"^vact_") & is_auth)
async def video_actions_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    task_id = data.rsplit("_", 1)[-1]

    session = pending_videos.get(task_id)
    if not session or session.get("user_id") != user_id:
        await callback_query.answer("⚠️ Session expired.", show_alert=True)
        return

    work_dir = session["work_dir"]
    input_file = session["file_path"]
    chat_id = session["chat_id"]

    if data.startswith("vact_cancel_"):
        pending_videos.pop(task_id, None)
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        await callback_query.answer("Cancelled.")
        await callback_query.message.edit_text("❌ Cancelled.")
        return

    if data.startswith("vact_back_"):
        await callback_query.message.edit_text("Choose an action below:", reply_markup=get_video_actions_keyboard(task_id))
        await callback_query.answer()
        return

    if data.startswith("vact_resmenu_"):
        await callback_query.message.edit_text("📐 **Select target resolution:**", reply_markup=get_resolution_selection_keyboard(task_id, "res"))
        await callback_query.answer()
        return

    if data.startswith("vact_reswmmenu_"):
        await callback_query.message.edit_text("✨ **Select target resolution for Resolution + Watermark:**", reply_markup=get_resolution_selection_keyboard(task_id, "reswm"))
        await callback_query.answer()
        return

    if data.startswith("vact_wmonly_"):
        pending_videos.pop(task_id, None)
        naming_sessions[user_id] = {
            "type": "video",
            "task_id": task_id,
            "work_dir": work_dir,
            "file_path": input_file,
            "chat_id": chat_id,
            "resolution_tag": None,
            "apply_wm": True
        }
        name_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Use Default Name", callback_data=f"name_def_{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"name_cancel_{user_id}")]
        ])
        await callback_query.message.edit_text("📝 **Enter Output Video Name:**", reply_markup=name_kb)
        await callback_query.answer()

@app.on_callback_query(filters.regex(r"^vres_") & is_auth)
async def resolution_selection_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    parts = data.split("_")
    mode = parts[1]
    target_res = parts[2]
    task_id = parts[3]

    session = pending_videos.pop(task_id, None)
    if not session or session.get("user_id") != user_id:
        await callback_query.answer("⚠️ Session expired.", show_alert=True)
        return

    naming_sessions[user_id] = {
        "type": "video",
        "task_id": task_id,
        "work_dir": session["work_dir"],
        "file_path": session["file_path"],
        "chat_id": session["chat_id"],
        "resolution_tag": target_res,
        "apply_wm": (mode == "reswm")
    }

    name_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Use Default Name", callback_data=f"name_def_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"name_cancel_{user_id}")]
    ])

    await callback_query.message.edit_text(f"📝 **Enter File Name for {target_res}:**", reply_markup=name_kb)
    await callback_query.answer()

async def execute_video_processing(client: Client, chat_id: int, status_msg: Message, work_dir: str, input_file: str, resolution_tag: str, apply_wm: bool, custom_title: str, task_id: str):
    try:
        desc = []
        if resolution_tag:
            desc.append(f"Scaling to {resolution_tag}")
        if apply_wm:
            desc.append("Watermarking")
        task_title = " + ".join(desc) if desc else "Processing Video"

        output_mp4 = os.path.join(work_dir, f"{custom_title}.mp4")

        async with ENCODE_SEMAPHORE:
            success, err = await process_video_file_async(input_file, output_mp4, status_msg, task_title, resolution_tag, apply_wm)

        # Remove raw input to reclaim disk immediately
        if os.path.exists(input_file):
            try:
                os.remove(input_file)
            except Exception:
                pass

        if not success:
            err_snippet = "\n".join(err.strip().splitlines()[-10:]) if err else "Unknown FFmpeg error"
            await status_msg.edit_text(f"❌ **Processing failed:**\n```text\n{err_snippet}\n```")
            return

        final_bytes = os.path.getsize(output_mp4)
        if final_bytes > 1950 * 1024 * 1024:
            gdrive_link = await upload_via_rclone(output_mp4, status_msg)
            await status_msg.edit_text(
                f"✅ **Uploaded to Google Drive (> 2GB)!**\n\n🔗 **Link:** {gdrive_link}",
                reply_markup=get_action_keyboard(task_id)
            )
        else:
            await upload_mp4_video(client, chat_id, output_mp4, caption_name=custom_title)
            await status_msg.edit_text(
                "✅ **Video processing complete and uploaded!**",
                reply_markup=get_action_keyboard(task_id)
            )

    except Exception as e:
        await status_msg.edit_text(f"❌ Error during video processing: {e}")

# ----------------- Task Control Callbacks ----------------- #

@app.on_callback_query(filters.regex(r"^stop_([a-zA-Z0-9]+)$"))
async def stop_callback(client: Client, callback_query: CallbackQuery):
    task_id = callback_query.matches[0].group(1)
    session = active_sessions.get(task_id)

    if not session:
        await callback_query.answer("Recording already finalized.", show_alert=True)
        return

    if session.get("user_id") != callback_query.from_user.id:
        await callback_query.answer("⚠️ Not authorized.", show_alert=True)
        return

    await callback_query.answer("Stopping capture...")
    await finalize_and_upload(client, task_id, callback_query.message)

@app.on_callback_query(filters.regex(r"^reup_([a-zA-Z0-9]+)$"))
async def reupload_callback(client: Client, callback_query: CallbackQuery):
    task_id = callback_query.matches[0].group(1)
    matching = [d for d in glob.glob(f"*_{task_id}") if os.path.isdir(d)]
    
    if not matching:
        await callback_query.answer("Session folder not found.", show_alert=True)
        return

    session_dir = matching[0]
    mp4_files = sorted([
        f for f in glob.glob(os.path.join(session_dir, "*.mp4"))
        if os.path.isfile(f) and os.path.getsize(f) > 50000
    ])

    if not mp4_files:
        await callback_query.answer("No playable video found.", show_alert=True)
        return

    await callback_query.answer("Starting upload...")
    for filepath in mp4_files:
        clean_name = os.path.splitext(os.path.basename(filepath))[0]
        if os.path.getsize(filepath) > 1950 * 1024 * 1024:
            drive_link = await upload_via_rclone(filepath, callback_query.message)
            await callback_query.message.edit_text(f"✅ **Uploaded to Google Drive!**\n\n🔗 {drive_link}")
        else:
            await upload_mp4_video(client, callback_query.message.chat.id, filepath, caption_name=clean_name)

@app.on_callback_query(filters.regex(r"^del_([a-zA-Z0-9]+)$"))
async def delete_files_callback(client: Client, callback_query: CallbackQuery):
    task_id = callback_query.matches[0].group(1)
    matching = [d for d in glob.glob(f"*_{task_id}") if os.path.isdir(d)]
    
    if matching:
        for d in matching:
            shutil.rmtree(d, ignore_errors=True)
        await callback_query.answer("Files deleted successfully!", show_alert=True)
        await callback_query.message.edit_text("🗑️ **All local recording files deleted.**")
    else:
        await callback_query.answer("Files already removed.", show_alert=True)

if __name__ == "__main__":
    print("Bot starting with 4-core FFmpeg allocation, 5m/30s hardcoded watermark, and clean UI...")
    app.run()
