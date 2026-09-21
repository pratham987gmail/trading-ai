# 🔐 Environment Variables Setup Guide - Render.com

This guide shows you **exactly** how to copy your Angel One credentials from your local `.env` file to Render.com.

---

## 📋 **WHAT YOU NEED**

You need these 4 values from your Angel One account:

| Variable Name | What It Is | Where You Got It |
|---------------|------------|------------------|
| `ANGEL_API_KEY` | Your API key | Angel One SmartAPI Portal |
| `ANGEL_CLIENT_ID` | Your client ID | Angel One login/trading account |
| `ANGEL_PASSWORD` | Your password | Angel One trading password |
| `ANGEL_TOTP_SECRET` | TOTP secret key | Angel One 2FA setup |

---

## 🔍 **STEP 1: FIND YOUR VALUES**

### **Option A: From Your Local .env File**

1. Open this file:
   ```
   c:\trading ai\trading ai\trading-ai\trading_system\.env
   ```

2. You'll see something like:
   ```bash
   ANGEL_API_KEY=OVrdNfkx
   ANGEL_CLIENT_ID=P53093898
   ANGEL_PASSWORD=YourPassword123
   ANGEL_TOTP_SECRET=ABCDEFGH12345678
   ```

3. **Copy each value** (the part after `=`)

### **Option B: From Angel One Portal**

If you don't have a `.env` file, get credentials from:
https://smartapi.angelbroking.com

---

## 🚀 **STEP 2: ADD TO RENDER.COM**

### **2.1 Open Render Dashboard**

1. Go to: https://dashboard.render.com
2. Log in to your account
3. Find your service: `paper-trading-system`
4. Click on it

### **2.2 Open Environment Settings**

1. Look at the **left sidebar**
2. Click **"Environment"**
3. You'll see a list of environment variables (might be empty)

### **2.3 Add Each Variable**

Click **"Add Environment Variable"** and add them **ONE BY ONE**:

#### **Variable 1: ANGEL_API_KEY**

```
Key:   ANGEL_API_KEY
Value: OVrdNfkx
```

(Use YOUR actual value from .env file)

Click **"Add"**

---

#### **Variable 2: ANGEL_CLIENT_ID**

```
Key:   ANGEL_CLIENT_ID
Value: P53093898
```

(Use YOUR actual value from .env file)

Click **"Add"**

---

#### **Variable 3: ANGEL_PASSWORD**

```
Key:   ANGEL_PASSWORD
Value: YourActualPassword
```

(Use YOUR actual password)

Click **"Add"**

---

#### **Variable 4: ANGEL_TOTP_SECRET**

```
Key:   ANGEL_TOTP_SECRET
Value: YourActualTOTPSecret
```

(Use YOUR actual TOTP secret - usually a long string)

Click **"Add"**

---

### **2.4 Save Changes**

1. After adding all 4 variables, click **"Save Changes"** at the bottom
2. Render will automatically **restart your service** (takes ~30 seconds)
3. Your paper trading system will reconnect with new credentials

---

## ✅ **STEP 3: VERIFY IT'S WORKING**

### **Check the Logs:**

1. In Render dashboard, click **"Logs"** tab
2. Wait 10-20 seconds for service to restart
3. Look for these SUCCESS messages:

```
[INFO] 🔍 Checking environment variables...
[INFO] ✅ All environment variables configured
[INFO] 📝 Creating .env file...
[INFO] ✅ .env file created
[INFO] Connecting to Angel One (attempt 1/3)...
[INFO] ✅ Connected to Angel One successfully (Client: P53093898)
[INFO] NIFTY: ₹23,683.55
```

### **If You See This = SUCCESS! ✅**

Your system is connected and monitoring NIFTY!

---

## 🐛 **TROUBLESHOOTING**

### **Error: "Missing Angel One credentials"**

**Problem:** One or more variables not set

**Solution:**
1. Go back to Environment tab
2. Check all 4 variables are listed
3. Names must be EXACT (case-sensitive):
   - ✅ `ANGEL_API_KEY`
   - ❌ `angel_api_key` (wrong)
   - ❌ `ANGEL_API_KEY ` (extra space, wrong)

---

### **Error: "Authentication failed"**

**Problem:** Credentials are wrong

**Solution:**
1. Double-check each value from your .env file
2. Make sure NO extra spaces before/after value
3. Copy-paste directly (don't type manually)
4. If still failing, regenerate credentials in Angel One portal

---

### **Error: "Invalid TOTP"**

**Problem:** TOTP secret is wrong or expired

**Solution:**
1. Check `ANGEL_TOTP_SECRET` value
2. It should be a long string (16-32 characters)
3. Re-copy from your .env file carefully
4. If still failing, reset 2FA in Angel One and get new secret

---

### **Service Keeps Restarting**

**Problem:** Start script failing

**Solution:**
1. Check logs for specific error message
2. Most common: missing/wrong environment variables
3. Verify all 4 variables are set correctly
4. Try manual deploy: Click "Manual Deploy" → "Deploy latest commit"

---

## 📸 **VISUAL GUIDE**

### **What Environment Tab Looks Like:**

```
Environment Variables

┌─────────────────────┬──────────────────────────┐
│ Key                 │ Value                    │
├─────────────────────┼──────────────────────────┤
│ ANGEL_API_KEY       │ *********** (hidden)     │
│ ANGEL_CLIENT_ID     │ *********** (hidden)     │
│ ANGEL_PASSWORD      │ *********** (hidden)     │
│ ANGEL_TOTP_SECRET   │ *********** (hidden)     │
└─────────────────────┴──────────────────────────┘

[Add Environment Variable]  [Save Changes]
```

**Note:** Values are automatically hidden for security.

---

## 🔒 **SECURITY BEST PRACTICES**

### **DO:**
- ✅ Keep credentials secret
- ✅ Use Render's encrypted environment variables
- ✅ Enable 2FA on Render account
- ✅ Keep GitHub repo private
- ✅ Regenerate credentials if compromised

### **DON'T:**
- ❌ Commit .env file to GitHub
- ❌ Share credentials in chat/email
- ❌ Use same credentials on multiple platforms
- ❌ Hardcode credentials in source code
- ❌ Take screenshots of credentials

---

## 📝 **CHECKLIST**

Before considering setup complete:

- [ ] Opened .env file and copied all 4 values
- [ ] Logged into Render dashboard
- [ ] Opened service → Environment tab
- [ ] Added ANGEL_API_KEY
- [ ] Added ANGEL_CLIENT_ID
- [ ] Added ANGEL_PASSWORD
- [ ] Added ANGEL_TOTP_SECRET
- [ ] Clicked "Save Changes"
- [ ] Checked logs for "Connected to Angel One successfully"
- [ ] Seeing "NIFTY: ₹XX,XXX.XX" in logs

---

## 🎯 **WHAT'S NEXT**

Once environment variables are set and verified:

1. ✅ Your system is running 24/7
2. ✅ Monitoring NIFTY every 60 seconds
3. ✅ Will generate Long Put signals automatically
4. ✅ Trades saved to `paper_trades.json`

**Check logs daily** to monitor progress!

---

## 📞 **NEED HELP?**

- **Render Support:** https://render.com/support
- **Angel One API Docs:** https://smartapi.angelbroking.com/docs

---

## ✅ **DONE!**

Your environment variables are configured. Return to the main deployment guide to complete setup.

See: `RENDER_DEPLOYMENT_GUIDE.md`

