# Database Persistence Fix - Render.com

## 🔴 Problem Identified

Your database was resetting on every deploy because:

1. **Wrong mount path**: Render's disk was mounted at `/opt/render/project/src` (where your code deploys)
2. **Code overwrites on deploy**: Every time you deploy, Render pulls fresh code and overwrites everything in that directory
3. **Database lost**: `league.db` was being overwritten with a fresh empty database

## ✅ Solution Applied

### 1. Changed Disk Mount Path
**render.yaml** now mounts disk to a separate directory:
```yaml
disk:
  name: league-data
  mountPath: /opt/render/project/src/data  # Changed from /opt/render/project/src
  sizeGB: 1
```

### 2. Updated Database Path in Code
**app.py** now checks for the persistent disk location:
```python
# Use persistent disk location on Render, local directory in development
if os.path.exists("/opt/render/project/src/data"):
    # Production: use persistent disk mount
    DATABASE = "/opt/render/project/src/data/league.db"
else:
    # Development: use local directory
    DATABASE = os.path.join(BASE_DIR, "league.db")
```

### 3. Removed Database from Git
```bash
git rm --cached league.db  # Already done
```

## 🚀 Deployment Steps

```bash
# Commit and push changes
git add -A
git commit -m "Fix database persistence: use separate data directory for Render disk mount"
git push origin main
```

## ✅ Verification Steps

After deployment, verify persistence:

### Test 1: Check Database Location
1. Go to Render Dashboard → Your Service → Shell
2. Run:
   ```bash
   ls -lh /opt/render/project/src/data/
   ls -lh league.db
   ```
3. **Expected**: You should see `league.db` in `/opt/render/project/src/data/`

### Test 2: Verify Disk Mount
1. In Render Shell, run:
   ```bash
   df -h | grep data
   mount | grep data
   ```
2. **Expected**: Should show the mounted disk

### Test 3: Test Persistence
1. Add a test player via the signup page
2. Check database:
   ```bash
   sqlite3 /opt/render/project/src/data/league.db "SELECT * FROM players;"
   ```
3. Trigger a manual redeploy in Render Dashboard
4. After redeploy, check database again
5. **Expected**: Player should still be there!

## 🔍 Troubleshooting

### If database still resets:

#### Option 1: Check Disk Creation
- Go to Render Dashboard → Your Service → Settings
- Scroll to "Disks" section
- Verify the disk named "league-data" exists
- If not, you may need to recreate it

#### Option 2: Verify Mount Path
In Render Shell:
```bash
echo "Current directory:"
pwd

echo "Database path from app:"
python3 -c "
import os
if os.path.exists('/opt/render/project/src/data'):
    print('✅ Persistent disk found')
    print('Database will be at: /opt/render/project/src/data/league.db')
else:
    print('❌ Persistent disk NOT found')
    print('Database will be at: ./league.db (WILL BE LOST ON REDEPLOY)')
"

echo "Check if database exists on persistent disk:"
ls -lh /opt/render/project/src/data/league.db
```

#### Option 3: Manual Migration (Last Resort)
If you need to manually move data:

```bash
# In Render Shell
# Copy current database to persistent location
cp league.db /opt/render/project/src/data/league.db

# Verify
ls -lh /opt/render/project/src/data/league.db
```

## 📊 How Render Disk Mounts Work

```
/opt/render/project/src/          ← Your code (replaced on every deploy)
    ├── app.py
    ├── templates/
    ├── static/
    └── data/                      ← Persistent disk mount (SURVIVES deploys)
        └── league.db              ← Your database (PERSISTENT)
```

**Key Points:**
- Everything in `/opt/render/project/src/` is replaced on deploy
- EXCEPT the `/data` subdirectory which is a mounted disk
- Files in `/data` survive deploys indefinitely

## 🔐 Important Notes

1. **Backups**: Even with persistence, always backup your database periodically
2. **Render Free Tier**: Disks on free tier may have limitations
3. **Disk Size**: Currently 1GB - should be plenty for a ping pong league
4. **Local Development**: Database still saves to project root locally (not persistent disk)

## 📝 Creating Backups

### From Render Shell:
```bash
# Create backup
cp /opt/render/project/src/data/league.db /opt/render/project/src/data/league_backup_$(date +%Y%m%d).db

# List backups
ls -lh /opt/render/project/src/data/league_backup_*.db
```

### Download to Local Machine:
1. In Render Shell:
   ```bash
   cat /opt/render/project/src/data/league.db | base64
   ```
2. Copy the output
3. On your local machine:
   ```bash
   echo "<paste-base64-here>" | base64 -d > league_backup.db
   ```

## ✨ After This Fix

**✅ Database WILL persist** through:
- Code deployments
- Application restarts
- Service scaling
- Render platform updates

**❌ Database WILL be lost** if:
- You delete the disk in Render Dashboard
- You change the disk name in render.yaml
- Your Render service is deleted
