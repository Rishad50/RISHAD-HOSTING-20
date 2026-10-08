import os
import sys
import json
import shutil
import zipfile
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Control Panel API")

# CORS এনাবল করা (যেকোনো ডোমেইন বা পোর্ট থেকে কল করার জন্য)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ইউজার ফাইল সংরক্ষণ ও সার্ভার রান করার মূল ডিরেক্টরি
BASE_WORKSPACE = Path("./workspace").resolve()
BASE_WORKSPACE.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = BASE_WORKSPACE / ".server_config.json"


# ==========================================
# হেল্পার ফাংশন (Path Traversal Security)
# ==========================================
def get_safe_path(rel_path: str) -> Path:
    """নিশ্চিত করে যে ফাইল পাথটি workspace ফোল্ডারের বাইরে না যায়"""
    rel_path = rel_path.lstrip("/\\")
    target = (BASE_WORKSPACE / rel_path).resolve()
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
    """রিয়েল-টাইম লগ পড়ার ব্যাকগ্রাউন্ড টাস্ক"""
    while True:
        line = await stream.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace")
        server_mgr.append_log(f"{prefix}{decoded}")


# ==========================================
# Pydantic Schemas (Request Models)
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
# ১. ফ্রন্টএন্ড পরিবেশন (HTML Serve)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = Path("index.html")
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>index.html ফাইলটি পাওয়া যায়নি! দয়া করে একই ফোল্ডারে রাখুন।</h2>")


# ==========================================
# ২. সার্ভার কন্ট্রোল API (Start, Stop, Restart)
# ==========================================
@app.post("/api/start/{server_id}")
async def start_server(server_id: str):
    if server_mgr.process and server_mgr.process.returncode is None:
        return {"message": "Server is already running!"}

    # কনফিগারেশন থেকে মেইন ফাইল ও requirements ফাইল বের করা
    cfg = get_startup_cfg()
    main_script = cfg.get("main_file", "main.py")
    req_file = cfg.get("req_file", "requirements.txt")
    
    script_path = BASE_WORKSPACE / main_script
    req_path = BASE_WORKSPACE / req_file

    # ১. requirements.txt ফাইল থাকলে আগে ইনস্টল করা হবে
    if req_path.exists() and req_path.is_file():
        server_mgr.append_log(f"\npip install -r {req_file}\n")
        try:
            pip_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-r", str(req_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BASE_WORKSPACE)
            )
            stdout, stderr = await pip_proc.communicate()
            if stdout:
                server_mgr.append_log(stdout.decode("utf-8", errors="replace"))
            if stderr:
                server_mgr.append_log(stderr.decode("utf-8", errors="replace"))

            if pip_proc.returncode != 0:
                server_mgr.append_log(f"[System] Warning: Failed to install some dependencies (Code: {pip_proc.returncode})\n")
        except Exception as e:
            server_mgr.append_log(f"[System] Error installing requirements: {str(e)}\n")

    # ২. স্ক্রিপ্ট না থাকলে ডামি ফাইল তৈরি করা
    if not script_path.exists():
        script_path.write_text("import time\nprint('Server started!')\nwhile True:\n    time.sleep(1)\n")

    # ৩. মেইন স্ক্রিপ্ট চালু করা
    server_mgr.append_log(f"\npython {main_script}\n")
    
    try:
        server_mgr.process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_WORKSPACE)
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
# ৩. টার্মিনাল লগস ও কমান্ড API
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
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_WORKSPACE)
        )
        stdout, stderr = await proc.communicate()
        if stdout:
            server_mgr.append_log(stdout.decode("utf-8", errors="replace"))
        if stderr:
            server_mgr.append_log(stderr.decode("utf-8", errors="replace"))
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
def get_startup_cfg():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except:
            pass
    return {"main_file": "main.py", "req_file": "requirements.txt"}


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
# সার্ভার চালু করার কোড
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # Render এর $PORT ধরবে, লোকাল পিসিতে থাকলে 8000 ব্যবহার করবে
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
