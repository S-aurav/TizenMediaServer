# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\database_manager.py
import json
import os

# Database files for tracking episodes
DOWNLOADED_FILES_DB = "downloaded_episodes.json"
UPLOADED_FILES_DB = "uploaded_episodes.json"

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