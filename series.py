import json
import os
import re
from telethon.sync import TelegramClient
from telethon.tl.types import MessageMediaDocument
from collections import defaultdict, OrderedDict
from dotenv import load_dotenv

# --- Load credentials ---
load_dotenv()
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
session_name = "scraper_session"

# --- Telegram setup ---
client = TelegramClient(session_name, api_id, api_hash)
client.start()

# --- Config ---
CHANNEL = "shindoraepisodes"  # without @
OUTPUT_JSON = "video.json"

# --- Series-specific rules ---
SERIES_RULES = {
    "Shinchan": [
        ("Season 1", re.compile(r"Crayon Shin-chan - S01E(\d+)", re.I)),
        ("Season 2", re.compile(r"Crayon Shin-chan - 00(\d+)", re.I)),
        ("Season 3", re.compile(r"Crayon Shin-chan - 010(\d+)", re.I)),
        ("Season 4", re.compile(r"Crayon Shin-chan - 015(\d+)", re.I)),
        ("Season 5", re.compile(r"Crayon Shin-chan - 021(\d+)", re.I)),
        ("Season 6", re.compile(r"Crayon Shin-chan - 026(\d+)", re.I)),
        ("Season 7", re.compile(r"Crayon Shin-chan - 032(\d+)", re.I)),
        ("Season 8", re.compile(r"Crayon Shin-chan - S08E(\d+)", re.I)),
        ("Season 9", re.compile(r"Shinchan S09E(\d+)", re.I)),
        ("Season 10", re.compile(r"Shinchan S10E(\d+)", re.I)),
        ("Season 12", re.compile(r"Shin-chan S12E(\d+)", re.I)),
        ("Season 13", re.compile(r"Shin-chan S13E(\d+)", re.I)),
        ("Season 14", re.compile(r"Shin-chan S14E(\d+)", re.I)),
        ("Season 15", re.compile(r"Shin-chan S15E(\d+)", re.I)),
        ("Season 16", re.compile(r"Shin-Chan\.S16E(\d+)", re.I)),
        ("Season 17", re.compile(r"Season 17.*Episode: (\d+)", re.I))
    ],
    "Doraemon": [
        ("Season 1", re.compile(r"Doraemon.*S01E(\d+)", re.I)),
        ("Season 2", re.compile(r"Doraemon.*S02E(\d+)", re.I)),
        ("Season 3", re.compile(r"Doraemon.*S03E(\d+)", re.I)),
        ("Season 4", re.compile(r"Doraemon.*S04E(\d+)", re.I)),
        ("Season 5", re.compile(r"S05E(\d+).mkv", re.I)),
        ("Season 6", re.compile(r"S06E(\d+).mkv", re.I)),
        ("Season 7", re.compile(r"Doraemon S07E(\d+)", re.I)),
        ("Season 8", re.compile(r"Doraemon S08E(\d+)", re.I)),
        ("Season 9", re.compile(r"Doraemon S09E(\d+)", re.I)),
        ("Season 10", re.compile(r"S10E(\d+)", re.I)),
        ("Season 14", re.compile(r"Doraemon S14E(\d+)", re.I)),
        ("Season 15", re.compile(r"Doraemon S15E(\d+)", re.I)),
        ("Season 16", re.compile(r"Doraemon S16E(\d+)", re.I)),
        ("Season 17", re.compile(r"Doraemon S17E(\d+)", re.I)),
        ("Season 18", re.compile(r"S18E(\d+)", re.I)),
        ("Season 19", re.compile(r"S19E(\d+)", re.I)),
        ("Season 20", re.compile(r"S20\.E(\d+)", re.I))
    ],
    "Kiteretsu": [
        ("Season 1", re.compile(r"Kiteretsu.*S01E(\d+)", re.I)),
        ("Season 2", re.compile(r"Kiteretsu.*S02E(\d+)", re.I)),
        ("Season 3", re.compile(r"Kiteretsu.*S03E(\d+)", re.I)),
        ("Season 4", re.compile(r"Kiteretsu.*S04E(\d+)", re.I)),
        ("Season 5", re.compile(r"Kiteretsu.*S05E(\d+)", re.I)),
        ("Season 6", re.compile(r"Kiteretsu.*S06E(\d+)", re.I))
    ],
    "Ninja Hattori": [
        ("Season 1", re.compile(r"Hattori.*S01E(\d+)", re.I)),
        ("Season 2", re.compile(r"Hattori.*S02E(\d+)", re.I)),
        ("Season 3", re.compile(r"Hattori.*S03E(\d+)", re.I)),
        ("Season 4", re.compile(r"Hattori.*S04E(\d+)", re.I)),
        ("Season 5", re.compile(r"Hattori.*S05E(\d+)", re.I))
    ],
    "Ninja Hattori Returns": [
        ("Season 1", re.compile(r"Ninja\.Hattori.*S01E(\d+)", re.I)),
        ("Season 2", re.compile(r"Ninja\.Hattori.*S02E(\d+)", re.I)),
        ("Season 3", re.compile(r"New\.S03E(\d+)", re.I)),
        ("Season 4", re.compile(r"New\.S04E(\d+)", re.I))
    ]
}

# --- Main Scraping Logic ---
def scrape():
    result = {}
    print(f"🔍 Scraping from channel: {CHANNEL}")
    for message in client.iter_messages(CHANNEL):
        if not message or not message.message:
            continue

        text = message.message.strip()
        link = f"https://t.me/{CHANNEL}/{message.id}"

        for series, patterns in SERIES_RULES.items():
            for season, pattern in patterns:
                match = pattern.search(text)
                if match:
                    ep_num = int(match.group(1))
                    result.setdefault(series, {}).setdefault(season, []).append({
                        "episode": ep_num,
                        "title": text[:100],
                        "url": link
                    })
                    break  # Stop at first matching pattern per series

    # Sort episodes in each season
    for series_data in result.values():
        for season in series_data:
            season_data = series_data[season]
            season_data.sort(key=lambda ep: ep["episode"])

    # Sort seasons numerically
    sorted_result = {}
    for series, seasons in result.items():
        sorted_seasons = OrderedDict(sorted(seasons.items(), key=lambda s: int(re.search(r"\d+", s[0]).group()) if re.search(r"\d+", s[0]) else float('inf')))

        sorted_result[series] = sorted_seasons

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(sorted_result, f, indent=2, ensure_ascii=False)
    print(f"✅ Done. JSON saved to {OUTPUT_JSON}")

if __name__ == "__main__":
    scrape()
