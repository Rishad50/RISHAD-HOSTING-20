import os
import sys
import json
import shutil
import zipfile
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Control Panel API")

# CORS এনাবল করা
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ইউজার ফাইল সংরক্ষণ এবং স্ক্রিপ্ট এক্সিকিউট করার ডিরেক্টরি
BASE_WORKSPACE = Path("./workspace").resolve()
BASE_WORKSPACE.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE_WORKSPACE / ".server_config.json"


# ==========================================
# সিকিউরিটি: Path Traversal রোধ করা
# ==========================================
def get_safe_path(rel_path: str = "") -> Path:
    clean = rel_path.lstrip("/\\") if rel_path else ""
    target = (BASE_WORKSPACE / clean).resolve()
    if not str(target).startswith(str(BASE_WORKSPACE)):
        raise HTTPException(status_code=400, detail="Invalid path / Path traversal detected!")
    return target


# ==========================================
# প্রসেস ও টার্মিনাল লগ ম্যানেজার
# ==========================================
class ServerProcessManager:
    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: list[str] = []
        self.max_logs: int = 3000

    def append_log(self, text: str):
        self.logs.append(text)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    def get_logs(self) -> str:
        return "".join(self.logs)

    def clear_logs(self):
        self.logs.clear()


server_mgr = ServerProcessManager()


async def stream_reader(stream, prefix=""):
    """রিয়েল-টাইম কনসোল লগ পড়ার জন্য ব্যাকগ্রাউন্ড টাস্ক"""
    while True:
        try:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            server_mgr.append_log(f"{prefix}{decoded}")
        except Exception:
            break


# ==========================================
# Pydantic মডেলস (Payload Validation)
# ==========================================
class CommandRequest(BaseModel):
    cmd: str
    server_id: str

class FileContentRequest(BaseModel):
    path: str
    content: str

class DeleteRequest(BaseModel):
    path: str

class RenameRequest(BaseModel):
    old_path: str
    new_path: str

class CreateFolderRequest(BaseModel):
    path: str
    folder_name: Optional[str] = None

class ExtractRequest(BaseModel):
    file_path: str
    target_path: Optional[str] = ""

class StartupConfigRequest(BaseModel):
    main_file: str
    req_file: Optional[str] = "requirements.txt"


# ==========================================
# ১. ফ্রন্টএন্ড পরিবেশন (HTML Route)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = Path("index.html")
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>index.html ফাইলটি পাওয়া যায়নি! একই ফোল্ডারে index.html ফাইলটি রাখুন।</h2>")


# ==========================================
# ২. সার্ভার লাইফসাইকেল কন্ট্রোল (Start / Stop / Restart)
# ==========================================
def get_startup_cfg():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"main_file": "main.py", "req_file": "requirements.txt"}


@app.post("/api/start/{server_id}")
async def start_server(server_id: str):
    if server_mgr.process and server_mgr.process.returncode is None:
        return {"message": "Server is already running!"}

    cfg = get_startup_cfg()
    main_script = cfg.get("main_file", "main.py")
    script_path = BASE_WORKSPACE / main_script

    if not script_path.exists():
        script_path.write_text("import time\nprint('Server started successfully!')\nwhile True:\n    time.sleep(1)\n")

    server_mgr.append_log(f"\n[System] Starting: python -u {main_script}...\n")

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        server_mgr.process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_WORKSPACE),
            env=env
        )

        asyncio.create_task(stream_reader(server_mgr.process.stdout))
        asyncio.create_task(stream_reader(server_mgr.process.stderr, prefix="[STDERR] "))
        return {"status": "started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stop/{server_id}")
async def stop_server(server_id: str):
    if server_mgr.process and server_mgr.process.returncode is None:
        try:
            server_mgr.process.terminate()
            await asyncio.sleep(0.5)
            if server_mgr.process.returncode is None:
                server_mgr.process.kill()
            server_mgr.append_log("\n[System] Server stopped successfully.\n")
            server_mgr.process = None
            return {"status": "stopped"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Server is not running"}


@app.post("/api/restart/{server_id}")
async def restart_server(server_id: str):
    await stop_server(server_id)
    await asyncio.sleep(0.5)
    return await start_server(server_id)


# ==========================================
# ৩. টার্মিনাল লগ ও নন-ব্লকিং কমান্ড এক্সিকিউটর
# ==========================================
@app.get("/api/logs/{server_id}")
async def get_logs(server_id: str):
    return {"logs": server_mgr.get_logs()}


@app.post("/api/clear_logs/{server_id}")
async def clear_logs(server_id: str):
    server_mgr.clear_logs()
    return {"status": "cleared"}


@app.post("/api/command")
async def run_command(payload: CommandRequest):
    cmd = payload.cmd.strip()
    if not cmd:
        return {"message": "Empty command"}

    server_mgr.append_log(f"\n$ {cmd}\n")
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_WORKSPACE),
            env=env
        )

        # ৩ সেকেন্ড অপেক্ষা করবে। যদি স্ক্রিপ্ট দীর্ঘ সময় ধরে চলে (যেমন বট/সার্ভার),
        # তাহলে ব্রাউজার হ্যাং না করে ব্যাকগ্রাউন্ড স্ট্রিমিং মোডে পাঠিয়ে দেবে।
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            if stdout:
                server_mgr.append_log(stdout.decode("utf-8", errors="replace"))
            if stderr:
                server_mgr.append_log(stderr.decode("utf-8", errors="replace"))
        except asyncio.TimeoutError:
            server_mgr.append_log("[Running in background...]\n")
            asyncio.create_task(stream_reader(proc.stdout))
            asyncio.create_task(stream_reader(proc.stderr, prefix="[STDERR] "))

        return {"status": "executed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ৪. ফাইল ম্যানেজার API (CRUD, Upload, Extract)
# ==========================================
@app.get("/api/files/{server_id}")
async def list_files(server_id: str, path: str = ""):
    target_dir = get_safe_path(path)
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    files_list = []
    try:
        for entry in os.scandir(target_dir):
            if entry.name == ".server_config.json":
                continue
            files_list.append({
                "name": entry.name,
                "is_dir": entry.is_dir()
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"files": files_list}


@app.get("/api/file/{server_id}")
async def read_file(server_id: str, path: str = ""):
    target_file = get_safe_path(path)
    if not target_file.exists() or target_file.is_dir():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target_file.read_text(encoding="utf-8", errors="replace")
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/file/{server_id}")
async def save_file(server_id: str, payload: FileContentRequest):
    target_file = get_safe_path(payload.path)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_file.write_text(payload.content, encoding="utf-8")
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/file/{server_id}")
async def delete_item(server_id: str, payload: DeleteRequest):
    target = get_safe_path(payload.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File/Folder not found")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rename/{server_id}")
async def rename_item(server_id: str, payload: RenameRequest):
    old_target = get_safe_path(payload.old_path)
    new_target = get_safe_path(payload.new_path)
    if not old_target.exists():
        raise HTTPException(status_code=404, detail="Item not found")
    try:
        old_target.rename(new_target)
        return {"status": "renamed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/create_folder/{server_id}")
async def create_folder(server_id: str, payload: CreateFolderRequest):
    target_folder = get_safe_path(payload.path)
    try:
        target_folder.mkdir(parents=True, exist_ok=True)
        return {"status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/extract/{server_id}")
async def extract_zip(server_id: str, payload: ExtractRequest):
    zip_path = get_safe_path(payload.file_path)
    target_dir = get_safe_path(payload.target_path or "")
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        raise HTTPException(status_code=400, detail="Invalid zip file")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)
        return {"status": "extracted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload/{server_id}")
async def upload_files(
    server_id: str,
    path: str = Form(""),
    file: List[UploadFile] = File(...)
):
    target_dir = get_safe_path(path)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        for f in file:
            dest = target_dir / f.filename
            with dest.open("wb") as buffer:
                shutil.copyfileobj(f.file, buffer)
        return {"status": "uploaded", "count": len(file)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ৫. স্টার্টআপ কনফিগারেশন API
# ==========================================
@app.get("/api/get_startup/{server_id}")
async def get_startup(server_id: str):
    return get_startup_cfg()


@app.post("/api/set_startup/{server_id}")
async def set_startup(server_id: str, payload: StartupConfigRequest):
    try:
        data = {"main_file": payload.main_file, "req_file": payload.req_file}
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# সার্ভার রানার (Render & Local Compatible)
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # Render-এর জন্য PORT ভেরিয়েবল ধরে নেবে, লোকাল কম্পিউটারে 8000 ব্যবহার করবে
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
