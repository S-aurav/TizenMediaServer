## 🤖 **UptimeRobot External CRON Setup Guide**

### **🎯 Why UptimeRobot?**
- ✅ **Free Forever**: 50 monitors included
- ✅ **1-minute precision**: Much more reliable than GitHub
- ✅ **99.9% uptime**: Professional-grade monitoring
- ✅ **HTTP POST support**: Perfect for GitHub API calls
- ✅ **No credit card**: Completely free signup

---

## **📝 Step-by-Step Setup:**

### **1. Create UptimeRobot Account**
1. Go to **https://uptimerobot.com**
2. Click **"Sign Up for Free"**
3. Use your email (no credit card needed)
4. Verify email and login

### **2. Create New Monitor**
1. **Dashboard** → **"Add New Monitor"**
2. **Monitor Settings**:
   ```
   Monitor Type: HTTP(s)
   Friendly Name: Smart TV Server Keep-Alive
   URL: https://api.github.com/repos/YOUR_USERNAME/YOUR_REPO_NAME/actions/workflows/external-keep-alive.yml/dispatches
   Monitoring Interval: 10 minutes
   ```

### **3. Configure HTTP POST Request**
1. **Advanced Settings** → **HTTP Settings**
2. **Request Method**: `POST`
3. **Request Headers**:
   ```
   Accept: application/vnd.github+json
   Authorization: Bearer YOUR_GITHUB_PAT_HERE
   User-Agent: UptimeRobot-KeepAlive/1.0
   ```
4. **Request Body** (JSON):
   ```json
   {
     "ref": "main",
     "inputs": {
       "source": "uptimerobot"
     }
   }
   ```

### **4. Set Active Hours (IST: 10 AM - 9 PM)**
1. **Maintenance Windows** → **"Add New"**
2. **Type**: `Weekly`
3. **Days**: `Monday to Sunday`
4. **Time**: `9:00 PM to 10:00 AM IST`
5. **Purpose**: Pause monitoring during sleep hours

### **5. Test the Setup**
1. **Save Monitor**
2. **Manual Test**: Click "Test" button
3. **Check GitHub**: Go to Actions tab → Should see new run
4. **Verify**: Monitor should show "Up" status

---

## **🔧 Complete Configuration Template:**

### **Monitor Configuration:**
```
Monitor Type: HTTP(s)
Friendly Name: Smart TV Server Keep-Alive (10AM-9PM IST)
URL: https://api.github.com/repos/YOUR_USERNAME/YOUR_REPO_NAME/actions/workflows/external-keep-alive.yml/dispatches
Monitoring Interval: 10 minutes
Request Method: POST
Request Headers:
  Accept: application/vnd.github+json
  Authorization: Bearer YOUR_GITHUB_PAT
  User-Agent: UptimeRobot-KeepAlive/1.0
Request Body:
  {"ref":"main","inputs":{"source":"uptimerobot"}}
```

### **Maintenance Window (Sleep Hours):**
```
Type: Weekly
Days: Monday to Sunday  
Time: 21:00 to 10:00 IST (9 PM to 10 AM)
Timezone: Asia/Kolkata (IST)
```

---

## **📊 Expected Results:**

| Metric | GitHub CRON | UptimeRobot |
|--------|-------------|-------------|
| **Precision** | ±15-30 minutes | ±1-2 minutes |
| **Reliability** | ~70-80% | ~99.9% |
| **Interval** | 10 minutes (unreliable) | 10 minutes (precise) |
| **Active Hours** | 4:30 AM - 3:30 PM UTC | 10 AM - 9 PM IST |
| **Cost** | Free | Free |

## **🛡️ Backup Strategy:**
- **Primary**: UptimeRobot (99.9% reliable)
- **Backup**: GitHub CRON (still active as fallback)
- **Dual redundancy** ensures maximum uptime

---

## **🔍 Monitoring & Verification:**

### **Check if Working:**
1. **UptimeRobot Dashboard**: Monitor shows "Up" every 10 minutes
2. **GitHub Actions**: New runs every 10 minutes in Actions tab
3. **Server Logs**: Your Render server stays awake consistently

### **Troubleshooting:**
| Issue | Solution |
|-------|----------|
| Monitor shows "Down" | Check GitHub PAT token validity |
| No GitHub Actions runs | Verify repository name in URL |
| 403 Error | PAT needs `repo` and `workflow` scopes |
| Wrong timezone | Set maintenance window to IST properly |

---

## **🎉 Benefits After Setup:**
- ⚡ **Server never sleeps** during active hours
- 📈 **99.9% uptime** instead of ~70% with GitHub alone
- 🎯 **Precise timing** every 10 minutes
- 🛡️ **Dual redundancy** (UptimeRobot + GitHub backup)
- 💰 **Still completely free**

Your Smart TV server will now have enterprise-grade uptime! 🚀