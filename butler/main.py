from fastapi import FastAPI

import butler.tasks  # noqa: F401  (import registers all built-in tasks)
from butler.routes import health, tasks

app = FastAPI(title="butler")
app.include_router(health.router)
app.include_router(tasks.router)


def run() -> None:
    import uvicorn

    from butler.config import settings

    uvicorn.run(app, host=settings.resolved_host(), port=settings.port)


if __name__ == "__main__":
    run()
