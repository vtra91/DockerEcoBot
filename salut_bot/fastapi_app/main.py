import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi_app.routes import all_routers

BASE_PATH = os.getenv("BASE_PATH", "/ecoassistant").rstrip("/")

app = FastAPI(
    title="EcoAssistant Search API (Salut Bot)",
    root_path=BASE_PATH,
)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in all_routers:
    app.include_router(r)


@app.get("/health")
async def health_check():
    return {"status": "ok", "base_path": BASE_PATH}
