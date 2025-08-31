import os
import json
import re
import asyncio
import tempfile
import threading
import time
import subprocess
import shutil
import httpx
from pathlib import Path
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# Import our custom modules
from database_manager import load_uploaded_files_db, save_uploaded_files_db, load_uploaded_files_db_async, save_uploaded_files_db_async, load_video_data, set_gist_manager
from utils import parse_telegram_url, get_file_extension_from_message, get_file_size_from_message

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

# File access limits (previously for PixelDrain, now for general usage)
MAX_ACCESS_COUNT = 4  # Maximum times a file can be accessed before re-download needed

# Database files for tracking episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

# Local cache configuration
CACHE_DIR = os.path.join(os.getcwd(), "cache")
DOWNLOADS_DIR = os.path.join(os.getcwd(), "downloads")  
TEMP_DIR = os.path.join(os.getcwd(), "tmp")

# Streaming configuration
STREAMING_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB buffer before starting stream

# Direct file serving configuration (Primary approach)
DIRECT_FILES_DIR = os.path.join(os.getcwd(), "direct_files")
MAX_STORAGE_MB = 1800  # Maximum storage in MB for ephemeral storage
direct_file_status = {}  # Track download status for direct files

# Ensure directories exist
os.makedirs(DIRECT_FILES_DIR, exist_ok=True)

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
cleanup_task = None

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

# ============= DIRECT FILE SERVING FUNCTIONS (Primary Approach) =============

# async def initialize_hls_streaming():
#     """Initialize HLS streaming system"""
#     global hls_cleanup_task
    
#     try:
#         # Create HLS output directory
#         os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)
#         print(f"📁 HLS output directory: {HLS_OUTPUT_DIR}")
        
#         # Check FFmpeg availability
#         ffmpeg_available = await check_ffmpeg_available()
#         if ffmpeg_available:
#             print("✅ FFmpeg is available for HLS conversion")
#         else:
#             print("⚠️ FFmpeg not found - HLS streaming will not work")
#             return False
        
#         # Start HLS cleanup task
#         hls_cleanup_task = asyncio.create_task(periodic_hls_cleanup())
#         print("🧹 Started HLS cleanup task")
        
#         return True
        
#     except Exception as e:
#         print(f"❌ Failed to initialize HLS streaming: {e}")
#         return False

# async def check_ffmpeg_available():
#     """Check if FFmpeg is available"""
#     try:
#         process = await asyncio.create_subprocess_exec(
#             FFMPEG_PATH, "-version",
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE
#         )
#         stdout, stderr = await process.communicate()
#         return process.returncode == 0
#     except Exception:
#         return False

# async def cleanup_all_hls_processes():
#     """Stop all running HLS processes"""
#     global hls_processes, hls_status
    
#     print("🛑 Stopping all HLS processes...")
#     for msg_id, process in hls_processes.items():
#         try:
#             if process and process.poll() is None:
#                 process.terminate()
#                 print(f"🛑 Terminated HLS process for {msg_id}")
#         except Exception as e:
#             print(f"⚠️ Error terminating process {msg_id}: {e}")
    
#     hls_processes.clear()
#     hls_status.clear()

# async def periodic_hls_cleanup():
#     """Clean up old HLS files periodically"""
#     while True:
#         try:
#             await asyncio.sleep(300)  # Check every 5 minutes
#             await cleanup_old_hls_files()
#         except Exception as e:
#             print(f"❌ Error in HLS cleanup: {e}")

# async def cleanup_old_hls_files():
#     """Remove HLS files older than HLS_CLEANUP_AFTER seconds"""
#     try:
#         current_time = time.time()
#         cleaned_files = 0
        
#         for msg_id_dir in os.listdir(HLS_OUTPUT_DIR):
#             dir_path = os.path.join(HLS_OUTPUT_DIR, msg_id_dir)
#             if os.path.isdir(dir_path):
#                 # Check if directory is old enough to clean
#                 dir_mtime = os.path.getmtime(dir_path)
#                 if current_time - dir_mtime > HLS_CLEANUP_AFTER:
#                     # Stop any running process for this msg_id
#                     if msg_id_dir in hls_processes:
#                         process = hls_processes[msg_id_dir]
#                         if process and process.poll() is None:
#                             process.terminate()
#                         del hls_processes[msg_id_dir]
                    
#                     if msg_id_dir in hls_status:
#                         del hls_status[msg_id_dir]
                    
#                     # Remove directory
#                     shutil.rmtree(dir_path)
#                     cleaned_files += 1
#                     print(f"🧹 Cleaned up old HLS stream: {msg_id_dir}")
        
#         if cleaned_files > 0:
#             print(f"🧹 HLS cleanup completed: {cleaned_files} old streams removed")
            
#     except Exception as e:
#         print(f"❌ Error cleaning HLS files: {e}")

# async def start_hls_conversion(msg_id: str, input_source: str, quality: str = "720p"):
#     """Start HLS conversion for a video source"""
#     global hls_processes, hls_status
    
#     try:
#         # Create output directory for this stream
#         output_dir = os.path.join(HLS_OUTPUT_DIR, msg_id)
#         os.makedirs(output_dir, exist_ok=True)
        
#         # Set quality parameters
#         quality_settings = HLS_QUALITIES.get(quality, HLS_QUALITIES["720p"])
        
#         # FFmpeg command for HLS conversion
#         ffmpeg_cmd = [
#             FFMPEG_PATH,
#             "-i", input_source,
#             "-c:v", "libx264",
#             "-preset", "fast",
#             "-crf", "23",
#             "-c:a", "aac",
#             "-b:v", quality_settings["bitrate"],
#             "-b:a", quality_settings["audio_bitrate"],
#             "-s", quality_settings["resolution"],
#             "-f", "hls",
#             "-hls_time", str(HLS_SEGMENT_DURATION),
#             "-hls_list_size", str(HLS_PLAYLIST_SIZE),
#             "-hls_flags", "independent_segments",
#             "-hls_playlist_type", "vod",  # Video on Demand - keeps all segments
#             "-hls_segment_filename", os.path.join(output_dir, "segment_%03d.ts"),
#             os.path.join(output_dir, "playlist.m3u8")
#         ]
        
#         print(f"🎬 Starting HLS conversion for {msg_id} ({quality})")
#         print(f"📁 Output directory: {output_dir}")
#         print(f"🔧 FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
#         # Start FFmpeg process
#         process = await asyncio.create_subprocess_exec(
#             *ffmpeg_cmd,
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE
#         )
        
#         hls_processes[msg_id] = process
#         hls_status[msg_id] = {
#             "status": "converting",
#             "quality": quality,
#             "output_dir": output_dir,
#             "started_at": time.time()
#         }
        
#         print(f"✅ FFmpeg process started for {msg_id} (PID: {process.pid})")
#         return True
        
#     except Exception as e:
#         error_msg = f"Failed to start HLS conversion: {str(e)}"
#         print(f"❌ {error_msg}")
#         print(f"❌ Error type: {type(e).__name__}")
#         import traceback
#         print(f"❌ Traceback: {traceback.format_exc()}")
#         hls_status[msg_id] = {"status": "error", "error": error_msg}
        # return False

# async def download_and_convert_to_hls(msg_id: str, quality: str = "720p"):
#     """Download from Telegram and convert to HLS"""
#     global hls_status, client
    
#     try:
#         print(f"🎬 Starting HLS conversion process for {msg_id} ({quality})")
        
#         # Load video data to find the Telegram message
#         data = await load_video_data()
#         episode_info = None
        
#         # Find the episode with this msg_id
#         for series_name, series_data in data.items():
#             for season_name, episodes in series_data.items():
#                 for episode in episodes:
#                     _, ep_msg_id = parse_telegram_url(episode["url"])
#                     if str(ep_msg_id) == str(msg_id):
#                         episode_info = {
#                             "channel": episode["url"].split("/")[-2],
#                             "msg_id": ep_msg_id,
#                             "title": episode.get("title", f"Episode {msg_id}")
#                         }
#                         break
#                 if episode_info:
#                     break
#             if episode_info:
#                 break
        
#         if not episode_info:
#             error_msg = "Episode not found in catalog"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
#             return False
        
#         print(f"📁 Found episode: {episode_info['title']} from channel {episode_info['channel']}")
        
#         # Check if client is connected
#         if not client or not client.is_connected():
#             error_msg = "Telegram client not connected"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
#             return False
        
#         # Download from Telegram to temp location
#         print(f"📥 Starting download from Telegram...")
#         hls_status[msg_id] = {"status": "downloading", "progress": 0}
        
#         temp_file = os.path.join(tempfile.gettempdir(), f"hls_temp_{msg_id}.mp4")
        
#         # Get message from Telegram
#         try:
#             message = await client.get_messages(episode_info["channel"], ids=episode_info["msg_id"])
#             if not message:
#                 error_msg = "Message not found on Telegram"
#                 print(f"❌ {error_msg}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#                 return False
            
#             # Check if it's a media message
#             if not (message.video or message.document):
#                 error_msg = "Message does not contain video file"
#                 print(f"❌ {error_msg}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#                 return False
            
#             # Get file size for progress tracking
#             file_size = message.file.size if message.file else 0
#             print(f"📏 File size: {file_size / (1024*1024):.1f} MB")
            
#             # Download with progress tracking
#             downloaded_size = 0
            
#             def progress_callback(current, total):
#                 nonlocal downloaded_size
#                 downloaded_size = current
#                 if total > 0:
#                     progress = (current / total) * 50  # 50% for download
#                     hls_status[msg_id] = {"status": "downloading", "progress": progress}
#                     # Log progress every 25MB to reduce spam
#                     if current % (25 * 1024 * 1024) < 1024 * 1024:
#                         print(f"📊 Downloaded: {current / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB ({progress:.1f}%)")
            
#             # Download the file with timeout protection
#             print(f"💾 Downloading to: {temp_file}")
            
#             try:
#                 # Use asyncio.wait_for to add timeout protection
#                 await asyncio.wait_for(
#                     client.download_media(message, file=temp_file, progress_callback=progress_callback),
#                     timeout=1800  # 30 minutes timeout for large files
#                 )
#             except asyncio.TimeoutError:
#                 error_msg = "Download timeout - file too large or connection slow"
#                 print(f"❌ {error_msg}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#                 return False
            
#             if not os.path.exists(temp_file):
#                 error_msg = "Download failed - file not created"
#                 print(f"❌ {error_msg}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#                 return False
            
#             print(f"✅ Downloaded {msg_id} to temp file: {temp_file} ({os.path.getsize(temp_file)} bytes)")
            
#         except Exception as download_error:
#             error_msg = f"Telegram download failed: {str(download_error)}"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
#             return False
        
#         # Start HLS conversion
#         print(f"🎬 Starting HLS conversion...")
#         hls_status[msg_id] = {"status": "converting", "progress": 50}
#         success = await start_hls_conversion(msg_id, temp_file, quality)
        
#         if success:
#             print(f"✅ HLS conversion started successfully")
#             # Monitor conversion progress
#             await monitor_hls_conversion(msg_id)
#         else:
#             error_msg = "Failed to start HLS conversion"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
        
#         # Clean up temp file
#         try:
#             if os.path.exists(temp_file):
#                 os.unlink(temp_file)
#                 print(f"🧹 Cleaned up temp file: {temp_file}")
#         except Exception as cleanup_error:
#             print(f"⚠️ Temp file cleanup warning: {cleanup_error}")
        
#         return success
        
#     except Exception as e:
#         error_msg = f"Unexpected error: {str(e)}"
#         print(f"❌ Error in download_and_convert_to_hls for {msg_id}: {e}")
#         print(f"❌ Error type: {type(e).__name__}")
#         import traceback
#         print(f"❌ Traceback: {traceback.format_exc()}")
#         hls_status[msg_id] = {"status": "error", "error": error_msg}
#         return False

# async def download_and_convert_to_hls_progressive(msg_id: str, quality: str = "720p"):
#     """Progressive HLS: Download and convert with chunked approach"""
#     global hls_status, client
    
#     try:
#         print(f"🚀 Starting CHUNKED PROGRESSIVE HLS conversion for {msg_id} ({quality})")
        
#         # Load video data to find the Telegram message
#         data = await load_video_data()
#         episode_info = None
        
#         # Find the episode with this msg_id
#         for series_name, series_data in data.items():
#             for season_name, episodes in series_data.items():
#                 for episode in episodes:
#                     _, ep_msg_id = parse_telegram_url(episode["url"])
#                     if ep_msg_id == int(msg_id):
#                         episode_info = {
#                             "url": episode["url"],
#                             "title": episode.get("title", f"Episode {msg_id}"),
#                             "series": series_name,
#                             "season": season_name
#                         }
#                         break
#                 if episode_info:
#                     break
#             if episode_info:
#                 break
        
#         if not episode_info:
#             error_msg = f"Episode with msg_id {msg_id} not found"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
#             return False
        
#         # Set up progressive conversion
#         hls_status[msg_id] = {
#             "status": "initializing",
#             "progress": 0,
#             "stage": "preparing_chunked_conversion"
#         }
        
#         # Create output directory
#         output_dir = os.path.join(HLS_OUTPUT_DIR, msg_id)
#         os.makedirs(output_dir, exist_ok=True)
        
#         # Create temporary file for complete download first
#         os.makedirs(TEMP_DIR, exist_ok=True)
#         temp_file = os.path.join(TEMP_DIR, f"chunked_{msg_id}.mkv")
        
#         # Get Telegram message
#         channel, telegram_msg_id = parse_telegram_url(episode_info["url"])
#         message = await client.get_messages(channel, ids=telegram_msg_id)
        
#         if not message or not message.document:
#             error_msg = "Message or document not found"
#             print(f"❌ {error_msg}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
#             return False
        
#         total_size = message.document.size
        
#         # Extract filename properly from document attributes
#         filename = f"{msg_id}.mkv"  # Default filename
#         if message.document.attributes:
#             for attr in message.document.attributes:
#                 if hasattr(attr, 'file_name') and attr.file_name:
#                     filename = attr.file_name
#                     break
        
#         print(f"📁 Chunked Progressive File: {filename} ({total_size / 1024 / 1024:.1f} MB)")
        
#         # Start download with chunked HLS conversion
#         hls_status[msg_id] = {
#             "status": "downloading_and_converting",
#             "progress": 0,
#             "total_size": total_size,
#             "filename": filename,
#             "stage": "chunked_processing"
#         }
        
#         # Download in chunks and convert each chunk
#         chunk_size = 50 * 1024 * 1024  # 50MB chunks
#         downloaded_size = 0
#         chunk_number = 0
        
#         def progress_callback(current, total):
#             nonlocal downloaded_size
#             downloaded_size = current
#             progress = (current / total) * 100
#             hls_status[msg_id]["progress"] = progress
#             if int(progress) % 5 == 0:  # Log every 5%
#                 print(f"📊 Progressive: {current / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB ({progress:.1f}%)")
        
#         # Start download with immediate HLS conversion
#         conversion_task = asyncio.create_task(
#             download_with_immediate_hls_conversion(
#                 message, temp_file, output_dir, quality, msg_id, progress_callback
#             )
#         )
        
#         # Wait for conversion to complete
#         success = await conversion_task
        
#         if success:
#             # Verify output
#             playlist_path = os.path.join(output_dir, "playlist.m3u8")
#             if os.path.exists(playlist_path):
#                 segments = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
#                 hls_status[msg_id] = {
#                     "status": "completed",
#                     "progress": 100,
#                     "playlist_url": f"/hls/{msg_id}/playlist.m3u8",
#                     "segments": len(segments),
#                     "type": "chunked_progressive"
#                 }
#                 print(f"✅ Chunked Progressive HLS conversion completed: {len(segments)} segments")
                
#                 # Clean up temp file
#                 try:
#                     if os.path.exists(temp_file):
#                         os.unlink(temp_file)
#                 except:
#                     pass
                
#                 return True
#             else:
#                 error_msg = "Chunked progressive playlist file not created"
#                 print(f"❌ {error_msg}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#                 return False
#         else:
#             hls_status[msg_id] = {"status": "error", "error": "Chunked conversion failed"}
#             return False
            
#     except Exception as e:
#         error_msg = f"Chunked progressive conversion error: {str(e)}"
#         print(f"❌ {error_msg}")
#         import traceback
#         print(f"❌ Traceback: {traceback.format_exc()}")
#         hls_status[msg_id] = {"status": "error", "error": error_msg}
#         return False

# async def download_with_immediate_hls_conversion(message, temp_file, output_dir, quality, msg_id, progress_callback):
#     """Download and start HLS conversion immediately, then update as more data arrives"""
#     try:
#         print(f"🔄 Starting immediate HLS conversion strategy...")
        
#         # Download the file completely first (faster and more reliable)
#         await client.download_media(
#             message, 
#             file=temp_file, 
#             progress_callback=progress_callback
#         )
        
#         print(f"✅ Download completed, starting HLS conversion...")
        
#         # Now convert to HLS (this will be fast since file is complete)
#         success = await start_hls_conversion(msg_id, temp_file, quality)
        
#         if success:
#             print(f"✅ HLS conversion completed successfully")
#             return True
#         else:
#             print(f"❌ HLS conversion failed")
#             return False
            
#     except Exception as e:
#         print(f"❌ Error in immediate HLS conversion: {e}")
#         return False

# async def start_progressive_ffmpeg(msg_id: str, input_file: str, output_dir: str, quality: str):
#     """Start FFmpeg process that can handle growing input file"""
#     quality_settings = HLS_QUALITIES.get(quality, HLS_QUALITIES["720p"])
    
#     ffmpeg_cmd = [
#         FFMPEG_PATH,
#         "-re",  # Read input at native frame rate
#         "-i", input_file,
#         "-c:v", "libx264",
#         "-preset", "veryfast",  # Fast encoding for real-time
#         "-crf", "23",
#         "-c:a", "aac",
#         "-b:v", quality_settings["bitrate"],
#         "-b:a", quality_settings["audio_bitrate"],
#         "-s", quality_settings["resolution"],
#         "-f", "hls",
#         "-hls_time", "6",  # 6-second segments for faster startup
#         "-hls_list_size", "0",  # Keep all segments
#         "-hls_flags", "independent_segments",
#         "-hls_playlist_type", "event",  # Event playlist grows over time
#         "-hls_segment_filename", os.path.join(output_dir, "segment_%05d.ts"),
#         os.path.join(output_dir, "playlist.m3u8")
#     ]
    
#     try:
#         print(f"🔧 Starting progressive FFmpeg")
#         process = await asyncio.create_subprocess_exec(
#             *ffmpeg_cmd,
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE
#         )
#         print(f"✅ Progressive FFmpeg started (PID: {process.pid})")
#         return process
#     except Exception as e:
#         print(f"❌ Error starting progressive FFmpeg: {e}")
#         return None

# async def monitor_hls_conversion(msg_id: str):
#     """Monitor HLS conversion progress"""
#     global hls_processes, hls_status
    
#     if msg_id not in hls_processes:
#         error_msg = "No conversion process found"
#         print(f"❌ {error_msg}")
#         hls_status[msg_id] = {"status": "error", "error": error_msg}
#         return
    
#     process = hls_processes[msg_id]
#     print(f"⏱️ Monitoring HLS conversion for {msg_id} (PID: {process.pid})")
    
#     try:
#         # Wait for process to complete
#         stdout, stderr = await process.communicate()
        
#         print(f"🏁 FFmpeg process completed for {msg_id} with return code: {process.returncode}")
        
#         if process.returncode == 0:
#             # Check if playlist file was created
#             output_dir = hls_status[msg_id].get("output_dir")
#             playlist_path = os.path.join(output_dir, "playlist.m3u8") if output_dir else None
            
#             if playlist_path and os.path.exists(playlist_path):
#                 # Count segments
#                 with open(playlist_path, 'r') as f:
#                     content = f.read()
#                     segments = [line for line in content.split('\n') if line.endswith('.ts')]
                
#                 hls_status[msg_id] = {
#                     "status": "ready",
#                     "progress": 100,
#                     "quality": hls_status[msg_id].get("quality", "720p"),
#                     "output_dir": output_dir,
#                     "completed_at": time.time(),
#                     "segments_count": len(segments)
#                 }
#                 print(f"✅ HLS conversion completed for {msg_id}")
#                 print(f"📺 Created {len(segments)} video segments")
#                 print(f"📋 Playlist: {playlist_path}")
#             else:
#                 error_msg = "Playlist file not created"
#                 print(f"❌ {error_msg}")
#                 if stdout:
#                     print(f"📤 FFmpeg stdout: {stdout.decode()}")
#                 if stderr:
#                     print(f"📥 FFmpeg stderr: {stderr.decode()}")
#                 hls_status[msg_id] = {"status": "error", "error": error_msg}
#         else:
#             error_msg = f"FFmpeg failed with return code {process.returncode}"
#             print(f"❌ {error_msg}")
#             if stdout:
#                 print(f"📤 FFmpeg stdout: {stdout.decode()}")
#             if stderr:
#                 print(f"📥 FFmpeg stderr: {stderr.decode()}")
#             hls_status[msg_id] = {"status": "error", "error": error_msg}
    
#     except Exception as e:
#         error_msg = f"Monitor error: {str(e)}"
#         print(f"❌ Error monitoring HLS conversion for {msg_id}: {e}")
#         import traceback
#         print(f"❌ Traceback: {traceback.format_exc()}")
#         hls_status[msg_id] = {"status": "error", "error": error_msg}
    
#     finally:
#         # Clean up process reference
#         if msg_id in hls_processes:
#             del hls_processes[msg_id]
#             print(f"🧹 Cleaned up process reference for {msg_id}")

# ============= End HLS Functions =============

# === DIRECT FILE SERVING FUNCTIONS (New Primary Approach) ===

def get_storage_usage() -> int:
    """Get current storage usage in bytes"""
    total_size = 0
    if os.path.exists(DIRECT_FILES_DIR):
        for filename in os.listdir(DIRECT_FILES_DIR):
            filepath = os.path.join(DIRECT_FILES_DIR, filename)
            if os.path.isfile(filepath):
                total_size += os.path.getsize(filepath)
    return total_size

def get_files_by_age():
    """Get list of files sorted by age (oldest first)"""
    files = []
    if os.path.exists(DIRECT_FILES_DIR):
        for filename in os.listdir(DIRECT_FILES_DIR):
            filepath = os.path.join(DIRECT_FILES_DIR, filename)
            if os.path.isfile(filepath):
                mtime = os.path.getmtime(filepath)
                size = os.path.getsize(filepath)
                files.append((filepath, mtime, size, filename))
    
    # Sort by modification time (oldest first)
    files.sort(key=lambda x: x[1])
    return files

async def cleanup_old_files(target_size: int):
    """Remove old files until storage is under target size"""
    current_size = get_storage_usage()
    print(f"🧹 Current storage: {current_size / 1024 / 1024:.1f} MB")
    
    if current_size <= target_size:
        return
    
    files_by_age = get_files_by_age()
    freed_space = 0
    
    for filepath, mtime, size, filename in files_by_age:
        try:
            os.unlink(filepath)
            freed_space += size
            print(f"🗑️ Deleted old file: {filename} ({size / 1024 / 1024:.1f} MB)")
            
            # Remove from status tracking
            msg_id = filename.split('_')[0] if '_' in filename else filename.split('.')[0]
            if msg_id in direct_file_status:
                del direct_file_status[msg_id]
            
            if current_size - freed_space <= target_size:
                break
                
        except Exception as e:
            print(f"⚠️ Error deleting {filepath}: {e}")
    
    new_size = get_storage_usage()
    print(f"✅ Cleanup complete. Storage: {new_size / 1024 / 1024:.1f} MB (freed {freed_space / 1024 / 1024:.1f} MB)")

async def ensure_storage_space(required_size: int):
    """Ensure we have enough space for new file"""
    max_storage_bytes = MAX_STORAGE_MB * 1024 * 1024
    current_size = get_storage_usage()
    
    if current_size + required_size > max_storage_bytes:
        # Need to free up space
        target_size = max_storage_bytes - required_size - (100 * 1024 * 1024)  # Leave 100MB buffer
        await cleanup_old_files(target_size)

def get_direct_file_path(msg_id: str) -> str:
    """Get file path for a message ID"""
    if msg_id in direct_file_status and "filepath" in direct_file_status[msg_id]:
        filepath = direct_file_status[msg_id]["filepath"]
        if os.path.exists(filepath):
            return filepath
    
    # Check if file exists with pattern
    if os.path.exists(DIRECT_FILES_DIR):
        for filename in os.listdir(DIRECT_FILES_DIR):
            if filename.startswith(f"{msg_id}_"):
                return os.path.join(DIRECT_FILES_DIR, filename)
    
    return None

async def download_direct_file(msg_id: str):
    """Download file from Telegram for direct serving"""
    global client, direct_file_status
    
    try:
        print(f"📁 Starting direct download for msg_id: {msg_id}")
        
        # Check if file already exists
        existing_path = get_direct_file_path(msg_id)
        if existing_path:
            print(f"✅ File already downloaded: {existing_path}")
            return True
        
        # Set initial status
        direct_file_status[msg_id] = {
            "status": "initializing",
            "progress": 0
        }
        
        # Load video data to find the episode
        data = await load_video_data()
        episode_info = None
        
        for series_name, series_data in data.items():
            for season_name, episodes in series_data.items():
                for episode in episodes:
                    _, ep_msg_id = parse_telegram_url(episode["url"])
                    if ep_msg_id == int(msg_id):
                        episode_info = {
                            "url": episode["url"],
                            "title": episode.get("title", f"Episode {msg_id}"),
                            "series": series_name,
                            "season": season_name
                        }
                        break
                if episode_info:
                    break
            if episode_info:
                break
        
        if not episode_info:
            print(f"❌ Episode with msg_id {msg_id} not found")
            direct_file_status[msg_id] = {"status": "error", "error": "Episode not found"}
            return False
        
        # Get Telegram message
        channel, telegram_msg_id = parse_telegram_url(episode_info["url"])
        message = await client.get_messages(channel, ids=telegram_msg_id)
        
        if not message or not message.document:
            print(f"❌ Message or document not found for {msg_id}")
            direct_file_status[msg_id] = {"status": "error", "error": "Message not found"}
            return False
        
        # Get file info
        total_size = message.document.size
        
        # Extract filename
        filename = f"{msg_id}.mkv"
        if message.document.attributes:
            for attr in message.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    filename = attr.file_name
                    break
        
        print(f"📁 Direct download: {filename} ({total_size / 1024 / 1024:.1f} MB)")
        
        # Ensure storage space
        await ensure_storage_space(total_size)
        
        # Set downloading status
        direct_file_status[msg_id] = {
            "status": "downloading",
            "progress": 0,
            "total_size": total_size,
            "filename": filename
        }
        
        # Download file
        filepath = os.path.join(DIRECT_FILES_DIR, f"{msg_id}_{filename}")
        
        def progress_callback(current, total):
            progress = (current / total) * 100
            direct_file_status[msg_id]["progress"] = progress
            if int(progress) % 10 == 0:  # Log every 10%
                print(f"📊 Direct download: {current / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB ({progress:.1f}%)")
        
        # Download the file
        await client.download_media(
            message,
            file=filepath,
            progress_callback=progress_callback
        )
        
        # Update status
        direct_file_status[msg_id] = {
            "status": "ready",
            "progress": 100,
            "filepath": filepath,
            "filename": filename,
            "size": total_size,
            "download_url": f"/direct/{msg_id}"
        }
        
        print(f"✅ Direct download completed: {filepath}")
        return True
        
    except Exception as e:
        error_msg = f"Direct download error: {str(e)}"
        print(f"❌ {error_msg}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        direct_file_status[msg_id] = {"status": "error", "error": error_msg}
        return False

def get_mime_type(filename: str) -> str:
    """Get MIME type for video file"""
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type:
        return mime_type
    
    # Default MIME types for common video formats
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    video_types = {
        'mkv': 'video/x-matroska',
        'mp4': 'video/mp4',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'wmv': 'video/x-ms-wmv',
        'flv': 'video/x-flv',
        'webm': 'video/webm',
        'm4v': 'video/mp4'
    }
    
    return video_types.get(ext, 'video/x-matroska')

# ============= End Direct Serving Functions =============

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
                # Remove from database
                del uploaded_files[msg_id]
        await save_uploaded_files_db_async(uploaded_files)
        print(f"🧹 Cleaned up {len(expired_files)} expired files during episode listing")

    return episodes

# Download and upload to PixelDrain
@app.get("/download")
async def trigger_download(url: str):
    """Download a single episode using direct file system"""
    try:
        # Parse the Telegram URL to get msg_id
        channel, msg_id = parse_telegram_url(url)
        if not msg_id:
            raise HTTPException(400, "Invalid Telegram URL")
        
        msg_id_str = str(msg_id)
        
        # Check if already exists
        existing_path = get_direct_file_path(msg_id_str)
        if existing_path:
            return {
                "status": "already_uploaded",
                "msg_id": msg_id_str,
                "message": "File already available for streaming"
            }
        
        # Check if download is already in progress
        if msg_id_str in direct_file_status:
            status = direct_file_status[msg_id_str]["status"]
            if status == "downloading":
                return {
                    "status": "already_queued",
                    "msg_id": msg_id_str,
                    "message": "Download already in progress"
                }
            elif status == "ready":
                return {
                    "status": "already_uploaded",
                    "msg_id": msg_id_str,
                    "message": "File ready for streaming"
                }
        
        # Start the download
        success = await download_direct_file(msg_id_str)
        
        if success:
            return {
                "status": "queued",
                "msg_id": msg_id_str,
                "message": "Download started successfully"
            }
        else:
            return {
                "status": "error",
                "msg_id": msg_id_str,
                "message": "Failed to start download"
            }
            
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        print(f"❌ Error in trigger_download: {e}")
        raise HTTPException(500, f"Download failed: {str(e)}")

@app.get("/download/real_progress/{msg_id}")
async def get_real_download_progress(msg_id: str):
    """Get accurate download progress for direct file downloads"""
    try:
        # Check direct file status
        if msg_id in direct_file_status:
            status = direct_file_status[msg_id]
            
            if status["status"] == "downloading":
                return {
                    "status": "downloading",
                    "msg_id": msg_id,
                    "downloading": True,
                    "temp_file_found": True,
                    "downloaded_size": status.get("downloaded_size", 0),
                    "total_size": status.get("total_size", 0),
                    "percentage": status.get("progress", 0),
                    "filename": status.get("filename", f"{msg_id}.mkv")
                }
            elif status["status"] == "ready":
                return {
                    "status": "not_downloading",
                    "msg_id": msg_id,
                    "downloading": False,
                    "completed": True,
                    "filename": status.get("filename", f"{msg_id}.mkv")
                }
            elif status["status"] == "error":
                return {
                    "status": "error",
                    "msg_id": msg_id,
                    "downloading": False,
                    "error": status.get("error", "Unknown error")
                }
        
        # Not found in direct file status
        return {
            "status": "not_downloading",
            "msg_id": msg_id,
            "downloading": False,
            "temp_file_found": False,
            "downloaded_size": 0,
            "total_size": 0,
            "percentage": 0
        }
        
    except Exception as e:
        print(f"❌ Error getting download progress for {msg_id}: {e}")
        return {
            "status": "error",
            "msg_id": msg_id,
            "error": str(e),
            "downloading": False
        }

@app.get("/test/hls_conversion")
async def test_direct_streaming(msg_id: str = "3899"):
    """Test direct streaming with Telegram download"""
    
    print(f"🧪 Testing direct streaming for episode {msg_id}")
    
    try:
        # Start download in background
        await download_direct_file(msg_id)
        
        return {
            "status": "success",
            "message": f"Direct download started for episode {msg_id}",
            "stream_url": f"/direct/{msg_id}",
            "status_url": f"/direct/status/{msg_id}"
        }
        
    except Exception as e:
        print(f"❌ Direct streaming test failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Direct streaming test failed"
        }

@app.get("/stream_local/{msg_id}")
async def stream_local(msg_id: str, request: Request, quality: str = Query("720p", description="Video quality (unused for direct streaming)")):
    """Stream video using direct file serving for Smart TV compatibility"""
    print(f"📺 Direct streaming request for msg_id: {msg_id}")
    
    # Redirect to direct streaming endpoint
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/direct/{msg_id}", status_code=302)

@app.head("/stream_local/{msg_id}")
async def stream_local_head(msg_id: str):
    """Head request for streaming endpoint"""
    # Check if file exists or can be downloaded
    filepath = get_direct_file_path(msg_id)
    
    if not filepath:
        # Try to check if file can be downloaded
        if msg_id in direct_file_status:
            status = direct_file_status[msg_id]["status"]
            if status == "ready":
                filepath = direct_file_status[msg_id].get("filepath")
            elif status in ["downloading", "initializing"]:
                # File is being prepared
                response_headers = {
                    'Content-Type': 'video/x-matroska',
                    'Accept-Ranges': 'bytes',
                    'Cache-Control': 'no-cache'
                }
                return JSONResponse(content={}, status_code=202, headers=response_headers)
            else:
                raise HTTPException(status_code=404, detail="File not found")
        else:
            raise HTTPException(status_code=404, detail="File not found")
    
    # File exists, return appropriate headers
    if filepath and os.path.exists(filepath):
        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        
        response_headers = {
            'Content-Length': str(file_size),
            'Content-Type': get_mime_type(filename),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'public, max-age=3600'
        }
        return JSONResponse(content={}, headers=response_headers)
    else:
        raise HTTPException(status_code=404, detail="File not found")

@app.get("/stream_status/{msg_id}")
async def get_stream_status(msg_id: str):
    """Get the status of direct file streaming for a video"""
    # Redirect to direct streaming status endpoint
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/direct/status/{msg_id}", status_code=302)

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
    """Clean up old direct files and outdated database entries"""
    print("🧹 Starting cleanup of old direct files...")
    
    try:
        # Clean up old direct files to maintain storage limits
        current_usage = get_storage_usage()
        max_usage = MAX_STORAGE_MB * 1024 * 1024 * 0.8  # Clean when 80% full
        
        if current_usage > max_usage:
            await cleanup_old_files(int(max_usage * 0.6))  # Clean to 60% of max
        
        print("✅ Direct file cleanup completed")
        return 0
        
    except Exception as e:
        print(f"❌ Error in cleanup: {e}")
        return 0
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
        "direct_streaming_enabled": True,
        "telegram_configured": bool(API_ID and API_HASH),
        "session_type": "StringSession" if TELEGRAM_SESSION_STRING else "FileSession",
        "storage_limit_mb": MAX_STORAGE_MB
    }

# === DIRECT FILE SERVING ENDPOINTS (New Primary Approach) ===

@app.get("/direct/{msg_id}")
async def direct_file_stream(msg_id: str, request: Request):
    """Direct file streaming endpoint with range support"""
    try:
        # Get file path
        filepath = get_direct_file_path(msg_id)
        
        if not filepath:
            # Try to download if not available
            print(f"📁 File not found locally, attempting download for {msg_id}")
            success = await download_direct_file(msg_id)
            if not success:
                raise HTTPException(status_code=404, detail="File not found or download failed")
            
            filepath = get_direct_file_path(msg_id)
            if not filepath:
                raise HTTPException(status_code=404, detail="Download failed")
        
        # Get file info
        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        
        # Handle range requests for video seeking
        range_header = request.headers.get('Range')
        
        if range_header:
            # Parse range header
            range_match = range_header.replace('bytes=', '').split('-')
            start = int(range_match[0]) if range_match[0] else 0
            end = int(range_match[1]) if range_match[1] else file_size - 1
            
            # Ensure valid range
            if start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")
            
            end = min(end, file_size - 1)
            chunk_size = end - start + 1
            
            def file_reader():
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    data = f.read(chunk_size)
                    yield data
            
            headers = {
                'Content-Range': f'bytes {start}-{end}/{file_size}',
                'Accept-Ranges': 'bytes',
                'Content-Length': str(chunk_size),
                'Content-Type': get_mime_type(filename)
            }
            
            return StreamingResponse(
                file_reader(),
                status_code=206,
                headers=headers
            )
        else:
            # Full file streaming
            def file_reader():
                with open(filepath, 'rb') as f:
                    while True:
                        chunk = f.read(8192)  # 8KB chunks
                        if not chunk:
                            break
                        yield chunk
            
            headers = {
                'Content-Length': str(file_size),
                'Content-Type': get_mime_type(filename),
                'Accept-Ranges': 'bytes'
            }
            
            return StreamingResponse(
                file_reader(),
                headers=headers
            )
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error serving direct file {msg_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.get("/direct/status/{msg_id}")
async def get_direct_file_status(msg_id: str):
    """Get status of direct file download/serving"""
    if msg_id in direct_file_status:
        status = direct_file_status[msg_id].copy()
        
        # Add storage info
        status["storage"] = {
            "used_mb": round(get_storage_usage() / 1024 / 1024, 1),
            "max_mb": MAX_STORAGE_MB,
            "available_mb": round((MAX_STORAGE_MB * 1024 * 1024 - get_storage_usage()) / 1024 / 1024, 1)
        }
        
        return status
    else:
        return {"status": "not_found", "error": "File not requested yet"}

@app.post("/direct/download/{msg_id}")
async def start_direct_download(msg_id: str, background_tasks: BackgroundTasks):
    """Start direct file download"""
    try:
        # Check if already downloading or ready
        if msg_id in direct_file_status:
            status = direct_file_status[msg_id]["status"]
            if status in ["downloading", "ready"]:
                return {"status": "already_exists", "current_status": status}
        
        # Start download in background
        background_tasks.add_task(download_direct_file, msg_id)
        
        return {
            "status": "download_started",
            "msg_id": msg_id,
            "message": "Download started in background"
        }
        
    except Exception as e:
        print(f"❌ Error starting direct download for {msg_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start download: {str(e)}")

@app.get("/direct/storage/info")
async def direct_storage_info():
    """Get storage information for direct files"""
    total_usage = get_storage_usage()
    files_info = []
    
    if os.path.exists(DIRECT_FILES_DIR):
        for filename in os.listdir(DIRECT_FILES_DIR):
            filepath = os.path.join(DIRECT_FILES_DIR, filename)
            if os.path.isfile(filepath):
                size = os.path.getsize(filepath)
                mtime = os.path.getmtime(filepath)
                msg_id = filename.split('_')[0] if '_' in filename else filename.split('.')[0]
                
                files_info.append({
                    "filename": filename,
                    "msg_id": msg_id,
                    "size_mb": round(size / 1024 / 1024, 1),
                    "modified": mtime,
                    "url": f"/direct/{msg_id}"
                })
    
    # Sort by modification time (newest first)
    files_info.sort(key=lambda x: x["modified"], reverse=True)
    
    return {
        "storage": {
            "used_mb": round(total_usage / 1024 / 1024, 1),
            "max_mb": MAX_STORAGE_MB,
            "available_mb": round((MAX_STORAGE_MB * 1024 * 1024 - total_usage) / 1024 / 1024, 1),
            "usage_percent": round((total_usage / (MAX_STORAGE_MB * 1024 * 1024)) * 100, 1)
        },
        "files": files_info,
        "files_count": len(files_info)
    }

@app.delete("/direct/cleanup")
async def cleanup_direct_storage():
    """Clean up old files to free storage space"""
    try:
        initial_usage = get_storage_usage()
        target_size = int(MAX_STORAGE_MB * 0.5 * 1024 * 1024)  # Clean to 50% of max
        
        await cleanup_old_files(target_size)
        
        final_usage = get_storage_usage()
        freed_mb = round((initial_usage - final_usage) / 1024 / 1024, 1)
        
        return {
            "status": "cleanup_completed",
            "freed_mb": freed_mb,
            "storage": {
                "before_mb": round(initial_usage / 1024 / 1024, 1),
                "after_mb": round(final_usage / 1024 / 1024, 1),
                "max_mb": MAX_STORAGE_MB
            }
        }
        
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")

# === END DIRECT SERVING ENDPOINTS ===

# Add info endpoint
@app.get("/")
async def root():
    """Root endpoint with complete service information and API documentation"""
    return {
        "service": "Smart TV Streaming Server",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "catalog": {
                "list_series": {
                    "path": "/catalog/series",
                    "method": "GET",
                    "description": "List all available series"
                },
                "list_seasons": {
                    "path": "/catalog/series/{series_name}",
                    "method": "GET",
                    "description": "List seasons for a specific series"
                },
                "list_episodes": {
                    "path": "/catalog/series/{series_name}/{season_name}",
                    "method": "GET",
                    "description": "List episodes for a specific season"
                },
                "get_episode": {
                    "path": "/catalog/episode/{msg_id}",
                    "method": "GET",
                    "description": "Get episode information by message ID"
                }
            },
            "downloads": {
                "trigger_download": {
                    "path": "/download?url={telegram_url}",
                    "method": "GET",
                    "description": "Download a single episode using direct file system"
                },
                "real_progress": {
                    "path": "/download/real_progress/{msg_id}",
                    "method": "GET",
                    "description": "Get accurate download progress for direct file downloads"
                }
            },
            "streaming": {
                "stream_local": {
                    "path": "/stream_local/{msg_id}",
                    "method": "GET",
                    "description": "Stream a downloaded file from PixelDrain for TV"
                },
                "stream_mobile": {
                    "path": "/stream_mobile/{msg_id}",
                    "method": "GET",
                    "description": "Stream/download simultaneously for mobile devices"
                },
                "get_stream_url": {
                    "path": "/get_stream_url/{msg_id}",
                    "method": "GET",
                    "description": "Get direct PixelDrain URL for streaming"
                }
            },
            "system": {
                "health_check": {
                    "path": "/health",
                    "method": "GET",
                    "description": "Health check endpoint for deployment platforms"
                },
                "storage_info": {
                    "path": "/storage/info",
                    "method": "GET",
                    "description": "Get PixelDrain storage information"
                },
                "files_status": {
                    "path": "/files/status",
                    "method": "GET",
                    "description": "Get overview of file storage status"
                },
                "cleanup_expired": {
                    "path": "/cleanup/expired",
                    "method": "POST",
                    "description": "Manually trigger cleanup of expired files"
                }
            },
            "gist": {
                "gist_status": {
                    "path": "/gist/status",
                    "method": "GET",
                    "description": "Get Gist synchronization status"
                },
                "manual_sync": {
                    "path": "/gist/sync",
                    "method": "POST",
                    "description": "Manually trigger Gist synchronization"
                },
                "force_update": {
                    "path": "/gist/force_update/{filename}",
                    "method": "POST",
                    "description": "Force update a specific file from Gist"
                }
            },
            "debug": {
                "database": {
                    "path": "/debug/database",
                    "method": "GET",
                    "description": "Check uploaded files database"
                },
                "temp_files": {
                    "path": "/temp/files",
                    "method": "GET",
                    "description": "List all temp files for debugging"
                }
            },
            "webapp": {
                "mobile": {
                    "path": "/mobile",
                    "method": "GET",
                    "description": "Netflix-like mobile webapp"
                }
            }
        },
        "static_paths": {
            "static_assets": "/static/",
            "mobile_assets": "/mobile/"
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
    """Get direct stream URL for Smart TV streaming (replaces HLS)"""
    print(f"🔍 Direct Stream URL requested for msg_id: {msg_id}")
    
    # Start direct download process if not already available
    try:
        # Check if file is already available
        filepath = get_direct_file_path(msg_id)
        
        if not filepath:
            # Start download in background
            await download_direct_file(msg_id)
        
        # Return HLS playlist URL
        base_url = "http://localhost:8000"  # Adjust based on your server
        hls_url = f"{base_url}/hls/{msg_id}/playlist.m3u8"
        
        print(f"� Providing HLS URL for Smart TV: {hls_url}")
        
        return {
            "success": True,
            "stream_url": hls_url,
            "stream_type": "hls",
            "msg_id": msg_id,
            "status_url": f"{base_url}/stream_status/{msg_id}"
        }
    except Exception as e:
        print(f"❌ Error getting HLS stream URL: {str(e)}")
        raise HTTPException(500, f"Failed to prepare HLS stream: {str(e)}")

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

# === Mobile/PC streaming endpoints (Testing) === #

@app.get("/stream_mobile/{msg_id}")
async def stream_mobile(msg_id: str, request: Request):
    """
    Mobile/PC streaming endpoint using direct streaming (replaces HLS)
    """
    print(f"📱 Mobile direct stream request for msg_id: {msg_id}")
    
    try:
        # Redirect to direct streaming endpoint
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/direct/{msg_id}", status_code=302)
        
    except Exception as e:
        print(f"❌ Error in mobile direct streaming: {e}")
        raise HTTPException(500, f"Mobile streaming failed: {str(e)}")

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

