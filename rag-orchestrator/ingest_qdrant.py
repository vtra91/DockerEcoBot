import os
import psycopg2
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http import models

DB_HOST = os.getenv("DB_HOST", "db")
DB_PASSWORD = os.getenv("DB_PASSWORD")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
MODEL_PATH = "/app/embedding_models/bge-m3"
COLLECTION_NAME = "eco_objects"

print("Загрузка модели...")
model = SentenceTransformer(MODEL_PATH)
client = QdrantClient(host=QDRANT_HOST, port=6333)

conn = psycopg2.connect(host=DB_HOST, dbname="eco", user="postgres", password=DB_PASSWORD)
cur = conn.cursor()
# Берем 200 объектов для теста
cur.execute("""
    SELECT id, db_id, object_properties 
    FROM eco_assistant.object 
    WHERE object_properties != '{}'::jsonb 
""")
rows = cur.fetchall()
conn.close()

points = []
print(f"Найдено {len(rows)} объектов. Генерация эмбеддингов...")

for r in rows:
    obj_id, db_id, props = r
    name = props.get("Вид (русское название)") or props.get("Вид (латинское название)") or props.get("Название") or "Без названия"
    
    # Генерируем вектор
    emb = model.encode(name, normalize_embeddings=True).tolist()
    
    points.append(models.PointStruct(
        id=obj_id,
        vector=emb,
        payload={"db_id": db_id, "name": name, "object_type": props.get("Тип ОФФ")}
    ))

print(f"Заливка {len(points)} точек в Qdrant батчами...")
BATCH_SIZE = 200  # Отправляем по 200 точек за раз

for i in range(0, len(points), BATCH_SIZE):
    batch = points[i:i+BATCH_SIZE]
    client.upsert(collection_name=COLLECTION_NAME, points=batch)
    print(f"Загружено: {min(i + BATCH_SIZE, len(points))}/{len(points)}")

print("Готово!")