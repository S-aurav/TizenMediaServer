# Database Manager - GitHub Gist Integration Only
# This module ensures all database operations go through GitHub Gist
# NO LOCAL JSON FILES - Everything syncs to Gist

from github_gist_manager import save_uploaded_files_db, load_uploaded_files_db

# Re-export the functions for backward compatibility
__all__ = ['save_uploaded_files_db', 'load_uploaded_files_db']

# Note: This module now acts as a wrapper around GitHub Gist integration
# All database operations are automatically synced to your GitHub Gist
# No local JSON files are created or maintained