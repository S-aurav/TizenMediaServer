import os
import json
import re
import asyncio
import aiohttp
import tempfile
import shutil
import time
import requests
from typing import List
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
from mega import Mega


# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# Telegram CDN configuration
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")  # For deployment

# Database file for tracking downloaded episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

# Mega.nz configuration
MEGA_EMAIL = os.getenv("MEGA_EMAIL")
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD") 
MEGA_FOLDER_NAME = "SmartTV_Episodes"

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

# Initialize Telegram client
client = None
progress = {}
download_tasks = {}  # message_id -> asyncio.Task

mega_client = None

@app.on_event("startup")
async def startup_event():
    global client, cleanup_task
    try:
        # Use StringSession for deployment, fallback to file session for local development
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
        
        # Initialize Mega client
        try:
            print("🔧 Initializing Mega.nz client...")
            init_mega_client()
            account_info = get_mega_account_info()
            print(f"💾 Mega.nz connected: {account_info['free_gb']:.2f}GB free of {account_info['total_gb']}GB")
        except Exception as mega_error:
            print(f"⚠️ Mega.nz initialization failed: {mega_error}")
            print("💡 Make sure MEGA_EMAIL and MEGA_PASSWORD are set in environment variables")
        
        # Run initial cleanup of expired files
        print("🧹 Running initial cleanup check...")
        expired_count = await cleanup_expired_files()
        
        # Start periodic cleanup task
        cleanup_task = asyncio.create_task(periodic_cleanup())
        print("⏰ Started periodic cleanup task (runs every 6 hours)")
        
    except Exception as e:
        print(f"❌ Failed to connect Telegram client: {e}")
        print("💡 Make sure TELEGRAM_SESSION_STRING is set in environment variables")
        # Don't raise exception to allow server to start (for debugging)

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
async def list_episodes(series_name: str, season_name: str, validate: bool = False):
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
        
        # Check if file is uploaded to Mega.nz or PixelDrain (legacy)
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            
            if "mega_url" in file_info:
                # New Mega.nz format
                mega_url = file_info["mega_url"]
                
                # Optional real-time validation (slower but accurate)
                if validate:
                    file_exists = await check_mega_file_exists(mega_url)
                    if not file_exists:
                        # File expired - remove from database
                        expired_files.append(str(msg_id))
                        ep["downloaded"] = False
                    else:
                        ep["downloaded"] = True
                        ep["mega_url"] = mega_url
                else:
                    # Fast mode - trust database
                    ep["downloaded"] = True
                    ep["mega_url"] = mega_url
            elif "pixeldrain_id" in file_info:
                # Legacy PixelDrain format - mark as not downloaded (need re-upload to Mega)
                expired_files.append(str(msg_id))
                ep["downloaded"] = False
                ep["needs_reupload"] = "pixeldrain_to_mega"
            else:
                # Unknown format
                expired_files.append(str(msg_id))
                ep["downloaded"] = False
        else:
            ep["downloaded"] = False
    
    # Clean up expired files from database
    if expired_files:
        for msg_id in expired_files:
            if msg_id in uploaded_files:
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

# Mega.nz helper functions
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

async def upload_to_mega(file_path: str, filename: str) -> str:
    """Upload a file to Mega.nz and return the public download URL"""
    try:
        print(f"🔧 Initializing Mega client for upload...")
        if not mega_client:
            print(f"⚠️ Mega client not initialized, initializing now...")
            folder_id = init_mega_client()
        else:
            print(f"✅ Using existing Mega client")
            # Get folder ID
            files = mega_client.get_files()
            folder_id = None
            for file_id, file_info in files.items():
                if (file_info.get('a') and 
                    file_info['a'].get('n') == MEGA_FOLDER_NAME and 
                    file_info.get('t') == 1):  # t=1 means folder
                    folder_id = file_id
                    print(f"📁 Found target folder: {MEGA_FOLDER_NAME} (ID: {file_id})")
                    break
            
            if not folder_id:
                print(f"� Target folder not found, creating: {MEGA_FOLDER_NAME}")
                folder_result = mega_client.create_folder(MEGA_FOLDER_NAME)
                # Handle different return formats from mega.py
                if isinstance(folder_result, dict) and 'f' in folder_result:
                    folder_id = folder_result['f'][0]['h']
                else:
                    folder_id = folder_result
                print(f"📁 Created folder with ID: {folder_id}")
        
        print(f"�📤 Uploading {filename} to Mega.nz folder {MEGA_FOLDER_NAME}...")
        print(f"📊 File size: {os.path.getsize(file_path) / (1024*1024):.2f} MB")
        
        # Check if we need to free up space first
        file_size_gb = os.path.getsize(file_path) / (1024**3)
        account_info = get_mega_account_info()
        print(f"💾 Current space: {account_info['free_gb']:.2f}GB free, need: {file_size_gb:.2f}GB")
        
        if account_info['free_gb'] < (MIN_FREE_SPACE_GB + file_size_gb):
            print(f"⚠️ Low space! Need {file_size_gb:.2f}GB, have {account_info['free_gb']:.2f}GB")
            await cleanup_old_mega_files(file_size_gb)
        
        # Upload file to the episodes folder
        print(f"🚀 Starting upload to folder ID: {folder_id}")
        uploaded_file = mega_client.upload(file_path, folder_id)
        print(f"✅ File uploaded successfully: {uploaded_file}")
        
        # Get public download link
        print(f"🔗 Getting public download link...")
        download_url = mega_client.get_upload_link(uploaded_file)
        print(f"🎯 Generated download URL: {download_url}")
        
        print(f"✅ Upload successful! Mega URL: {download_url}")
        return download_url
        
    except Exception as e:
        print(f"❌ Error uploading to Mega: {e}")
        import traceback
        print(f"🔍 Full upload error trace: {traceback.format_exc()}")
        raise e

async def check_mega_file_exists(mega_url: str) -> bool:
    """Check if a file exists on Mega"""
    try:
        # Try to get file info using URL
        import requests
        response = requests.head(mega_url, timeout=10)
        return response.status_code == 200
    except:
        return False

# Download and upload to Mega.nz
@app.get("/download")
async def trigger_download(url: str):
    channel, msg_id = parse_telegram_url(url)
    if not channel or not msg_id:
        raise HTTPException(400, "Invalid Telegram URL")

    # Check if already uploaded to Mega.nz
    uploaded_files = load_uploaded_files_db()
    if str(msg_id) in uploaded_files:
        mega_url = uploaded_files[str(msg_id)]["mega_url"]
        # Verify file still exists on Mega
        if await check_mega_file_exists(mega_url):
            return {"status": "already_uploaded", "mega_url": mega_url}
        else:
            # File expired or deleted, remove from database
            del uploaded_files[str(msg_id)]
            save_uploaded_files_db(uploaded_files)

    if not client.is_connected():
        await client.connect()

    message = await client.get_messages(channel, ids=msg_id)
    if not message or not message.video:
        raise HTTPException(404, detail="No video found")

    # Check if download is already in progress
    if msg_id in download_tasks:
        return {"status": "uploading"}

    async def do_download_and_upload():
        temp_path = None
        try:
            print(f"🔄 Starting download and upload for message ID: {msg_id}")
            
            # Get original file extension from the message
            original_filename = None
            if message.video.attributes:
                for attr in message.video.attributes:
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
                # Use system temp directory (default)
                with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
                    temp_path = temp_file.name
            
            print(f"⬇️ Downloading {filename} to temporary file: {temp_path}")
            await client.download_media(message, file=temp_path)
            print(f"✅ Download complete: {filename}")
            
            # Verify file exists before upload
            if not os.path.exists(temp_path):
                raise Exception(f"Downloaded file not found: {temp_path}")
            
            file_size = os.path.getsize(temp_path)
            print(f"📊 File size: {file_size / (1024*1024):.2f} MB")
            
            # Upload to Mega.nz
            print(f"🚀 Starting upload to Mega.nz...")
            mega_url = await upload_to_mega(temp_path, filename)
            print(f"🎯 Received Mega URL: {mega_url}")
            
            # Save to database
            print(f"💾 Saving to database...")
            uploaded_files = load_uploaded_files_db()
            print(f"📋 Current database has {len(uploaded_files)} files")
            
            uploaded_files[str(msg_id)] = {
                "mega_url": mega_url,
                "filename": filename,
                "uploaded_at": int(time.time()),
                "file_size": message.video.size
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
            
            print(f"✅ Successfully uploaded {filename} to Mega.nz: {mega_url}")
            
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
        if "mega_url" in file_info:
            # New Mega.nz format
            files.append({
                "msg_id": msg_id,
                "mega_url": file_info["mega_url"],
                "filename": file_info["filename"],
                "storage": "mega.nz"
            })
        elif "pixeldrain_id" in file_info:
            # Legacy PixelDrain format
            files.append({
                "msg_id": msg_id,
                "pixeldrain_id": file_info["pixeldrain_id"],
                "filename": file_info["filename"],
                "storage": "pixeldrain_legacy",
                "note": "Re-download needed to upload to Mega.nz"
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
    
    if "mega_url" in file_info:
        # New Mega.nz format
        mega_url = file_info["mega_url"]
        
        # Verify file still exists on Mega.nz
        if not await check_mega_file_exists(mega_url):
            # File expired or deleted, remove from database
            del uploaded_files[str(msg_id)]
            save_uploaded_files_db(uploaded_files)
            raise HTTPException(404, "File expired or deleted from Mega.nz")
        
        print(f"📺 Redirecting to Mega.nz direct URL: {filename}")
        
        # For Smart TVs, redirect to the direct Mega.nz URL
        # Mega.nz handles range requests natively
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=mega_url, status_code=302)
    
    elif "pixeldrain_id" in file_info:
        # Legacy PixelDrain format - file needs to be re-uploaded to Mega.nz
        raise HTTPException(410, "File was stored on PixelDrain. Please re-download to upload to Mega.nz")
    
    else:
        raise HTTPException(404, "File format not supported")

@app.head("/stream_local/{msg_id}")
async def stream_local_head(msg_id: str):
    # Get file URL from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    
    if "mega_url" in file_info:
        mega_url = file_info["mega_url"]
        
        # Verify file still exists
        if not await check_mega_file_exists(mega_url):
            raise HTTPException(404, "File not found on Mega.nz")
        
        # Return basic headers for video content
        response_headers = {
            'Accept-Ranges': 'bytes',
            'Content-Type': 'video/mp4',
            'Cache-Control': 'no-cache'
        }
        
        return JSONResponse(content={}, headers=response_headers)
    
    elif "pixeldrain_id" in file_info:
        # Legacy PixelDrain format
        raise HTTPException(410, "File was stored on PixelDrain. Please re-download to upload to Mega.nz")
    
    else:
        raise HTTPException(404, "File format not supported")

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
        
        # Handle both old PixelDrain format and new Mega.nz format
        if "mega_url" in file_info:
            # New Mega.nz format
            mega_url = file_info["mega_url"]
            # Check if file still exists on Mega.nz
            if not await check_mega_file_exists(mega_url):
                expired_files.append(msg_id)
                print(f"🗑️ Found expired Mega file: {filename} (URL: {mega_url})")
        elif "pixeldrain_id" in file_info:
            # Old PixelDrain format - mark for removal since we switched to Mega.nz
            expired_files.append(msg_id)
            print(f"🗑️ Found old PixelDrain file (switching to Mega.nz): {filename}")
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
    
    return expired_files

async def validate_all_mega_files():
    """Validate all files in database and return status"""
    uploaded_files = load_uploaded_files_db()
    validation_results = {
        "total_files": len(uploaded_files),
        "valid_files": 0,
        "expired_files": 0,
        "pixeldrain_files": 0,
        "expired_list": []
    }
    
    for msg_id, file_info in uploaded_files.items():
        filename = file_info.get("filename", f"{msg_id}.mkv")
        
        if "mega_url" in file_info:
            # New Mega.nz format
            mega_url = file_info["mega_url"]
            if await check_mega_file_exists(mega_url):
                validation_results["valid_files"] += 1
            else:
                validation_results["expired_files"] += 1
                validation_results["expired_list"].append({
                    "msg_id": msg_id,
                    "filename": filename,
                    "mega_url": mega_url,
                    "status": "expired"
                })
        elif "pixeldrain_id" in file_info:
            # Old PixelDrain format
            validation_results["pixeldrain_files"] += 1
            validation_results["expired_list"].append({
                "msg_id": msg_id,
                "filename": filename,
                "pixeldrain_id": file_info["pixeldrain_id"],
                "status": "old_format_pixeldrain"
            })
        else:
            # Unknown format
            validation_results["expired_files"] += 1
            validation_results["expired_list"].append({
                "msg_id": msg_id,
                "filename": filename,
                "status": "unknown_format"
            })
    
    return validation_results

# Background task for periodic cleanup
cleanup_task = None

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
            "expired_files_count": len(expired_files),
            "expired_files": expired_files
        }
    except Exception as e:
        raise HTTPException(500, f"Cleanup failed: {str(e)}")

@app.get("/files/validate")
async def validate_files():
    """Validate all uploaded files and return status"""
    try:
        results = await validate_all_mega_files()
        return results
    except Exception as e:
        raise HTTPException(500, f"Validation failed: {str(e)}")

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
        "note": "Use /files/validate for detailed validation (slower)"
    }

# Add this endpoint to check temp directory info
@app.get("/server/temp-info")
async def get_temp_info():
    """Get information about temporary directory and disk space"""
    import tempfile
    import shutil
    
    temp_dir = tempfile.gettempdir()
    
    try:
        # Get disk usage of temp directory
        total, used, free = shutil.disk_usage(temp_dir)
        
        return {
            "temp_directory": temp_dir,
            "disk_space": {
                "total_gb": round(total / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "free_gb": round(free / (1024**3), 2),
                "free_percentage": round((free / total) * 100, 1)
            },
            "current_downloads": len(download_tasks),
            "note": "Temporary files are deleted after upload to Mega.nz"
        }
    except Exception as e:
        return {
            "temp_directory": temp_dir,
            "error": str(e),
            "current_downloads": len(download_tasks)
        }

# Add direct streaming URL endpoint
@app.get("/stream_direct/{msg_id}")
async def get_direct_stream_url(msg_id: str):
    """Get direct Mega.nz streaming URL instead of proxying through our server"""
    # Get file URL from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    filename = file_info.get("filename", f"{msg_id}.mkv")
    
    if "mega_url" in file_info:
        # New Mega.nz format
        mega_url = file_info["mega_url"]
        
        # Verify file still exists on Mega.nz
        if not await check_mega_file_exists(mega_url):
            # File expired or deleted, remove from database
            del uploaded_files[str(msg_id)]
            save_uploaded_files_db(uploaded_files)
            raise HTTPException(404, "File expired or deleted from Mega.nz")
        
        # Return direct Mega.nz streaming URL
        print(f"🔗 Providing direct Mega.nz URL for {filename}: {mega_url}")
        
        return {
            "direct_url": mega_url,
            "filename": filename,
            "note": "Use this URL directly in your video player for better streaming performance"
        }
    
    elif "pixeldrain_id" in file_info:
        # Legacy PixelDrain format
        raise HTTPException(410, "File was stored on PixelDrain. Please re-download to upload to Mega.nz")
    
    else:
        raise HTTPException(404, "File format not supported")

# Add health check endpoint for deployment
@app.get("/health")
async def health_check():
    """Health check endpoint for deployment platforms"""
    telegram_status = "connected" if client and client.is_connected() else "disconnected"
    
    try:
        mega_status = "connected" if mega_client else "not initialized"
    except:
        mega_status = "error"
    
    return {
        "status": "healthy",
        "service": "Smart TV Streaming Server",
        "telegram_status": telegram_status,
        "mega_configured": bool(MEGA_EMAIL and MEGA_PASSWORD),
        "mega_status": mega_status,
        "telegram_configured": bool(API_ID and API_HASH),
        "session_type": "StringSession" if TELEGRAM_SESSION_STRING else "FileSession"
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

# HLS Streaming Implementation for AVPlay compatibility
@app.get("/hls/{msg_id}/playlist.m3u8")
async def hls_playlist(msg_id: str):
    """Generate HLS playlist for progressive downloading episodes"""
    # Get file info from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    filename = file_info.get("filename", f"{msg_id}.mkv")
    
    if "mega_url" not in file_info:
        raise HTTPException(404, "File not available for HLS streaming")
    
    mega_url = file_info["mega_url"]
    
    # Verify file still exists on Mega.nz
    if not await check_mega_file_exists(mega_url):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        raise HTTPException(404, "File expired or deleted from Mega.nz")
    
    print(f"🎬 Generating HLS playlist for {filename}")
    
    # For now, create a simple single-segment HLS playlist that points to the Mega.nz URL
    # This allows AVPlay to treat it as an HLS stream while still using the direct URL
    playlist_content = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:3600
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:3600.0,
{mega_url}
#EXT-X-ENDLIST
"""
    
    # Return as HLS playlist with proper content type
    from fastapi.responses import Response
    return Response(
        content=playlist_content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*"
        }
    )

@app.get("/hls/{msg_id}/segment_{segment_id}.ts")
async def hls_segment(msg_id: str, segment_id: str, request: Request):
    """Handle HLS segment requests (for future advanced implementation)"""
    # For now, redirect all segment requests to the main Mega.nz URL
    # This is a simplified approach - in the future we could implement true segmentation
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    file_info = uploaded_files[str(msg_id)]
    
    if "mega_url" not in file_info:
        raise HTTPException(404, "File not available for HLS streaming")
    
    mega_url = file_info["mega_url"]
    
    print(f"🧩 HLS segment {segment_id} requested for {msg_id}, redirecting to Mega.nz")
    
    # Redirect to the main Mega.nz URL with range headers if provided
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=mega_url, status_code=302)

async def cleanup_old_mega_files(required_space_gb: float = 0):
    """Delete old files from Mega to free up space"""
    try:
        if not mega_client:
            init_mega_client()
        
        # Get current uploaded files from database
        uploaded_files = load_uploaded_files_db()
        
        if not uploaded_files:
            return 0
        
        # Sort files by upload time (oldest first)
        sorted_files = sorted(
            uploaded_files.items(),
            key=lambda x: x[1].get('uploaded_at', 0)
        )
        
        deleted_count = 0
        freed_space_gb = 0
        
        account_info = get_mega_account_info()
        current_free_gb = account_info['free_gb']
        
        print(f"🧹 Current free space: {current_free_gb:.2f}GB")
        print(f"🎯 Target free space: {MIN_FREE_SPACE_GB + required_space_gb:.2f}GB")
        
        for msg_id, file_info in sorted_files:
            # Check if we have enough free space
            if current_free_gb >= (MIN_FREE_SPACE_GB + required_space_gb):
                break
            
            try:
                # Delete from Mega
                mega_url = file_info.get('mega_url', '')
                if mega_url:
                    # Extract file ID from Mega URL
                    file_id = extract_mega_file_id(mega_url)
                    if file_id:
                        mega_client.delete(file_id)
                        
                        file_size_gb = file_info.get('file_size', 0) / (1024**3)
                        current_free_gb += file_size_gb
                        freed_space_gb += file_size_gb
                        deleted_count += 1
                        
                        print(f"🗑️ Deleted old file: {file_info.get('filename', msg_id)} ({file_size_gb:.2f}GB)")
                
                # Remove from database
                del uploaded_files[msg_id]
                
            except Exception as e:
                print(f"⚠️ Error deleting file {msg_id}: {e}")
                # Remove from database anyway to keep it clean
                del uploaded_files[msg_id]
        
        # Save updated database
        save_uploaded_files_db(uploaded_files)
        
        print(f"✅ Cleanup complete: Deleted {deleted_count} files, freed {freed_space_gb:.2f}GB")
        return deleted_count
        
    except Exception as e:
        print(f"❌ Error during Mega cleanup: {e}")
        return 0

def extract_mega_file_id(mega_url: str) -> str:
    """Extract file ID from Mega URL"""
    try:
        # Mega URLs format: https://mega.nz/file/FILE_ID#KEY
        if '/file/' in mega_url:
            file_part = mega_url.split('/file/')[1]
            return file_part.split('#')[0]
        return None
    except:
        return None

async def delete_from_mega(mega_url: str):
    """Delete a file from Mega"""
    try:
        if not mega_client:
            init_mega_client()
        
        file_id = extract_mega_file_id(mega_url)
        if file_id:
            mega_client.delete(file_id)
            print(f"🗑️ Deleted from Mega: {file_id}")
            
    except Exception as e:
        print(f"❌ Error deleting from Mega: {e}")

# Add endpoint for storage management
@app.get("/storage/info")
async def get_storage_info():
    """Get Mega storage information"""
    try:
        account_info = get_mega_account_info()
        uploaded_files = load_uploaded_files_db()
        
        return {
            "provider": "mega.nz",
            "storage": account_info,
            "files_count": len(uploaded_files),
            "min_free_space_gb": MIN_FREE_SPACE_GB
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/storage/cleanup")
async def manual_storage_cleanup():
    """Manually trigger storage cleanup"""
    try:
        deleted_count = await cleanup_old_mega_files()
        account_info = get_mega_account_info()
        
        return {
            "deleted_files": deleted_count,
            "storage_after_cleanup": account_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def init_mega_client():
    """Initialize Mega client and return folder info"""
    global mega_client
    try:
        print(f"🔧 Initializing Mega client...")
        if not MEGA_EMAIL or not MEGA_PASSWORD:
            raise Exception("Mega credentials not found. Set MEGA_EMAIL and MEGA_PASSWORD in .env")
        
        print(f"🔐 Logging in to Mega with email: {MEGA_EMAIL}")
        mega = Mega()
        mega_client = mega.login(MEGA_EMAIL, MEGA_PASSWORD)
        print(f"✅ Successfully logged into Mega.nz")
        
        # Get or create the episodes folder
        print(f"📁 Getting file list from Mega...")
        files = mega_client.get_files()
        print(f"📊 Found {len(files)} items in Mega account")
        
        folder_id = None
        
        # Look for existing folder
        print(f"🔍 Searching for folder: {MEGA_FOLDER_NAME}")
        for file_id, file_info in files.items():
            if (file_info.get('a') and 
                file_info['a'].get('n') == MEGA_FOLDER_NAME and 
                file_info.get('t') == 1):  # t=1 means folder
                folder_id = file_id
                print(f"📁 Found existing folder: {MEGA_FOLDER_NAME} (ID: {file_id})")
                break
        
        # Create folder if it doesn't exist
        if not folder_id:
            print(f"📁 Creating new folder: {MEGA_FOLDER_NAME}")
            folder_result = mega_client.create_folder(MEGA_FOLDER_NAME)
            print(f"📁 Folder creation result: {folder_result}")
            
            # Handle different return formats from mega.py
            if isinstance(folder_result, dict) and 'f' in folder_result:
                folder_id = folder_result['f'][0]['h']
            else:
                folder_id = folder_result
            print(f"📁 Created Mega folder: {MEGA_FOLDER_NAME} (ID: {folder_id})")
        else:
            print(f"📁 Using existing Mega folder: {MEGA_FOLDER_NAME} (ID: {folder_id})")
        
        return folder_id
        
    except Exception as e:
        print(f"❌ Failed to initialize Mega client: {e}")
        import traceback
        print(f"🔍 Full init error trace: {traceback.format_exc()}")
        raise e

def get_mega_account_info():
    """Get Mega account storage information"""
    try:
        if not mega_client:
            init_mega_client()
        
        quota = mega_client.get_quota()
        used_gb = quota / (1024**3)  # Convert to GB
        total_gb = 20  # Free account limit
        free_gb = total_gb - used_gb
        
        return {
            "total_gb": total_gb,
            "used_gb": round(used_gb, 2),
            "free_gb": round(free_gb, 2)
        }
        
    except Exception as e:
        print(f"❌ Error getting Mega account info: {e}")
        return {"total_gb": 20, "used_gb": 0, "free_gb": 20}

# Debug endpoints for testing
@app.get("/debug/database")
async def debug_database():
    """Debug endpoint to check database status"""
    try:
        uploaded_files = load_uploaded_files_db()
        return {
            "database_file": UPLOADED_FILES_DB,
            "database_exists": os.path.exists(UPLOADED_FILES_DB),
            "file_count": len(uploaded_files),
            "files": uploaded_files,
            "file_size_bytes": os.path.getsize(UPLOADED_FILES_DB) if os.path.exists(UPLOADED_FILES_DB) else 0
        }
    except Exception as e:
        return {
            "error": str(e),
            "database_file": UPLOADED_FILES_DB,
            "database_exists": os.path.exists(UPLOADED_FILES_DB)
        }

@app.get("/debug/mega")
async def debug_mega():
    """Debug endpoint to check Mega connection"""
    try:
        if not mega_client:
            init_mega_client()
        
        account_info = get_mega_account_info()
        files = mega_client.get_files()
        
        # Find the episodes folder
        episodes_folder = None
        for file_id, file_info in files.items():
            if (file_info.get('a') and 
                file_info['a'].get('n') == MEGA_FOLDER_NAME and 
                file_info.get('t') == 1):
                episodes_folder = {
                    "id": file_id,
                    "name": file_info['a']['n']
                }
                break
        
        return {
            "mega_connected": bool(mega_client),
            "account_info": account_info,
            "total_files": len(files),
            "episodes_folder": episodes_folder,
            "folder_name": MEGA_FOLDER_NAME
        }
    except Exception as e:
        return {
            "error": str(e),
            "mega_connected": bool(mega_client)
        }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)