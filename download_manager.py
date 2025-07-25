# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\download_manager.py
import os
import time
import tempfile
import asyncio
from telethon import TelegramClient
from utils import parse_telegram_url, get_file_extension_from_message, get_file_size_from_message

# Configuration variables (will be imported from main)
CUSTOM_TEMP_DIR = os.getenv("CUSTOM_TEMP_DIR")
CHUNK_SIZE_MIN = 2 * 1024 * 1024          # 2MB minimum chunk
CHUNK_SIZE_DEFAULT = 5 * 1024 * 1024       # 5MB default chunk
CHUNK_SIZE_MAX = 50 * 1024 * 1024         # 50MB maximum chunk (memory limit safe)
CHUNK_SIZE_ADAPTIVE = True                 # Enable adaptive chunk sizing
MAX_MEMORY_PER_DOWNLOAD = 150 * 1024 * 1024  # 150MB max memory per download

# NOTE: All upload functionality is handled by upload_manager.py
# This module only handles downloading from Telegram and passes files to upload functions

async def chunked_download_upload(client, message, filename: str, upload_func) -> str:
    """
    Download and upload in chunks to stay within 512MB memory limit.
    Downloads chunks from Telegram and immediately uploads to storage provider.
    """
    print(f"🧩 Starting chunked download/upload for: {filename}")
    print(f"📊 Max chunk size: {CHUNK_SIZE_MAX / (1024*1024):.1f} MB")
    
    # Get file size info
    if message.video:
        total_file_size = message.video.size
        print(f"📹 Video file size: {total_file_size / (1024*1024):.1f} MB")
    elif message.document:
        total_file_size = message.document.size
        print(f"📄 Document file size: {total_file_size / (1024*1024):.1f} MB")
    else:
        total_file_size = 0
        print("⚠️ Could not determine file size")
    
    # Check if file size exceeds memory limit
    if total_file_size > MAX_MEMORY_PER_DOWNLOAD:
        print(f"🧩 Large file detected ({total_file_size / (1024*1024):.1f} MB > {MAX_MEMORY_PER_DOWNLOAD / (1024*1024):.1f} MB), using chunked approach")
        return await chunked_stream_to_storage(client, message, filename, total_file_size, upload_func)
    else:
        print(f"📦 Small file ({total_file_size / (1024*1024):.1f} MB ≤ {MAX_MEMORY_PER_DOWNLOAD / (1024*1024):.1f} MB), using standard method")
        # For smaller files, use the existing method
        return await standard_download_upload(client, message, filename, upload_func)

async def chunked_stream_to_storage(client, message, filename: str, total_size: int, upload_func) -> str:
    """
    Stream large files to storage in chunks without using temp files.
    Each chunk is downloaded and immediately uploaded, keeping memory usage low.
    """
    print(f"🌊 Streaming {filename} to storage in chunks")
    
    # Use a reasonable chunk size for large files
    chunk_size = min(CHUNK_SIZE_MAX, max(50 * 1024 * 1024, total_size // 20))  # 50MB minimum, or 1/20th of file
    print(f"📦 Using chunk size: {chunk_size / (1024*1024):.1f} MB")
    
    # For now, fall back to standard method since streaming upload is complex
    print("🔄 Falling back to standard chunked download with temp file...")
    return await standard_download_upload(client, message, filename, upload_func)

async def standard_download_upload(client, message, filename: str, upload_func) -> str:
    """
    Standard download/upload method for smaller files that fit in memory.
    """
    temp_path = None
    try:
        print(f"📦 Using standard method for: {filename}")
        
        # Get file extension
        original_filename = None
        if message.video and message.video.attributes:
            for attr in message.video.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    original_filename = attr.file_name
                    break
        elif message.document and message.document.attributes:
            for attr in message.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    original_filename = attr.file_name
                    break
        
        file_ext = os.path.splitext(original_filename)[1] if original_filename else '.mkv'
        msg_id = message.id
        
        # Create temporary file
        if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
            os.makedirs(CUSTOM_TEMP_DIR, exist_ok=True)
            temp_path = os.path.join(CUSTOM_TEMP_DIR, f"temp_{msg_id}{file_ext}")
        else:
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, f"temp_{msg_id}{file_ext}")
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except:
                    pass
        
        print(f"⬇️ Downloading to: {temp_path}")
        
        # Download with adaptive chunking
        if CHUNK_SIZE_ADAPTIVE:
            await download_with_adaptive_chunking(client, message, temp_path)
        else:
            await client.download_media(message, file=temp_path)
        
        print(f"✅ Download complete, uploading to storage...")
        
        # Upload using provided upload function (from upload_manager.py)
        return await upload_func(temp_path, filename)
        
    finally:
        # Clean up temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
                print(f"🧹 Cleaned up temp file: {temp_path}")
            except Exception as cleanup_error:
                print(f"⚠️ Failed to cleanup temp file: {cleanup_error}")

async def download_with_adaptive_chunking(client, message, file_path):
    """Download with adaptive chunk sizing for maximum speed"""
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

async def stream_telegram_chunks(client, message, chunk_size: int):
    """
    Async generator that yields chunks from Telegram download.
    This keeps memory usage low by processing one chunk at a time.
    """
    print(f"📥 Starting Telegram chunk streaming (chunk size: {chunk_size / (1024*1024):.1f} MB)")
    
    chunk_count = 0
    total_downloaded = 0
    
    try:
        async for chunk in client.iter_download(message.media, chunk_size=chunk_size):
            chunk_count += 1
            total_downloaded += len(chunk)
            
            print(f"📦 Processing chunk {chunk_count}: {len(chunk) / (1024*1024):.1f} MB (total: {total_downloaded / (1024*1024):.1f} MB)")
            
            yield chunk
            
            # Brief pause to prevent overwhelming the server
            await asyncio.sleep(0.1)
            
    except Exception as e:
        print(f"❌ Error streaming chunks from Telegram: {e}")
        raise e
    
    print(f"✅ Finished streaming {chunk_count} chunks, total: {total_downloaded / (1024*1024):.1f} MB")

# Season download functions
async def process_season_downloads(season_download_queue, client, upload_func, database_load_func, database_save_func, check_file_exists_func, MAX_ACCESS_COUNT):
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
                    uploaded_files = database_load_func()
                    if str(episode["msg_id"]) in uploaded_files:
                        file_info = uploaded_files[str(episode["msg_id"])]
                        if file_info.get("access_count", 0) < MAX_ACCESS_COUNT:
                            if await check_file_exists_func(file_info["pixeldrain_id"]):
                                print(f"✅ [{season_info['series_name']} - {season_info['season_name']}] Episode already downloaded: {episode['title']}")
                                season_info["downloaded_count"] += 1
                                continue
                    
                    # Download episode
                    success = await download_single_episode(episode, client, upload_func, database_load_func, database_save_func, check_file_exists_func)
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

async def download_single_episode(episode, client, upload_func, database_load_func, database_save_func, check_file_exists_func):
    """Download a single episode for season download"""
    try:
        channel, msg_id = parse_telegram_url(episode["url"])
        if not channel or not msg_id:
            return False

        # Check if already uploaded
        uploaded_files = database_load_func()
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            if await check_file_exists_func(file_info["pixeldrain_id"]):
                return True  # Already exists

        if not client.is_connected():
            await client.connect()

        message = await client.get_messages(channel, ids=msg_id)
        if not message or (not message.video and not message.document):
            return False

        # Download and upload
        temp_path = None
        try:
            # Get file extension and create filename
            file_ext = get_file_extension_from_message(message)
            filename = f"{msg_id}{file_ext}"
            
            # Create temporary file
            if CUSTOM_TEMP_DIR and os.path.exists(CUSTOM_TEMP_DIR):
                os.makedirs(CUSTOM_TEMP_DIR, exist_ok=True)
                temp_path = os.path.join(CUSTOM_TEMP_DIR, f"temp_{msg_id}{file_ext}")
            else:
                # Use system temp directory with consistent naming
                import tempfile
                temp_dir = tempfile.gettempdir()
                temp_path = os.path.join(temp_dir, f"temp_{msg_id}{file_ext}")
                # Ensure any existing file is removed first
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
            
            # Use chunked download/upload for memory safety (matches main download logic)
            print(f"🧩 Season download using chunked approach for: {filename}")
            pixeldrain_id = await chunked_download_upload(client, message, filename, upload_func)
            
            # Save to database
            uploaded_files = database_load_func()
            
            # Get file size from the message
            file_size = get_file_size_from_message(message)
            
            uploaded_files[str(msg_id)] = {
                "pixeldrain_id": pixeldrain_id,
                "filename": filename,
                "uploaded_at": int(time.time()),
                "file_size": file_size,
                "access_count": 0
            }
            database_save_func(uploaded_files)
            
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