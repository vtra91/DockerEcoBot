import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from search_api.adapters.sqlalchemy_repository import SQLAlchemySearchRepository
from search_api.config import SearchConfig
from search_api.infrastructure import get_session, init_db

logger = logging.getLogger(__name__)
router = APIRouter()

# Роут может быть вызван раньше первого /search — гарантируем инициализацию БД.
init_db(SearchConfig.from_env())


class SimilarRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    object_type: Optional[str] = None
    exclude_db_id: Optional[str] = None
    limit: int = 3


@router.post("/objects/similar")
async def similar_objects(request: SimilarRequest):
    """Похожие по названию объекты, у которых есть фотографии."""
    try:
        repo = SQLAlchemySearchRepository(get_session)
        items = repo.find_similar_objects_with_images(
            query=request.query,
            object_type=request.object_type,
            exclude_db_id=request.exclude_db_id,
            limit=request.limit,
        )
        return {"objects": items}
    except Exception as exc:
        logger.error("similar_objects error: %s", exc, exc_info=True)
        return {"objects": []}
