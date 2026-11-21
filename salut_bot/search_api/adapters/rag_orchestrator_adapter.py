import os
import logging
import requests

logger = logging.getLogger(__name__)

class RagOrchestratorAdapter:
    def __init__(self):
        self.base_url = os.getenv("RAG_ORCHESTRATOR_URL", "http://rag-orchestrator:8000")

    def search(self, query: str, object_type: str = "all", limit: int = 20) -> list:
        try:
            logger.info(f"RAG Adapter: query='{query}', limit={limit}")
            resp = requests.post(
                f"{self.base_url}/search",
                json={"query": query, "top_k": limit},
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for obj in data.get("objects", []):
                    # Возвращаем в формате, который ожидает search_use_case
                    results.append({
                        "content": obj.get("name", ""),
                        "source": "rag_orchestrator",
                        "similarity": obj.get("similarity", 0),
                        "object_name": obj.get("name", ""),
                        "db_id": obj.get("db_id")
                    })
                return results
            else:
                logger.error(f"RAG returned {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"RAG connection error: {e}")
        return []