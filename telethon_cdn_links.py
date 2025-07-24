from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument
import json
import os

api_id = 28148186  # ← your API ID here
api_hash = 'f657adb2fe3212f4d293118b9a14f382'  # ← your API hash here
channel_username = 'https://t.me/+PBwaJqJntIlkNTk1'  # e.g. @yourchannel or https://t.me/yourchannel

client = TelegramClient('session_name', api_id, api_hash)

async def main():
    await client.start()
    print("✅ Logged in successfully!")

    messages = await client.get_messages(channel_username, limit=50)
    
    videos = []

    for msg in messages:
        if msg.media and isinstance(msg.media, MessageMediaDocument):
            if msg.file and msg.file.mime_type and "video" in msg.file.mime_type:
                file_name = msg.file.name or msg.text or "episode"
                print(f"Found video: {file_name}")

                try:
                    url = await client.download_media(msg, file=None)  # Downloading the file locally (optional)
                    # Instead of downloading, get the CDN media link:
                    cdn_link = await client.get_download_url(msg)
                    print("CDN:", cdn_link)

                    videos.append({
                        "title": file_name,
                        "url": cdn_link
                    })
                except Exception as e:
                    print("❌ Failed to get CDN link:", e)

    with open("video.json", "w") as f:
        json.dump(videos, f, indent=2)
        print("✅ Saved to video.json")

with client:
    client.loop.run_until_complete(main())
