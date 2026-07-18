"""材料协同办理系统 — FastAPI 入口。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import audit, auth, dict_api, documents, items, oa, onlyoffice
from app.services.files import ensure_upload_dir
from app.services.scheduler import start_background_tasks, stop_background_tasks
from app.services.seed import seed_all
from app.services.startup_checks import check_production_secrets

settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"

# 生产安全：模拟 OA 标识仅允许 DEBUG 环境
if settings.oa_mock_enabled and not settings.debug:
    raise RuntimeError(
        "配置错误：OA_MOCK_ENABLED=true 时必须同时设置 DEBUG=true。"
        "正式环境请关闭 OA_MOCK_ENABLED。"
    )

app = FastAPI(
    title=settings.app_name,
    description="公安内网材料协同审核系统 MVP",
    version="1.0.0",
)


@app.on_event("startup")
def on_startup() -> None:
    # 再次校验（防止测试中动态改配置后仍误启动）
    s = get_settings()
    if s.oa_mock_enabled and not s.debug:
        raise RuntimeError(
            "配置错误：OA_MOCK_ENABLED=true 时必须同时设置 DEBUG=true"
        )
    # 示例 SECRET_KEY 等：只警告，不阻断启动
    check_production_secrets(s)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_upload_dir()
    init_db()
    db = SessionLocal()
    try:
        seed_all(db)
    finally:
        db.close()
    start_background_tasks()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_background_tasks()


app.include_router(auth.router)
app.include_router(items.router)
app.include_router(documents.router)
app.include_router(dict_api.router)
app.include_router(oa.router)
app.include_router(onlyoffice.router)
app.include_router(audit.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": settings.app_name}


# 静态前端
if FRONTEND_DIR.is_dir():
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/")
    def index():
        return RedirectResponse(url="/login.html")

    @app.get("/{page_name}.html")
    def html_page(page_name: str):
        path = FRONTEND_DIR / f"{page_name}.html"
        if path.is_file():
            return FileResponse(path)
        return RedirectResponse(url="/login.html")
