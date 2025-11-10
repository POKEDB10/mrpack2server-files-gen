# Running Locally

This application works both locally and on Render.com. Here's how to run it locally:

## Quick Start

### Option 1: Simple Local Runner (Recommended for Development)

**Windows:**
```bash
run_local.bat
```

**Linux/Mac:**
```bash
chmod +x run_local.sh
./run_local.sh
```

**Or directly with Python:**
```bash
python run_local.py
```

This will start the app on `http://127.0.0.1:8090` with debug mode enabled.

### Option 2: Production-like with Gunicorn

```bash
bash start.sh
```

This uses Gunicorn (same as Render) and is good for testing production behavior locally.

### Option 3: Direct Python

```bash
python app.py
```

## Environment Variables

You can customize the local setup with environment variables:

```bash
# Windows (PowerShell)
$env:PORT=8090
$env:HOST="127.0.0.1"
$env:DEBUG="True"
python app.py

# Linux/Mac
export PORT=8090
export HOST=127.0.0.1
export DEBUG=True
python app.py
```

### Available Variables:

- `PORT`: Server port (default: `8090`)
- `HOST`: Server host (default: `0.0.0.0` for `app.py`, `127.0.0.1` for `run_local.py`)
- `DEBUG`: Enable debug mode (default: `False` for `app.py`, `True` for `run_local.py`)
- `PRIMARY_WORKER`: Set to `1` for full logging (auto-set by scripts)
- `RUNNING_LOCALLY`: Set to `1` for local development mode (auto-set by scripts)
- `COUNT_FILE_DIR`: Directory for the server count file (for syncing between environments)

## Storage Locations

### Local Development:
- **Server files**: `{temp_dir}/servers/`
- **Forge cache**: `{temp_dir}/forge_cache/`
- **Quilt cache**: `{temp_dir}/quilt_cache/`
- **Count file**: `{project_root}/generated_server_count.txt` (project directory, persistent)

### Render.com:
- **Server files**: `/opt/render/project/src/data/servers/`
- **Forge cache**: `/opt/render/project/src/data/forge_cache/`
- **Quilt cache**: `/opt/render/project/src/data/quilt_cache/`
- **Count file**: `/opt/render/project/src/data/generated_server_count.txt`

The app automatically detects the environment and uses the appropriate storage location.

## Syncing Count Between Local and Docker/Render

To keep the same server count value across local development and Docker/Render deployments:

### Option 1: Use COUNT_FILE_DIR Environment Variable

**Local (Windows PowerShell):**
```powershell
$env:COUNT_FILE_DIR="C:\path\to\shared\data"
python run_local.py
```

**Local (Linux/Mac):**
```bash
export COUNT_FILE_DIR="/path/to/shared/data"
python run_local.py
```

**Docker:**
```bash
docker run -v /host/path/to/data:/data -e COUNT_FILE_DIR=/data ...
```

**Docker Compose:**
```yaml
services:
  app:
    volumes:
      - ./data:/data
    environment:
      - COUNT_FILE_DIR=/data
```

**Render:**
Set `COUNT_FILE_DIR` environment variable in Render dashboard to `/opt/render/project/src/data` (or your disk mount path).

### Option 2: Manual Sync

1. **Export from one environment:**
   - Copy the `generated_server_count.txt` file

2. **Import to another environment:**
   - Place the file in the count file directory
   - Restart the application

The count file location is logged at startup - check the logs to see where it's stored.

## Accessing the App

Once running:
- **Main app**: http://127.0.0.1:8090
- **Admin dashboard**: http://127.0.0.1:8090/admin/dashboard
- **Health check**: http://127.0.0.1:8090/health

## Troubleshooting

### Port Already in Use
```bash
# Change the port
export PORT=8091
python run_local.py
```

### Permission Errors
- Make sure you have write permissions in the current directory
- The app will fall back to temp directories if needed

### Java Not Found
- Install Java (required for server generation)
- The app will show a warning but may still work for some operations

### Module Not Found
```bash
# Install dependencies
pip install -r requirements.txt
```

## Differences: Local vs Render

| Feature | Local | Render |
|---------|-------|--------|
| Storage | Temp directories | Persistent disk mount |
| Workers | Single process | Multiple Gunicorn workers |
| Logging | Full logging | Primary worker only |
| Debug | Can enable | Disabled |
| Auto-reload | Yes (with debug) | No |

The code automatically adapts to both environments!

