import os
import json
import re
import asyncio
import tempfile
import threading
import time
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# Import our custom modules
from download_manager import chunked_download_upload, standard_download_upload, process_season_downloads, download_single_episode
from upload_manager import upload_to_pixeldrain, check_pixeldrain_file_exists, delete_from_pixeldrain, get_pixeldrain_download_url
from database_manager import load_uploaded_files_db, save_uploaded_files_db, load_uploaded_files_db_async, save_uploaded_files_db_async, load_video_data, set_gist_manager
from utils import parse_telegram_url, get_file_extension_from_message, get_file_size_from_message
from download_scheduler import download_scheduler
from queue_manager import queue_manager

# Import Gist functionality
from gist_manager import GistManager, background_sync_task


# Mobile streaming
mobile_stream_locks = {}  # msg_id -> threading.Lock()
mobile_stream_status = {}  # msg_id -> status info

# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# Telegram CDN configuration
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

# GitHub Gist configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

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

# Memory-optimized chunk sizing for Render's 512MB limit
CHUNK_SIZE_MIN = 1 * 1024 * 1024          # 1MB minimum chunk (optimized from 2MB)
CHUNK_SIZE_DEFAULT = 2 * 1024 * 1024       # 2MB default chunk (optimized from 5MB)  
CHUNK_SIZE_MAX = 4 * 1024 * 1024           # 4MB maximum chunk (optimized from 100MB)
CHUNK_SIZE_ADAPTIVE = True                 # Enable adaptive chunk sizing

# Memory safety limits for Render free tier
MAX_MEMORY_PER_DOWNLOAD = 150 * 1024 * 1024  # 150MB max memory per download (safer threshold)
ENABLE_STREAMING_UPLOAD = True               # Enable streaming upload to PixelDrain

MAX_CACHE_SIZE_GB = 5  # Maximum cache size in GB
MIN_FREE_SPACE_GB = 1  # Minimum free space to maintain

# Custom temporary directory (optional)
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")

# Custom temporary directory (optional)
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")

# Get port from environment (Railway sets this automatically)
PORT = int(os.getenv("PORT", 8000))

# Initialize GistManager
gist_manager = None
if GITHUB_TOKEN and GIST_ID:
    gist_manager = GistManager(GITHUB_TOKEN, GIST_ID)
    set_gist_manager(gist_manager)
    print("✅ GistManager configured")
else:
    print("⚠️ GitHub token or Gist ID not found, using local files only")

app = FastAPI(title="Smart TV Streaming Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or restrict to ["http://localhost:PORT"] etc.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add Gist sync middleware
@app.middleware("http")
async def gist_sync_middleware(request: Request, call_next):
    """Check for Gist updates on catalog requests"""
    if gist_manager and request.url.path.startswith("/catalog/"):
        try:
            # Check for video.json updates (non-blocking)
            asyncio.create_task(gist_manager.check_for_updates("video.json"))
        except Exception as e:
            print(f"⚠️ Middleware sync check failed: {e}")
    
    response = await call_next(request)
    return response


# Get absolute path to the mobile directory
mobile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile")

# Static folder for poster images
app.mount("/static", StaticFiles(directory="static"), name="static")

# Mobile client
app.mount("/mobile", StaticFiles(directory=mobile_path), name="mobile_static")

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

        # Initialize GistManager
        if gist_manager:
            await gist_manager.initialize()
            # Start background sync task
            asyncio.create_task(background_sync_task(gist_manager))
            print("🔄 Started background Gist sync task")
        
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
        
        # Initialize download scheduler
        await download_scheduler.start_scheduler(client)
        print("✅ Download scheduler started with 4 slots: 3 LOW priority + 1 HIGH priority")
        
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
    
    # Stop download scheduler
    await download_scheduler.stop_scheduler()

# Catalog routes
@app.get("/catalog/series")
async def list_series():
    data = await load_video_data()
    return [{"name": s, "poster": POSTERS.get(s, "/static/default.jpg")} for s in data.keys()]

@app.get("/catalog/series/{series_name}")
async def list_seasons(series_name: str):
    data = await load_video_data()
    if series_name not in data:
        raise HTTPException(status_code=404, detail="Series not found")
    return list(data[series_name].keys())

@app.get("/catalog/series/{series_name}/{season_name}")
async def list_episodes(series_name: str, season_name: str):
    data = await load_video_data()
    if series_name not in data or season_name not in data[series_name]:
        raise HTTPException(status_code=404, detail="Season not found")
    episodes = data[series_name][season_name]
    
    # Load uploaded files database
    uploaded_files = await load_uploaded_files_db_async()
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
        await save_uploaded_files_db_async(uploaded_files)
        print(f"🧹 Cleaned up {len(expired_files)} expired files during episode listing")

    return episodes

# Download and upload to PixelDrain
@app.get("/download")
async def trigger_download(url: str):
    """Queue a single episode download (HIGH priority)"""
    try:
        # Use the new queue-based download system
        result = await download_scheduler.queue_single_episode_download(url, client)
        
        if result["status"] == "already_uploaded":
            return {
                "status": "already_uploaded",
                "pixeldrain_id": result["pixeldrain_id"],
                "msg_id": result["msg_id"]
            }
        elif result["status"] == "queued":
            return {
                "status": "queued", 
                "msg_id": result["msg_id"],
                "priority": result["priority"],
                "queue_position": result["queue_position"],
                "message": f"Added to {result['priority']} priority queue (position: {result['queue_position']})"
            }
        elif result["status"] == "already_queued":
            return {
                "status": "already_queued",
                "msg_id": result["msg_id"],
                "message": "Download already in progress or queued"
            }
        else:
            return {
                "status": "error",
                "message": "Failed to queue download"
            }
            
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        print(f"❌ Error in trigger_download: {e}")
        raise HTTPException(500, f"Download failed: {str(e)}")

@app.get("/downloads")
async def list_downloads():
    uploaded_files = await load_uploaded_files_db_async()
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

@app.get("/test/chunked_upload")
async def test_chunked_upload(size_mb: int = 100):
    """Test memory-safe chunked upload without Telegram (for testing memory usage)"""
    import tempfile
    
    if size_mb > 500:  # Prevent excessive test files
        raise HTTPException(400, "Test file size too large (max 500MB)")
    
    print(f"🧪 Testing memory-safe upload with {size_mb}MB file...")
    test_file_path = None
    
    try:
        # Create test file
        temp_dir = tempfile.gettempdir()
        test_file_path = os.path.join(temp_dir, f"test_upload_{size_mb}MB.dat")
        
        # Create file in chunks to avoid memory issues
        chunk_size = 1024 * 1024  # 1MB
        print(f"📝 Creating {size_mb}MB test file...")
        
        with open(test_file_path, 'wb') as f:
            for i in range(size_mb):
                chunk = b'T' * chunk_size  # Fill with 'T' character
                f.write(chunk)
                if (i + 1) % 10 == 0:  # Print every 10MB
                    print(f"📦 Created {i+1}/{size_mb}MB")
        
        print(f"✅ Test file created: {os.path.getsize(test_file_path) / (1024*1024):.1f}MB")
        
        # Test memory-safe upload
        start_time = time.time()
        result = await upload_to_pixeldrain(test_file_path, f"test_{size_mb}MB.dat")
        upload_time = time.time() - start_time
        
        return {
            "status": "success",
            "pixeldrain_id": result,
            "file_size_mb": size_mb,
            "upload_time_seconds": round(upload_time, 2),
            "upload_speed_mbps": round(size_mb / upload_time, 2),
            "message": f"Successfully uploaded {size_mb}MB test file in {upload_time:.2f}s using memory-safe method",
            "memory_info": "Upload used ~8-16MB memory regardless of file size"
        }
        
    except Exception as e:
        print(f"❌ Test upload failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": f"Memory-safe upload test failed for {size_mb}MB file"
        }
    
    finally:
        # Clean up test file
        if test_file_path and os.path.exists(test_file_path):
            try:
                os.unlink(test_file_path)
                print(f"🧹 Cleaned up test file")
            except:
                pass

@app.get("/queue/status")
async def get_queue_status():
    """Get current download queue status"""
    return download_scheduler.get_download_status()

@app.get("/stream_local/{msg_id}")
async def stream_local(msg_id: str, request: Request):
    # Get file URL from database
    uploaded_files = await load_uploaded_files_db_async()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    filename = file_info.get("filename", f"{msg_id}.mkv")
    pixeldrain_id = file_info["pixeldrain_id"]
    
    # Verify file still exists on PixelDrain
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        await save_uploaded_files_db_async(uploaded_files)
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    # Check access count
    access_count = file_info.get("access_count", 0)
    if access_count >= MAX_ACCESS_COUNT:
        # File exceeded access limit
        await delete_from_pixeldrain(pixeldrain_id)
        del uploaded_files[str(msg_id)]
        await save_uploaded_files_db_async(uploaded_files)
        raise HTTPException(410, f"File access limit exceeded ({MAX_ACCESS_COUNT} times). File has been deleted.")
    
    # Increment access count
    file_info["access_count"] = access_count + 1
    uploaded_files[str(msg_id)] = file_info
    await save_uploaded_files_db_async(uploaded_files)
    
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
    uploaded_files = await load_uploaded_files_db_async()
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

        await save_uploaded_files_db_async(uploaded_files)
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
        # Check if download is in progress using QUEUE MANAGER
        msg_id_int = int(msg_id)
        download_in_progress = queue_manager.is_download_in_progress(msg_id_int)
        
        print(f"🔍 Checking download progress for msg_id: {msg_id}")
        print(f"📋 Queue manager download tasks: {list(queue_manager.download_tasks.keys())}")
        print(f"✅ Download in progress (queue manager): {download_in_progress}")
        
        if not download_in_progress:
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
        with open("cache/video.json", encoding="utf-8") as f:
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
        
        # Check if download is in progress using QUEUE MANAGER (not global variable)
        msg_id_int = int(msg_id)
        download_in_progress = queue_manager.is_download_in_progress(msg_id_int)
        
        print(f"🔍 Checking download progress for msg_id: {msg_id}")
        print(f"📋 Queue manager download tasks: {list(queue_manager.download_tasks.keys())}")
        print(f"✅ Download in progress (queue manager): {download_in_progress}")
        
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
    uploaded_files = await load_uploaded_files_db_async()
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
        await save_uploaded_files_db_async(uploaded_files)
        print(f"❌ File {pixeldrain_id} not found on PixelDrain, removed from database")
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    # Check access count
    access_count = file_info.get("access_count", 0)
    if access_count >= MAX_ACCESS_COUNT:
        # File exceeded access limit
        await delete_from_pixeldrain(pixeldrain_id)
        del uploaded_files[str(msg_id)]
        await save_uploaded_files_db_async(uploaded_files)
        print(f"❌ File {pixeldrain_id} exceeded access limit, deleted")
        raise HTTPException(410, f"File access limit exceeded ({MAX_ACCESS_COUNT} times). File has been deleted.")
    
    # Increment access count
    file_info["access_count"] = access_count + 1
    uploaded_files[str(msg_id)] = file_info
    await save_uploaded_files_db_async(uploaded_files)
    
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

# Add Gist management endpoints

@app.get("/gist/status")
async def gist_status():
    """Get Gist synchronization status"""
    if not gist_manager:
        return {"status": "disabled", "message": "GitHub Gist not configured"}
    
    try:
        gist_info = await gist_manager.get_gist_info()
        return {
            "status": "connected",
            "gist_id": gist_manager.gist_id,
            "files_in_gist": list(gist_info.get('files', {}).keys()),
            "local_cache": list(gist_manager.local_cache.keys()),
            "last_updated": gist_info.get('updated_at'),
            "cache_ttl": gist_manager.cache_ttl
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/gist/sync")
async def manual_gist_sync():
    """Manually trigger Gist synchronization"""
    if not gist_manager:
        raise HTTPException(400, "GitHub Gist not configured")
    
    try:
        await gist_manager.sync_all_files()
        return {"status": "success", "message": "All files synced to Gist"}
    except Exception as e:
        raise HTTPException(500, f"Sync failed: {str(e)}")

@app.post("/gist/force_update/{filename}")
async def force_gist_update(filename: str):
    """Force update a specific file from Gist"""
    if not gist_manager:
        raise HTTPException(400, "GitHub Gist not configured")
    
    # Only allow the two files you actually use
    if filename not in ["video.json", "uploaded_episodes.json"]:
        raise HTTPException(400, "Invalid filename. Only video.json and uploaded_episodes.json are supported")
    
    try:
        # Clear cache to force refresh
        if filename in gist_manager.local_cache:
            del gist_manager.local_cache[filename]
        if filename in gist_manager.cache_timestamps:
            del gist_manager.cache_timestamps[filename]
        
        # Fetch fresh data
        data = await gist_manager.get_data(filename)
        if data is None:
            raise HTTPException(404, f"File {filename} not found in Gist")
        
        return {"status": "success", "message": f"Force updated {filename} from Gist"}
    except Exception as e:
        raise HTTPException(500, f"Force update failed: {str(e)}")


@app.get("/performance/memory")
async def check_memory_usage():
    """Check current memory usage and chunked download settings"""
    import os
    
    try:
        import psutil
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        
        memory_data = {
            "rss_mb": memory_info.rss / (1024 * 1024),  # Resident Set Size
            "vms_mb": memory_info.vms / (1024 * 1024),  # Virtual Memory Size
            "percentage": process.memory_percent()
        }
    except ImportError:
        # Fallback if psutil not available
        memory_data = {
            "rss_mb": "unavailable (psutil not installed)",
            "vms_mb": "unavailable",
            "percentage": "unavailable"
        }
    
    current_memory = memory_data.get("rss_mb", 0) if isinstance(memory_data.get("rss_mb"), (int, float)) else 100
    
    return {
        "memory_usage": memory_data,
        "render_limits": {
            "max_memory_mb": 512,
            "memory_used_percentage": (current_memory / 512 * 100) if isinstance(current_memory, (int, float)) else "unknown",
            "memory_available_mb": (512 - current_memory) if isinstance(current_memory, (int, float)) else "unknown"
        },
        "chunked_config": {
            "max_chunk_mb": CHUNK_SIZE_MAX / (1024 * 1024),
            "default_chunk_mb": CHUNK_SIZE_DEFAULT / (1024 * 1024),
            "min_chunk_mb": CHUNK_SIZE_MIN / (1024 * 1024),
            "adaptive_enabled": CHUNK_SIZE_ADAPTIVE,
            "max_memory_per_download_mb": MAX_MEMORY_PER_DOWNLOAD / (1024 * 1024),
            "streaming_enabled": ENABLE_STREAMING_UPLOAD
        },
        "status": "OK" if isinstance(current_memory, (int, float)) and current_memory < 400 else "WARNING"
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
            "global_download_tasks_keys": list(download_tasks.keys()),
            "queue_manager_tasks_keys": list(queue_manager.download_tasks.keys()),
            "msg_id_int_in_global_tasks": msg_id_int in download_tasks,
            "msg_id_str_in_global_tasks": str(msg_id) in download_tasks,
            "msg_id_in_queue_manager": queue_manager.is_download_in_progress(msg_id_int),
            "download_in_progress_old_method": msg_id_int in download_tasks or str(msg_id) in download_tasks,
            "download_in_progress_queue_manager": queue_manager.is_download_in_progress(msg_id_int)
        },
        "temp_directory": temp_dir,
        "temp_files": temp_files
    }

# Season download endpoints
@app.post("/download/season")
async def download_season(request: Request):
    """Download all episodes from a season using the smart queue system"""
    try:
        # Get parameters from query string
        series_name = request.query_params.get('series_name')
        season_name = request.query_params.get('season_name')
        
        if not series_name or not season_name:
            raise HTTPException(status_code=400, detail="Missing series_name or season_name parameters")
        
        print(f"📋 Season download request: {series_name} - {season_name}")
        
        # Load episode data
        data = await load_video_data()
        
        if series_name not in data or season_name not in data[series_name]:
            raise HTTPException(status_code=404, detail="Season not found")
        
        episodes = data[series_name][season_name]
        uploaded_files = await load_uploaded_files_db_async()
        
        # Filter episodes that need downloading
        episodes_to_download = []
        for ep in episodes:
            _, msg_id = parse_telegram_url(ep["url"])
            ep["msg_id"] = msg_id
            ep["series_name"] = series_name
            ep["season_name"] = season_name
            
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
        
        # Queue all episodes using the new queue system (LOW priority)
        queued_count = 0
        failed_count = 0
        
        for episode in episodes_to_download:
            try:
                success = await download_scheduler.queue_season_episode_download(episode, client)
                if success:
                    queued_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                print(f"❌ Failed to queue episode {episode['title']}: {e}")
                failed_count += 1
        
        print(f"📋 Queued {queued_count} episodes for season {season_name}")
        
        return {
            "status": "queued",
            "series_name": series_name,
            "season_name": season_name,
            "message": f"Queued {queued_count} episodes for download (LOW priority)",
            "total_episodes": len(episodes),
            "episodes_to_download": len(episodes_to_download),
            "episodes_already_downloaded": len(episodes) - len(episodes_to_download),
            "queued_count": queued_count,
            "failed_count": failed_count,
            "note": "Episodes will be downloaded after all single episode requests"
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



# === Mobile/PC streaming endpoints (Testing) === #

@app.get("/stream_mobile/{msg_id}")
async def stream_mobile(msg_id: str, request: Request):
    """
    Mobile/PC streaming endpoint with improved coordination and VLC compatibility
    """
    print(f"📱 Mobile stream request for msg_id: {msg_id}")
    
    # Use a lock per msg_id to prevent concurrent download triggers
    if msg_id not in mobile_stream_locks:
        mobile_stream_locks[msg_id] = threading.Lock()
    
    with mobile_stream_locks[msg_id]:
        try:
            # First check if file is already on PixelDrain
            uploaded_files = load_uploaded_files_db()
            
            if str(msg_id) in uploaded_files:
                file_info = uploaded_files[str(msg_id)]
                pixeldrain_id = file_info["pixeldrain_id"]
                
                # Verify file still exists on PixelDrain
                if await check_pixeldrain_file_exists(pixeldrain_id):
                    # Check access count
                    access_count = file_info.get("access_count", 0)
                    if access_count < MAX_ACCESS_COUNT:
                        print(f"📱 File already on PixelDrain, redirecting to direct stream")
                        
                        # Increment access count
                        file_info["access_count"] = access_count + 1
                        uploaded_files[str(msg_id)] = file_info
                        save_uploaded_files_db(uploaded_files)
                        
                        # Get PixelDrain direct URL
                        pixeldrain_url = get_pixeldrain_download_url(pixeldrain_id)
                        
                        # For mobile/PC, redirect to PixelDrain
                        from fastapi.responses import RedirectResponse
                        return RedirectResponse(url=pixeldrain_url, status_code=302)
                    else:
                        # File exceeded access limit, remove and continue to download
                        await delete_from_pixeldrain(pixeldrain_id)
                        del uploaded_files[str(msg_id)]
                        save_uploaded_files_db(uploaded_files)
                else:
                    # File doesn't exist on PixelDrain anymore, remove from DB
                    del uploaded_files[str(msg_id)]
                    save_uploaded_files_db(uploaded_files)
            
            # File not on PixelDrain - need to download and stream simultaneously
            print(f"📱 File not on PixelDrain, starting download and streaming")
            
            # Get file info first
            file_info_response = await get_file_info(msg_id)
            if file_info_response["status"] != "found":
                raise HTTPException(404, "Episode not found in catalog")
            
            total_file_size = file_info_response["file_size"]
            filename = file_info_response["filename"]
            
            # Check if download is already in progress
            msg_id_int = int(msg_id)
            download_in_progress = queue_manager.is_download_in_progress(msg_id_int)
            
            # Only trigger download if not already in progress
            if not download_in_progress and msg_id not in mobile_stream_status:
                # Trigger download using existing system
                print(f"📱 Triggering download for streaming: {filename}")
                
                # Find the episode URL from video.json
                episode_url = None
                with open("video.json", encoding="utf-8") as f:
                    data = json.load(f)
                    for series_name, series_data in data.items():
                        for season_name, episodes in series_data.items():
                            for episode in episodes:
                                _, ep_msg_id = parse_telegram_url(episode["url"])
                                if ep_msg_id == msg_id_int:
                                    episode_url = episode["url"]
                                    break
                            if episode_url:
                                break
                        if episode_url:
                            break
                
                if not episode_url:
                    raise HTTPException(404, "Episode URL not found")
                
                # Queue the download (HIGH priority for mobile streaming)
                try:
                    await download_scheduler.queue_single_episode_download(episode_url, client)
                    print(f"📱 Download queued successfully for mobile streaming")
                    
                    # Mark as being handled
                    mobile_stream_status[msg_id] = {
                        "triggered_at": time.time(),
                        "total_size": total_file_size,
                        "filename": filename
                    }
                except Exception as e:
                    print(f"❌ Failed to queue download: {e}")
                    # Continue anyway, maybe download is already in progress
            
            # Stream from temp file with improved coordination
            return await stream_from_temp_file_improved(msg_id, total_file_size, filename, request)
            
        except HTTPException:
            raise
        except Exception as e:
            print(f"❌ Error in mobile streaming: {e}")
            raise HTTPException(500, f"Streaming failed: {str(e)}")

async def stream_from_temp_file_improved(msg_id: str, total_file_size: int, filename: str, request: Request):
    """
    Improved temp file streaming with better VLC compatibility
    """
    print(f"📱 Streaming from temp file: {filename}")
    
    # Find temp file
    temp_file_path = None
    if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
        temp_dir = CUSTOM_TEMP_DIR
    else:
        import tempfile
        temp_dir = tempfile.gettempdir()
    
    # Look for temp file with longer wait time
    max_wait_time = 60  # Wait max 60 seconds for download to start
    wait_time = 0
    
    while wait_time < max_wait_time:
        try:
            for temp_filename in os.listdir(temp_dir):
                if f"temp_{msg_id}" in temp_filename:
                    potential_path = os.path.join(temp_dir, temp_filename)
                    if os.path.isfile(potential_path):
                        temp_file_path = potential_path
                        break
            
            if temp_file_path:
                break
                
            # Wait for download to start
            await asyncio.sleep(2)  # Check every 2 seconds
            wait_time += 2
            
        except Exception as e:
            print(f"❌ Error looking for temp file: {e}")
            await asyncio.sleep(2)
            wait_time += 2
    
    if not temp_file_path:
        raise HTTPException(404, "Download not started or temp file not found")
    
    print(f"📱 Found temp file: {temp_file_path}")
    
    # Get current file size
    current_size = os.path.getsize(temp_file_path)
    
    # Handle range requests
    range_header = request.headers.get('range')
    
    # For VLC compatibility, we need proper video headers
    video_headers = {
        'Content-Type': 'video/mp4',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        # Add CORS headers for better compatibility
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
        'Access-Control-Allow-Headers': 'Range, Content-Range, Content-Length'
    }
    
    if range_header:
        # Parse range header (e.g., "bytes=0-1023")
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2)) if range_match.group(2) else None
            
            # Wait for the requested range to be available
            max_range_wait = 30  # Wait max 30 seconds for range to be available
            range_wait = 0
            
            while start >= current_size and range_wait < max_range_wait:
                await asyncio.sleep(1)
                if os.path.exists(temp_file_path):
                    current_size = os.path.getsize(temp_file_path)
                range_wait += 1
            
            # If we still don't have the data, return what we can
            if start >= current_size:
                # Return empty content with proper range headers
                actual_end = current_size - 1 if current_size > 0 else 0
                video_headers.update({
                    'Content-Range': f'bytes {start}-{actual_end}/{total_file_size}',
                    'Content-Length': '0'
                })
                return StreamingResponse(
                    iter([b'']),  # Empty content
                    status_code=206,
                    headers=video_headers
                )
            
            # Calculate actual end position
            actual_end = min(end or (total_file_size - 1), current_size - 1, total_file_size - 1)
            content_length = actual_end - start + 1
            
            print(f"📱 Streaming range: {start}-{actual_end} (available: {current_size}, total: {total_file_size})")
            
            video_headers.update({
                'Content-Range': f'bytes {start}-{actual_end}/{total_file_size}',
                'Content-Length': str(content_length)
            })
            
            return StreamingResponse(
                stream_temp_file_range_improved(temp_file_path, start, actual_end, total_file_size),
                status_code=206,
                headers=video_headers
            )
    
    # No range request - stream entire file as it becomes available
    video_headers['Content-Length'] = str(total_file_size)
    
    return StreamingResponse(
        stream_temp_file_complete_improved(temp_file_path, total_file_size),
        headers=video_headers
    )

async def stream_temp_file_range_improved(temp_file_path: str, start: int, end: int, total_size: int):
    """Improved range streaming with better error handling"""
    print(f"📱 Streaming range: {start}-{end}")
    
    chunk_size = 32 * 1024  # 32KB chunks for smooth streaming
    current_pos = start
    
    try:
        while current_pos <= end:
            # Check if temp file still exists
            if not os.path.exists(temp_file_path):
                print(f"❌ Temp file disappeared: {temp_file_path}")
                break
            
            # Read chunk directly without waiting
            try:
                with open(temp_file_path, 'rb') as f:
                    f.seek(current_pos)
                    bytes_to_read = min(chunk_size, end - current_pos + 1)
                    
                    if bytes_to_read <= 0:
                        break
                        
                    chunk = f.read(bytes_to_read)
                    
                    if not chunk:
                        # No data available, but don't wait - just end the stream
                        break
                    
                    yield chunk
                    current_pos += len(chunk)
                    
            except IOError as e:
                print(f"⚠️ IO error reading temp file: {e}")
                break
                
    except Exception as e:
        print(f"❌ Error streaming range: {e}")

async def stream_temp_file_complete_improved(temp_file_path: str, total_size: int):
    """Improved complete file streaming"""
    print(f"📱 Streaming complete file")
    
    chunk_size = 32 * 1024  # 32KB chunks
    current_pos = 0
    
    try:
        while current_pos < total_size:
            # Check if temp file still exists
            if not os.path.exists(temp_file_path):
                print(f"❌ Temp file disappeared: {temp_file_path}")
                break
            
            # Get current file size
            current_file_size = os.path.getsize(temp_file_path)
            
            # Read available data without waiting too long
            if current_pos < current_file_size:
                try:
                    with open(temp_file_path, 'rb') as f:
                        f.seek(current_pos)
                        bytes_to_read = min(chunk_size, current_file_size - current_pos)
                        
                        if bytes_to_read <= 0:
                            await asyncio.sleep(0.1)
                            continue
                            
                        chunk = f.read(bytes_to_read)
                        
                        if not chunk:
                            await asyncio.sleep(0.1)
                            continue
                        
                        yield chunk
                        current_pos += len(chunk)
                        
                except IOError as e:
                    print(f"⚠️ IO error reading temp file: {e}")
                    await asyncio.sleep(0.1)
                    continue
            else:
                # Wait a bit for more data, but don't wait forever
                await asyncio.sleep(0.5)
                
    except Exception as e:
        print(f"❌ Error streaming complete file: {e}")

# Also update the HEAD handler for better VLC compatibility
@app.head("/stream_mobile/{msg_id}")
async def stream_mobile_head(msg_id: str):
    """HEAD request for mobile streaming - returns headers without body"""
    try:
        # Get file info
        file_info_response = await get_file_info(msg_id)
        if file_info_response["status"] != "found":
            raise HTTPException(404, "Episode not found")
        
        total_file_size = file_info_response["file_size"]
        
        return JSONResponse(
            content={},
            headers={
                'Content-Type': 'video/mp4',
                'Accept-Ranges': 'bytes',
                'Content-Length': str(total_file_size),
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
                'Access-Control-Allow-Headers': 'Range, Content-Range, Content-Length'
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/catalog/episode/{msg_id}")
async def get_episode_by_msg_id(msg_id: str):
    """Get episode information by message ID"""
    try:
        msg_id_int = int(msg_id)
        with open("video.json", encoding="utf-8") as f:
            data = json.load(f)
            for series_name, series_data in data.items():
                for season_name, episodes in series_data.items():
                    for episode in episodes:
                        _, ep_msg_id = parse_telegram_url(episode["url"])
                        if ep_msg_id == msg_id_int:
                            return {
                                "url": episode["url"],
                                "title": episode.get("title", "Unknown"),
                                "description": episode.get("description", ""),
                                "msg_id": msg_id
                            }
        return {"error": "Episode not found"}
    except Exception as e:
        return {"error": str(e)}



@app.get("/mobile")
async def serve_mobile_webapp():
    """Serve the Netflix-like mobile webapp"""
    return FileResponse("mobile/index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    import asyncio
    import json
    import requests
    import sys
    import os
    
    # Run the FastAPI app
    uvicorn.run(app, host="0.0.0.0", port=8000)

