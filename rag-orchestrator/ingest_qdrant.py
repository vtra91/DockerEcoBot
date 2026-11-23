import os
import sys
import psycopg2
from datetime import datetime
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http import models

DB_HOST = os.getenv("DB_HOST", "db")
DB_PASSWORD = os.getenv("DB_PASSWORD")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
MODEL_PATH = "/app/embedding_models/bge-m3"
COLLECTION_NAME = "eco_objects"
SYNC_FILE = "/app/state/last_sync.txt"

def run_ingestion(full_reindex=False):
    print(f"Загрузка модели. Full reindex: {full_reindex}")
    model = SentenceTransformer(MODEL_PATH)
    client = QdrantClient(host=QDRANT_HOST, port=6333)

    # Читаем время последней синхронизации
    last_sync = "1970-01-01 00:00:00+00"
    if not full_reindex and os.path.exists(SYNC_FILE):
        with open(SYNC_FILE, "r") as f:
            last_sync = f.read().strip()

    conn = psycopg2.connect(host=DB_HOST, dbname="eco", user="postgres", password=DB_PASSWORD)
    cur = conn.cursor()
    
    print(f"Ищем объекты, измененные после: {last_sync}")
    cur.execute("""
        SELECT id, db_id, object_properties, updated_at 
        FROM eco_assistant.object 
        WHERE object_properties != '{}'::jsonb 
        AND updated_at > %s::timestamptz
    """, (last_sync,))
    
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("Нет новых или измененных объектов.")
        return

    print(f"Найдено {len(rows)} объектов. Генерация эмбеддингов БАТЧАМИ...")

    ENCODE_BATCH = 64
    UPSERT_BATCH = 200
    points = []
    max_updated_at = last_sync

    for i in range(0, len(rows), ENCODE_BATCH):
        batch_rows = rows[i:i+ENCODE_BATCH]
        texts = []
        for r in batch_rows:
            props = r[2]
            name = (props.get("Вид (русское название)") or 
                    props.get("Вид (латинское название)") or 
                    props.get("Название") or "Без названия")
            texts.append(name)
        
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        
        for (r, emb) in zip(batch_rows, embeddings):
            obj_id, db_id, props, updated_at = r
            name = (props.get("Вид (русское название)") or 
                    props.get("Вид (латинское название)") or 
                    props.get("Название") or "Без названия")
            points.append(models.PointStruct(
                id=obj_id,
                vector=emb.tolist(),
                payload={"db_id": db_id, "name": name, "object_type": props.get("Тип ОФФ")}
            ))
            if str(updated_at) > max_updated_at:
                max_updated_at = str(updated_at)

        print(f"Закодировано: {min(i + ENCODE_BATCH, len(rows))}/{len(rows)}")

    print(f"Заливка {len(points)} векторов в Qdrant")
    for i in range(0, len(points), UPSERT_BATCH):
        client.upsert(collection_name=COLLECTION_NAME, points=points[i:i+UPSERT_BATCH])

    # Сохраняем время последней синхронизации
    with open(SYNC_FILE, "w") as f:
        f.write(max_updated_at)
    
    print(f"Готово. Обновлено {len(points)} объектов. Последняя синхронизация: {max_updated_at}")

if __name__ == "__main__":
    full = "--full" in sys.argv
    run_ingestion(full_reindex=full)