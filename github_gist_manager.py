import requests
import json
import os
from typing import Dict, Any, Optional

class GitHubGistManager:
    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.gist_id = os.getenv("GIST_ID")
        self.filename = "uploaded_files.json"
        
    def save_uploaded_files_db(self, data: Dict[str, Any]) -> bool:
        """Save uploaded episodes data to GitHub Gist ONLY - no local backup"""
        
        # Save to GitHub Gist only
        success_gist = self._save_to_gist(data)
        
        if success_gist:
            print(f"✅ Data synced to GitHub Gist ({len(data)} episodes)")
            self._print_access_urls()
        else:
            print(f"❌ Gist sync failed!")
            
        return success_gist
    
    def load_uploaded_files_db(self) -> Dict[str, Any]:
        """Load uploaded episodes data from GitHub Gist ONLY - no local fallback"""
        
        # Load from GitHub Gist only
        data = self._load_from_gist()
        if data is not None:
            print(f"✅ Loaded {len(data)} episodes from GitHub Gist")
            return data
        
        # Return empty dict if Gist fails
        print("❌ No data found in Gist - returning empty database")
        return {}
    
    def _save_to_gist(self, data: Dict[str, Any]) -> bool:
        """Save data to GitHub Gist"""
        if not self.github_token or not self.gist_id:
            print("⚠️ GitHub token or Gist ID not configured")
            return False
            
        try:
            response = requests.patch(
                f"https://api.github.com/gists/{self.gist_id}",
                headers={
                    "Authorization": f"token {self.github_token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "TizenMediaServer/1.0"
                },
                json={
                    "files": {
                        self.filename: {
                            "content": json.dumps(data, indent=2, ensure_ascii=False)
                        }
                    }
                },
                timeout=10
            )
            
            if response.status_code == 200:
                return True
            else:
                print(f"❌ Gist save failed: {response.status_code} - {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Gist request error: {e}")
            return False
        except Exception as e:
            print(f"❌ Gist save error: {e}")
            return False
    
    def _load_from_gist(self) -> Optional[Dict[str, Any]]:
        """Load data from GitHub Gist"""
        if not self.gist_id:
            return None
            
        try:
            # Use public API (works for both public and private gists)
            headers = {}
            if self.github_token:
                headers["Authorization"] = f"token {self.github_token}"
            headers["User-Agent"] = "TizenMediaServer/1.0"
            
            response = requests.get(
                f"https://api.github.com/gists/{self.gist_id}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                gist_data = response.json()
                if self.filename in gist_data["files"]:
                    file_content = gist_data["files"][self.filename]["content"]
                    return json.loads(file_content)
                else:
                    print(f"❌ File {self.filename} not found in Gist")
                    return None
            else:
                print(f"❌ Gist load failed: {response.status_code}")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Gist request error: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"❌ Gist JSON decode error: {e}")
            return None
        except Exception as e:
            print(f"❌ Gist load error: {e}")
            return None
    
    def _print_access_urls(self):
        """Print access URLs for the Gist"""
        if self.gist_id:
            print(f"🔗 Gist URL: https://gist.github.com/{self.gist_id}")
            print(f"📋 Raw URL: https://gist.githubusercontent.com/raw/{self.gist_id}/{self.filename}")
    
    def get_access_urls(self) -> Dict[str, str]:
        """Get access URLs for the Gist"""
        if not self.gist_id:
            return {}
            
        return {
            "gist_url": f"https://gist.github.com/{self.gist_id}",
            "raw_url": f"https://gist.githubusercontent.com/raw/{self.gist_id}/{self.filename}",
            "api_url": f"https://api.github.com/gists/{self.gist_id}"
        }
    
    def test_connection(self) -> bool:
        """Test GitHub Gist connection"""
        print("🧪 Testing GitHub Gist connection...")
        
        if not self.github_token:
            print("❌ GITHUB_TOKEN not set")
            return False
            
        if not self.gist_id:
            print("❌ GIST_ID not set")
            return False
        
        # Test load
        data = self._load_from_gist()
        if data is not None:
            print(f"✅ Successfully connected to Gist ({len(data)} episodes)")
            self._print_access_urls()
            return True
        else:
            print("❌ Failed to connect to Gist")
            return False

# Global instance
gist_manager = GitHubGistManager()

# Backward compatible functions
def save_uploaded_files_db(data: Dict[str, Any]) -> bool:
    """Backward compatible function"""
    return gist_manager.save_uploaded_files_db(data)

def load_uploaded_files_db() -> Dict[str, Any]:
    """Backward compatible function"""
    return gist_manager.load_uploaded_files_db()

def test_gist_connection() -> bool:
    """Test the Gist connection"""
    return gist_manager.test_connection()

if __name__ == "__main__":
    # Test the connection
    test_gist_connection()