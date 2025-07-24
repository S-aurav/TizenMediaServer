import os
import json
import threading
import asyncio
from flask import Flask, Response, request, jsonify
from telegram import Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackContext
from telethon.sync import TelegramClient

# --- CONFIG ---
api_id = 28148186
api_hash = 'f657adb2fe3212f4d293118b9a14f382'
BOT_TOKEN = '7995768178:AAEUvXLsuOvDlJf1XIotY5ZUsfpsWKJaiy4'
SESSION_NAME = 'session'
MEDIA_CHANNEL = '@MediaServer'  # Bot must be admin here

# --- FOLDERS ---
TEMP_FOLDER = 'temp_videos'
JSON_PATH = 'video.json'
os.makedirs(TEMP_FOLDER, exist_ok=True)

# --- GLOBAL STATE ---
video_data = {}
VIDEO_MAP = {}

# --- INIT TELETHON CLIENT ---
client = TelegramClient(SESSION_NAME, api_id, api_hash)
client.start()

# --- FLASK APP ---
app = Flask(__name__)

# --- JSON SYNC ---
def load_video_data():
    global video_data, VIDEO_MAP
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            video_data = json.load(f)
    else:
        video_data = {}

    VIDEO_MAP.clear()
    for series, seasons in video_data.items():
        for season, episodes in seasons.items():
            for ep in episodes:
                episode_id = f"{series}_{season}_{ep['episode']}".lower().replace(' ', '_')
                VIDEO_MAP[episode_id] = ep['url']


def save_video_data():
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(video_data, f, indent=2)
    load_video_data()


# --- BOT HANDLER ---
def parse_episode_title(text):
    import re
    match = re.search(r'(S\d{2})E(\d{2})', text, re.IGNORECASE)
    if not match:
        return None, None
    season = f"Season {int(match.group(1)[1:])}"
    episode = f"E{match.group(2)}"
    return season, episode


def handle_forwarded(update: Update, context: CallbackContext):
    msg = update.message or update.channel_post
    if not msg.forward_from_chat or not msg.forward_from_chat.username:
        context.bot.send_message(
            chat_id=msg.chat_id,
            text="❌ Please forward a valid message from the original media channel."
        )
        return
    


    series = msg.caption or msg.text or "Unknown Series"
    season, episode = parse_episode_title(series)
    if not season or not episode:
        msg.reply_text("❌ Could not detect season/episode info from message")
        return

    series_title = msg.forward_from_chat.title
    if series_title not in video_data:
        video_data[series_title] = {}
    if season not in video_data[series_title]:
        video_data[series_title][season] = []

    # Avoid duplicates
    for ep in video_data[series_title][season]:
        if ep['episode'] == episode:
            return

    video_data[series_title][season].append({
        'episode': episode,
        'url': msg.link  # e.g. https://t.me/c/123/456
    })
    save_video_data()
    msg.reply_text(f"✅ Added: {series_title} {season} {episode}")


# --- STREAMING FUNCTION ---
def delete_later(path, delay=300):
    def deleter():
        import time
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
    threading.Thread(target=deleter, daemon=True).start()


@app.route('/stream')
def stream():
    vid_id = request.args.get('id')
    if vid_id not in VIDEO_MAP:
        return 'Invalid ID', 404

    url = VIDEO_MAP[vid_id]
    parts = url.strip().split('/')
    msg_id = int(parts[-1].lstrip('+'))

    local_path = os.path.join(TEMP_FOLDER, f"{vid_id}.mp4")
    if os.path.exists(local_path):
        def gen():
            with open(local_path, 'rb') as f:
                while chunk := f.read(8192):
                    yield chunk
        delete_later(local_path)
        return Response(gen(), mimetype='video/mp4')

    async def download():
        msg = await client.get_messages(MEDIA_CHANNEL, ids=msg_id)
        return await client.download_media(msg, file=local_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(download())

    def generate():
        with open(local_path, 'rb') as f:
            while chunk := f.read(8192):
                yield chunk
    delete_later(local_path)
    return Response(generate(), mimetype='video/mp4')


@app.route('/video.json')
def get_video_json():
    return jsonify(video_data)


# --- START BOTH ---
def start_bot():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(MessageHandler(Filters.forwarded, handle_forwarded))
    updater.start_polling()
    print("✅ Telegram bot running...")
    updater.idle()


def start_flask():
    print("✅ Flask server running at http://localhost:5000")
    app.run(host='0.0.0.0', port=5000)


if __name__ == '__main__':
    load_video_data()
    # threading.Thread(target=start_bot, daemon=True).start()
    start_bot()
    start_flask()
