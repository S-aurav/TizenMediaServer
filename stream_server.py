import asyncio
import re
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, AuthKeyDuplicatedError
from dotenv import load_dotenv
from fastapi.responses import FileResponse

# Load environment variables from .env file
load_dotenv()

# Telegram API credentials (loaded from .env file)
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# Initialize FastAPI app
app = FastAPI()

# Dictionary to track download progress (message_id: {total_size, downloaded_size})
progress = {}

# Initialize Telegram client globally
client = None

async def start_client():
    global client
    try:
        client = TelegramClient('stream_session', int(API_ID), API_HASH)
        await client.start(phone=PHONE_NUMBER)
        print("✅ Telegram client connected")
    except AuthKeyDuplicatedError:
        print("❌ Auth key duplicated, resetting session")
        os.remove('stream_session.session')
        client = TelegramClient('stream_session', int(API_ID), API_HASH)
        await client.start(phone=PHONE_NUMBER)
        print("✅ Telegram client reconnected with new session")
    except Exception as e:
        print(f"❌ Failed to start Telegram client: {e}")
        raise

# Parse Telegram URL to extract channel and message ID
def parse_telegram_url(url):
    public_match = re.match(r"https://t\.me/([^/]+)/(\d+)", url)
    private_match = re.match(r"https://t\.me/c/(\d+)/(\d+)", url)
    if public_match:
        return public_match.group(1), int(public_match.group(2))
    elif private_match:
        return int(private_match.group(1)), int(private_match.group(2))
    return None, None

# Stream video directly from Telegram
async def stream_video(channel_id, message_id):
    if not client or not client.is_connected():
        print("❌ Client disconnected, attempting to reconnect")
        await start_client()

    try:
        # Get message
        message = await client.get_messages(channel_id, ids=message_id)
        if not message:
            print(f"❌ Message {message_id} not found")
            return None
        if not message.video:
            print(f"❌ No video found for message {message_id}")
            return None

        # Initialize progress tracking
        total_size = message.video.size
        progress[message_id] = {"total_size": total_size, "downloaded_size": 0}

        # Stream video chunks
        async def generate():
            try:
                async for chunk in client.iter_download(message.video, chunk_size=8*1024*1024):  # 8MB chunks
                    progress[message_id]["downloaded_size"] += len(chunk)
                    yield chunk
                print(f"✅ Streamed video for message {message_id}")
            except FloodWaitError as e:
                print(f"⚠️ Rate limit hit, waiting {e.seconds} seconds")
                await asyncio.sleep(e.seconds)
                async for chunk in client.iter_download(message.video, chunk_size=8*1024*1024):
                    progress[message_id]["downloaded_size"] += len(chunk)
                    yield chunk
            except ConnectionError as e:
                print(f"❌ Connection error: {e}, attempting to reconnect")
                await start_client()
                async for chunk in client.iter_download(message.video, chunk_size=8*1024*1024):
                    progress[message_id]["downloaded_size"] += len(chunk)
                    yield chunk
            except Exception as e:
                print(f"❌ Error streaming video: {e}")
                raise
            finally:
                # Clean up progress tracking
                if message_id in progress:
                    del progress[message_id]

        return generate(), total_size
    except Exception as e:
        print(f"❌ Error fetching video: {e}")
        return None, None

# FastAPI startup event to initialize Telegram client
@app.on_event("startup")
async def startup_event():
    await start_client()

# FastAPI shutdown event to disconnect Telegram client
@app.on_event("shutdown")
async def shutdown_event():
    global client
    if client and client.is_connected():
        await client.disconnect()
        print("✅ Telegram client disconnected")

# FastAPI route to stream video
@app.get("/stream")
async def stream(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="Missing Telegram URL")

    # Parse Telegram URL
    channel_id, message_id = parse_telegram_url(url)
    if not channel_id or not message_id:
        raise HTTPException(status_code=400, detail="Invalid Telegram URL")

    # Get async generator and total size
    async_gen, total_size = await stream_video(channel_id, message_id)
    if not async_gen:
        raise HTTPException(status_code=500, detail="Failed to fetch video")

    # Stream the video directly
    return StreamingResponse(
        async_gen,
        media_type="video/mp4",
        headers={"Content-Disposition": f"inline; filename=video_{message_id}.mp4"}
    )

# FastAPI route for progress updates (SSE)
@app.get("/progress/{message_id}")
async def progress_update(message_id: int):
    async def generate_progress():
        while message_id in progress:
            data = progress.get(message_id, {"total_size": 0, "downloaded_size": 0})
            total = data["total_size"]
            downloaded = data["downloaded_size"]
            percentage = (downloaded / total * 100) if total > 0 else 0
            yield f"data: {{\"percentage\": {percentage:.2f}, \"downloaded\": {downloaded}, \"total\": {total}}}\n\n"
            await asyncio.sleep(1)  # Update every second
        yield "data: {\"percentage\": 100}\n\n"  # Signal completion

    return StreamingResponse(
        generate_progress(),
        media_type="text/event-stream"
    )

@app.get("/series_info.json")
async def get_series_info():
    return FileResponse("series_info.json")

@app.get("/video.json")
async def get_video():
    return FileResponse("video.json")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)