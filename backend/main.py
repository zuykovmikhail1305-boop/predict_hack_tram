from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.database import init_db
from app.routers import routes_router, stops_router, forecast_router, export_router, external_router, ml_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Трамваи — Прогноз пассажиропотока",
    description="Веб-сервис для ИИ-прогнозирования загрузки трамвайных маршрутов",
    version="1.0.0",
    lifespan=lifespan,
)

# Пути вычисляются относительно этого файла, поэтому запуск работает
# из любой рабочей директории (корень проекта, папка backend/, Docker).
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

templates = Jinja2Templates(directory=FRONTEND_DIR / "templates")

# Раздача статики: /static/css/style.css, /static/js/app.js и т.д.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_router.router)
app.include_router(stops_router.router)
app.include_router(forecast_router.router)
app.include_router(export_router.router)
app.include_router(external_router.router)
app.include_router(ml_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/index")
async def index(request: Request):
    return templates.TemplateResponse(name="index.html", request=request, context={})

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(name="code.html", request=request, context={})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)