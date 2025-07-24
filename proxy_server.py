import os
import json
import re
import asyncio
import aiohttp
import tempfile
import shutil
import time
import requests
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
from pathlib import Path


# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# Telegram CDN configuration
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

# PixelDrain configuration
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY")  # Optional for better upload limits
PIXELDRAIN_UPLOAD_URL = "https://pixeldrain.com/api/file"
PIXELDRAIN_DOWNLOAD_URL = "https://pixeldrain.com/api/file/{file_id}"
MAX_ACCESS_COUNT = 4  # Maximum times a file can be accessed before expiring

# Database files for tracking episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

# Local cache configuration
CACHE_DIR = os.path.join(os.getcwd(), "cache")
DOWNLOADS_DIR = os.path.join(os.getcwd(), "downloads")  
TEMP_DIR = os.path.join(os.getcwd(), "tmp")

# Streaming configuration
STREAMING_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB buffer before starting stream

# Dynamic chunk sizing for optimal download speed
CHUNK_SIZE_MIN = 2048 * 1024      # 2MB minimum chunk
CHUNK_SIZE_DEFAULT = 5 * 1024 * 1024    # 5MB default chunk (increased from 2MB)
CHUNK_SIZE_MAX = 100 * 1024 * 1024       # 100MB maximum chunk for high-speed servers
CHUNK_SIZE_ADAPTIVE = True               # Enable adaptive chunk sizing

MAX_CACHE_SIZE_GB = 5  # Maximum cache size in GB
MIN_FREE_SPACE_GB = 1  # Minimum free space to maintain

# Custom temporary directory (optional)
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")

# Custom temporary directory (optional)
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")

# Get port from environment (Railway sets this automatically)
PORT = int(os.getenv("PORT", 8000))

app = FastAPI(title="Smart TV Streaming Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or restrict to ["http://localhost:PORT"] etc.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Static folder for poster images
app.mount("/static", StaticFiles(directory="static"), name="static")

POSTERS = {
    "Shinchan": "/static/shinchan.jpg",
    "Doraemon": "/static/doraemon.jpg",
    "Kiteretsu": "/static/kiteretsu.jpg",
    "Ninja Hattori": "/static/ninja hattori.jpg",
    "Ninja Hattori Returns": "/static/ninja hattori returns.jpg"
}

# Global variables
client = None
download_tasks = {}  # message_id -> asyncio.Task
cleanup_task = None
season_download_queue = {}  # season_id -> {"episodes": [...], "current_index": 0, "status": "downloading"}
season_download_task = None

@app.on_event("startup")
async def startup_event():
    global client, cleanup_task
    try:
        # Initialize Telegram client
        if TELEGRAM_SESSION_STRING:
            print("🔐 Using StringSession for Telegram authentication...")
            session = StringSession(TELEGRAM_SESSION_STRING)
            client = TelegramClient(session, API_ID, API_HASH)
        else:
            print("📁 Using file session for Telegram authentication...")
            client = TelegramClient('stream_session', API_ID, API_HASH)
        
        await client.start()
        print("✅ Telegram client connected successfully")
        
        # Verify connection
        me = await client.get_me()
        print(f"👤 Logged in as: {me.first_name} ({me.phone})")
        
        # Initialize PixelDrain configuration
        print("🎯 Initializing PixelDrain configuration...")
        if PIXELDRAIN_API_KEY:
            print("� PixelDrain API key configured - better upload limits available")
        else:
            print("⚠️ No PixelDrain API key - using anonymous uploads (limited)")
        
        # Run initial cleanup of expired files
        print("🧹 Running initial cleanup check...")
        cleaned_count = await cleanup_expired_files()
        print(f"🧹 Cleaned up {cleaned_count} expired files")
        
        # Start periodic cleanup task
        cleanup_task = asyncio.create_task(periodic_cleanup())
        print("⏰ Started periodic cleanup task (runs every 6 hours)")
        
    except Exception as e:
        print(f"❌ Failed to connect Telegram client: {e}")
        print("💡 Make sure TELEGRAM_SESSION_STRING is set in environment variables")

@app.on_event("shutdown")
async def shutdown_event():
    global cleanup_task
    if client and client.is_connected():
        await client.disconnect()
    
    # Cancel cleanup task
    if cleanup_task:
        cleanup_task.cancel()
        print("⏰ Stopped periodic cleanup task")

# Catalog routes
@app.get("/catalog/series")
async def list_series():
    with open("video.json", encoding="utf-8") as f:
        data = json.load(f)
    return [{"name": s, "poster": POSTERS.get(s, "/static/default.jpg")} for s in data.keys()]

@app.get("/catalog/series/{series_name}")
async def list_seasons(series_name: str):
    with open("video.json", encoding="utf-8") as f:
        data = json.load(f)
    if series_name not in data:
        raise HTTPException(status_code=404, detail="Series not found")
    return list(data[series_name].keys())

@app.get("/catalog/series/{series_name}/{season_name}")
async def list_episodes(series_name: str, season_name: str):
    with open("video.json", encoding="utf-8") as f:
        data = json.load(f)
    if series_name not in data or season_name not in data[series_name]:
        raise HTTPException(status_code=404, detail="Season not found")
    episodes = data[series_name][season_name]
    
    # Load uploaded files database
    uploaded_files = load_uploaded_files_db()
    expired_files = []

    for ep in episodes:
        # Extract msg_id from URL
        _, msg_id = parse_telegram_url(ep["url"])
        ep["msg_id"] = msg_id
        
        # Check if file is uploaded to PixelDrain
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            
            # Check if file has exceeded access limit
            access_count = file_info.get("access_count", 0)
            if access_count >= MAX_ACCESS_COUNT:
                # File exceeded access limit - mark as expired
                expired_files.append(str(msg_id))
                ep["downloaded"] = False
                ep["expired"] = True
            else:
                # File is available
                ep["downloaded"] = True
                ep["pixeldrain_id"] = file_info["pixeldrain_id"]
                ep["access_count"] = access_count
                ep["remaining_access"] = MAX_ACCESS_COUNT - access_count
        else:
            ep["downloaded"] = False
    
    # Clean up expired files from database
    if expired_files:
        for msg_id in expired_files:
            if msg_id in uploaded_files:
                file_info = uploaded_files[msg_id]
                # Delete from PixelDrain
                await delete_from_pixeldrain(file_info.get("pixeldrain_id"))
                # Remove from database
                del uploaded_files[msg_id]
        save_uploaded_files_db(uploaded_files)
        print(f"🧹 Cleaned up {len(expired_files)} expired files during episode listing")

    return episodes

# Telegram helpers
def parse_telegram_url(url):
    public_match = re.match(r"https://t\.me/([^/]+)/(\d+)", url)
    private_match = re.match(r"https://t\.me/c/(\d+)/(\d+)", url)
    if public_match:
        return public_match.group(1), int(public_match.group(2))
    elif private_match:
        return int(private_match.group(1)), int(private_match.group(2))
    return None, None

# PixelDrain helper functions
def load_uploaded_files_db():
    """Load the database of uploaded files"""
    if os.path.exists(UPLOADED_FILES_DB):
        with open(UPLOADED_FILES_DB, 'r') as f:
            return json.load(f)
    return {}

def save_uploaded_files_db(data):
    """Save the database of uploaded files"""
    try:
        print(f"💾 Attempting to save database with {len(data)} entries")
        with open(UPLOADED_FILES_DB, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"✅ Database saved successfully to {UPLOADED_FILES_DB}")
    except Exception as e:
        print(f"❌ Error saving database: {e}")
        raise e

async def download_with_adaptive_chunking(client, message, file_path):
    """Download with adaptive chunk sizing for maximum speed"""
    import time
    
    print(f"🚀 Starting adaptive download to: {file_path}")
    
    # Initial settings
    current_chunk_size = CHUNK_SIZE_DEFAULT
    download_speeds = []  # Track speeds for adaptation
    total_downloaded = 0
    start_time = time.time()
    last_speed_check = start_time
    speed_check_interval = 5.0  # Check speed every 5 seconds
    
    # Speed thresholds for chunk size adjustment (MB/s)
    SPEED_THRESHOLD_HIGH = 10.0    # > 10 MB/s = increase chunk size
    SPEED_THRESHOLD_MEDIUM = 5.0   # 5-10 MB/s = maintain chunk size
    SPEED_THRESHOLD_LOW = 2.0      # < 2 MB/s = decrease chunk size
    
    try:
        # Use Telethon's iter_download for chunked downloading
        downloaded_bytes = 0
        chunk_start_time = time.time()
        
        async for chunk in client.iter_download(message.media, chunk_size=current_chunk_size):
            # Write chunk to file
            with open(file_path, 'ab') as f:
                f.write(chunk)
            
            downloaded_bytes += len(chunk)
            total_downloaded += len(chunk)
            current_time = time.time()
            
            # Calculate speed and adapt chunk size every 5 seconds
            if current_time - last_speed_check >= speed_check_interval:
                time_elapsed = current_time - chunk_start_time
                if time_elapsed > 0:
                    # Calculate speed in MB/s
                    speed_mbps = (downloaded_bytes / (1024 * 1024)) / time_elapsed
                    download_speeds.append(speed_mbps)
                    
                    print(f"📊 Current speed: {speed_mbps:.2f} MB/s, Chunk size: {current_chunk_size / (1024*1024):.1f} MB")
                    
                    # Adapt chunk size based on speed
                    if speed_mbps > SPEED_THRESHOLD_HIGH and current_chunk_size < CHUNK_SIZE_MAX:
                        # High speed - increase chunk size for better efficiency
                        new_chunk_size = min(current_chunk_size * 2, CHUNK_SIZE_MAX)
                        print(f"⬆️ High speed detected, increasing chunk size to {new_chunk_size / (1024*1024):.1f} MB")
                        current_chunk_size = new_chunk_size
                        
                    elif speed_mbps < SPEED_THRESHOLD_LOW and current_chunk_size > CHUNK_SIZE_MIN:
                        # Low speed - decrease chunk size to avoid timeouts
                        new_chunk_size = max(current_chunk_size // 2, CHUNK_SIZE_MIN)
                        print(f"⬇️ Low speed detected, decreasing chunk size to {new_chunk_size / (1024*1024):.1f} MB")
                        current_chunk_size = new_chunk_size
                        
                    elif SPEED_THRESHOLD_LOW <= speed_mbps <= SPEED_THRESHOLD_HIGH:
                        # Medium speed - maintain current chunk size
                        print(f"✅ Optimal speed, maintaining chunk size at {current_chunk_size / (1024*1024):.1f} MB")
                    
                    # Reset counters for next speed check
                    downloaded_bytes = 0
                    chunk_start_time = current_time
                    last_speed_check = current_time
        
        # Calculate final statistics
        total_time = time.time() - start_time
        average_speed = (total_downloaded / (1024 * 1024)) / total_time if total_time > 0 else 0
        
        print(f"✅ Download completed!")
        print(f"📊 Final stats:")
        print(f"   Total size: {total_downloaded / (1024*1024):.2f} MB")
        print(f"   Total time: {total_time:.2f} seconds")
        print(f"   Average speed: {average_speed:.2f} MB/s")
        print(f"   Final chunk size: {current_chunk_size / (1024*1024):.1f} MB")
        
        if download_speeds:
            max_speed = max(download_speeds)
            print(f"   Peak speed: {max_speed:.2f} MB/s")
            
        return True
        
    except Exception as e:
        print(f"❌ Adaptive download failed: {e}")
        raise e

async def upload_to_pixeldrain(file_path: str, filename: str) -> str:
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Upload attempt {attempt + 1}/{max_retries} to PixelDrain: {filename}")
            print(f"📊 File size: {os.path.getsize(file_path) / (1024*1024):.2f} MB")
            
            # Use the most reliable upload method based on file size
            file_size = os.path.getsize(file_path)
            if file_size > 100 * 1024 * 1024:  # 100MB+
                print("📦 Large file detected, using requests method...")
                return await upload_with_requests(file_path, filename)
            else:
                print("📄 Standard file, using aiohttp method...")
                return await upload_with_aiohttp(file_path, filename)
                
        except Exception as e:
            print(f"❌ Upload attempt {attempt + 1} failed: {e}")
            
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                raise Exception(f"Upload failed after {max_retries} attempts: {e}")
    
    raise Exception(f"Upload failed after {max_retries} attempts")

async def upload_with_aiohttp(file_path: str, filename: str) -> str:
    """Upload using aiohttp for better async handling"""
    headers = {
        'User-Agent': 'Smart-TV-Streaming-Server/1.0'
    }
    if PIXELDRAIN_API_KEY:
        import base64
        auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
        headers['Authorization'] = f'Basic {auth_string}'
        print("🔑 Using API key for better upload limits")
    
    timeout = aiohttp.ClientTimeout(total=900, connect=60)  # 15 min total, 60s connect
    
    connector = aiohttp.TCPConnector(
        limit=10,
        limit_per_host=2,
        ttl_dns_cache=300,
        use_dns_cache=True,
        ssl=False  # Disable SSL verification for better compatibility
    )
    
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        with open(file_path, 'rb') as f:
            data = aiohttp.FormData()
            data.add_field('file', f, filename=filename, content_type='application/octet-stream')
            
            print("📤 Uploading to PixelDrain with aiohttp...")
            async with session.post(
                PIXELDRAIN_UPLOAD_URL,
                data=data,
                headers=headers
            ) as response:
                
                if response.status == 201:
                    file_id = await parse_pixeldrain_response(response)
                    print(f"✅ aiohttp upload successful! PixelDrain ID: {file_id}")
                    return file_id
                else:
                    error_text = await response.text()
                    print(f"❌ aiohttp upload failed: {response.status} - {error_text}")
                    raise Exception(f"PixelDrain upload failed: {response.status} - {error_text}")

async def upload_with_requests(file_path: str, filename: str) -> str:
    """Upload using requests for large files"""
    import requests
    import urllib3
    urllib3.disable_warnings()
    
    headers = {'User-Agent': 'Smart-TV-Streaming-Server/1.0'}
    if PIXELDRAIN_API_KEY:
        import base64
        auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
        headers['Authorization'] = f'Basic {auth_string}'
        print("🔑 Using API key for better upload limits")
    
    with open(file_path, 'rb') as f:
        files = {'file': (filename, f, 'application/octet-stream')}
        
        print("📤 Uploading to PixelDrain with requests...")
        response = requests.post(
            PIXELDRAIN_UPLOAD_URL,
            files=files,
            headers=headers,
            verify=False,
            timeout=(30, 1800)  # 30s connect, 30min read
        )
    
    if response.status_code == 201:
        file_id = parse_pixeldrain_response_sync(response)
        print(f"✅ requests upload successful! PixelDrain ID: {file_id}")
        return file_id
    else:
        print(f"❌ requests upload failed: {response.status_code} - {response.text}")
        raise Exception(f"PixelDrain upload failed: {response.status_code} - {response.text}")

async def parse_pixeldrain_response(response) -> str:
    """Parse PixelDrain response to extract file ID"""
    try:
        result = await response.json()
        file_id = result.get('id')
        if file_id:
            return file_id
        else:
            raise Exception("No file ID found in JSON response")
    except Exception as json_error:
        # Handle case where response is plain text (common with PixelDrain)
        result_text = await response.text()
        print(f"📄 Got plain text response: {result_text[:100]}...")
        
        # PixelDrain sometimes returns just the file ID as plain text
        if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
            file_id = result_text.strip()
            print(f"✅ Extracted file ID from text response: {file_id}")
            return file_id
        else:
            # Try to extract ID from text response
            import re
            id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
            if id_match:
                file_id = id_match.group(1)
                print(f"✅ Extracted file ID from text: {file_id}")
                return file_id
            else:
                print(f"❌ Could not parse file ID from response: {result_text}")
                raise Exception(f"Could not parse PixelDrain response: {json_error}")

def parse_pixeldrain_response_sync(response) -> str:
    """Parse PixelDrain response to extract file ID (sync version)"""
    try:
        result = response.json()
        file_id = result.get('id')
        if file_id:
            return file_id
        else:
            raise Exception("No file ID found in JSON response")
    except Exception as json_error:
        # Handle case where response is plain text (common with PixelDrain)
        result_text = response.text
        print(f"📄 Got plain text response: {result_text[:100]}...")
        
        # PixelDrain sometimes returns just the file ID as plain text
        if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
            file_id = result_text.strip()
            print(f"✅ Extracted file ID from text response: {file_id}")
            return file_id
        else:
            # Try to extract ID from text response
            import re
            id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
            if id_match:
                file_id = id_match.group(1)
                print(f"✅ Extracted file ID from text: {file_id}")
                return file_id
            else:
                print(f"❌ Could not parse file ID from response: {result_text}")
                raise Exception(f"Could not parse PixelDrain response: {json_error}")


async def check_pixeldrain_file_exists(file_id: str) -> bool:
    """Check if a file exists on PixelDrain"""
    try:
        url = PIXELDRAIN_DOWNLOAD_URL.format(file_id=file_id)
        response = requests.head(url, timeout=10)
        return response.status_code == 200
    except:
        return False

async def delete_from_pixeldrain(file_id: str):
    """Delete a file from PixelDrain"""
    try:
        if not file_id:
            return
            
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {auth_string}'
        
        delete_url = f"https://pixeldrain.com/api/file/{file_id}"
        response = requests.delete(delete_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            print(f"�️ Deleted from PixelDrain: {file_id}")
        else:
            print(f"⚠️ Delete failed (may already be expired): {file_id} - {response.status_code}")
            
    except Exception as e:
        print(f"❌ Error deleting from PixelDrain: {e}")

def get_pixeldrain_download_url(file_id: str) -> str:
    """Get direct download URL for PixelDrain file"""
    return PIXELDRAIN_DOWNLOAD_URL.format(file_id=file_id)

# Download and upload to PixelDrain
@app.get("/download")
async def trigger_download(url: str):
    channel, msg_id = parse_telegram_url(url)
    if not channel or not msg_id:
        raise HTTPException(400, "Invalid Telegram URL")

    # Check if already uploaded to PixelDrain
    uploaded_files = load_uploaded_files_db()
    if str(msg_id) in uploaded_files:
        file_info = uploaded_files[str(msg_id)]
        pixeldrain_id = file_info["pixeldrain_id"]
        
        # Verify file still exists on PixelDrain
        if await check_pixeldrain_file_exists(pixeldrain_id):
            return {
                "status": "already_uploaded", 
                "pixeldrain_id": pixeldrain_id,
                "access_count": file_info.get("access_count", 0),
                "remaining_access": MAX_ACCESS_COUNT - file_info.get("access_count", 0)
            }
        else:
            # File expired or deleted, remove from database
            del uploaded_files[str(msg_id)]
            save_uploaded_files_db(uploaded_files)

    if not client.is_connected():
        await client.connect()

    message = await client.get_messages(channel, ids=msg_id)
    if not message or (not message.video and not message.document):
        raise HTTPException(404, detail="No video or document found")

    # Check if download is already in progress
    if msg_id in download_tasks:
        return {"status": "uploading"}

    async def do_download_and_upload():
        temp_path = None
        try:
            print(f"🔄 Starting download and upload for message ID: {msg_id}")
            
            # Get original file extension from the message
            original_filename = None
            
            if message.video:
                # Video message
                if message.video.attributes:
                    for attr in message.video.attributes:
                        if hasattr(attr, 'file_name') and attr.file_name:
                            original_filename = attr.file_name
                            break
            elif message.document:
                # Document message (video sent as document)
                if message.document.attributes:
                    for attr in message.document.attributes:
                        if hasattr(attr, 'file_name') and attr.file_name:
                            original_filename = attr.file_name
                            break
            
            if original_filename:
                file_ext = os.path.splitext(original_filename)[1] or '.mkv'
            else:
                file_ext = '.mkv'
            
            filename = f"{msg_id}{file_ext}"
            print(f"📝 Determined filename: {filename}")
            
            # Create temporary file for download
            if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
                # Use custom temp directory if specified
                os.makedirs(CUSTOM_TEMP_DIR, exist_ok=True)
                temp_path = os.path.join(CUSTOM_TEMP_DIR, f"temp_{msg_id}{file_ext}")
            else:
                # Use system temp directory with consistent naming
                temp_dir = tempfile.gettempdir()
                temp_path = os.path.join(temp_dir, f"temp_{msg_id}{file_ext}")
                # Ensure any existing file is removed first
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
            
            print(f"⬇️ Downloading {filename} to temporary file: {temp_path}")
            
            # Use adaptive chunking for optimal download speed
            if CHUNK_SIZE_ADAPTIVE:
                print("🚀 Using adaptive chunking for maximum download speed...")
                await download_with_adaptive_chunking(client, message, temp_path)
            else:
                print("📥 Using standard download method...")
                await client.download_media(message, file=temp_path)
                
            print(f"✅ Download complete: {filename}")
            
            # Verify file exists before upload
            if not os.path.exists(temp_path):
                raise Exception(f"Downloaded file not found: {temp_path}")
            
            file_size = os.path.getsize(temp_path)
            print(f"📊 File size: {file_size / (1024*1024):.2f} MB")
            
            # Upload to PixelDrain with better retry logic
            print(f"🚀 Starting upload to PixelDrain...")
            pixeldrain_id = await upload_to_pixeldrain(temp_path, filename)
            print(f"🎯 Received PixelDrain ID: {pixeldrain_id}")
            
            # Save to database
            print(f"💾 Saving to database...")
            uploaded_files = load_uploaded_files_db()
            print(f"📋 Current database has {len(uploaded_files)} files")
            
            # Get file size from the appropriate message type
            if message.video:
                original_file_size = message.video.size
            elif message.document:
                original_file_size = message.document.size
            else:
                original_file_size = file_size  # Use actual downloaded size as fallback
            
            uploaded_files[str(msg_id)] = {
                "pixeldrain_id": pixeldrain_id,
                "filename": filename,
                "uploaded_at": int(time.time()),
                "file_size": original_file_size,
                "access_count": 0  # Track how many times file was accessed
            }
            
            print(f"📝 Adding entry for msg_id: {msg_id}")
            save_uploaded_files_db(uploaded_files)
            print(f"💾 Database saved successfully")
            
            # Verify save worked
            verification_db = load_uploaded_files_db()
            if str(msg_id) in verification_db:
                print(f"✅ Verification: File {msg_id} found in database")
            else:
                print(f"⚠️ Warning: File {msg_id} NOT found in database after save")
            
            print(f"✅ Successfully uploaded {filename} to PixelDrain: {pixeldrain_id}")
            
        except Exception as e:
            print(f"❌ Failed to download/upload {msg_id}: {e}")
            import traceback
            print(f"🔍 Full error trace: {traceback.format_exc()}")
        finally:
            # Clean up temporary file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    print(f"🧹 Cleaned up temp file: {temp_path}")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp file: {cleanup_error}")
            download_tasks.pop(msg_id, None)
            print(f"🏁 Download task completed for {msg_id}")

    download_tasks[msg_id] = asyncio.create_task(do_download_and_upload())
    return {"status": "uploading"}

@app.get("/downloads")
async def list_downloads():
    uploaded_files = load_uploaded_files_db()
    files = []
    
    for msg_id, file_info in uploaded_files.items():
        if "pixeldrain_id" in file_info:
            # PixelDrain format
            files.append({
                "msg_id": msg_id,
                "pixeldrain_id": file_info["pixeldrain_id"],
                "filename": file_info["filename"],
                "storage": "pixeldrain",
                "access_count": file_info.get("access_count", 0),
                "remaining_access": MAX_ACCESS_COUNT - file_info.get("access_count", 0)
            })
        else:
            # Unknown format
            files.append({
                "msg_id": msg_id,
                "filename": file_info.get("filename", f"{msg_id}.mkv"),
                "storage": "unknown",
                "note": "Unknown format, re-download recommended"
            })
    
    return files

@app.get("/stream_local/{msg_id}")
async def stream_local(msg_id: str, request: Request):
    # Get file URL from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    filename = file_info.get("filename", f"{msg_id}.mkv")
    pixeldrain_id = file_info["pixeldrain_id"]
    
    # Verify file still exists on PixelDrain
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    # Check access count
    access_count = file_info.get("access_count", 0)
    if access_count >= MAX_ACCESS_COUNT:
        # File exceeded access limit
        await delete_from_pixeldrain(pixeldrain_id)
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        raise HTTPException(410, f"File access limit exceeded ({MAX_ACCESS_COUNT} times). File has been deleted.")
    
    # Increment access count
    file_info["access_count"] = access_count + 1
    uploaded_files[str(msg_id)] = file_info
    save_uploaded_files_db(uploaded_files)
    
    print(f"📺 Redirecting to PixelDrain direct URL: {filename} (access {access_count + 1}/{MAX_ACCESS_COUNT})")
    
    # Get PixelDrain direct URL
    pixeldrain_url = get_pixeldrain_download_url(pixeldrain_id)
    
    # For Smart TVs, redirect to the direct PixelDrain URL
    # PixelDrain handles range requests natively
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=pixeldrain_url, status_code=302)

@app.head("/stream_local/{msg_id}")
async def stream_local_head(msg_id: str):
    # Get file URL from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    pixeldrain_id = file_info["pixeldrain_id"]
    
    # Verify file still exists
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        raise HTTPException(404, "File not found on PixelDrain")
    
    # Check access count
    access_count = file_info.get("access_count", 0)
    if access_count >= MAX_ACCESS_COUNT:
        raise HTTPException(410, f"File access limit exceeded ({MAX_ACCESS_COUNT} times)")
    
    # Return basic headers for video content
    response_headers = {
        'Accept-Ranges': 'bytes',
        'Content-Type': 'video/mp4',
        'Cache-Control': 'no-cache'
    }
    
    return JSONResponse(content={}, headers=response_headers)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    
    # Special handling for streaming-related assertion errors
    if isinstance(exc, AssertionError) and "Data should not be empty" in str(exc):
        print("🔥 Streaming AssertionError caught (likely client disconnect): Data should not be empty")
        # Don't log full traceback for this common streaming error
        return JSONResponse(status_code=500, content={"detail": "Streaming interrupted"})
    
    print("🔥 Uncaught Exception:", traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": str(exc)})

# File expiration and cleanup functions
async def cleanup_expired_files():
    """Check all uploaded files and remove expired ones from database"""
    uploaded_files = load_uploaded_files_db()
    expired_files = []
    
    print("🧹 Starting proactive cleanup of expired files...")
    
    for msg_id, file_info in uploaded_files.items():
        filename = file_info.get("filename", f"{msg_id}.mkv")
        
        # Check if file is PixelDrain format
        if "pixeldrain_id" in file_info:
            pixeldrain_id = file_info["pixeldrain_id"]
            access_count = file_info.get("access_count", 0)
            
            # Check if file has exceeded access limit
            if access_count >= MAX_ACCESS_COUNT:
                expired_files.append(msg_id)
                print(f"🗑️ Found expired file (access limit reached): {filename} ({access_count}/{MAX_ACCESS_COUNT} accesses)")
                # Delete from PixelDrain
                await delete_from_pixeldrain(pixeldrain_id)
            else:
                # Check if file still exists on PixelDrain
                if not await check_pixeldrain_file_exists(pixeldrain_id):
                    expired_files.append(msg_id)
                    print(f"🗑️ Found expired file (not found on PixelDrain): {filename}")
        else:
            # Unknown format - mark for removal
            expired_files.append(msg_id)
            print(f"🗑️ Found unknown format file: {filename}")
    
    # Remove expired files from database
    if expired_files:
        for msg_id in expired_files:
            del uploaded_files[msg_id]
        
        save_uploaded_files_db(uploaded_files)
        print(f"✅ Cleaned up {len(expired_files)} expired files from database")
    else:
        print("✅ No expired files found")
    
    return len(expired_files)

# Background task for periodic cleanup
async def periodic_cleanup():
    """Run cleanup every 6 hours"""
    while True:
        try:
            await asyncio.sleep(6 * 60 * 60)  # 6 hours
            await cleanup_expired_files()
        except Exception as e:
            print(f"❌ Error in periodic cleanup: {e}")

# File management endpoints
@app.post("/cleanup/expired")
async def manual_cleanup():
    """Manually trigger cleanup of expired files"""
    try:
        expired_files = await cleanup_expired_files()
        return {
            "status": "success",
            "expired_files_count": expired_files
        }
    except Exception as e:
        raise HTTPException(500, f"Cleanup failed: {str(e)}")

@app.get("/download/progress/{msg_id}")
async def get_download_progress(msg_id: str):
    """Get real download progress from temp directory"""
    try:
        # Check if download is in progress
        if int(msg_id) not in download_tasks:
            return {
                "status": "not_downloading",
                "msg_id": msg_id,
                "downloading": False
            }
        
        # Look for temp file in temp directory
        temp_files = []
        
        # Check custom temp directory if specified
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            temp_dir = CUSTOM_TEMP_DIR
        else:
            temp_dir = tempfile.gettempdir()
        
        # Look for files with our message ID pattern
        for filename in os.listdir(temp_dir):
            if f"temp_{msg_id}" in filename or str(msg_id) in filename:
                temp_file_path = os.path.join(temp_dir, filename)
                if os.path.isfile(temp_file_path):
                    temp_files.append({
                        "filename": filename,
                        "path": temp_file_path,
                        "size": os.path.getsize(temp_file_path),
                        "modified": os.path.getmtime(temp_file_path)
                    })
        
        if not temp_files:
            return {
                "status": "downloading",
                "msg_id": msg_id,
                "downloading": True,
                "temp_file_found": False,
                "downloaded_size": 0,
                "total_size": 0,
                "percentage": 0
            }
        
        # Get the most recent temp file (in case of multiple matches)
        latest_temp = max(temp_files, key=lambda x: x["modified"])
        downloaded_size = latest_temp["size"]
        
        # Try to get total size from Telegram message
        total_size = 0
        try:
            if client and client.is_connected():
                channel, msg_id_int = parse_telegram_url(f"https://t.me/c/1/{msg_id}")  # Dummy URL to get msg_id
                if msg_id_int:
                    # We need to find the actual channel - this is a limitation
                    # For now, we'll estimate or use a default
                    pass
        except:
            pass
        
        # If we can't get total size, estimate based on typical file sizes
        if total_size == 0:
            # Estimate based on downloaded size and time elapsed
            if downloaded_size > 50 * 1024 * 1024:  # If > 50MB downloaded
                total_size = downloaded_size * 2  # Conservative estimate
            else:
                total_size = 150 * 1024 * 1024  # Default 150MB estimate
        
        percentage = (downloaded_size / total_size * 100) if total_size > 0 else 0
        percentage = min(percentage, 95)  # Cap at 95% until confirmed complete
        
        return {
            "status": "downloading",
            "msg_id": msg_id,
            "downloading": True,
            "temp_file_found": True,
            "temp_filename": latest_temp["filename"],
            "downloaded_size": downloaded_size,
            "total_size": total_size,
            "percentage": percentage,
            "download_speed_estimate": "calculating..."
        }
        
    except Exception as e:
        print(f"❌ Error getting download progress for {msg_id}: {e}")
        return {
            "status": "error",
            "msg_id": msg_id,
            "error": str(e),
            "downloading": False
        }

@app.get("/download/file_info/{msg_id}")
async def get_file_info(msg_id: str):
    """Get file information from Telegram message"""
    try:
        if not client or not client.is_connected():
            await client.connect()
        
        # Load video data to find the channel for this message
        with open("video.json", encoding="utf-8") as f:
            data = json.load(f)
        
        # Find the episode with this msg_id
        episode_info = None
        for series_name, series_data in data.items():
            for season_name, episodes in series_data.items():
                for episode in episodes:
                    channel, ep_msg_id = parse_telegram_url(episode["url"])
                    if ep_msg_id == int(msg_id):
                        episode_info = {
                            "channel": channel,
                            "msg_id": ep_msg_id,
                            "title": episode["title"],
                            "url": episode["url"],
                            "series": series_name,
                            "season": season_name
                        }
                        break
                if episode_info:
                    break
            if episode_info:
                break
        
        if not episode_info:
            return {
                "status": "not_found",
                "msg_id": msg_id,
                "note": "Episode not found in catalog"
            }
        
        # Get message from Telegram
        message = await client.get_messages(episode_info["channel"], ids=episode_info["msg_id"])
        if not message:
            return {
                "status": "no_message",
                "msg_id": msg_id,
                "note": "Message not found"
            }
        
        # Check if it's a video or document
        if message.video:
            # It's a video message
            file_size = message.video.size
            duration = getattr(message.video, 'duration', None)
            filename = None
            
            # Try to get original filename
            if message.video.attributes:
                for attr in message.video.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
        elif message.document:
            # It's a document (video file sent as document)
            file_size = message.document.size
            duration = None  # Documents don't have duration attribute
            filename = None
            
            # Try to get original filename from document attributes
            if message.document.attributes:
                for attr in message.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
                    # Check for video attributes in document
                    if hasattr(attr, 'duration'):
                        duration = attr.duration
        else:
            return {
                "status": "no_video",
                "msg_id": msg_id,
                "note": "No video or document found in message"
            }
        
        if not filename:
            filename = f"{msg_id}.mkv"
        
        return {
            "status": "found",
            "msg_id": msg_id,
            "file_size": file_size,
            "file_size_mb": file_size / (1024 * 1024),
            "duration": duration,
            "filename": filename,
            "title": episode_info["title"],
            "series": episode_info["series"],
            "season": episode_info["season"]
        }
        
    except Exception as e:
        print(f"❌ Error getting file info for {msg_id}: {e}")
        return {
            "status": "error",
            "msg_id": msg_id,
            "error": str(e)
        }

@app.get("/download/real_progress/{msg_id}")
async def get_real_download_progress(msg_id: str):
    """Get accurate download progress by checking temp file size vs actual file size"""
    try:
        # First get the actual file size from Telegram
        file_info_response = await get_file_info(msg_id)
        
        if file_info_response["status"] != "found":
            return {
                "status": "file_info_unavailable",
                "msg_id": msg_id,
                "error": "Cannot get file size from Telegram"
            }
        
        actual_total_size = file_info_response["file_size"]
        filename = file_info_response["filename"]
        
        # Check if download is in progress - try both string and int versions
        msg_id_int = int(msg_id)
        download_in_progress = (msg_id_int in download_tasks) or (str(msg_id) in download_tasks)
        
        print(f"🔍 Checking download progress for msg_id: {msg_id}")
        print(f"📋 Download tasks keys: {list(download_tasks.keys())}")
        print(f"✅ Download in progress: {download_in_progress}")
        
        if not download_in_progress:
            return {
                "status": "not_downloading",
                "msg_id": msg_id,
                "downloading": False,
                "total_size": actual_total_size,
                "filename": filename
            }
        
        # Look for temp file in temp directory
        temp_files = []
        
        # Check custom temp directory if specified
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            temp_dir = CUSTOM_TEMP_DIR
        else:
            temp_dir = tempfile.gettempdir()
        
        print(f"📁 Checking temp directory: {temp_dir}")
        
        # Look for files with our message ID pattern
        try:
            for temp_filename in os.listdir(temp_dir):
                if f"temp_{msg_id}" in temp_filename or str(msg_id) in temp_filename:
                    temp_file_path = os.path.join(temp_dir, temp_filename)
                    if os.path.isfile(temp_file_path):
                        file_size = os.path.getsize(temp_file_path)
                        temp_files.append({
                            "filename": temp_filename,
                            "path": temp_file_path,
                            "size": file_size,
                            "modified": os.path.getmtime(temp_file_path)
                        })
                        print(f"📄 Found temp file: {temp_filename} ({file_size / (1024*1024):.1f} MB)")
        except Exception as e:
            print(f"❌ Error listing temp directory: {e}")
        
        if not temp_files:
            print(f"⚠️ No temp files found for msg_id: {msg_id}")
            return {
                "status": "downloading",
                "msg_id": msg_id,
                "downloading": True,
                "temp_file_found": False,
                "downloaded_size": 0,
                "total_size": actual_total_size,
                "percentage": 0,
                "filename": filename,
                "temp_directory": temp_dir
            }
        
        # Get the most recent temp file
        latest_temp = max(temp_files, key=lambda x: x["modified"])
        downloaded_size = latest_temp["size"]
        
        # Calculate accurate percentage
        percentage = (downloaded_size / actual_total_size * 100) if actual_total_size > 0 else 0
        percentage = min(percentage, 99.9)  # Cap at 99.9% until confirmed complete
        
        print(f"📊 Progress: {downloaded_size / (1024*1024):.1f}MB / {actual_total_size / (1024*1024):.1f}MB ({percentage:.1f}%)")
        
        return {
            "status": "downloading",
            "msg_id": msg_id,
            "downloading": True,
            "temp_file_found": True,
            "temp_filename": latest_temp["filename"],
            "downloaded_size": downloaded_size,
            "total_size": actual_total_size,
            "percentage": percentage,
            "filename": filename,
            "temp_directory": temp_dir
        }
        
    except Exception as e:
        print(f"❌ Error getting real download progress for {msg_id}: {e}")
        import traceback
        print(f"🔍 Full traceback: {traceback.format_exc()}")
        return {
            "status": "error",
            "msg_id": msg_id,
            "error": str(e),
            "downloading": False
        }

@app.get("/temp/files")
async def list_temp_files():
    """List all temp files for debugging"""
    try:
        temp_files = []
        
        # Check custom temp directory if specified
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            temp_dir = CUSTOM_TEMP_DIR
        else:
            temp_dir = tempfile.gettempdir()
        
        for filename in os.listdir(temp_dir):
            if "temp_" in filename or filename.endswith(('.mkv', '.mp4', '.avi')):
                temp_file_path = os.path.join(temp_dir, filename)
                if os.path.isfile(temp_file_path):
                    file_size = os.path.getsize(temp_file_path)
                    temp_files.append({
                        "filename": filename,
                        "size_mb": file_size / (1024 * 1024),
                        "size_bytes": file_size,
                        "modified": time.ctime(os.path.getmtime(temp_file_path))
                    })
        
        return {
            "temp_directory": temp_dir,
            "temp_files": temp_files,
            "total_files": len(temp_files),
            "total_size_mb": sum(f["size_mb"] for f in temp_files)
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "temp_directory": temp_dir if 'temp_dir' in locals() else "unknown"
        }

@app.get("/files/status")
async def files_status():
    """Get overview of file storage status"""
    uploaded_files = load_uploaded_files_db()
    
    # Quick status without validation (faster)
    return {
        "total_files_in_database": len(uploaded_files),
        "database_file": UPLOADED_FILES_DB,
        "database_exists": os.path.exists(UPLOADED_FILES_DB),
        "periodic_cleanup_running": cleanup_task is not None and not cleanup_task.done(),
        "max_access_count": MAX_ACCESS_COUNT,
        "provider": "PixelDrain"
    }

# Health check endpoint for deployment
@app.get("/health")
async def health_check():
    """Health check endpoint for deployment platforms"""
    telegram_status = "connected" if client and client.is_connected() else "disconnected"
    
    return {
        "status": "healthy",
        "service": "Smart TV Streaming Server",
        "telegram_status": telegram_status,
        "pixeldrain_configured": bool(PIXELDRAIN_API_KEY),
        "telegram_configured": bool(API_ID and API_HASH),
        "session_type": "StringSession" if TELEGRAM_SESSION_STRING else "FileSession",
        "max_access_count": MAX_ACCESS_COUNT
    }

# Add info endpoint
@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Smart TV Streaming Server",
        "status": "running",
        "endpoints": {
            "catalog": "/catalog/series",
            "download": "/download?url=TELEGRAM_URL",
            "stream_direct": "/stream_direct/{msg_id}",
            "stream_local": "/stream_local/{msg_id}",
            "hls": "/hls/{msg_id}/playlist.m3u8",
            "health": "/health"
        }
    }

# Add info endpoint
@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Smart TV Streaming Server",
        "status": "running",
        "endpoints": {
            "catalog": "/catalog/series",
            "download": "/download?url=TELEGRAM_URL",
            "stream_local": "/stream_local/{msg_id}",
            "health": "/health"
        }
    }

# Storage info endpoint
@app.get("/storage/info")
async def get_storage_info():
    """Get PixelDrain storage information"""
    try:
        uploaded_files = load_uploaded_files_db()
        
        # Calculate total access counts
        total_accesses = sum(file_info.get("access_count", 0) for file_info in uploaded_files.values())
        expired_files = sum(1 for file_info in uploaded_files.values() if file_info.get("access_count", 0) >= MAX_ACCESS_COUNT)
        
        return {
            "provider": "PixelDrain",
            "storage": {
                "total_files": len(uploaded_files),
                "active_files": len(uploaded_files) - expired_files,
                "expired_files": expired_files,
                "total_accesses": total_accesses
            },
            "files_count": len(uploaded_files),
            "max_access_count": MAX_ACCESS_COUNT
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Add exception handler for better error handling  
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    
    # Special handling for streaming-related assertion errors
    if isinstance(exc, AssertionError) and "Data should not be empty" in str(exc):
        print("🔥 Streaming AssertionError caught (likely client disconnect): Data should not be empty")
        # Don't log full traceback for this common streaming error
        return JSONResponse(status_code=500, content={"detail": "Streaming interrupted"})
    
    print("🔥 Uncaught Exception:", traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": str(exc)})

@app.get("/get_stream_url/{msg_id}")
async def get_stream_url(msg_id: str):
    """Get direct PixelDrain URL for AVPlay streaming"""
    print(f"🔍 Stream URL requested for msg_id: {msg_id}")
    
    # Get file URL from database
    uploaded_files = load_uploaded_files_db()
    print(f"📋 Database contains {len(uploaded_files)} files")
    print(f"📋 Available msg_ids: {list(uploaded_files.keys())}")
    
    if str(msg_id) not in uploaded_files:
        print(f"❌ msg_id {msg_id} not found in database")
        print(f"🔍 Checking if msg_id exists with different type...")
        
        # Check if exists as int
        if int(msg_id) in uploaded_files:
            print(f"✅ Found as integer key: {int(msg_id)}")
            file_info = uploaded_files[int(msg_id)]
        else:
            print(f"❌ msg_id {msg_id} not found as string or integer")
            raise HTTPException(404, f"File not uploaded yet. Available files: {len(uploaded_files)}")
    else:
        file_info = uploaded_files[str(msg_id)]
    
    filename = file_info.get("filename", f"{msg_id}.mkv")
    pixeldrain_id = file_info["pixeldrain_id"]
    
    print(f"📁 Found file: {filename}, PixelDrain ID: {pixeldrain_id}")
    
    # Verify file still exists on PixelDrain
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        print(f"❌ File {pixeldrain_id} not found on PixelDrain, removed from database")
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    # Check access count
    access_count = file_info.get("access_count", 0)
    if access_count >= MAX_ACCESS_COUNT:
        # File exceeded access limit
        await delete_from_pixeldrain(pixeldrain_id)
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        print(f"❌ File {pixeldrain_id} exceeded access limit, deleted")
        raise HTTPException(410, f"File access limit exceeded ({MAX_ACCESS_COUNT} times). File has been deleted.")
    
    # Increment access count
    file_info["access_count"] = access_count + 1
    uploaded_files[str(msg_id)] = file_info
    save_uploaded_files_db(uploaded_files)
    
    # Get PixelDrain direct URL
    pixeldrain_url = get_pixeldrain_download_url(pixeldrain_id)
    
    print(f"📺 Providing direct URL for AVPlay: {filename} (access {access_count + 1}/{MAX_ACCESS_COUNT})")
    print(f"🔗 PixelDrain URL: {pixeldrain_url}")
    
    return {
        "success": True,
        "stream_url": pixeldrain_url,
        "filename": filename,
        "access_count": access_count + 1,
        "remaining_access": MAX_ACCESS_COUNT - (access_count + 1)
    }

@app.get("/performance/test")
async def test_download_performance():
    """Test download performance and suggest optimal chunk sizes"""
    import time
    import tempfile
    import os
    
    try:
        # Create a test message or use a small existing file
        test_results = {
            "server_location": "Render (likely US)",
            "test_timestamp": int(time.time()),
            "current_config": {
                "adaptive_enabled": CHUNK_SIZE_ADAPTIVE,
                "min_chunk_mb": CHUNK_SIZE_MIN / (1024 * 1024),
                "default_chunk_mb": CHUNK_SIZE_DEFAULT / (1024 * 1024),
                "max_chunk_mb": CHUNK_SIZE_MAX / (1024 * 1024)
            }
        }
        
        # Test network speed with a simple download test
        print("🧪 Running server performance test...")
        
        # Test memory allocation speed (proxy for server performance)
        start_time = time.time()
        test_data = bytearray(10 * 1024 * 1024)  # 10MB test
        memory_time = time.time() - start_time
        
        # Test disk I/O speed
        start_time = time.time()
        temp_path = os.path.join(tempfile.gettempdir(), "speed_test.tmp")
        with open(temp_path, 'wb') as f:
            f.write(test_data)
        f.flush()
        os.fsync(f.fileno()) if hasattr(os, 'fsync') else None
        disk_write_time = time.time() - start_time
        
        # Test disk read speed
        start_time = time.time()
        with open(temp_path, 'rb') as f:
            read_data = f.read()
        disk_read_time = time.time() - start_time
        
        # Clean up
        try:
            os.unlink(temp_path)
        except:
            pass
        
        # Calculate speeds
        memory_speed = (10 / memory_time) if memory_time > 0 else 0
        disk_write_speed = (10 / disk_write_time) if disk_write_time > 0 else 0
        disk_read_speed = (10 / disk_read_time) if disk_read_time > 0 else 0
        
        test_results.update({
            "performance_metrics": {
                "memory_allocation_mbps": round(memory_speed, 2),
                "disk_write_mbps": round(disk_write_speed, 2),
                "disk_read_mbps": round(disk_read_speed, 2)
            }
        })
        
        # Suggest optimal settings based on performance
        if disk_write_speed > 100:  # Very fast server
            suggested_max = 32 * 1024 * 1024  # 32MB
            suggested_default = 8 * 1024 * 1024  # 8MB
        elif disk_write_speed > 50:  # Fast server
            suggested_max = 16 * 1024 * 1024  # 16MB (current)
            suggested_default = 4 * 1024 * 1024  # 4MB
        elif disk_write_speed > 20:  # Medium server
            suggested_max = 8 * 1024 * 1024   # 8MB
            suggested_default = 2 * 1024 * 1024  # 2MB (current)
        else:  # Slower server
            suggested_max = 4 * 1024 * 1024   # 4MB
            suggested_default = 1 * 1024 * 1024  # 1MB
        
        test_results.update({
            "recommendations": {
                "suggested_max_chunk_mb": suggested_max / (1024 * 1024),
                "suggested_default_chunk_mb": suggested_default / (1024 * 1024),
                "performance_tier": (
                    "Ultra High" if disk_write_speed > 100 else
                    "High" if disk_write_speed > 50 else
                    "Medium" if disk_write_speed > 20 else
                    "Standard"
                )
            }
        })
        
        return test_results
        
    except Exception as e:
        return {
            "error": str(e),
            "current_config": {
                "adaptive_enabled": CHUNK_SIZE_ADAPTIVE,
                "min_chunk_mb": CHUNK_SIZE_MIN / (1024 * 1024),
                "default_chunk_mb": CHUNK_SIZE_DEFAULT / (1024 * 1024),
                "max_chunk_mb": CHUNK_SIZE_MAX / (1024 * 1024)
            }
        }

@app.post("/performance/configure")
async def configure_chunk_sizes(max_chunk_mb: float = None, default_chunk_mb: float = None, adaptive: bool = None):
    """Dynamically configure chunk sizes based on server performance"""
    global CHUNK_SIZE_MAX, CHUNK_SIZE_DEFAULT, CHUNK_SIZE_ADAPTIVE
    
    changes = {}
    
    if max_chunk_mb is not None and 1 <= max_chunk_mb <= 64:  # Limit between 1MB and 64MB
        old_max = CHUNK_SIZE_MAX / (1024 * 1024)
        CHUNK_SIZE_MAX = int(max_chunk_mb * 1024 * 1024)
        changes["max_chunk_mb"] = {"old": old_max, "new": max_chunk_mb}
        print(f"🔧 Updated max chunk size: {old_max:.1f}MB → {max_chunk_mb:.1f}MB")
    
    if default_chunk_mb is not None and 0.5 <= default_chunk_mb <= 32:  # Limit between 512KB and 32MB
        old_default = CHUNK_SIZE_DEFAULT / (1024 * 1024)
        CHUNK_SIZE_DEFAULT = int(default_chunk_mb * 1024 * 1024)
        changes["default_chunk_mb"] = {"old": old_default, "new": default_chunk_mb}
        print(f"🔧 Updated default chunk size: {old_default:.1f}MB → {default_chunk_mb:.1f}MB")
    
    if adaptive is not None:
        old_adaptive = CHUNK_SIZE_ADAPTIVE
        CHUNK_SIZE_ADAPTIVE = adaptive
        changes["adaptive_enabled"] = {"old": old_adaptive, "new": adaptive}
        print(f"🔧 Updated adaptive chunking: {old_adaptive} → {adaptive}")
    
    return {
        "status": "updated",
        "changes": changes,
        "current_config": {
            "min_chunk_mb": CHUNK_SIZE_MIN / (1024 * 1024),
            "default_chunk_mb": CHUNK_SIZE_DEFAULT / (1024 * 1024),
            "max_chunk_mb": CHUNK_SIZE_MAX / (1024 * 1024),
            "adaptive_enabled": CHUNK_SIZE_ADAPTIVE
        },
        "note": "Changes will apply to new downloads immediately"
    }

# Debug endpoint to check database contents
@app.get("/debug/database")
async def debug_database():
    """Debug endpoint to check uploaded files database"""
    uploaded_files = load_uploaded_files_db()
    
    debug_info = {
        "database_file": UPLOADED_FILES_DB,
        "database_exists": os.path.exists(UPLOADED_FILES_DB),
        "total_files": len(uploaded_files),
        "files": {}
    }
    
    for msg_id, file_info in uploaded_files.items():
        debug_info["files"][str(msg_id)] = {
            "filename": file_info.get("filename", "unknown"),
            "pixeldrain_id": file_info.get("pixeldrain_id", "unknown"),
            "access_count": file_info.get("access_count", 0),
            "uploaded_at": file_info.get("uploaded_at", "unknown"),
            "file_size": file_info.get("file_size", "unknown")
        }
    
    return debug_info

@app.get("/debug/temp_progress/{msg_id}")
async def debug_temp_progress(msg_id: str):
    """Debug temp file progress without checking download tasks"""
    try:
        # Get file info for total size
        file_info_response = await get_file_info(msg_id)
        actual_total_size = 0
        if file_info_response["status"] == "found":
            actual_total_size = file_info_response["file_size"]
        
        # Check temp directory
        temp_files = []
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            temp_dir = CUSTOM_TEMP_DIR
        else:
            temp_dir = tempfile.gettempdir()
        
        print(f"🔍 [DEBUG] Checking temp directory: {temp_dir}")
        print(f"🔍 [DEBUG] Looking for patterns: temp_{msg_id} or {msg_id}")
        
        try:
            all_files = os.listdir(temp_dir)
            print(f"🔍 [DEBUG] Total files in temp dir: {len(all_files)}")
            
            for temp_filename in all_files:
                if f"temp_{msg_id}" in temp_filename or str(msg_id) in temp_filename:
                    temp_file_path = os.path.join(temp_dir, temp_filename)
                    if os.path.isfile(temp_file_path):
                        file_size = os.path.getsize(temp_file_path)
                        temp_files.append({
                            "filename": temp_filename,
                            "path": temp_file_path,
                            "size": file_size,
                            "size_mb": file_size / (1024 * 1024),
                            "modified": os.path.getmtime(temp_file_path),
                            "modified_readable": time.ctime(os.path.getmtime(temp_file_path))
                        })
                        print(f"🔍 [DEBUG] Found matching file: {temp_filename} ({file_size / (1024*1024):.1f} MB)")
        except Exception as e:
            print(f"❌ [DEBUG] Error listing temp directory: {e}")
        
        # Calculate progress if we have files
        progress_info = {
            "msg_id": msg_id,
            "temp_directory": temp_dir,
            "total_size": actual_total_size,
            "total_size_mb": actual_total_size / (1024 * 1024) if actual_total_size > 0 else 0,
            "temp_files_found": len(temp_files),
            "temp_files": temp_files
        }
        
        if temp_files:
            latest_temp = max(temp_files, key=lambda x: x["modified"])
            downloaded_size = latest_temp["size"]
            percentage = (downloaded_size / actual_total_size * 100) if actual_total_size > 0 else 0
            
            progress_info.update({
                "latest_temp_file": latest_temp["filename"],
                "downloaded_size": downloaded_size,
                "downloaded_size_mb": downloaded_size / (1024 * 1024),
                "percentage": percentage
            })
            
            print(f"📊 [DEBUG] Progress: {downloaded_size / (1024*1024):.1f}MB / {actual_total_size / (1024*1024):.1f}MB ({percentage:.1f}%)")
        
        return progress_info
        
    except Exception as e:
        print(f"❌ [DEBUG] Error in debug_temp_progress: {e}")
        import traceback
        print(f"🔍 [DEBUG] Full traceback: {traceback.format_exc()}")
        return {
            "error": str(e),
            "msg_id": msg_id
        }

# Debug endpoint to check specific episode
@app.get("/debug/episode/{msg_id}")
async def debug_episode(msg_id: str):
    """Debug specific episode"""
    uploaded_files = load_uploaded_files_db()
    
    # Check download tasks
    msg_id_int = int(msg_id)
    download_in_progress = (msg_id_int in download_tasks) or (str(msg_id) in download_tasks)
    
    # Check temp files
    temp_files = []
    if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
        temp_dir = CUSTOM_TEMP_DIR
    else:
        temp_dir = tempfile.gettempdir()
    
    try:
        for temp_filename in os.listdir(temp_dir):
            if f"temp_{msg_id}" in temp_filename or str(msg_id) in temp_filename:
                temp_file_path = os.path.join(temp_dir, temp_filename)
                if os.path.isfile(temp_file_path):
                    file_size = os.path.getsize(temp_file_path)
                    temp_files.append({
                        "filename": temp_filename,
                        "size_mb": file_size / (1024 * 1024),
                        "size_bytes": file_size,
                        "modified": time.ctime(os.path.getmtime(temp_file_path))
                    })
    except Exception as e:
        temp_files = [{"error": str(e)}]
    
    return {
        "msg_id": msg_id,
        "exists_as_string": str(msg_id) in uploaded_files,
        "exists_as_int": int(msg_id) in uploaded_files,
        "database_keys": list(uploaded_files.keys()),
        "database_size": len(uploaded_files),
        "file_info": uploaded_files.get(str(msg_id), uploaded_files.get(int(msg_id), "Not found")),
        "download_tasks": {
            "keys": list(download_tasks.keys()),
            "msg_id_int_in_tasks": msg_id_int in download_tasks,
            "msg_id_str_in_tasks": str(msg_id) in download_tasks,
            "download_in_progress": download_in_progress
        },
        "temp_directory": temp_dir,
        "temp_files": temp_files
    }

# Season download endpoints
@app.post("/download/season")
async def download_season(series_name: str, season_name: str):
    """Download all episodes from a season"""
    try:
        # Load episode data
        with open("video.json", encoding="utf-8") as f:
            data = json.load(f)
        
        if series_name not in data or season_name not in data[series_name]:
            raise HTTPException(status_code=404, detail="Season not found")
        
        episodes = data[series_name][season_name]
        uploaded_files = load_uploaded_files_db()
        
        # Filter episodes that need downloading
        episodes_to_download = []
        for ep in episodes:
            _, msg_id = parse_telegram_url(ep["url"])
            ep["msg_id"] = msg_id
            
            # Skip if already uploaded and still exists
            if str(msg_id) in uploaded_files:
                file_info = uploaded_files[str(msg_id)]
                access_count = file_info.get("access_count", 0)
                if access_count < MAX_ACCESS_COUNT:
                    # Check if file still exists on PixelDrain
                    if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                        continue  # Skip this episode
            
            episodes_to_download.append(ep)
        
        if not episodes_to_download:
            return {
                "status": "already_downloaded",
                "message": "All episodes are already downloaded",
                "total_episodes": len(episodes),
                "episodes_to_download": 0
            }
        
        # Create season download entry
        season_id = f"{series_name}_{season_name}".replace(" ", "_")
        season_download_queue[season_id] = {
            "series_name": series_name,
            "season_name": season_name,
            "episodes": episodes_to_download,
            "current_index": 0,
            "total_episodes": len(episodes_to_download),
            "downloaded_count": 0,
            "failed_count": 0,
            "status": "queued",
            "started_at": int(time.time()),
            "current_episode": None
        }
        
        # Start season download processor if not running
        global season_download_task
        if season_download_task is None or season_download_task.done():
            season_download_task = asyncio.create_task(process_season_downloads())
        
        print(f"📋 Queued season download: {season_name} ({len(episodes_to_download)} episodes)")
        
        return {
            "status": "queued",
            "season_id": season_id,
            "message": f"Queued {len(episodes_to_download)} episodes for download",
            "total_episodes": len(episodes),
            "episodes_to_download": len(episodes_to_download),
            "episodes_already_downloaded": len(episodes) - len(episodes_to_download)
        }
        
    except Exception as e:
        print(f"❌ Error queuing season download: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/season/status")
async def get_season_download_status():
    """Get status of all season downloads"""
    status_info = {}
    
    for season_id, info in season_download_queue.items():
        status_info[season_id] = {
            "series_name": info["series_name"],
            "season_name": info["season_name"],
            "total_episodes": info["total_episodes"],
            "downloaded_count": info["downloaded_count"],
            "failed_count": info["failed_count"],
            "status": info["status"],
            "progress_percentage": (info["downloaded_count"] / info["total_episodes"]) * 100 if info["total_episodes"] > 0 else 0,
            "current_episode": info.get("current_episode", {}).get("title", "None") if info.get("current_episode") else "None",
            "started_at": info["started_at"]
        }
    
    return {
        "season_downloads": status_info,
        "active_downloads": len([info for info in season_download_queue.values() if info["status"] in ["downloading", "queued"]]),
        "processor_running": season_download_task is not None and not season_download_task.done()
    }

@app.delete("/download/season/{season_id}")
async def cancel_season_download(season_id: str):
    """Cancel a season download"""
    if season_id in season_download_queue:
        season_info = season_download_queue[season_id]
        if season_info["status"] in ["queued", "downloading"]:
            season_info["status"] = "cancelled"
            print(f"🚫 Cancelled season download: {season_info['season_name']}")
            return {"status": "cancelled", "message": "Season download cancelled"}
        else:
            return {"status": "error", "message": "Season download cannot be cancelled"}
    else:
        raise HTTPException(status_code=404, detail="Season download not found")

async def process_season_downloads():
    """Background task to process season downloads one by one"""
    print("🎬 Season download processor started in background")
    
    while True:
        try:
            # Find next queued season
            next_season = None
            for season_id, info in season_download_queue.items():
                if info["status"] == "queued":
                    next_season = (season_id, info)
                    break
            
            if not next_season:
                # No more queued seasons, wait a bit and check again
                await asyncio.sleep(10)  # Check every 10 seconds for new queued seasons
                continue
            
            season_id, season_info = next_season
            season_info["status"] = "downloading"
            
            print(f"📥 🎬 BACKGROUND DOWNLOAD STARTED: '{season_info['season_name']}' from '{season_info['series_name']}' ({len(season_info['episodes'])} episodes)")
            
            # Process each episode
            for i in range(season_info["current_index"], len(season_info["episodes"])):
                if season_info["status"] == "cancelled":
                    print(f"🚫 Background season download cancelled: {season_info['season_name']}")
                    break
                
                episode = season_info["episodes"][i]
                season_info["current_index"] = i
                season_info["current_episode"] = episode
                
                print(f"📺 [{season_info['series_name']} - {season_info['season_name']}] Downloading episode {i + 1}/{len(season_info['episodes'])}: {episode['title']}")
                
                try:
                    # Check if episode is already downloaded
                    uploaded_files = load_uploaded_files_db()
                    if str(episode["msg_id"]) in uploaded_files:
                        file_info = uploaded_files[str(episode["msg_id"])]
                        if file_info.get("access_count", 0) < MAX_ACCESS_COUNT:
                            if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                                print(f"✅ [{season_info['series_name']} - {season_info['season_name']}] Episode already downloaded: {episode['title']}")
                                season_info["downloaded_count"] += 1
                                continue
                    
                    # Download episode
                    success = await download_single_episode(episode)
                    if success:
                        season_info["downloaded_count"] += 1
                        print(f"✅ [{season_info['series_name']} - {season_info['season_name']}] Episode downloaded successfully: {episode['title']} ({season_info['downloaded_count']}/{len(season_info['episodes'])})")
                    else:
                        season_info["failed_count"] += 1
                        print(f"❌ [{season_info['series_name']} - {season_info['season_name']}] Episode download failed: {episode['title']}")
                    
                    # Small delay between downloads to avoid overwhelming
                    await asyncio.sleep(3)
                    
                except Exception as e:
                    print(f"❌ [{season_info['series_name']} - {season_info['season_name']}] Error downloading episode {episode['title']}: {e}")
                    season_info["failed_count"] += 1
            
            # Mark season as completed
            if season_info["status"] != "cancelled":
                season_info["status"] = "completed"
                print(f"🎉 🎬 BACKGROUND DOWNLOAD COMPLETED: '{season_info['season_name']}' from '{season_info['series_name']}' - {season_info['downloaded_count']}/{season_info['total_episodes']} successful, {season_info['failed_count']} failed")
            
            season_info["current_episode"] = None
            
            # Small delay before processing next season
            await asyncio.sleep(5)
            
        except Exception as e:
            print(f"❌ Error in background season download processor: {e}")
            await asyncio.sleep(15)  # Wait longer before retrying on error

async def download_single_episode(episode):
    """Download a single episode for season download"""
    try:
        channel, msg_id = parse_telegram_url(episode["url"])
        if not channel or not msg_id:
            return False

        # Check if already uploaded
        uploaded_files = load_uploaded_files_db()
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                return True  # Already exists

        if not client.is_connected():
            await client.connect()

        message = await client.get_messages(channel, ids=msg_id)
        if not message or (not message.video and not message.document):
            return False

        # Download and upload
        temp_path = None
        try:
            # Get file extension from video or document
            original_filename = None
            
            if message.video:
                # It's a video message
                if message.video.attributes:
                    for attr in message.video.attributes:
                        if hasattr(attr, 'file_name') and attr.file_name:
                            original_filename = attr.file_name
                            break
            elif message.document:
                # It's a document (video file sent as document)
                if message.document.attributes:
                    for attr in message.document.attributes:
                        if hasattr(attr, 'file_name') and attr.file_name:
                            original_filename = attr.file_name
                            break
            
            file_ext = os.path.splitext(original_filename)[1] if original_filename else '.mkv'
            filename = f"{msg_id}{file_ext}"
            
            # Create temporary file
            if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
                os.makedirs(CUSTOM_TEMP_DIR, exist_ok=True)
                temp_path = os.path.join(CUSTOM_TEMP_DIR, f"temp_{msg_id}{file_ext}")
            else:
                # Use system temp directory with consistent naming
                temp_dir = tempfile.gettempdir()
                temp_path = os.path.join(temp_dir, f"temp_{msg_id}{file_ext}")
                # Ensure any existing file is removed first
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
            
            # Download
            if CHUNK_SIZE_ADAPTIVE:
                print(f"🚀 Season download using adaptive chunking: {filename}")
                await download_with_adaptive_chunking(client, message, temp_path)
            else:
                await client.download_media(message, file=temp_path)
            
            # Upload to PixelDrain
            pixeldrain_id = await upload_to_pixeldrain(temp_path, filename)
            
            # Save to database
            uploaded_files = load_uploaded_files_db()
            
            # Get file size from the appropriate source
            if message.video:
                file_size = message.video.size
            elif message.document:
                file_size = message.document.size
            else:
                file_size = os.path.getsize(temp_path)  # Fallback to actual file size
            
            uploaded_files[str(msg_id)] = {
                "pixeldrain_id": pixeldrain_id,
                "filename": filename,
                "uploaded_at": int(time.time()),
                "file_size": file_size,
                "access_count": 0
            }
            save_uploaded_files_db(uploaded_files)
            
            return True
            
        finally:
            # Clean up temp file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except:
                    pass
                    
    except Exception as e:
        print(f"❌ Error in download_single_episode: {e}")
        return False


if __name__ == "__main__":
    import uvicorn
    import asyncio
    
    # Run the FastAPI app
    uvicorn.run(app, host="0.0.0.0", port=8000)