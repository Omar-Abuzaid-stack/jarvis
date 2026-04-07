# ☁️ How to Host JARVIS in the Cloud

Follow these 3 simple steps to move the JARVIS "Brain" to the cloud and solve your storage/port issues forever. I recommend using **Railway.app** because it is the easiest.

### Step 1: Push to GitHub
1. Create a **Private** repository on GitHub named `jarvis`.
2. Open your terminal in `~/Desktop/jarvis` and run:
   ```bash
   git init
   git add .
   git commit -m "Initialize Jarvis Cloud"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/jarvis.git
   git push -u origin main
   ```

### Step 2: Deploy to Railway
1. Go to [Railway.app](https://railway.app) and log in with GitHub.
2. Click **"New Project"** -> **"Deploy from GitHub repo"**.
3. Select your `jarvis` repo.
4. Click **Variables** and add your `MISTRAL_API_KEY` and any other secrets from your local `.env`.
5. Railway will automatically see the `railway.json` I created and start the server.

### Step 3: Link your Mac to the Cloud
1. Once deployed, Railway will give you a URL (e.g., `https://jarvis-production.up.railway.app`).
2. Run the local JARVIS helper pointing to that URL:
   ```bash
   open -n -a ~/Desktop/jarvis/macos-assistant/JarvisAssistant.app --args https://your-railway-url.com
   ```

---

### 🚮 Immediate Disk Cleanup (Run these NOW)
To make your Mac work while you set this up, run these exactly as shown:
```bash
rm -rf ~/Library/Logs/Jarvis/*.log
rm -rf ~/Library/Developer/Xcode/DerivedData/*
rm -rf ~/.Trash/*
```
