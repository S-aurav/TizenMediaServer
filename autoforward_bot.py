import json
import os
from telegram.ext import Updater, MessageHandler, Filters
import logging
import re

# === CONFIG ===
BOT_TOKEN = '7995768178:AAEUvXLsuOvDlJf1XIotY5ZUsfpsWKJaiy4'
CHANNEL_ID = -1002859206175  # Your channel ID (negative number)
CHANNEL_LINK_PREFIX = 'https://t.me/+PBwaJqJntIlkNTk1'  # Change to your channel

OUTPUT_JSON = 'video.json'

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === JSON I/O ===
def load_json():
    if os.path.exists(OUTPUT_JSON):
        with open(OUTPUT_JSON, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                else:
                    logger.warning("⚠️ video.json is not a dict. Resetting...")
                    return {}
            except Exception as e:
                logger.error(f"Failed to load JSON: {e}")
                return {}
    return {}


def save_json(data):
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# === Handler for channel posts ===
def handle_channel_post(update, context):
    msg = update.channel_post
    if msg.chat.id != CHANNEL_ID:
        return

    message_id = msg.message_id
    full_title = msg.text or msg.caption or "Untitled"
    url = CHANNEL_LINK_PREFIX + str(message_id)

    # Extract using regex
    match = re.search(r"^(.*?)\s*-\s*S(\d{2})E(\d{2})", full_title, re.IGNORECASE)
    if not match:
        logger.warning(f"❌ Failed to parse: {full_title}")
        return

    series = match.group(1).strip()
    season_num = int(match.group(2))
    episode_num = match.group(3)

    season_key = f"Season {season_num}"
    episode_code = f"E{episode_num}"

    data = load_json()

    # Initialize structure
    if series not in data:
        data[series] = {}

    if season_key not in data[series]:
        data[series][season_key] = []

    # Avoid duplicate episodes
    if any(ep["url"] == url for ep in data[series][season_key]):
        logger.info("Already added")
        return

    # Add episode entry
    data[series][season_key].append({
        "episode": episode_code,
        "url": url
    })

    save_json(data)
    logger.info(f"✅ Added {series} - {season_key} - {episode_code}")

# === Main ===
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.chat(CHANNEL_ID), handle_channel_post))

    print("📡 Bot listening for new posts in your channel...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
