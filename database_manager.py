# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\database_manager.py
import json
import os
import asyncio

# Database files for tracking episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

gist_manager = None

def set_gist_manager(manager):
    """Set the global gist manager reference"""
    global gist_manager
    gist_manager = manager
    print("🔧 Database manager configured with GistManager")

def load_uploaded_files_db():
    """Load the database of uploaded files (sync version)"""
    if gist_manager and hasattr(gist_manager, 'local_cache') and "uploaded_episodes.json" in gist_manager.local_cache:
        return gist_manager.local_cache["uploaded_episodes.json"]
    
    # Fallback to local file
    if os.path.exists(UPLOADED_FILES_DB):
        with open(UPLOADED_FILES_DB, 'r') as f:
            return json.load(f)
    return {}

async def load_uploaded_files_db_async():
    """Async version of load_uploaded_files_db"""
    if gist_manager:
        data = await gist_manager.get_data("uploaded_episodes.json")
        if data is not None:
            return data
    
    # Fallback to local file
    if os.path.exists(UPLOADED_FILES_DB):
        with open(UPLOADED_FILES_DB, 'r') as f:
            return json.load(f)
    return {}

def save_uploaded_files_db(data):
    """Save the database of uploaded files (sync version)"""
    try:
        print(f"💾 Attempting to save database with {len(data)} entries")
        
        # Save locally first (immediate)
        with open(UPLOADED_FILES_DB, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Queue Gist update if available
        if gist_manager:
            gist_manager.local_cache["uploaded_episodes.json"] = data
            # Schedule async Gist update
            asyncio.create_task(gist_manager.update_data("uploaded_episodes.json", data))
        
        print(f"✅ Database saved successfully to {UPLOADED_FILES_DB}")
        
    except Exception as e:
        print(f"❌ Error saving database: {e}")
        raise e
    
async def save_uploaded_files_db_async(data):
    """Async version of save_uploaded_files_db"""
    try:
        print(f"💾 Attempting to save database with {len(data)} entries (async)")
        
        if gist_manager:
            await gist_manager.update_data("uploaded_episodes.json", data)
        else:
            # Fallback to local file
            with open(UPLOADED_FILES_DB, 'w') as f:
                json.dump(data, f, indent=2)
        
        print(f"✅ Database saved successfully (async)")
        
    except Exception as e:
        print(f"❌ Error saving database (async): {e}")
        raise e
    



def load_downloaded_files_db():
    """Load the database of downloaded files"""
    if os.path.exists(DOWNLOADED_FILES_DB):
        with open(DOWNLOADED_FILES_DB, 'r') as f:
            return json.load(f)
    return {}

def save_downloaded_files_db(data):
    """Save the database of downloaded files"""
    try:
        print(f"💾 Attempting to save downloaded files database with {len(data)} entries")
        with open(DOWNLOADED_FILES_DB, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"✅ Downloaded files database saved successfully to {DOWNLOADED_FILES_DB}")
    except Exception as e:
        print(f"❌ Error saving downloaded files database: {e}")
        raise e
    

# === VIDEO DATA FUNCTIONS === #

async def load_video_data():
    """Load video.json with Gist support"""
    if gist_manager:
        data = await gist_manager.get_data("video.json")
        if data is not None:
            return data
    
    # Fallback to local file
    try:
        with open("video.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ video.json not found locally or in Gist")
        return {}

def load_video_data_sync():
    """Load video.json (sync version)"""
    if gist_manager and hasattr(gist_manager, 'local_cache') and "video.json" in gist_manager.local_cache:
        return gist_manager.local_cache["video.json"]
    
    # Fallback to local file
    try:
        with open("video.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ video.json not found locally")
        return {}