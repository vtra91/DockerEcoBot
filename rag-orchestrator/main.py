import os
import subprocess
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http import models
import sys  

app = FastAPI(title="RAG Orchestrator (Qdrant)")

EMBEDDING_BASE_DIR = os.getenv("EMBEDDING_MODELS_BASE_DIR", "/app/embedding_models")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "bge-m3")
MODEL_PATH = f"{EMBEDDING_BASE_DIR}/{EMBEDDING_MODEL}"

QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

print(f"Загрузка модели: {MODEL_PATH}")
model = SentenceTransformer(MODEL_PATH)
print("Модель загружена.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаемся к Qdrant
client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
COLLECTION_NAME = "eco_objects"

# Создаем коллекцию, если её нет
if not client.collection_exists(COLLECTION_NAME):
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
    )
    print(f"Создана коллекция {COLLECTION_NAME}")

class SearchRequest(BaseModel):
    query: str
    slots: dict = {}
    top_k: int = 5

@app.get("/health")
async def health():
    return {"status": "ok", "model": EMBEDDING_MODEL, "db": "qdrant"}

@app.post("/ingest")
async def ingest_data(full: bool = False):
    """
    Запускает индексацию в фоне.
    Если full=true — пересчитывает ВСЕ объекты.
    Если full=false — только новые/измененные.
    """
    args = ["python", "-u", "/app/ingest_qdrant.py"] 
    if full:
        args.append("--full")
    
    # Запускаем в отдельном процессе
    subprocess.Popen(args, stdout=sys.stdout, stderr=sys.stderr)
    
    mode = "полная переиндексация" if full else "инкрементальное обновление"
    return {"status": "started", "message": f"Запущена {mode}"}

@app.post("/search")
async def search(req: SearchRequest):
    # 1. Эмбеддинг запроса
    emb = model.encode(req.query, normalize_embeddings=True).tolist()

    # 2. Поиск в Qdrant
    search_result = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=emb,
        limit=req.top_k,
        with_payload=True
    )

    # 3. Формируем ответ
    objects = []
    for hit in search_result:
        objects.append({
            "id": hit.id,
            "db_id": hit.payload.get("db_id"),
            "name": hit.payload.get("name"),
            "similarity": hit.score,
        })

    return {
        "objects": objects,
        "debug": {"query": req.query, "count": len(objects), "engine": "qdrant"}
    }