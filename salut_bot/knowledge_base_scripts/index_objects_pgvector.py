"""
Индексатор: Postgres -> bge-m3 ->vector.

Читает объекты из eco_assistant.object, генерирует эмбеддинги,
пишет в eco_assistant.object_embeddings.
"""
import os
import sys
import psycopg2
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

DB_HOST = os.getenv("DB_HOST", "db")
DB_NAME = os.getenv("DB_NAME", "eco")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "changeme")
MODEL_PATH = "/app/embedding_models/bge-m3"
BATCH_SIZE = 32


def build_text(obj_props: dict, synonyms: list) -> str:
    """Обогащаем текст для эмбеддинга — добавляем контекст.
    Если объект пустой — возвращаем пустую строку (такие пропускаем)."""
    # Пропускаем пустые
    if not obj_props and not synonyms:
        return ""

    parts = []
    # Имя
    for key in ("Вид (русское название)", "Вид_ru", "Название", "название",
                "scientific_name", "Вид (латинское название)"):
        if obj_props.get(key):
            parts.append(str(obj_props[key]))
    # Тип
    if obj_props.get("Тип ОФФ"):
        parts.append(f"Тип: {obj_props['Тип ОФФ']}")
    if obj_props.get("Подтип объекта"):
        parts.append(f"Подтип: {obj_props['Подтип объекта']}")
    # Семейство, род
    for key in ("Семейство (русское название)", "Семейство (латинское название)",
                "Род (русское название)", "Род (латинское название)"):
        if obj_props.get(key):
            parts.append(str(obj_props[key]))
    # Редкость
    rarity = obj_props.get("Редкость") or obj_props.get("редкость")
    if rarity:
        parts.append(f"Редкость: {rarity}")
    # Локация
    for key in ("Расположение относительно Байкала", "расположение относительно байкала"):
        if obj_props.get(key):
            parts.append(f"Расположение: {obj_props[key]}")
    if obj_props.get("Детальное расположение"):
        parts.append(f"Место: {obj_props['Детальное расположение']}")
    # Описание (если есть)
    desc = (obj_props.get("Описание") or obj_props.get("описание")
            or obj_props.get("description") or "")
    if desc:
        parts.append(str(desc)[:500])
    # Синонимы
    if synonyms:
        parts.append("Синонимы: " + ", ".join(synonyms))

    return ". ".join(parts) if parts else ""


def main():
    print(f"Загрузка модели {MODEL_PATH}...")
    model = SentenceTransformer(MODEL_PATH)
    print(f"Модель загружена. Размерность: {model.get_sentence_embedding_dimension()}")

    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()

    print("Чтение объектов из Postgres...")
    cur.execute("""
        SELECT o.id, o.object_properties,
               array_agg(s.synonym) FILTER (WHERE s.synonym IS NOT NULL) AS synonyms
        FROM eco_assistant.object o
        LEFT JOIN eco_assistant.object_name_synonym_link osl ON o.id = osl.object_id
        LEFT JOIN eco_assistant.object_name_synonym s ON osl.synonym_id = s.id
        GROUP BY o.id
        ORDER BY o.id
    """)
    rows = cur.fetchall()
    print(f"Всего объектов: {len(rows)}")

    print("Генерация эмбеддингов и запись в pgvector...")
    for i in tqdm(range(0, len(rows), BATCH_SIZE)):
        batch = rows[i:i + BATCH_SIZE]
        texts = [build_text(r[1] or {}, r[2] or []) for r in batch]
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

        for (obj_id, _, _), text, emb in zip(batch, texts, embeddings):
            if not text.strip():
                continue  # <- ПРОПУСКАЕМ ПУСТЫЕ
            cur.execute("""
                INSERT INTO eco_assistant.object_embeddings (object_id, embedding, text_for_embedding)
                VALUES (%s, %s::vector, %s)
                ON CONFLICT (object_id) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    text_for_embedding = EXCLUDED.text_for_embedding
            """, (obj_id, emb.tolist(), text))
        conn.commit()

    cur.execute("SELECT COUNT(*) FROM eco_assistant.object_embeddings")
    count = cur.fetchone()[0]
    print(f"\nГотово. В object_embeddings: {count} записей.")
    conn.close()


if __name__ == "__main__":
    main()