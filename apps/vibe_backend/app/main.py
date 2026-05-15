from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers.analysis_sessions import router as analysis_sessions_router
from .routers.admin import router as admin_router
from .routers.artifacts import router as artifacts_router
from .routers.bridge import router as bridge_router
from .routers.clothing import router as clothing_router
from .routers.decks import router as decks_router
from .routers.llm_agent import router as llm_agent_router
from .routers.memory import router as memory_router
from .routers.preferences import router as preferences_router
from .routers.scene_builder import router as scene_builder_router
from .routers.semantic_cache import router as semantic_cache_router
from .routers.scenes import router as scenes_router, warm_scene_cache
from .routers.slides import router as slides_router
from .routers.sql_result_agent import router as sql_result_agent_router
from .store import load_state

app = FastAPI(title="Vibe Data Analysis Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenes_router)
app.include_router(analysis_sessions_router)
app.include_router(slides_router)
app.include_router(decks_router)
app.include_router(artifacts_router)
app.include_router(llm_agent_router)
app.include_router(scene_builder_router)
app.include_router(semantic_cache_router)
app.include_router(preferences_router)
app.include_router(memory_router)
app.include_router(bridge_router)
app.include_router(clothing_router)
app.include_router(admin_router)
app.include_router(sql_result_agent_router)


@app.on_event("startup")
async def startup_load_state() -> None:
    load_state()
    warm_scene_cache()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "vibe-backend",
        "phase": "persistent-skeleton",
    }
