# Deploying to Render.com Free Plan

This guide will help you deploy the Minecraft Server File Generator to Render.com's free plan.

## Running Locally

Before deploying, you can test the application locally:

### Option 1: Using the local runner script (Recommended)
```bash
# Linux/Mac
chmod +x run_local.sh
./run_local.sh

# Windows
run_local.bat

# Or directly with Python
python run_local.py
```

### Option 2: Using Gunicorn (Production-like)
```bash
bash start.sh
```

### Option 3: Direct Python (Development)
```bash
python app.py
```

The app will be available at:
- **Local**: http://127.0.0.1:8090
- **Network**: http://0.0.0.0:8090 (if HOST=0.0.0.0)

---

## Deploying to Render.com Free Plan

## Prerequisites

1. A GitHub account
2. A Render.com account (sign up at https://render.com)
3. Your code pushed to a GitHub repository

## Step 1: Push Your Code to GitHub

If you haven't already, push your code to GitHub:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

## Step 2: Create a New Web Service on Render

1. Go to https://dashboard.render.com
2. Click "New +" and select "Web Service"
3. Connect your GitHub account if you haven't already
4. Select your repository: `mrpack2server-files-gen`
5. Configure the service:
   - **Name**: `mrpack2server-files-gen` (or any name you prefer)
   - **Region**: Choose the closest region (Oregon recommended)
   - **Branch**: `main`
   - **Root Directory**: Leave empty (root)
   - **Runtime**: `Python 3`
   - **Build Command**: 
     ```bash
     pip install --upgrade pip && pip install -r requirements.txt && bash setup_java.sh
     ```
   - **Start Command**: 
     ```bash
     bash start.sh
     ```
   - **Plan**: `Free`

## Step 3: Configure Environment Variables

In the Render dashboard, go to your service → Environment tab and add:

- `PORT`: `8090` (Render will override this, but set it as default)
- `PRIMARY_WORKER`: `1`
- `SECRET_KEY`: Click "Generate" to auto-generate a secure key
- `RENDER_DISK_PATH`: `/opt/render/project/src/data` (for persistent storage)

## Step 4: Add Persistent Disk (Optional but Recommended)

1. In your service settings, go to "Disks"
2. Click "Add Disk"
3. Configure:
   - **Name**: `server-storage`
   - **Mount Path**: `/opt/render/project/src/data`
   - **Size**: `1 GB` (free plan limit)

This disk will persist data across deployments and restarts.

## Step 5: Deploy

1. Click "Create Web Service"
2. Render will start building and deploying your application
3. Wait for the build to complete (usually 5-10 minutes)
4. Your app will be available at: `https://your-service-name.onrender.com`

## Important Notes for Free Plan

### Limitations:
- **Sleep after inactivity**: Free services sleep after 15 minutes of inactivity. The first request after sleep may take 30-60 seconds to wake up.
- **Memory**: 512 MB RAM limit
- **CPU**: Shared CPU resources
- **Storage**: Ephemeral (data is lost on restart unless using persistent disk)
- **Build time**: 90 minutes max

### Optimizations Made:
- Reduced worker count for free tier memory constraints
- Using persistent disk for server files and cache
- Optimized for graceful shutdowns
- Health check endpoint configured

## Step 6: Access Your Application

Once deployed:
- Main app: `https://your-service-name.onrender.com`
- Admin dashboard: `https://your-service-name.onrender.com/admin/dashboard`
- Health check: `https://your-service-name.onrender.com/health`

## Troubleshooting

### Build Fails
- Check build logs in Render dashboard
- Ensure all dependencies are in `requirements.txt`
- Verify `setup_java.sh` has execute permissions

### App Crashes
- Check logs in Render dashboard
- Verify environment variables are set correctly
- Check memory usage (free plan has 512MB limit)

### Files Not Persisting
- Ensure persistent disk is mounted correctly
- Check `RENDER_DISK_PATH` environment variable
- Verify disk mount path matches in render.yaml

### Slow Performance
- Free plan has shared CPU - this is normal
- First request after sleep will be slow (wake-up time)
- Consider upgrading to paid plan for better performance

## Updating Your Deployment

To update your app:
1. Push changes to your GitHub repository
2. Render will automatically detect and deploy the changes
3. Or manually trigger a deploy from the Render dashboard

## Monitoring

- View logs: Render dashboard → Your service → Logs
- View metrics: Render dashboard → Your service → Metrics
- Set up alerts: Render dashboard → Your service → Alerts

## Support

- Render Docs: https://render.com/docs
- Render Community: https://community.render.com
- Render Status: https://status.render.com

