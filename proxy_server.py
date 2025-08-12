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

# Import queue system
from download_scheduler import download_scheduler
from queue_manager import DownloadTask, DownloadPriority, queue_manager

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
MAX_ACCESS_COUNT = 3  # Maximum times a file can be accessed before expiring

# Database files for tracking episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

# Local cache configuration
CACHE_DIR = os.path.join(os.getcwd(), "cache")
DOWNLOADS_DIR = os.path.join(os.getcwd(), "downloads")  
TEMP_DIR = os.path.join(os.getcwd(), "tmp")

# Streaming configuration
STREAMING_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB buffer before starting stream
CHUNK_SIZE = 1024 * 1024  # 1MB chunks for downloading/streaming
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
    "Ninja Hattori Returns": "/static/ninja hattori returns.jpg",
    "default": "/static/default.png"
}

# Global variables
client = None
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
        
        # Initialize download scheduler
        await download_scheduler.start_scheduler(client)
        print("📋 Download scheduler initialized with 4-slot queue system (3 low priority + 1 high priority)")
        
        # Initialize PixelDrain configuration
        print("🎯 Initializing PixelDrain configuration...")
        if PIXELDRAIN_API_KEY:
            print("🔑 PixelDrain API key configured - better upload limits available")
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
    data = get_video_data(force_refresh=False)
    return [{"name": s, "poster": POSTERS.get(s, "/static/default.jpg")} for s in data.keys()]

@app.get("/catalog/series/{series_name}")
async def list_seasons(series_name: str):
    data = get_video_data()
    if series_name not in data:
        raise HTTPException(status_code=404, detail="Series not found")
    return list(data[series_name].keys())

@app.get("/catalog/series/{series_name}/{season_name}")
async def list_episodes(series_name: str, season_name: str):
    data = get_video_data()
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

async def upload_to_pixeldrain(file_path: str, filename: str) -> str:
    """Upload a file to PixelDrain with retry logic and return the file ID"""
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Upload attempt {attempt + 1}/{max_retries} to PixelDrain: {filename}")
            print(f"📊 File size: {os.path.getsize(file_path) / (1024*1024):.2f} MB")
            
            # Prepare upload data
            headers = {
                'User-Agent': 'Smart-TV-Streaming-Server/1.0'
            }
            if PIXELDRAIN_API_KEY:
                # PixelDrain requires API key as password with empty username in Basic Auth
                import base64
                auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
                headers['Authorization'] = f'Basic {auth_string}'
                print("🔑 Using API key for better upload limits")
            
            # Use aiohttp for better async handling and connection management
            timeout = aiohttp.ClientTimeout(total=900, connect=60)  # 15 min total, 60s connect
            
            # Create connector with SSL settings for better reliability
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=2,
                ttl_dns_cache=300,
                use_dns_cache=True,
                ssl=False  # Disable SSL verification for better compatibility
            )
            
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                with open(file_path, 'rb') as f:
                    # Create multipart form data
                    data = aiohttp.FormData()
                    data.add_field('file', f, filename=filename, content_type='application/octet-stream')
                    
                    print("📤 Uploading to PixelDrain...")
                    async with session.post(
                        PIXELDRAIN_UPLOAD_URL,
                        data=data,
                        headers=headers
                    ) as response:
                        
                        if response.status == 201:
                            try:
                                result = await response.json()
                                file_id = result.get('id')
                            except Exception as json_error:
                                # Handle case where response is plain text (common with PixelDrain)
                                result_text = await response.text()
                                print(f"📄 Got plain text response: {result_text[:100]}...")
                                
                                # PixelDrain sometimes returns just the file ID as plain text
                                if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
                                    file_id = result_text.strip()
                                    print(f"✅ Extracted file ID from text response: {file_id}")
                                else:
                                    # Try to extract ID from text response
                                    import re
                                    id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
                                    if id_match:
                                        file_id = id_match.group(1)
                                        print(f"✅ Extracted file ID from text: {file_id}")
                                    else:
                                        print(f"❌ Could not parse file ID from response: {result_text}")
                                        raise Exception(f"Could not parse PixelDrain response: {json_error}")
                            
                            if file_id:
                                print(f"✅ Upload successful! PixelDrain ID: {file_id}")
                                return file_id
                            else:
                                raise Exception("No file ID found in response")
                        else:
                            error_text = await response.text()
                            print(f"❌ Upload failed: {response.status} - {error_text}")
                            
                            # If it's a server error (5xx) or timeout, retry
                            if response.status >= 500 or response.status == 408:
                                if attempt < max_retries - 1:
                                    print(f"⏳ Retrying in {retry_delay} seconds...")
                                    await asyncio.sleep(retry_delay)
                                    retry_delay *= 2  # Exponential backoff
                                    continue
                            
                            # If authentication failed and we have an API key, try anonymous upload
                            if response.status == 401 and PIXELDRAIN_API_KEY:
                                print("⚠️ API key authentication failed, trying anonymous upload...")
                                if attempt < max_retries - 1:
                                    # Remove API key for next attempt
                                    headers.pop('Authorization', None)
                                    await asyncio.sleep(retry_delay)
                                    retry_delay *= 2
                                    continue
                            
                            raise Exception(f"PixelDrain upload failed: {response.status} - {error_text}")
                            
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
            print(f"❌ Network error on attempt {attempt + 1}: {e}")
            
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                raise Exception(f"Upload failed after {max_retries} attempts: {e}")
                
        except Exception as e:
            print(f"❌ Unexpected error on attempt {attempt + 1}: {e}")
            
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                raise e
    
    raise Exception(f"Upload failed after {max_retries} attempts")

# Add chunked upload support for large files
async def upload_to_pixeldrain_chunked(file_path: str, filename: str) -> str:
    """Upload large files to PixelDrain using the most reliable method"""
    file_size = os.path.getsize(file_path)
    
    # For files larger than 50MB, use our optimized strategy
    if file_size > 50 * 1024 * 1024:  # 50MB
        print(f"📦 Large file detected ({file_size / (1024*1024):.2f}MB), using optimized strategy...")
        
        # Skip problematic methods and go directly to basic requests (most reliable)
        try:
            print("🚀 Using direct basic upload for large files...")
            return await upload_to_pixeldrain_basic(file_path, filename)
        except Exception as basic_error:
            print(f"⚠️ Basic upload failed: {basic_error}")
            print("🔄 Trying fallback aiohttp method...")
            try:
                return await upload_to_pixeldrain_fallback(file_path, filename)
            except Exception as fallback_error:
                print(f"⚠️ Fallback upload also failed: {fallback_error}")
                print("🔄 Trying standard aiohttp as last resort...")
                return await upload_to_pixeldrain(file_path, filename)
    
    # Use standard upload for smaller files
    return await upload_to_pixeldrain(file_path, filename)

async def upload_to_pixeldrain_with_chunks(file_path: str, filename: str) -> str:
    """Upload file in smaller chunks to avoid connection timeouts"""
    chunk_size = 10 * 1024 * 1024  # 10MB chunks
    file_size = os.path.getsize(file_path)
    
    print(f"📤 Uploading {filename} in chunks ({chunk_size / (1024*1024):.0f}MB each)")
    
    # For now, use standard upload but with smaller timeout and better connection handling
    # PixelDrain doesn't support chunked uploads, so we'll optimize the connection instead
    
    max_retries = 5  # More retries for large files
    retry_delay = 3
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Large file upload attempt {attempt + 1}/{max_retries}: {filename}")
            
            # Use requests with connection pooling and retries
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            import urllib3
            import ssl
            
            # Disable SSL warnings for better compatibility
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            
            # Create session with retry strategy and custom SSL context
            session = requests.Session()
            
            # Create a custom SSL context with more lenient settings
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')  # Lower security level for compatibility
            
            retry_strategy = Retry(
                total=2,  # Reduced retries for faster failover
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
                backoff_factor=0.5,
                raise_on_status=False
            )
            
            adapter = HTTPAdapter(
                max_retries=retry_strategy
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            
            # Disable SSL verification completely
            session.verify = False
            
            # Set custom timeout values for large files
            timeout_config = (10, 300)  # 10s connect, 5min read
            
            headers = {
                'User-Agent': 'Smart-TV-Streaming-Server/1.0'
            }
            if PIXELDRAIN_API_KEY:
                # PixelDrain requires API key as password with empty username in Basic Auth
                import base64
                auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
                headers['Authorization'] = f'Basic {auth_string}'
            
            with open(file_path, 'rb') as f:
                files = {'file': (filename, f, 'application/octet-stream')}
                
                print("📤 Uploading large file to PixelDrain...")
                response = session.post(
                    PIXELDRAIN_UPLOAD_URL,
                    files=files,
                    headers=headers,
                    timeout=timeout_config,
                    stream=False  # Don't stream for better compatibility
                )
            
            if response.status_code == 201:
                try:
                    result = response.json()
                    file_id = result.get('id')
                except Exception as json_error:
                    # Handle case where response is plain text (common with PixelDrain)
                    result_text = response.text
                    print(f"📄 Got plain text response from large file upload: {result_text[:100]}...")
                    
                    # PixelDrain sometimes returns just the file ID as plain text
                    if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
                        file_id = result_text.strip()
                        print(f"✅ Extracted file ID from text response: {file_id}")
                    else:
                        # Try to extract ID from text response
                        import re
                        id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
                        if id_match:
                            file_id = id_match.group(1)
                            print(f"✅ Extracted file ID from text: {file_id}")
                        else:
                            print(f"❌ Could not parse file ID from response: {result_text}")
                            raise Exception(f"Could not parse PixelDrain response: {json_error}")
                
                if file_id:
                    print(f"✅ Large file upload successful! PixelDrain ID: {file_id}")
                    return file_id
                else:
                    raise Exception("No file ID found in response")
            elif response.status_code == 401 and PIXELDRAIN_API_KEY:
                print("⚠️ API key authentication failed, trying anonymous upload...")
                if attempt < max_retries - 1:
                    # Remove API key for next attempt
                    headers.pop('Authorization', None)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    print(f"❌ Large file upload failed: {response.status_code} - {response.text}")
                    raise Exception(f"PixelDrain upload failed: {response.status_code}")
            else:
                print(f"❌ Large file upload failed: {response.status_code} - {response.text}")
                raise Exception(f"PixelDrain upload failed: {response.status_code}")
                
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            print(f"❌ Connection error on attempt {attempt + 1}: {e}")
            
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5  # Gradual backoff
                continue
            else:
                raise Exception(f"Large file upload failed after {max_retries} attempts: {e}")
                
        except Exception as e:
            print(f"❌ Unexpected error uploading large file on attempt {attempt + 1}: {e}")
            
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
                continue
            else:
                raise e
    
    raise Exception(f"Large file upload failed after {max_retries} attempts")

async def upload_to_pixeldrain_fallback(file_path: str, filename: str) -> str:
    """Fallback upload method using aiohttp with modified settings"""
    print(f"🔄 Fallback upload using aiohttp: {filename}")
    
    try:
        headers = {'User-Agent': 'Smart-TV-Streaming-Server/1.0'}
        if PIXELDRAIN_API_KEY:
            import base64
            auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {auth_string}'
        
        # Use very lenient timeout and connector settings
        timeout = aiohttp.ClientTimeout(total=1800, connect=120)  # 30 min total, 2 min connect
        
        connector = aiohttp.TCPConnector(
            limit=1,
            limit_per_host=1,
            ttl_dns_cache=60,
            use_dns_cache=False,
            ssl=False  # Completely disable SSL verification
        )
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=filename, content_type='application/octet-stream')
                
                print("📤 Fallback upload in progress...")
                async with session.post(PIXELDRAIN_UPLOAD_URL, data=data, headers=headers) as response:
                    if response.status == 201:
                        try:
                            result = await response.json()
                            file_id = result.get('id')
                        except:
                            result_text = await response.text()
                            if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
                                file_id = result_text.strip()
                            else:
                                raise Exception("Could not parse response")
                        
                        print(f"✅ Fallback upload successful! PixelDrain ID: {file_id}")
                        return file_id
                    else:
                        error_text = await response.text()
                        raise Exception(f"Upload failed: {response.status} - {error_text}")
                        
    except Exception as e:
        print(f"❌ Fallback upload failed: {e}")
        raise e

async def upload_to_pixeldrain_basic(file_path: str, filename: str) -> str:
    """Basic upload method using requests with minimal configuration"""
    print(f"🔄 Basic upload method: {filename}")
    
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        
        headers = {'User-Agent': 'Smart-TV-Streaming-Server/1.0'}
        if PIXELDRAIN_API_KEY:
            import base64
            auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {auth_string}'
        
        with open(file_path, 'rb') as f:
            files = {'file': (filename, f, 'application/octet-stream')}
            
            print("📤 Basic upload in progress...")
            response = requests.post(
                PIXELDRAIN_UPLOAD_URL,
                files=files,
                headers=headers,
                verify=False,
                timeout=(30, 1800)  # 30s connect, 30min read
            )
        
        if response.status_code == 201:
            try:
                result = response.json()
                file_id = result.get('id')
            except:
                result_text = response.text
                if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
                    file_id = result_text.strip()
                else:
                    import re
                    id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
                    if id_match:
                        file_id = id_match.group(1)
                    else:
                        raise Exception("Could not parse response")
            
            print(f"✅ Basic upload successful! PixelDrain ID: {file_id}")
            return file_id
        else:
            raise Exception(f"Upload failed: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"❌ Basic upload failed: {e}")
        raise e

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

# Download and upload to PixelDrain using queue system
@app.get("/download")
async def trigger_download(url: str):
    """Download a single episode using the queue system (HIGH priority) - Legacy GET endpoint"""
    try:
        # Use the download scheduler for single episode downloads
        result = await download_scheduler.queue_single_episode_download(url, client)
        
        if result["status"] == "queued":
            return {
                "status": "queued",
                "message": f"Episode queued for download with HIGH priority",
                "msg_id": result["msg_id"],
                "priority": "HIGH",
                "queue_position": result.get("queue_position", 0)
            }
        elif result["status"] == "already_uploaded":
            return {
                "status": "already_uploaded",
                "pixeldrain_id": result["pixeldrain_id"],
                "msg_id": result["msg_id"],
                "message": "Episode is already uploaded and available"
            }
        elif result["status"] == "already_queued":
            return {
                "status": "already_queued", 
                "msg_id": result["msg_id"],
                "message": "Episode is already in the download queue"
            }
        else:
            return {
                "status": "error",
                "message": "Failed to queue episode for download"
            }
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"❌ Error queuing single episode download: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

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
    """Get real download progress from temp directory using queue system"""
    try:
        # Check if download is in progress using queue manager
        msg_id_int = int(msg_id)
        if not queue_manager.is_download_in_progress(msg_id_int):
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
        data = get_video_data()
        
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
        
        # Check if download is in progress using queue manager
        msg_id_int = int(msg_id)
        download_in_progress = queue_manager.is_download_in_progress(msg_id_int)
        
        print(f"🔍 Checking download progress for msg_id: {msg_id}")
        print(f"📋 Queue manager download status: {download_in_progress}")
        
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

# Debug endpoint to check database contents
@app.get("/debug/video_data")
async def debug_video_data():
    """Debug endpoint to check video data source and content"""
    try:
        data = get_video_data()
        
        return {
            "video_data_source": "gist" if (GITHUB_TOKEN and VIDEO_GIST_ID) else "local",
            "gist_configured": bool(GITHUB_TOKEN and VIDEO_GIST_ID),
            "github_token_set": bool(GITHUB_TOKEN and GITHUB_TOKEN != "ghp_your_token_here"),
            "video_gist_id_set": bool(VIDEO_GIST_ID and VIDEO_GIST_ID != "your_video_gist_id_here"),
            "total_series": len(data),
            "series_list": list(data.keys()),
            "cache_timestamp": video_cache_timestamp,
            "cache_age_seconds": time.time() - video_cache_timestamp if video_cache_timestamp > 0 else 0
        }
    except Exception as e:
        return {
            "error": str(e),
            "video_data_source": "error",
            "gist_configured": bool(GITHUB_TOKEN and VIDEO_GIST_ID)
        }
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
    
    # Check download status using queue manager
    msg_id_int = int(msg_id)
    download_in_progress = queue_manager.is_download_in_progress(msg_id_int)
    
    # Get queue status
    queue_status = download_scheduler.get_download_status()
    
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
        "queue_system": {
            "download_in_progress": download_in_progress,
            "queue_status": queue_status,
            "msg_id_being_downloaded": msg_id_int in [task.get("msg_id") for task in queue_status.get("active_downloads", [])]
        },
        "temp_directory": temp_dir,
        "temp_files": temp_files
    }

# Download endpoints
@app.post("/download")
async def download_episode(url: str):
    """Download a single episode using the queue system (HIGH priority)"""
    try:
        # Use the download scheduler for single episode downloads
        result = await download_scheduler.queue_single_episode_download(url, client)
        
        if result["status"] == "queued":
            return {
                "status": "queued",
                "message": f"Episode queued for download with HIGH priority",
                "msg_id": result["msg_id"],
                "priority": "HIGH",
                "queue_position": result.get("queue_position", 0)
            }
        elif result["status"] == "already_uploaded":
            return {
                "status": "already_uploaded",
                "message": "Episode is already uploaded and available",
                "pixeldrain_id": result["pixeldrain_id"],
                "msg_id": result["msg_id"]
            }
        elif result["status"] == "already_queued":
            return {
                "status": "already_queued", 
                "message": "Episode is already in the download queue",
                "msg_id": result["msg_id"]
            }
        else:
            return {
                "status": "error",
                "message": "Failed to queue episode for download"
            }
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"❌ Error queuing single episode download: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/download/status/{msg_id}")
async def get_download_status(msg_id: int):
    """Get status of a specific download by message ID"""
    try:
        # Check if it's in progress
        if queue_manager.is_download_in_progress(msg_id):
            return {
                "status": "downloading",
                "msg_id": msg_id,
                "message": "Episode is currently being downloaded"
            }
        
        # Check if it's already uploaded
        uploaded_files = load_uploaded_files_db()
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            return {
                "status": "completed",
                "msg_id": msg_id,
                "pixeldrain_id": file_info["pixeldrain_id"],
                "filename": file_info["filename"],
                "uploaded_at": file_info["uploaded_at"],
                "access_count": file_info.get("access_count", 0)
            }
        
        # Check if it's in queue
        download_status = download_scheduler.get_download_status()
        
        # Check high priority queue
        for task_info in download_status.get("high_priority_queue", []):
            if task_info.get("msg_id") == msg_id:
                return {
                    "status": "queued",
                    "msg_id": msg_id,
                    "priority": "HIGH",
                    "queue_position": task_info.get("position", 0)
                }
        
        # Check low priority queue
        for task_info in download_status.get("low_priority_queue", []):
            if task_info.get("msg_id") == msg_id:
                return {
                    "status": "queued", 
                    "msg_id": msg_id,
                    "priority": "LOW",
                    "queue_position": task_info.get("position", 0)
                }
        
        return {
            "status": "not_found",
            "msg_id": msg_id,
            "message": "Episode not found in queue or completed downloads"
        }
        
    except Exception as e:
        return {"error": str(e)}

# Season download endpoints
@app.post("/download/season")
async def download_season(series_name: str, season_name: str):
    """Download all episodes from a season"""
    try:
        # Load episode data from Gist
        data = get_video_data()
        
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
    """Background task to process season downloads using the 4-slot queue system"""
    print("🎬 Season download processor started in background with queue system")
    
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
            
            print(f"📥 🎬 QUEUE DOWNLOAD STARTED: '{season_info['season_name']}' from '{season_info['series_name']}' ({len(season_info['episodes'])} episodes)")
            print(f"🔄 Using 4-slot queue system: 3 low priority + 1 high priority slots")
            
            # Queue all episodes for download using the queue system
            queued_episodes = []
            for i, episode in enumerate(season_info["episodes"]):
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
                    
                    # Determine priority: First episode high priority, rest low priority
                    priority = DownloadPriority.HIGH if i == 0 else DownloadPriority.LOW
                    
                    # Prepare episode data for download scheduler
                    episode_data = {
                        "url": episode["url"],
                        "series_name": season_info['series_name'],
                        "season_name": season_info['season_name'],
                        "title": episode["title"]
                    }
                    
                    # Queue episode for download using the appropriate method
                    if priority == DownloadPriority.HIGH:
                        result = await download_scheduler.queue_single_episode_download(episode["url"], client)
                        success = result["status"] == "queued"
                    else:
                        success = await download_scheduler.queue_season_episode_download(episode_data, client)
                    
                    if success:
                        queued_episodes.append(episode)
                        priority_name = "HIGH" if priority == DownloadPriority.HIGH else "LOW"
                        print(f"📋 [{season_info['series_name']} - {season_info['season_name']}] Episode queued ({priority_name}): {episode['title']}")
                    else:
                        print(f"⚠️ [{season_info['series_name']} - {season_info['season_name']}] Failed to queue episode: {episode['title']}")
                        
                except Exception as e:
                    print(f"❌ [{season_info['series_name']} - {season_info['season_name']}] Error queuing episode {episode['title']}: {e}")
                    season_info["failed_count"] += 1
            
            if queued_episodes:
                print(f"📊 🎬 Season '{season_info['season_name']}': {len(queued_episodes)} episodes queued for download using queue system")
                
                # Wait for all episodes to complete
                total_episodes = len(queued_episodes)
                completed_episodes = 0
                
                while completed_episodes < total_episodes and season_info["status"] != "cancelled":
                    # Check queue status
                    queue_status = download_scheduler.get_download_status()
                    
                    # Count completed episodes for this season
                    season_completed = 0
                    for episode in queued_episodes:
                        if not queue_manager.is_download_in_progress(episode["msg_id"]):
                            # Check if it completed successfully
                            uploaded_files = load_uploaded_files_db()
                            if str(episode["msg_id"]) in uploaded_files:
                                season_completed += 1
                    
                    if season_completed > completed_episodes:
                        completed_episodes = season_completed
                        season_info["downloaded_count"] = season_info.get("downloaded_count", 0) + (completed_episodes - season_info.get("last_completed_count", 0))
                        season_info["last_completed_count"] = completed_episodes
                        print(f"📊 [{season_info['series_name']} - {season_info['season_name']}] Progress: {completed_episodes}/{total_episodes} episodes completed")
                    
                    # Check if all downloads are complete
                    if completed_episodes >= total_episodes:
                        break
                        
                    # Wait before checking again
                    await asyncio.sleep(5)
                
                # Calculate final stats
                uploaded_files = load_uploaded_files_db()
                successful_downloads = 0
                for episode in queued_episodes:
                    if str(episode["msg_id"]) in uploaded_files:
                        successful_downloads += 1
                
                season_info["downloaded_count"] = successful_downloads
                season_info["failed_count"] = total_episodes - successful_downloads
            
            # Mark season as completed
            if season_info["status"] != "cancelled":
                season_info["status"] = "completed"
                print(f"🎉 🎬 QUEUE DOWNLOAD COMPLETED: '{season_info['season_name']}' from '{season_info['series_name']}' - {season_info['downloaded_count']}/{season_info['total_episodes']} successful, {season_info['failed_count']} failed")
            
            season_info["current_episode"] = None
            
            # Small delay before processing next season
            await asyncio.sleep(5)
            
        except Exception as e:
            print(f"❌ Error in background season download processor: {e}")
            await asyncio.sleep(15)  # Wait longer before retrying on error

# Season download status endpoints
@app.get("/download/season/status")
async def get_season_download_status():
    """Get status of all season downloads"""
    try:
        return {
            "seasons": season_download_queue,
            "queue_status": download_scheduler.get_download_status()
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/download/queue/status")
async def get_download_queue_status():
    """Get current download queue status"""
    try:
        return download_scheduler.get_download_status()
    except Exception as e:
        return {"error": str(e)}

# GitHub Gist Integration (Phase 1: Add alongside existing JSON)
# This keeps the old system working while adding Gist support

import json
import requests
import time
import os
import hashlib

# Local JSON file path
uploaded_files_path = "uploaded_files.json"

# GitHub Gist configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")  
VIDEO_GIST_ID = os.getenv("VIDEO_GIST_ID")
GIST_FILENAME = "uploaded_files.json"
VIDEO_GIST_FILENAME = "video.json"


# Global variables to track Gist ETag and last modified
gist_etag = None
gist_last_modified = None

def load_video_data_from_gist(force_refresh=False):
    """Load video data from GitHub Gist with conditional request support"""
    global gist_etag, gist_last_modified
    
    try:
        headers = {}
        if gist_etag and not force_refresh:
            headers['If-None-Match'] = gist_etag
        if gist_last_modified and not force_refresh:
            headers['If-Modified-Since'] = gist_last_modified
            
        if GITHUB_TOKEN:
            headers['Authorization'] = f'token {GITHUB_TOKEN}'
            
        # Use conditional request to check if Gist was modified
        gist_url = f"https://api.github.com/gists/{VIDEO_GIST_ID}"
        response = requests.get(gist_url, headers=headers)
        
        # Handle 304 Not Modified
        if response.status_code == 304:
            print("📦 Gist not modified, using cached data")
            return video_data_cache
            
        # Handle successful response
        if response.status_code == 200:
            # Save ETag and Last-Modified headers for future requests
            gist_etag = response.headers.get('ETag')
            gist_last_modified = response.headers.get('Last-Modified')
            
            gist_data = response.json()
            if VIDEO_GIST_FILENAME in gist_data['files']:
                content = gist_data['files'][VIDEO_GIST_FILENAME]['content']
                parsed_data = json.loads(content)
                print(f"✅ Video data loaded from GitHub Gist: {len(parsed_data)} series")
                
                # Also save a local backup
                save_video_data_local(parsed_data)
                return parsed_data
        
        print(f"⚠️ Failed to load from Gist (status {response.status_code}), falling back to local file")
        return load_video_data_local()
            
    except Exception as e:
        print(f"❌ Error loading video data from Gist: {e}")
        return load_video_data_local()


# def load_video_data_from_gist():
#     """Load video data from GitHub Gist (primary with local fallback)"""
#     try:
#         if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_your_token_here":
#             print("⚠️ GitHub token not configured for video data, using local JSON only")
#             return load_video_data_local()
        
#         if not VIDEO_GIST_ID or VIDEO_GIST_ID == "your_video_gist_id_here":
#             print("⚠️ Video Gist ID not configured, using local JSON only")
#             return load_video_data_local()
        
#         print("📡 Loading video data from GitHub Gist...")
#         headers = {
#             'Authorization': f'token {GITHUB_TOKEN}',
#             'Accept': 'application/vnd.github.v3+json'
#         }
        
#         response = requests.get(f'https://api.github.com/gists/{VIDEO_GIST_ID}', headers=headers, timeout=10)
#         response.raise_for_status()
        
#         gist_data = response.json()
        
#         if VIDEO_GIST_FILENAME in gist_data['files']:
#             content = gist_data['files'][VIDEO_GIST_FILENAME]['content']
#             data = json.loads(content)
#             print(f"✅ Loaded video data from Gist: {len(data)} series found")
            
#             # Create local backup for fallback
#             try:
#                 save_video_data_local(data)
#                 print(f"💾 Created local backup of video data")
#             except Exception as backup_error:
#                 print(f"⚠️ Video data local backup error: {backup_error}")
            
#             return data
#         else:
#             print(f"⚠️ File {VIDEO_GIST_FILENAME} not found in video Gist")
#             return load_video_data_local()
            
#     except Exception as e:
#         print(f"❌ Error loading video data from Gist: {e}")
#         print("🔄 Falling back to local video.json file")
#         return load_video_data_local()

def load_video_data_local():
    """Load video data from local JSON file (fallback function)"""
    try:
        with open("video.json", encoding="utf-8") as f:
            data = json.load(f)
            print(f"📁 Loaded video data from local file: {len(data)} series found")
            return data
    except FileNotFoundError:
        print("❌ Local video.json not found")
        return {}
    except Exception as e:
        print(f"❌ Error loading local video.json: {e}")
        return {}

def save_video_data_local(data):
    """Save video data to local JSON file (backup function)"""
    try:
        with open("video.json", 'w', encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"💾 Saved video data to local JSON ({len(data)} series)")
    except Exception as e:
        print(f"❌ Error saving local video data: {e}")

# Video data cache
video_data_cache = None
video_cache_timestamp = 0
VIDEO_CACHE_DURATION = 300  # 5 minutes

def get_video_data(force_refresh=False):
    """Get video data with caching to avoid excessive API calls"""
    global video_data_cache, video_cache_timestamp
    
    current_time = time.time()
    
    # Return cached data if still fresh and not forcing refresh
    if not force_refresh and video_data_cache and (current_time - video_cache_timestamp) < VIDEO_CACHE_DURATION:
        return video_data_cache
    
    # Load fresh data
    video_data_cache = load_video_data_from_gist(force_refresh)
    video_cache_timestamp = current_time
    
    return video_data_cache

def save_uploaded_files_to_gist(data):
    """Save uploaded files database to GitHub Gist (primary with local backup)"""
    try:
        if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_your_token_here":
            print("⚠️ GitHub token not configured, saving to local JSON only")
            save_uploaded_files_db_local(data)
            return
        
        print("📡 Saving uploaded files to GitHub Gist...")
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        content = json.dumps(data, indent=2)
        
        update_data = {
            'files': {
                GIST_FILENAME: {
                    'content': content
                }
            }
        }
        
        response = requests.patch(f'https://api.github.com/gists/{GIST_ID}', 
                                headers=headers, 
                                json=update_data)
        response.raise_for_status()
        
        print(f"✅ Saved {len(data)} files to Gist")
        
        # Smart backup: Only update local if different
        try:
            local_data = load_uploaded_files_db_local()
            # Update if local is empty/corrupted or has different count
            if not local_data or len(local_data) != len(data):
                save_uploaded_files_db_local(data)
                print(f"💾 Updated local backup ({len(data)} episodes)")
            # If same size and not empty, assume it's current (saves disk I/O)
        except Exception as backup_error:
            # Always backup on error to be safe
            try:
                save_uploaded_files_db_local(data)
                print(f"💾 Local backup completed (failsafe)")
            except Exception as failsafe_error:
                print(f"❌ Local backup failed completely: {failsafe_error}")
        
    except Exception as e:
        print(f"❌ Error saving to Gist: {e}")
        print("💾 Saving to local JSON file (fallback)")
        save_uploaded_files_db_local(data)
        # Don't raise exception - local backup ensures data is safe

def load_uploaded_files_db_local():
    """Load from local JSON file (original function)"""
    try:
        with open(uploaded_files_path, 'r') as f:
            content = f.read().strip()
            if not content:
                print("📁 Local uploaded files database is empty, creating new one")
                return {}
            return json.loads(content)
    except FileNotFoundError:
        print("📁 No local uploaded files database found, creating new one")
        return {}
    except json.JSONDecodeError as e:
        print(f"❌ Local uploaded files database is corrupted: {e}")
        print("🔧 Creating backup and resetting local database")
        # Backup corrupted file
        try:
            import shutil
            backup_path = uploaded_files_path + ".corrupted.backup"
            shutil.copy2(uploaded_files_path, backup_path)
            print(f"💾 Corrupted file backed up to: {backup_path}")
        except Exception as backup_error:
            print(f"⚠️ Could not backup corrupted file: {backup_error}")
        return {}
    except Exception as e:
        print(f"❌ Unexpected error reading local uploaded files database: {e}")
        return {}

def save_uploaded_files_db_local(data):
    """Save to local JSON file (original function)"""
    try:
        with open(uploaded_files_path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"💾 Saved {len(data)} files to local JSON")
    except Exception as e:
        print(f"❌ Error saving local uploaded files database: {e}")

def check_and_fix_local_json():
    """Check and fix corrupted local JSON file"""
    try:
        # Check if file exists and is readable
        if not os.path.exists(uploaded_files_path):
            print("📁 No local JSON file exists yet")
            return True
        
        # Try to read and parse the file
        with open(uploaded_files_path, 'r') as f:
            content = f.read().strip()
            
        if not content:
            print("⚠️ Local JSON file is empty, will be recreated")
            return False
            
        # Try to parse JSON
        json.loads(content)
        print("✅ Local JSON file is valid")
        return True
        
    except json.JSONDecodeError as e:
        print(f"❌ Local JSON file is corrupted: {e}")
        # Create backup and reset
        try:
            import shutil
            timestamp = int(time.time())
            backup_path = f"{uploaded_files_path}.corrupted.{timestamp}.backup"
            shutil.copy2(uploaded_files_path, backup_path)
            print(f"💾 Corrupted file backed up to: {backup_path}")
            
            # Create empty valid JSON file
            with open(uploaded_files_path, 'w') as f:
                json.dump({}, f, indent=2)
            print("🔧 Created new empty JSON file")
            return False
            
        except Exception as fix_error:
            print(f"❌ Could not fix corrupted file: {fix_error}")
            return False
            
    except Exception as e:
        print(f"❌ Error checking local JSON file: {e}")
        return False

# Primary Gist functions (Phase 2: Full migration to Gist)
def load_uploaded_files_db():
    """Load uploaded files database (Gist primary with local fallback)"""
    return load_uploaded_files_from_gist()
def load_uploaded_files_from_gist():
    """Load uploaded files database from GitHub Gist (primary with local fallback)"""
    try:
        if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_your_token_here":
            print("⚠️ GitHub token not configured, using local JSON only")
            return load_uploaded_files_db_local()
        
        print("📡 Loading uploaded files from GitHub Gist...")
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        response = requests.get(f'https://api.github.com/gists/{GIST_ID}', headers=headers)
        response.raise_for_status()
        
        gist_data = response.json()
        
        if GIST_FILENAME in gist_data['files']:
            content = gist_data['files'][GIST_FILENAME]['content']
            data = json.loads(content)
            print(f"✅ Loaded {len(data)} files from Gist")
            
            # Smart backup: Only backup if local is outdated or missing
            try:
                local_data = load_uploaded_files_db_local()
                
                # Always update if local is empty/corrupted or has different count
                if not local_data or len(local_data) != len(data):
                    save_uploaded_files_db_local(data)
                    if not local_data:
                        print(f"💾 Created local backup ({len(data)} episodes)")
                    else:
                        print(f"💾 Updated local backup ({len(data)} episodes)")
                else:
                    print(f"✅ Local backup is current ({len(data)} episodes)")
            except Exception as backup_error:
                print(f"⚠️ Local backup error: {backup_error}")
                # Try to save anyway as a failsafe
                try:
                    save_uploaded_files_db_local(data)
                    print(f"💾 Failsafe backup completed ({len(data)} episodes)")
                except Exception as failsafe_error:
                    print(f"❌ Failsafe backup also failed: {failsafe_error}")
            
            return data
        else:
            print(f"⚠️ File {GIST_FILENAME} not found in Gist")
            # Try to load from local and migrate to Gist
            local_data = load_uploaded_files_db_local()
            if local_data:
                print("🔄 Migrating local data to Gist...")
                save_uploaded_files_to_gist(local_data)
                return local_data
            return {}
            
    except Exception as e:
        print(f"❌ Error loading from Gist: {e}")
        print("🔄 Falling back to local JSON file")
        return load_uploaded_files_db_local()

def save_uploaded_files_to_gist(data):
    """Save uploaded files database to GitHub Gist (primary with local backup)"""
    try:
        if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_your_token_here":
            print("⚠️ GitHub token not configured, saving to local JSON only")
            save_uploaded_files_db_local(data)
            return
        
        print("📡 Saving uploaded files to GitHub Gist...")
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        content = json.dumps(data, indent=2)
        
        update_data = {
            'files': {
                GIST_FILENAME: {
                    'content': content
                }
            }
        }
        
        response = requests.patch(f'https://api.github.com/gists/{GIST_ID}', 
                                headers=headers, 
                                json=update_data)
        response.raise_for_status()
        
        print(f"✅ Saved {len(data)} files to Gist")
        
        # Smart backup: Only update local if different
        try:
            local_data = load_uploaded_files_db_local()
            # Update if local is empty/corrupted or has different count
            if not local_data or len(local_data) != len(data):
                save_uploaded_files_db_local(data)
                print(f"💾 Updated local backup ({len(data)} episodes)")
            # If same size and not empty, assume it's current (saves disk I/O)
        except Exception as backup_error:
            # Always backup on error to be safe
            try:
                save_uploaded_files_db_local(data)
                print(f"💾 Local backup completed (failsafe)")
            except Exception as failsafe_error:
                print(f"❌ Local backup failed completely: {failsafe_error}")
        
    except Exception as e:
        print(f"❌ Error saving to Gist: {e}")
        print("💾 Saving to local JSON file (fallback)")
        save_uploaded_files_db_local(data)
        # Don't raise exception - local backup ensures data is safe

def save_uploaded_files_db(data):
    """Save uploaded files database (Gist primary with local backup)"""
    save_uploaded_files_to_gist(data)

def migrate_local_to_gist():
    """One-time migration function to move local JSON data to Gist"""
    try:
        print("🔄 Checking for local data migration to Gist...")
        
        # Check if we have local data that's not in Gist
        local_data = load_uploaded_files_db_local()
        
        if not local_data:
            print("📭 No local data to migrate")
            return
        
        print(f"📁 Found {len(local_data)} episodes in local JSON")
        
        # Try to load from Gist to see what's already there
        try:
            # Direct Gist check (bypass the auto-backup logic)
            headers = {
                'Authorization': f'token {GITHUB_TOKEN}',
                'Accept': 'application/vnd.github.v3+json'
            }
            response = requests.get(f'https://api.github.com/gists/{GIST_ID}', headers=headers)
            
            if response.ok:
                gist_data = response.json()
                if GIST_FILENAME in gist_data['files']:
                    content = gist_data['files'][GIST_FILENAME]['content']
                    existing_gist_data = json.loads(content)
                    
                    if len(existing_gist_data) >= len(local_data):
                        print("✅ Gist already has complete data, no migration needed")
                        return
                    else:
                        print(f"🔄 Gist has {len(existing_gist_data)} episodes, local has {len(local_data)}")
                        print("📤 Migrating additional local data to Gist...")
                        
                        # Merge data (local takes precedence for conflicts)
                        merged_data = existing_gist_data.copy()
                        merged_data.update(local_data)
                        
                        save_uploaded_files_to_gist(merged_data)
                        print(f"✅ Successfully migrated {len(merged_data)} episodes to Gist")
                        return
                else:
                    print("📤 Gist file not found, uploading all local data...")
            else:
                print("⚠️ Could not access Gist, uploading all local data...")
            
            # Upload all local data
            save_uploaded_files_to_gist(local_data)
            print(f"✅ Successfully uploaded {len(local_data)} episodes to Gist")
                
        except Exception as gist_error:
            print(f"⚠️ Gist migration error: {gist_error}")
            print("💾 Local data remains safe, continuing...")
            
    except Exception as e:
        print(f"❌ Migration error: {e}")
        print("💾 Local data remains safe, continuing with existing setup")

def initialize_gist_database():
    """Initialize and verify Gist database on server startup"""
    print("🚀 Initializing Gist Database...")
    
    # Check and fix local JSON file first
    check_and_fix_local_json()
    
    # Skip migration if Gist token not configured
    if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_your_token_here":
        print("⚠️ GitHub token not configured - using local JSON mode")
        try:
            data = load_uploaded_files_db_local()
            print(f"💾 Local database initialized with {len(data)} episodes")
            return True
        except Exception as e:
            print(f"❌ Local initialization failed: {e}")
            return False
    
    # Run migration only if needed
    # migrate_local_to_gist()
    
    # Test Gist connection with minimal operations
    try:
        data = load_uploaded_files_db()
        print(f"✅ Gist database ready with {len(data)} episodes")
        return True
    except Exception as e:
        print(f"❌ Gist initialization failed: {e}")
        print("💾 Falling back to local JSON mode")
        return False
    
# Initialize Gist database on server startup
print("🚀 Smart TV Streaming Server - GitHub Gist Integration")
print("=" * 55)
initialize_gist_database()
print("=" * 55)

# Add a new endpoint to force refresh the cache
@app.get("/refresh/video_data")
async def refresh_video_data():
    """Force refresh video data from Gist"""
    try:
        data = get_video_data(force_refresh=True)
        return {"status": "success", "message": "Video data refreshed", "series_count": len(data)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to refresh: {str(e)}"}


#================Mobile Client support========================

# Mobile/PC Streaming Endpoints (added after existing code)
@app.get("/mobile/stream/{msg_id}")
async def mobile_stream(msg_id: str):
    """Smart streaming endpoint for mobile/PC - uses PixelDrain cache or progressive download"""
    print(f"📱 Mobile stream request for msg_id: {msg_id}")
    
    # Check if already uploaded to PixelDrain
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) in uploaded_files:
        file_info = uploaded_files[str(msg_id)]
        pixeldrain_id = file_info["pixeldrain_id"]
        filename = file_info.get("filename", f"{msg_id}.mkv")
        
        # Verify file still exists on PixelDrain
        if await check_pixeldrain_file_exists(pixeldrain_id):
            access_count = file_info.get("access_count", 0)
            if access_count < MAX_ACCESS_COUNT:
                # Increment access count for mobile streaming
                file_info["access_count"] = access_count + 1
                uploaded_files[str(msg_id)] = file_info
                save_uploaded_files_db(uploaded_files)
                
                pixeldrain_url = get_pixeldrain_download_url(pixeldrain_id)
                print(f"📡 Streaming from PixelDrain cache: {filename}")
                
                return {
                    "success": True,
                    "stream_type": "pixeldrain",
                    "stream_url": pixeldrain_url,
                    "filename": filename,
                    "access_count": access_count + 1,
                    "remaining_access": MAX_ACCESS_COUNT - (access_count + 1),
                    "file_size": file_info.get("file_size", 0),
                    "message": "Streaming from PixelDrain cache"
                }
            else:
                # File exceeded access limit - remove from database
                await delete_from_pixeldrain(pixeldrain_id)
                del uploaded_files[str(msg_id)]
                save_uploaded_files_db(uploaded_files)
        else:
            # File not found on PixelDrain - remove from database
            del uploaded_files[str(msg_id)]
            save_uploaded_files_db(uploaded_files)
    
    # File not cached - trigger download and start progressive streaming
    print(f"📥 File not cached, starting progressive download for: {msg_id}")
    
    # Trigger download using existing endpoint
    try:
        # Load video data to find the URL for this msg_id
        data = get_video_data()
        
        episode_url = None
        episode_title = None
        for series_name, series_data in data.items():
            for season_name, episodes in series_data.items():
                for episode in episodes:
                    channel, ep_msg_id = parse_telegram_url(episode["url"])
                    if ep_msg_id == int(msg_id):
                        episode_url = episode["url"]
                        episode_title = episode["title"]
                        break
                if episode_url:
                    break
            if episode_url:
                break
        
        if not episode_url:
            raise HTTPException(404, "Episode not found in catalog")
        
        # Start download using existing download function
        await trigger_download(episode_url)
        
        # Wait for temp file to have sufficient data for streaming (5MB buffer)
        buffer_size = 5 * 1024 * 1024  # 5MB
        max_wait_time = 60  # 60 seconds max wait
        wait_interval = 2   # Check every 2 seconds
        
        temp_file_path = None
        waited_time = 0
        
        while waited_time < max_wait_time:
            # Check for temp file
            if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
                temp_dir = CUSTOM_TEMP_DIR
            else:
                temp_dir = tempfile.gettempdir()
            
            # Look for temp file
            for filename in os.listdir(temp_dir):
                if f"temp_{msg_id}" in filename:
                    temp_path = os.path.join(temp_dir, filename)
                    if os.path.isfile(temp_path):
                        file_size = os.path.getsize(temp_path)
                        if file_size >= buffer_size:
                            temp_file_path = temp_path
                            break
            
            if temp_file_path:
                break
            
            await asyncio.sleep(wait_interval)
            waited_time += wait_interval
        
        if temp_file_path:
            # Stream the temp file while download continues
            from fastapi.responses import FileResponse
            print(f"📺 Starting progressive stream: {os.path.basename(temp_file_path)}")
            
            return {
                "success": True,
                "stream_type": "progressive",
                "stream_url": f"/mobile/progressive/{msg_id}",
                "filename": episode_title or f"{msg_id}.mkv",
                "message": "Progressive streaming (downloading while playing)",
                "buffer_ready": True
            }
        else:
            return {
                "success": False,
                "stream_type": "progressive",
                "message": "Download started but buffer not ready yet. Please try again in a few seconds.",
                "buffer_ready": False,
                "retry_after": 5
            }
            
    except Exception as e:
        print(f"❌ Error setting up progressive stream: {e}")
        raise HTTPException(500, f"Failed to setup streaming: {str(e)}")

@app.get("/mobile/progressive/{msg_id}")
async def mobile_progressive_stream(msg_id: str, request: Request):
    """Stream temp file while download is in progress"""
    try:
        # Find temp file
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            temp_dir = CUSTOM_TEMP_DIR
        else:
            temp_dir = tempfile.gettempdir()
        
        temp_file_path = None
        for filename in os.listdir(temp_dir):
            if f"temp_{msg_id}" in filename:
                temp_path = os.path.join(temp_dir, filename)
                if os.path.isfile(temp_path):
                    temp_file_path = temp_path
                    break
        
        if not temp_file_path:
            raise HTTPException(404, "Temp file not found")
        
        # Get file info
        file_size = os.path.getsize(temp_file_path)
        
        # Handle range requests for video seeking
        range_header = request.headers.get('range')
        
        if range_header:
            # Parse range header
            range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                end = min(end, file_size - 1)
                
                # Stream the requested range
                def file_streamer():
                    with open(temp_file_path, 'rb') as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk_size = min(8192, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk
                
                headers = {
                    'Content-Range': f'bytes {start}-{end}/{file_size}',
                    'Accept-Ranges': 'bytes',
                    'Content-Length': str(end - start + 1),
                    'Content-Type': 'video/mp4'
                }
                
                return StreamingResponse(file_streamer(), status_code=206, headers=headers)
        
        # Stream entire file
        def file_streamer():
            with open(temp_file_path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    yield chunk
        
        headers = {
            'Content-Length': str(file_size),
            'Accept-Ranges': 'bytes',
            'Content-Type': 'video/mp4'
        }
        
        return StreamingResponse(file_streamer(), headers=headers)
        
    except Exception as e:
        print(f"❌ Error in progressive streaming: {e}")
        raise HTTPException(500, f"Streaming error: {str(e)}")

@app.get("/mobile/status")
async def mobile_status():
    """Status endpoint for mobile clients"""
    uploaded_files = load_uploaded_files_db()
    
    return {
        "service": "Smart TV Streaming Server - Mobile Client",
        "status": "running",
        "total_cached_episodes": len(uploaded_files),
        "telegram_connected": client.is_connected() if client else False,
        "features": {
            "pixeldrain_cache": True,
            "progressive_streaming": True,
            "range_requests": True
        }
    }

@app.get("/mobile_client.html")
async def serve_mobile_client():
    """Serve the mobile client HTML"""
    return FileResponse("mobile_client.html", media_type="text/html")




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)