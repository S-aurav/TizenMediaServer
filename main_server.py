import os
import json
import re
import asyncio
import aiohttp
import tempfile
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


# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# Telegram session configuration
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")  # For deployment

# PixelDrain configuration
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY")  # Required for uploads
PIXELDRAIN_BASE_URL = "https://pixeldrain.com/api"

# Database file for tracking uploaded files
UPLOADED_FILES_DB = "uploaded_files.json"

# Custom temporary directory (optional) - Railway provides temp space
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")  # Set in .env if needed

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
    "Doraemon": "/static/dora.png",
    "Kiteretsu": "/static/kiteretsu.jpg",
    "Ninja Hattori": "/static/ninja.jpg",
    "Ninja Hattori Returns": "/static/ninjaReturns.jpg"
}

# Initialize Telegram client
client = None
progress = {}
download_tasks = {}  # message_id -> asyncio.Task

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
    return [{"name": s, "poster": f"/static/{s}.jpg"} for s in data.keys()]

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
        
        # Check if file is uploaded to PixelDrain
        if str(msg_id) in uploaded_files:
            pixeldrain_id = uploaded_files[str(msg_id)]["pixeldrain_id"]
            
            # Optional real-time validation (slower but accurate)
            if validate:
                file_exists = await check_pixeldrain_file_exists(pixeldrain_id)
                if not file_exists:
                    # File expired - remove from database
                    expired_files.append(str(msg_id))
                    ep["downloaded"] = False
                else:
                    ep["downloaded"] = True
                    ep["pixeldrain_id"] = pixeldrain_id
            else:
                # Fast mode - trust database
                ep["downloaded"] = True
                ep["pixeldrain_id"] = pixeldrain_id
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

# PixelDrain helper functions
def load_uploaded_files_db():
    """Load the database of uploaded files"""
    if os.path.exists(UPLOADED_FILES_DB):
        with open(UPLOADED_FILES_DB, 'r') as f:
            return json.load(f)
    return {}

def save_uploaded_files_db(data):
    """Save the database of uploaded files"""
    with open(UPLOADED_FILES_DB, 'w') as f:
        json.dump(data, f, indent=2)

async def upload_to_pixeldrain(file_path: str, filename: str) -> str:
    """Upload a file to PixelDrain and return the file ID"""
    try:
        # PixelDrain requires API key for uploads (even anonymous uploads need a key)
        if not PIXELDRAIN_API_KEY:
            raise Exception("PixelDrain API key is required for uploads. Please set PIXELDRAIN_API_KEY in .env file")
        
        # Create Basic Auth (username can be empty, password is the API key)
        import base64
        credentials = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
        
        headers = {
            'Authorization': f'Basic {credentials}'
        }
        
        async with aiohttp.ClientSession() as session:
            # Read file content
            async with aiofiles.open(file_path, 'rb') as f:
                file_content = await f.read()
                
                print(f"📤 Uploading {filename} to PixelDrain...")
                
                # Use PUT method with file content as body (like curl -T)
                async with session.put(
                    f"{PIXELDRAIN_BASE_URL}/file/{filename}",
                    data=file_content,
                    headers=headers
                ) as response:
                    print(f"🔍 Upload response status: {response.status}")
                    print(f"🔍 Response headers: {dict(response.headers)}")
                    
                    if response.status == 201:
                        # Handle both JSON and text responses
                        content_type = response.headers.get('content-type', '')
                        response_text = await response.text()
                        
                        try:
                            # Try to parse as JSON first
                            result = json.loads(response_text)
                            file_id = result.get('id')
                        except json.JSONDecodeError:
                            # If not JSON, the response text might be the file ID directly
                            file_id = response_text.strip()
                        
                        print(f"✅ Upload successful! PixelDrain ID: {file_id}")
                        return file_id
                    else:
                        response_text = await response.text()
                        print(f"❌ Upload failed: {response.status} - {response_text}")
                        raise Exception(f"PixelDrain upload failed: {response.status} - {response_text}")
                        
    except Exception as e:
        print(f"❌ Error uploading to PixelDrain: {e}")
        raise e

async def get_pixeldrain_download_url(file_id: str) -> str:
    """Get direct download URL from PixelDrain"""
    return f"{PIXELDRAIN_BASE_URL}/file/{file_id}"

async def check_pixeldrain_file_exists(file_id: str) -> bool:
    """Check if a file exists on PixelDrain"""
    try:
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            credentials = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
            
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PIXELDRAIN_BASE_URL}/file/{file_id}/info",
                headers=headers
            ) as response:
                return response.status == 200
    except:
        return False

async def stream_from_pixeldrain(file_id: str, start: int = 0, end: int = None):
    """Stream video file from PixelDrain with range support"""
    try:
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            credentials = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
        
        # Add range header if specified
        if start > 0 or end is not None:
            if end is not None:
                headers['Range'] = f'bytes={start}-{end}'
            else:
                headers['Range'] = f'bytes={start}-'
        
        # Use shorter timeout and larger chunks for better TV compatibility
        timeout = aiohttp.ClientTimeout(total=120, connect=10)  # 2 min total, 10s connect
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{PIXELDRAIN_BASE_URL}/file/{file_id}",
                headers=headers
            ) as response:
                if response.status in [200, 206]:  # 200 OK or 206 Partial Content
                    chunk_count = 0
                    bytes_sent = 0
                    
                    try:
                        # Use larger chunks (4MB) for better streaming performance
                        async for chunk in response.content.iter_chunked(4 * 1024 * 1024):  # 4MB chunks
                            if chunk:  # Only yield non-empty chunks
                                chunk_count += 1
                                bytes_sent += len(chunk)
                                
                                # Log progress every 25 chunks (100MB)
                                if chunk_count % 25 == 0:
                                    print(f"📊 Streamed {chunk_count} chunks ({bytes_sent // (1024*1024)} MB)")
                                
                                yield chunk
                            else:
                                # End of stream
                                print(f"✅ Stream complete: {chunk_count} chunks, {bytes_sent} bytes")
                                return
                                
                    except asyncio.CancelledError:
                        print(f"🛑 Stream cancelled by client after {bytes_sent // (1024*1024)} MB")
                        return
                    except Exception as stream_error:
                        print(f"❌ Error during streaming after {bytes_sent // (1024*1024)} MB: {stream_error}")
                        return
                        
                else:
                    response_text = await response.text()
                    raise Exception(f"PixelDrain streaming failed: {response.status} - {response_text}")
                    
    except asyncio.CancelledError:
        print("🛑 Stream cancelled")
        raise
    except Exception as e:
        print(f"❌ Error streaming from PixelDrain: {e}")
        raise

# Download and upload to PixelDrain
@app.get("/download")
async def trigger_download(url: str):
    channel, msg_id = parse_telegram_url(url)
    if not channel or not msg_id:
        raise HTTPException(400, "Invalid Telegram URL")

    # Check if already uploaded to PixelDrain
    uploaded_files = load_uploaded_files_db()
    if str(msg_id) in uploaded_files:
        pixeldrain_id = uploaded_files[str(msg_id)]["pixeldrain_id"]
        # Verify file still exists on PixelDrain
        if await check_pixeldrain_file_exists(pixeldrain_id):
            return {"status": "already_uploaded", "pixeldrain_id": pixeldrain_id}
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
        temp_file = None
        try:
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
            
            # Create temporary file for download
            if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
                # Use custom temp directory if specified
                os.makedirs(CUSTOM_TEMP_DIR, exist_ok=True)
                temp_path = os.path.join(CUSTOM_TEMP_DIR, f"temp_{msg_id}{file_ext}")
            else:
                # Use system temp directory (default)
                with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
                    temp_path = temp_file.name
            
            print(f"⬇️ Downloading {filename} to temporary file...")
            await client.download_media(message, file=temp_path)
            print(f"✅ Download complete: {filename}")
            
            # Upload to PixelDrain
            pixeldrain_id = await upload_to_pixeldrain(temp_path, filename)
            
            # Save to database
            uploaded_files = load_uploaded_files_db()
            uploaded_files[str(msg_id)] = {
                "pixeldrain_id": pixeldrain_id,
                "filename": filename,
                "uploaded_at": asyncio.get_event_loop().time()
            }
            save_uploaded_files_db(uploaded_files)
            
            print(f"✅ Successfully uploaded {filename} to PixelDrain: {pixeldrain_id}")
            
        except Exception as e:
            print(f"❌ Failed to download/upload {msg_id}: {e}")
        finally:
            # Clean up temporary file
            if temp_file and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except:
                    pass
            download_tasks.pop(msg_id, None)

    download_tasks[msg_id] = asyncio.create_task(do_download_and_upload())
    return {"status": "uploading"}

@app.get("/downloads")
async def list_downloads():
    uploaded_files = load_uploaded_files_db()
    files = []
    
    for msg_id, file_info in uploaded_files.items():
        # Verify file still exists on PixelDrain (optional, might be slow)
        files.append({
            "msg_id": msg_id,
            "pixeldrain_id": file_info["pixeldrain_id"],
            "filename": file_info["filename"]
        })
    
    return files

@app.get("/stream_local/{msg_id}")
async def stream_local(msg_id: str, request: Request):
    # Get PixelDrain file ID from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    pixeldrain_id = uploaded_files[str(msg_id)]["pixeldrain_id"]
    filename = uploaded_files[str(msg_id)]["filename"]
    
    # Verify file still exists on PixelDrain
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    print(f"📺 Streaming from PixelDrain: {filename} (ID: {pixeldrain_id})")
    
    # Handle range requests for video seeking
    range_header = request.headers.get('Range')
    start = 0
    end = None
    
    if range_header:
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if range_match:
            start = int(range_match.group(1))
            if range_match.group(2):
                end = int(range_match.group(2))
            print(f"🎯 Range request: {start}-{end if end else 'end'}")
    
    try:
        # Get file info first to determine size for proper headers
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            credentials = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
            
        async with aiohttp.ClientSession() as session:
            # Get file info
            async with session.get(
                f"{PIXELDRAIN_BASE_URL}/file/{pixeldrain_id}/info",
                headers=headers
            ) as info_response:
                if info_response.status == 200:
                    file_info = await info_response.json()
                    file_size = file_info.get('size', 0)
                    print(f"📊 File size: {file_size} bytes")
                else:
                    # Fallback - proceed without size info
                    file_size = None
                    print("⚠️ Could not get file size, proceeding without size info")
        
        # Prepare response headers
        response_headers = {
            'Accept-Ranges': 'bytes',
            'Content-Type': 'video/mp4',
            'Cache-Control': 'no-cache'
        }
        
        if range_header and file_size:
            if end is None:
                end = file_size - 1
            end = min(end, file_size - 1)
            
            response_headers.update({
                'Content-Range': f'bytes {start}-{end}/{file_size}',
                'Content-Length': str(end - start + 1)
            })
            
            print(f"📤 Sending partial content: {start}-{end} ({end - start + 1} bytes)")
            
            return StreamingResponse(
                stream_from_pixeldrain(pixeldrain_id, start, end),
                status_code=206,
                headers=response_headers
            )
        else:
            if file_size:
                response_headers['Content-Length'] = str(file_size)
            
            print(f"📤 Sending full content ({file_size} bytes)" if file_size else "📤 Sending full content")
            
            return StreamingResponse(
                stream_from_pixeldrain(pixeldrain_id),
                status_code=200,
                headers=response_headers
            )
            
    except Exception as e:
        print(f"❌ Error setting up PixelDrain stream: {e}")
        raise HTTPException(500, f"Streaming error: {str(e)}")

@app.head("/stream_local/{msg_id}")
async def stream_local_head(msg_id: str):
    # Get PixelDrain file ID from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    pixeldrain_id = uploaded_files[str(msg_id)]["pixeldrain_id"]
    
    # Verify file still exists and get info
    try:
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            credentials = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
            
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PIXELDRAIN_BASE_URL}/file/{pixeldrain_id}/info",
                headers=headers
            ) as response:
                if response.status == 200:
                    file_info = await response.json()
                    file_size = file_info.get('size', 0)
                    
                    response_headers = {
                        'Accept-Ranges': 'bytes',
                        'Content-Length': str(file_size),
                        'Content-Type': 'video/mp4',
                        'Cache-Control': 'no-cache'
                    }
                    
                    return JSONResponse(content={}, headers=response_headers)
                else:
                    raise HTTPException(404, "File not found on PixelDrain")
    except Exception as e:
        print(f"❌ Error getting PixelDrain file info: {e}")
        raise HTTPException(500, f"File info error: {str(e)}")

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
        pixeldrain_id = file_info["pixeldrain_id"]
        filename = file_info.get("filename", f"{msg_id}.mkv")
        
        # Check if file still exists on PixelDrain
        if not await check_pixeldrain_file_exists(pixeldrain_id):
            expired_files.append(msg_id)
            print(f"🗑️ Found expired file: {filename} (ID: {pixeldrain_id})")
    
    # Remove expired files from database
    if expired_files:
        for msg_id in expired_files:
            del uploaded_files[msg_id]
        
        save_uploaded_files_db(uploaded_files)
        print(f"✅ Cleaned up {len(expired_files)} expired files from database")
    else:
        print("✅ No expired files found")
    
    return expired_files

async def validate_all_pixeldrain_files():
    """Validate all files in database and return status"""
    uploaded_files = load_uploaded_files_db()
    validation_results = {
        "total_files": len(uploaded_files),
        "valid_files": 0,
        "expired_files": 0,
        "expired_list": []
    }
    
    for msg_id, file_info in uploaded_files.items():
        pixeldrain_id = file_info["pixeldrain_id"]
        filename = file_info.get("filename", f"{msg_id}.mkv")
        
        if await check_pixeldrain_file_exists(pixeldrain_id):
            validation_results["valid_files"] += 1
        else:
            validation_results["expired_files"] += 1
            validation_results["expired_list"].append({
                "msg_id": msg_id,
                "filename": filename,
                "pixeldrain_id": pixeldrain_id
            })
    
    return validation_results

# Background task for periodic cleanup
import asyncio
from contextlib import asynccontextmanager

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
        results = await validate_all_pixeldrain_files()
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
            "note": "Temporary files are deleted after upload to PixelDrain"
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
    """Get direct PixelDrain streaming URL instead of proxying through our server"""
    # Get PixelDrain file ID from database
    uploaded_files = load_uploaded_files_db()
    
    if str(msg_id) not in uploaded_files:
        raise HTTPException(404, "File not uploaded yet")
    
    pixeldrain_id = uploaded_files[str(msg_id)]["pixeldrain_id"]
    filename = uploaded_files[str(msg_id)]["filename"]
    
    # Verify file still exists on PixelDrain
    if not await check_pixeldrain_file_exists(pixeldrain_id):
        # File expired or deleted, remove from database
        del uploaded_files[str(msg_id)]
        save_uploaded_files_db(uploaded_files)
        raise HTTPException(404, "File expired or deleted from PixelDrain")
    
    # Return direct PixelDrain streaming URL
    direct_url = f"{PIXELDRAIN_BASE_URL}/file/{pixeldrain_id}"
    
    print(f"🔗 Providing direct PixelDrain URL for {filename}: {direct_url}")
    
    return {
        "direct_url": direct_url,
        "pixeldrain_id": pixeldrain_id,
        "filename": filename,
        "note": "Use this URL directly in your video player for better streaming performance"
    }

# Add health check endpoint for deployment
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
            "health": "/health"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
