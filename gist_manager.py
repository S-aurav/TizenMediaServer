# filepath: gist_manager.py

import os
import json
import asyncio
import aiohttp
import aiofiles
import time
import hashlib
from typing import Dict, Optional, Any
from datetime import datetime

class GistManager:
    def __init__(self, github_token: str, gist_id: str):
        self.github_token = github_token
        self.gist_id = gist_id
        self.local_cache = {}  # In-memory cache
        self.cache_timestamps = {}  # Track when files were last updated
        self.cache_ttl = 300  # 5 minutes TTL
        self.lock = asyncio.Lock()
        self.api_base = "https://api.github.com"
        self.local_cache_dir = "cache"
        self.max_retries = 3
        self.retry_delay = 2
        
        # Ensure cache directory exists
        os.makedirs(self.local_cache_dir, exist_ok=True)
        
        print(f"🔧 GistManager initialized with Gist ID: {gist_id}")
    
    def _get_local_cache_path(self, filename: str) -> str:
        """Get local cache file path"""
        return os.path.join(self.local_cache_dir, filename)
    
    def _get_cache_metadata_path(self, filename: str) -> str:
        """Get cache metadata file path"""
        return os.path.join(self.local_cache_dir, f"{filename}.meta")
    
    async def _make_github_request(self, method: str, url: str, data: Dict = None) -> Dict:
        """Make authenticated GitHub API request with retry logic"""
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Smart-TV-Streaming-Server/1.0"
        }
        
        for attempt in range(self.max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, json=data) as response:
                        if response.status == 200:
                            return await response.json()
                        elif response.status == 403:
                            # Rate limit hit
                            reset_time = response.headers.get('X-RateLimit-Reset')
                            if reset_time:
                                wait_time = int(reset_time) - int(time.time()) + 1
                                print(f"⏳ Rate limit hit, waiting {wait_time} seconds...")
                                await asyncio.sleep(min(wait_time, 3600))  # Max 1 hour wait
                            continue
                        elif response.status in [500, 502, 503, 504] and attempt < self.max_retries - 1:
                            # Server errors, retry
                            await asyncio.sleep(self.retry_delay * (attempt + 1))
                            continue
                        else:
                            error_text = await response.text()
                            raise Exception(f"GitHub API error {response.status}: {error_text}")
            
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < self.max_retries - 1:
                    print(f"⚠️ Network error (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                else:
                    raise Exception(f"GitHub API request failed after {self.max_retries} attempts: {e}")
        
        raise Exception("GitHub API request failed")
    
    async def get_gist_info(self) -> Dict:
        """Get Gist information from GitHub"""
        url = f"{self.api_base}/gists/{self.gist_id}"
        return await self._make_github_request("GET", url)
    
    async def is_cache_fresh(self, filename: str) -> bool:
        """Check if local cache is still fresh (within TTL)"""
        if filename not in self.cache_timestamps:
            return False
        
        age = time.time() - self.cache_timestamps[filename]
        return age < self.cache_ttl
    
    async def load_local_cache(self, filename: str) -> Optional[Dict]:
        """Load data from local cache file"""
        try:
            cache_path = self._get_local_cache_path(filename)
            if not os.path.exists(cache_path):
                return None
            
            async with aiofiles.open(cache_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
                
            # Load metadata
            meta_path = self._get_cache_metadata_path(filename)
            if os.path.exists(meta_path):
                async with aiofiles.open(meta_path, 'r') as f:
                    meta_content = await f.read()
                    metadata = json.loads(meta_content)
                    self.cache_timestamps[filename] = metadata.get('timestamp', 0)
            
            print(f"📁 Loaded {filename} from local cache")
            return data
            
        except Exception as e:
            print(f"⚠️ Failed to load local cache for {filename}: {e}")
            return None
    
    async def save_local_cache(self, filename: str, data: Dict):
        """Save data to local cache file"""
        try:
            cache_path = self._get_local_cache_path(filename)
            
            # Save data
            async with aiofiles.open(cache_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=2))
            
            # Save metadata
            metadata = {
                'timestamp': time.time(),
                'checksum': hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()
            }
            
            meta_path = self._get_cache_metadata_path(filename)
            async with aiofiles.open(meta_path, 'w') as f:
                await f.write(json.dumps(metadata, indent=2))
            
            self.cache_timestamps[filename] = metadata['timestamp']
            print(f"💾 Saved {filename} to local cache")
            
        except Exception as e:
            print(f"❌ Failed to save local cache for {filename}: {e}")
    
    async def check_for_updates(self, filename: str) -> bool:
        """Check if remote Gist has updates for a file"""
        try:
            # Skip check if cache is fresh
            if await self.is_cache_fresh(filename):
                return False
            
            gist_info = await self.get_gist_info()
            
            if filename not in gist_info['files']:
                print(f"⚠️ File {filename} not found in Gist")
                return False
            
            # Parse GitHub's updated_at timestamp
            remote_updated_str = gist_info['updated_at']
            remote_updated = datetime.fromisoformat(remote_updated_str.replace('Z', '+00:00'))
            remote_timestamp = remote_updated.timestamp()
            
            local_timestamp = self.cache_timestamps.get(filename, 0)
            
            has_updates = remote_timestamp > local_timestamp
            if has_updates:
                print(f"🔄 Updates available for {filename}")
            
            return has_updates
            
        except Exception as e:
            print(f"⚠️ Failed to check for updates for {filename}: {e}")
            return False
    
    async def fetch_from_gist(self, filename: str) -> Optional[Dict]:
        """Fetch file content from Gist"""
        try:
            gist_info = await self.get_gist_info()
            
            if filename not in gist_info['files']:
                print(f"⚠️ File {filename} not found in Gist")
                return None
            
            file_info = gist_info['files'][filename]
            content = file_info['content']
            
            try:
                data = json.loads(content)
                print(f"📥 Fetched {filename} from Gist")
                return data
            except json.JSONDecodeError as e:
                print(f"❌ Invalid JSON in Gist file {filename}: {e}")
                return None
                
        except Exception as e:
            print(f"❌ Failed to fetch {filename} from Gist: {e}")
            return None
    
    async def update_gist(self, filename: str, data: Dict):
        """Update file in Gist"""
        try:
            url = f"{self.api_base}/gists/{self.gist_id}"
            
            payload = {
                "files": {
                    filename: {
                        "content": json.dumps(data, indent=2)
                    }
                }
            }
            
            await self._make_github_request("PATCH", url, payload)
            print(f"📤 Updated {filename} in Gist")
            
            # Update local timestamp to mark as fresh
            self.cache_timestamps[filename] = time.time()
            
        except Exception as e:
            print(f"❌ Failed to update {filename} in Gist: {e}")
            raise e
    
    async def get_data(self, filename: str) -> Optional[Dict]:
        """Get data with multi-layered caching strategy"""
        async with self.lock:
            try:
                # Layer 1: In-memory cache (fastest)
                if filename in self.local_cache and await self.is_cache_fresh(filename):
                    print(f"⚡ Serving {filename} from memory cache")
                    return self.local_cache[filename]
                
                # Layer 2: Check for updates
                has_updates = await self.check_for_updates(filename)
                
                if has_updates:
                    # Fetch from Gist
                    data = await self.fetch_from_gist(filename)
                    if data is not None:
                        # Update all cache layers
                        self.local_cache[filename] = data
                        await self.save_local_cache(filename, data)
                        return data
                
                # Layer 3: Use local cache if available
                if filename in self.local_cache:
                    print(f"📁 Serving {filename} from memory (no updates)")
                    return self.local_cache[filename]
                
                # Layer 4: Load from local file cache
                data = await self.load_local_cache(filename)
                if data is not None:
                    self.local_cache[filename] = data
                    return data
                
                # Layer 5: Fallback to Gist
                print(f"🔄 No local cache found, fetching {filename} from Gist...")
                data = await self.fetch_from_gist(filename)
                if data is not None:
                    self.local_cache[filename] = data
                    await self.save_local_cache(filename, data)
                    return data
                
                print(f"❌ Could not load {filename} from any source")
                return None
                
            except Exception as e:
                print(f"❌ Error in get_data for {filename}: {e}")
                
                # Fallback to local cache on error
                if filename in self.local_cache:
                    print(f"⚠️ Using stale cache for {filename} due to error")
                    return self.local_cache[filename]
                
                # Try local file as last resort
                data = await self.load_local_cache(filename)
                if data is not None:
                    print(f"⚠️ Using local file cache for {filename} due to error")
                    self.local_cache[filename] = data
                    return data
                
                return None
    
    async def update_data(self, filename: str, data: Dict):
        """Update data with write-through pattern"""
        async with self.lock:
            try:
                # Layer 1: Update in-memory cache immediately
                self.local_cache[filename] = data
                
                # Layer 2: Save to local file (backup)
                await self.save_local_cache(filename, data)
                
                # Layer 3: Update Gist (async, but wait for completion)
                await self.update_gist(filename, data)
                
                print(f"✅ Successfully updated {filename} in all layers")
                
            except Exception as e:
                print(f"❌ Error updating {filename}: {e}")
                # Even if Gist update fails, we have local copies
                print(f"⚠️ Data saved locally, will retry Gist sync later")
                raise e
    
    async def sync_to_gist(self, filename: str):
        """Sync local data to Gist (for background tasks)"""
        try:
            if filename in self.local_cache:
                await self.update_gist(filename, self.local_cache[filename])
        except Exception as e:
            print(f"❌ Background sync failed for {filename}: {e}")
    
    async def sync_all_files(self):
        """Sync all cached files to Gist"""
        for filename in self.local_cache.keys():
            try:
                await self.sync_to_gist(filename)
                await asyncio.sleep(1)  # Rate limiting
            except Exception as e:
                print(f"❌ Failed to sync {filename}: {e}")
    
    async def initialize(self):
        """Initialize the manager by loading initial data"""
        try:
            print("🚀 Initializing GistManager...")
            
            # Try to load video.json
            video_data = await self.get_data("video.json")
            if video_data is None:
                print("⚠️ video.json not found in Gist, will use local file")
            
            # Try to load uploaded_episodes.json
            episodes_data = await self.get_data("uploaded_episodes.json")
            if episodes_data is None:
                print("⚠️ uploaded_episodes.json not found in Gist, will create empty")
                await self.update_data("uploaded_episodes.json", {})
            
            print("✅ GistManager initialized successfully")
            
        except Exception as e:
            print(f"❌ Failed to initialize GistManager: {e}")
            print("⚠️ Will continue with local files only")

# Background sync task
async def background_sync_task(gist_manager: GistManager):
    """Background task for periodic Gist synchronization"""
    while True:
        try:
            print("🔄 Running background Gist sync...")
            await gist_manager.sync_all_files()
            print("✅ Background sync completed")
            await asyncio.sleep(1800)  # Every 30 minutes
        except Exception as e:
            print(f"❌ Background sync failed: {e}")
            await asyncio.sleep(300)   # Retry in 5 minutes on error
