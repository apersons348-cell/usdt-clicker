from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI()

# CORS на время разработки (потом ужесточим)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")
INDEX_PATH = os.path.join(WEBAPP_DIR, "index.html")

# статика (css/js/img)
app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

@app.get("/")
def home():
    if os.path.exists(INDEX_PATH):
        return FileResponse(INDEX_PATH)
    return JSONResponse({"error": "index.html not found", "path": INDEX_PATH}, status_code=404)

@app.get("/api/health")
def health():
    return {"ok": True}
