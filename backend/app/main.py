"""学生管理系统 - FastAPI 入口"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db, async_session
from .models import User
from .utils.security import hash_password
from .routers import auth, students, classes


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 首次启动创建默认管理员
    async with async_session() as db:
        from sqlalchemy import select
        result = await db.execute(select(User).where(User.username == "admin"))
        if not result.scalar_one_or_none():
            db.add(User(username="admin", password_hash=hash_password("admin123"),
                        real_name="系统管理员", role="admin"))
            await db.commit()
    yield


app = FastAPI(
    title="学生管理系统 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth)
app.include_router(students)
app.include_router(classes)


@app.get("/")
async def root():
    return {"message": "学生管理系统 API", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
