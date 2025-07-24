## 🔑 GitHub Personal Access Token Setup

### Create PAT for External CRON Triggering:

1. **Go to GitHub Settings**:
   - Click your profile → Settings
   - Left sidebar → Developer settings
   - Personal access tokens → Tokens (classic)

2. **Generate New Token**:
   - Click "Generate new token (classic)"
   - Note: `Smart TV Server External CRON`
   - Expiration: `No expiration` (or 1 year)
   - **Scopes needed**:
     - ✅ `repo` (Full control of private repositories)
     - ✅ `workflow` (Update GitHub Action workflows)

3. **Copy Token**:
   - Copy the token immediately (you won't see it again)
   - Format: `ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

4. **Test Token**:
   ```bash
   curl -H "Authorization: Bearer YOUR_PAT_HERE" \
        https://api.github.com/user
   ```

### Security Notes:
- ⚠️ Keep this token secure and private
- 🔄 Token allows triggering workflows but not repository access
- 🛡️ UptimeRobot will store this securely in their system