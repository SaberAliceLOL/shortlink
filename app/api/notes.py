"""Day 5 练习路由：notes 的增查。

注意两点：
  1. db / cache 都来自依赖注入，路由里看不到任何连接创建代码
  2. 返回值标注 NoteOut，FastAPI 自动做响应过滤（多余的内部字段不会漏出去）
"""

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, get_redis
from app.models.note import Note
from app.schemas.note import NoteCreate, NoteOut

router = APIRouter(prefix="/api/notes", tags=["notes"])


@router.post("", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def create_note(payload: NoteCreate, db: AsyncSession = Depends(get_db)) -> NoteOut:
    note = Note(title=payload.title, content=payload.content)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return NoteOut.model_validate(note)


@router.get("/{note_id}", response_model=NoteOut)
async def get_note(
    note_id: int,
    db: AsyncSession = Depends(get_db),
    cache: Redis = Depends(get_redis),
) -> NoteOut:
    # 先查 Redis（缓存读路径雏形，Week 3 展开成完整 Cache Aside）
    cached = await cache.get(f"note:{note_id}")
    if cached is not None:
        return NoteOut.model_validate_json(cached)

    note = await db.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")

    out = NoteOut.model_validate(note)
    await cache.set(f"note:{note_id}", out.model_dump_json(), ex=300)
    return out
