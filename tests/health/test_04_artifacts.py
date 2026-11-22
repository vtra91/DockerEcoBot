"""Уровень 3: «всё подтянулось» — модели, индексы, шаблоны и тома на месте и доступны на запись
тому пользователю, под которым работает процесс в контейнере."""
import pytest

# FAISS-индекс должен быть построен той же моделью, которой backend кодирует запросы,
# иначе размерности не совпадут (bge-m3 = 1024, BERTA = 768) и векторный поиск упадёт.
FAISS_DIM_CODE = r"""
import glob
faiss_dir = "/app/knowledge_base_scripts/Vector/faiss_index"
model_dir = "/app/embedding_models/" + (os.environ.get("EMBEDDING_MODEL") or "bge-m3")
try:
    import faiss
    idx_files = glob.glob(faiss_dir + "/*.faiss")
    if not idx_files:
        result = (False, "в %s нет *.faiss" % faiss_dir)
    else:
        index = faiss.read_index(idx_files[0])
        cfg = json.load(open(model_dir + "/config.json"))
        hidden = cfg.get("hidden_size") or cfg.get("d_model")
        result = (index.d == hidden,
                  "индекс %s: dim=%s, %s векторов; модель %s: hidden_size=%s"
                  % (os.path.basename(idx_files[0]), index.d, index.ntotal, model_dir, hidden))
except Exception as exc:
    result = (False, "%s: %s" % (type(exc).__name__, exc))
"""

CHECKS = {
    "backend": [
        {"name": "embedding-модель", "type": "path", "expect": "file",
         "path": "/app/embedding_models/${EMBEDDING_MODEL:-bge-m3}/config.json"},
        {"name": "reranker", "type": "path", "expect": "dir_nonempty",
         "path": "/app/embedding_models/rerankers/DiTy_cross-encoder-russian-msmarco"},
        {"name": "faiss index.faiss", "type": "path", "expect": "file",
         "path": "/app/knowledge_base_scripts/Vector/faiss_index/index.faiss"},
        {"name": "faiss index.pkl", "type": "path", "expect": "file",
         "path": "/app/knowledge_base_scripts/Vector/faiss_index/index.pkl"},
        {"name": "faiss размерность = модель", "type": "python", "code": FAISS_DIM_CODE, "timeout": 60},
        {"name": "maps на запись", "type": "path", "expect": "writable", "path": "${MAPS_DIR:-/app/maps}"},
        {"name": "logs на запись", "type": "path", "expect": "writable", "path": "/app/logs"},
    ],
    "core-api": [
        {"name": "промпт классификации", "type": "path", "expect": "file",
         "path": "/app/prompts/classification_prompt.txt"},
    ],
    "max": [
        {"name": "data на запись", "type": "path", "expect": "writable", "path": "/app/data"},
        {"name": "user_settings на запись", "type": "path", "expect": "writable",
         "path": "${USER_SETTINGS_PATH:-/app/data/user_settings.json}"},
        {"name": "logs на запись", "type": "path", "expect": "writable", "path": "/app/logs"},
    ],
    "admin": [
        {"name": "admin_db на запись", "type": "path", "expect": "writable", "path": "/app/admin_db"},
        {"name": "shared.env примонтирован", "type": "path", "expect": "file", "path": "/app/shared.env"},
        {"name": "shared.env на запись", "type": "path", "expect": "writable", "path": "/app/shared.env"},
        {"name": "шаблоны", "type": "path", "expect": "dir_nonempty", "path": "/app/templates"},
    ],
    "dsapi": [
        {"name": "локальные модели", "type": "path", "expect": "dir_nonempty",
         "path": "${PATH_LOCAL_MODELS:-/app/local_models}"},
        {"name": "шаблоны вопросов", "type": "path", "expect": "file",
         "path": "${PATH_TEMPLATES_EXCEL:-/app/doc/template_sen.xlsx}"},
        {"name": "список ОФФ", "type": "path", "expect": "file",
         "path": "${PATH_OFF_LIST_EXCEL:-/app/doc/OFF_actual.xlsx}"},
        # evaluator.py читает промпты из prompts_structure; в репозитории этой папки нет,
        # docker создаёт на её месте пустой каталог
        {"name": "промпты LLM-оценщика", "type": "path", "expect": "dir_nonempty", "path": "/app/prompts_structure"},
        {"name": "results на запись", "type": "path", "expect": "writable", "path": "${PATH_RESULTS_DIR:-/app/results}"},
        {"name": "logs на запись", "type": "path", "expect": "writable", "path": "${PATH_LOGS_DIR:-/app/logs}"},
    ],
    "stt-service": [
        {"name": "кэш моделей на запись", "type": "path", "expect": "writable", "path": "/models"},
        {"name": "словарь", "type": "path", "expect": "file", "path": "${STT_WORDS_FILE:-/app/data/words.txt}"},
    ],
    "annotator": [
        {"name": "модели", "type": "path", "expect": "dir_nonempty", "path": "${MODELS_DIR:-/app/models}"},
    ],
}

CASES = [(svc, c["name"]) for svc, checks in CHECKS.items() for c in checks]


@pytest.mark.parametrize("service,check", CASES, ids=["%s: %s" % c for c in CASES])
def test_artifact_present(stack, require, service, check):
    require(service)
    r = stack.probe(service, CHECKS[service])[check]
    if r["skipped"]:
        pytest.skip(r["detail"])
    assert r["ok"], "%s — %s: %s" % (service, check, r["detail"])
