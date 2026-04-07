import asyncio
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
import subprocess
import os
import logging
import time

LOG_PATH = "/tmp/mobile-gateway.out.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH)
    ]
)
logger = logging.getLogger("mobile_gateway")

app = FastAPI()

JARVIS_URL = "http://127.0.0.1:8340"
NGROK_CONFIG = "/Users/user/Library/Application Support/ngrok/ngrok.yml"
BASE_DIR = "/Users/user/Desktop/jarvis"
PYTHON_BIN = f"{BASE_DIR}/venv/bin/python3"
HELPER_BIN = f"{BASE_DIR}/macos-assistant/JarvisAssistant"

# Aggregate all likely PYTHONPATH candidates from the installer and workspace
PYTHONPATH_CANDIDATES = [
    BASE_DIR,
    "/Users/user/OpenJarvis/src",
    "/Users/user/Desktop/OpenJarvis/src",
    "/Users/user/Desktop/openjarvis/src"
]

env = os.environ.copy()
env["PYTHONPATH"] = ":".join([p for p in PYTHONPATH_CANDIDATES if os.path.isdir(p)] + [env.get("PYTHONPATH", "")])

async def check_backend_up():
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{JARVIS_URL}/api/health")
            # If it responds at all (even 405), the server is alive
            return resp.status_code < 500
    except:
        return False

async def start_all_services():
    logger.info("Initializing full JARVIS system startup...")
    
    # Ensure backend starts with the correct pathing
    if not await check_backend_up():
        logger.info(f"Starting JARVIS backend (8340) with PYTHONPATH={env['PYTHONPATH']}...")
        subprocess.Popen([
            PYTHON_BIN, "-m", "uvicorn", "server:app",
            "--host", "127.0.0.1", "--port", "8340"
        ], cwd=BASE_DIR, env=env, stdout=open("/tmp/jarvis_server.log", "a"), stderr=subprocess.STDOUT)
        
        for _ in range(20):
            await asyncio.sleep(2)
            if await check_backend_up():
                logger.info("Backend is ONLINE.")
                break
    
    # Helper
    helper_check = subprocess.run(["pgrep", "-f", "JarvisAssistant"], capture_output=True)
    if helper_check.returncode != 0:
        logger.info("Starting macOS Assistant Helper...")
        subprocess.Popen([HELPER_BIN, JARVIS_URL], cwd=BASE_DIR, env=env)

async def full_watchdog():
    while True:
        try:
            if not await check_backend_up():
                logger.warning("Backend offline. Restarting...")
                subprocess.Popen([
                    PYTHON_BIN, "-m", "uvicorn", "server:app",
                    "--host", "127.0.0.1", "--port", "8340"
                ], cwd=BASE_DIR, env=env, stdout=open("/tmp/jarvis_server.log", "a"), stderr=subprocess.STDOUT)
            
            # Ngrok check
            ngrok_up = False
            async with httpx.AsyncClient(timeout=2.0) as client:
                try:
                    resp = await client.get("http://127.0.0.1:4040/api/tunnels")
                    if resp.status_code == 200:
                        tunnels = resp.json().get("tunnels", [])
                        if any(t.get("name") == "jarvis" for t in tunnels):
                            ngrok_up = True
                except:
                    pass
            
            if not ngrok_up:
                logger.warning("Ngrok offline. Restarting tunnel...")
                subprocess.run(["pkill", "-9", "-f", "ngrok"], capture_output=True)
                await asyncio.sleep(1)
                subprocess.Popen([
                    "/usr/local/bin/ngrok", "start", "jarvis", 
                    "--config", NGROK_CONFIG
                ], env=env)
                
            await asyncio.sleep(20)
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(10)

@app.get("/")
@app.get("/{path:path}")
async def proxy_or_fallback(request: Request, path: str = ""):
    raw_query = request.url.query
    user_agent = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in user_agent for m in ["iphone", "android", "mobile"])
    
    source = "mobile" if is_mobile else "browser"
    if is_mobile and (not path or path == "/"):
        return Response(status_code=307, headers={"Location": "/phone"})

    full_url = f"{JARVIS_URL}/{path}"
    query_param = f"source={source}"
    full_url += f"?{raw_query}&{query_param}" if raw_query else f"?{query_param}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method=request.method,
                url=full_url,
                headers=dict(request.headers),
                content=await request.body(),
                follow_redirects=True
            )
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
    except:
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>JARVIS</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ background: #000; color: #00d2ff; font-family: 'Inter', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; }}
                .orb {{ width: 80px; height: 80px; background: radial-gradient(circle, #00d2ff 0%, #003344 100%); border-radius: 50%; margin: 0 auto 1.5rem; animation: pulse 2s infinite; opacity: 0.8; }}
                @keyframes pulse {{ 0% {{ transform: scale(0.95); opacity: 0.6; }} 50% {{ transform: scale(1.02); opacity: 1; }} 100% {{ transform: scale(0.95); opacity: 0.6; }} }}
                h1 {{ font-weight: 200; letter-spacing: 4px; margin: 0 0 1rem; color: #00d2ff; text-transform: uppercase; }}
                .status {{ font-family: monospace; font-size: 0.6rem; color: #00d2ff; text-transform: uppercase; padding: 4px 10px; border: 1px solid #00d2ff44; border-radius: 4px; display: inline-block; background: rgba(0, 210, 255, 0.1); }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="orb"></div>
                <h1>System Recovering</h1>
                <div class="status">Relay online. Reconnecting...</div>
            </div>
        </body>
        </html>
        """, status_code=503)

@app.on_event("startup")
async def app_startup():
    asyncio.create_task(start_all_services())
    asyncio.create_task(full_watchdog())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8341)
