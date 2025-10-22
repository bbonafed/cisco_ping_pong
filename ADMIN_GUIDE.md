# Admin Guide - Cisco Ping Pong League

## 🔐 Security Features

### Session Timeout
- Admin sessions expire after **30 minutes of inactivity**
- Timer resets with each admin action
- You'll be automatically logged out and redirected to login page
- This protects against unauthorized access if you leave your computer unlocked

### Password Management
- Admin password is set via `ADMIN_PASSWORD` environment variable
- Never hardcoded in the application
- Change it regularly for security

---

## 🎯 Common Admin Tasks

### 1. Starting a New Season

**Steps:**
1. Go to Admin Dashboard
2. Click "Reset League Data" (requires confirmation)
3. Have players sign up via the Signup page
4. Once you have enough players (minimum 2), click "Generate 8-Week Schedule"
5. Week 1 will be set automatically

**What gets reset:**
- All players deleted
- All matches deleted
- Week counter reset to 1
- Playoff bracket cleared

---

### 2. Advancing Weeks

**Normal advancement (Week 1 → Week 2):**
1. Update the "Current Week" field
2. Check the confirmation checkbox
3. Click "Update Week"

**⚠️ Jumping multiple weeks (e.g., Week 1 → Week 4):**
- System will **auto-forfeit** all unreported matches in skipped weeks
- You'll see a warning showing how many matches will be forfeited
- Must check the confirmation checkbox to proceed
- **This cannot be undone** - use carefully!

**When to advance:**
- When you want to close reporting for the current week
- When you're ready to open the next week's matches

**Rolling back:**
- You can set the week to a previous number
- This reopens old matches for reporting
- No matches are deleted when rolling back

---

### 3. Managing Players Mid-Season

**Adding players:**
- Players can sign up anytime via Signup page
- BUT: They won't be in the schedule automatically
- You must regenerate the schedule to include them

**Deleting players:**
The system protects against data corruption:

✅ **ALLOWED:** Delete player with only unreported matches
- Use case: Player signed up by mistake, never played

❌ **BLOCKED:** Delete player with reported matches
- Error: "Cannot delete player with reported match history"
- Why: Would corrupt rankings and standings
- Solution: Wait until next season to remove them

❌ **BLOCKED:** Delete player in playoff bracket
- Error: "Cannot delete player in playoff bracket"
- Why: Would break tournament structure
- Solution: Reset league or finish playoffs first

**If you need to remove an active player:**
1. Note their name
2. Complete current season
3. Reset league data
4. Don't regenerate schedule with that player

---

### 4. Schedule Regeneration

**⚠️ DANGEROUS OPERATION - Use carefully!**

**What it does:**
- Deletes ALL regular season matches (reported and unreported)
- Generates new 8-week round-robin schedule from current player list
- Resets to Week 1
- Preserves player registrations

**When to use:**
- At start of new season after reset
- After adding/removing players (before season starts)
- To fix a broken schedule

**When NOT to use:**
- Mid-season (destroys all match history)
- If any matches have been reported (unless you want to discard them)

**Confirmation required:**
- Shows warning with count of matches to be deleted
- Must use the confirmation checkbox in the form
- Cannot be undone

---

### 5. Playoff Management

**Automatic Playoffs:**
- After Week 8, if all matches are reported, playoffs auto-generate
- Based on current standings
- Top seeds get byes if player count isn't power of 2

**Force Playoffs Early:**
- Click "Start Playoffs Now" in admin dashboard
- Use this if you want to skip remaining regular season weeks
- Confirmation required if playoff matches already exist

**Playoff Bracket Rules:**
- Bracket size is always power of 2 (2, 4, 8, 16 players)
- Seeded by regular season rankings
- Byes auto-advance to next round
- Each round advances when all matches reported

**Handling Playoff Issues:**

🔴 **Double Forfeit in Playoffs:**
- Neither player advances
- Bracket advancement stops
- **Admin must intervene:**
  1. Go to Admin Dashboard → Matches section
  2. Delete the double-forfeit match
  3. Report it again with a proper winner
  4. OR manually create next round match

🔴 **Tied Playoff Match:**
- System won't advance automatically
- Delete and re-report with correct winner

**Regenerating Playoffs:**
- Use "Start Playoffs Now" button
- Will delete existing playoff bracket
- Confirmation required if matches already reported

---

### 6. Match Management

**Viewing matches:**
- Admin Dashboard shows all matches (regular + playoff)
- Columns: ID, Type, Week/Round, Players, Score, Reported status

**Deleting matches:**
- Click "Delete" button next to any match
- Instant deletion (confirmation in JavaScript only)
- Use cases:
  - Test match reported by mistake
  - Incorrect score that players can't fix
  - Duplicate match created by bug

**⚠️ Side effects of match deletion:**
- Rankings recalculate automatically (no action needed)
- Players involved lose that match from their record
- In playoffs: May break bracket advancement if winner already advanced
- Regular season: Creates gap in schedule but doesn't break anything

**Best practice:**
- If a match needs correction, delete it and have players report again
- Don't delete matches unless necessary (preserves history)

---

## 🐛 Troubleshooting Common Issues

### Issue: "Player has reported matches" when trying to delete
**Solution:** This is intentional - don't delete active players mid-season. Wait for season end.

### Issue: Week advancement warning about forfeiting matches
**Solution:** This is normal when jumping weeks. Review the count and confirm if intended.

### Issue: Playoff bracket stopped advancing
**Possible causes:**
1. Double forfeit in a match - delete and re-report
2. Tied match - delete and re-report with winner
3. All players in round forfeited - manually create next round or reset playoffs

### Issue: Rankings look wrong
**Check:**
1. Are there any double-forfeit matches? (Both players get a loss)
2. Were matches deleted recently? (Rankings update automatically but page might need refresh)
3. Is playoff data mixed with regular season? (Use "Preview Playoff Bracket" to see regular-season-only rankings)

### Issue: Player can't report match
**Check:**
1. Is it the current week? (Matches only open for current week)
2. Is the match already reported? (Can't report twice)
3. Is it past weeks? (Old weeks are locked)
4. Is it a BYE? (BYEs don't need reporting)

### Issue: Schedule has wrong number of matches
**Explanation:** Round-robin with N players creates (N-1) weeks, up to 8-week maximum
- 2 players: 1 match/week
- 4 players: 2 matches/week
- 8 players: 4 matches/week
- Odd numbers get a rotating BYE each week

### Issue: Admin session keeps expiring
**Solution:** Set `ADMIN_SESSION_TIMEOUT_MINUTES` environment variable to longer duration (default 30 minutes)

---

## 🎛️ Manual Database Fixes (Advanced)

**⚠️ Only for emergency situations. These require direct database access.**

### Fixing a corrupted match:
```bash
# Access SQLite database
sqlite3 league.db

# View match details
SELECT * FROM matches WHERE id = <match_id>;

# Manually update score
UPDATE matches SET
    game1_score1 = 11, game1_score2 = 9,
    game2_score1 = 11, game2_score2 = 7,
    game3_score1 = 0, game3_score2 = 0,
    score1 = 22, score2 = 16,
    reported = 1
WHERE id = <match_id>;
```

### Manually advancing someone in playoffs:
```sql
-- Find current playoff round matches
SELECT * FROM matches WHERE playoff = 1 AND playoff_round = 1;

-- Create next round match manually
INSERT INTO matches (week, player1_id, player2_id, score1, score2, reported, playoff, playoff_round, created_at)
VALUES (NULL, <winner1_id>, <winner2_id>, NULL, NULL, 0, 1, 2, datetime('now'));
```

### Fixing double forfeit in playoffs:
```sql
-- Update the match to have a winner (e.g., player1 wins by forfeit)
UPDATE matches SET
    double_forfeit = 0,
    game1_score1 = 11, game1_score2 = 0,
    game2_score1 = 11, game2_score2 = 0,
    score1 = 22, score2 = 0
WHERE id = <match_id>;
```

### Resetting just playoffs (keep regular season):
```sql
DELETE FROM matches WHERE playoff = 1;
```

---

## 📊 Understanding Rankings

**Tie-breaking order:**
1. **Wins** (most important)
2. **Point differential** (total points scored - total points allowed)
3. **Points scored** (total points across all games)
4. **Alphabetical by last name** (final tiebreaker)

**Double forfeits:**
- Both players get a loss
- No points scored by either player
- Negatively impacts rankings

**BYEs:**
- Do NOT count as wins
- Do NOT affect statistics
- Player simply advances that week

**Note:** Head-to-head record is NOT considered in tiebreaking (could be a future enhancement)

---

## 🔒 Security Best Practices

1. **Never share admin password** - Each admin should have their own environment setup
2. **Use strong passwords** - Minimum 12 characters, mix of letters/numbers/symbols
3. **Log out when done** - Don't leave sessions open on shared computers
4. **Regular backups** - Copy `league.db` file periodically
5. **Monitor admin actions** - Check match deletion/regeneration frequency
6. **Rotate passwords** - Change `ADMIN_PASSWORD` every season

---

## 💾 Backup & Recovery

**Before major operations, backup the database:**

```bash
# Backup
cp league.db league_backup_$(date +%Y%m%d_%H%M%S).db

# List backups
ls -lh league_backup_*.db

# Restore from backup
cp league_backup_20250115_143022.db league.db
```

**Automated backup script (optional):**
```bash
#!/bin/bash
# Save as backup_db.sh
cd /path/to/cisco_ping_pong
cp league.db "backups/league_$(date +%Y%m%d_%H%M%S).db"
# Keep only last 10 backups
ls -t backups/league_*.db | tail -n +11 | xargs rm -f
```

---

## 🚀 Quick Reference

| Task | Steps | Danger Level |
|------|-------|--------------|
| Advance 1 week | Update week number + confirm checkbox | 🟢 Safe |
| Jump multiple weeks | Update week number + confirm checkbox (auto-forfeits) | 🟡 Medium |
| Delete unreported player | Click Delete on player | 🟢 Safe |
| Delete active player | ❌ Blocked by system | 🔴 Not allowed |
| Regenerate schedule | Click Generate + confirm (destroys matches) | 🔴 Dangerous |
| Force playoffs | Click Start Playoffs + confirm | 🟡 Medium |
| Delete match | Click Delete on match | 🟡 Medium |
| Reset league | Click Reset + confirm (destroys everything) | 🔴 Dangerous |

---

## 📞 Support

**Application Issues:**
- Check EDGE_CASES.md for known issues and fixes
- Review error messages carefully - they guide you to solutions

**Deployment Issues:**
- Verify `ADMIN_PASSWORD` environment variable is set on Render
- Check Render logs for detailed error messages
- Ensure disk mount is configured for database persistence

**Feature Requests:**
- Document desired functionality
- Consider implications on existing data
- Test in development environment first

---

## 🎓 Pro Tips

1. **Test in development first** - Run locally before making changes in production
2. **Use week rollback** - If you advance too far, roll back to reopen matches
3. **Preview playoffs** - Check "Preview Playoff Bracket" before forcing playoffs
4. **Monitor session timeout** - Refresh admin page if inactive for 25+ minutes
5. **Delete test matches immediately** - Don't let test data pollute standings
6. **Communicate with players** - Let them know when weeks advance
7. **Backup before resets** - Always backup database before destructive operations
8. **Check current week** - Always visible in admin dashboard header

---

**Version:** 2.0
**Last Updated:** October 2025
**Admin Session Timeout:** 30 minutes (configurable)
