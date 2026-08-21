"""班级管理接口"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import Class, Student
from ..schemas import ClassCreate, ClassUpdate, ClassResponse, MessageResponse
from ..utils.dependencies import get_current_user, require_editor

router = APIRouter(prefix="/api/classes", tags=["班级管理"])


@router.get("", response_model=list[ClassResponse])
async def list_classes(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
    grade: int | None = Query(default=None, ge=2000, le=2100),
):
    query = (
        select(Class, func.count(Student.id).label("cnt"))
        .outerjoin(Student, Class.id == Student.class_id)
        .group_by(Class.id)
        .order_by(Class.grade.desc(), Class.name)
    )
    if grade is not None:
        query = query.where(Class.grade == grade)
    result = await db.execute(query)
    return [
        ClassResponse(id=c.id, name=c.name, grade=c.grade, head_teacher=c.head_teacher, student_count=cnt, created_at=c.created_at)
        for c, cnt in result.all()
    ]


@router.post("", response_model=ClassResponse, status_code=status.HTTP_201_CREATED)
async def create_class(req: ClassCreate, db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    cls = Class(**req.model_dump())
    db.add(cls)
    await db.flush()
    await db.refresh(cls)
    return ClassResponse(id=cls.id, name=cls.name, grade=cls.grade, head_teacher=cls.head_teacher, student_count=0, created_at=cls.created_at)


@router.put("/{class_id}", response_model=ClassResponse)
async def update_class(class_id: str, req: ClassUpdate, db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    result = await db.execute(select(Class).where(Class.id == class_id))
    cls = result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="班级不存在")
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(cls, k, v)
    await db.flush()
    await db.refresh(cls)
    cnt = (await db.execute(select(func.count()).select_from(Student).where(Student.class_id == class_id))).scalar()
    return ClassResponse(id=cls.id, name=cls.name, grade=cls.grade, head_teacher=cls.head_teacher, student_count=cnt, created_at=cls.created_at)


@router.delete("/{class_id}", response_model=MessageResponse)
async def delete_class(class_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    cnt = (await db.execute(select(func.count()).select_from(Student).where(Student.class_id == class_id))).scalar()
    if cnt > 0:
        raise HTTPException(status_code=400, detail=f"班级下有 {cnt} 名学生，请先移除")
    result = await db.execute(select(Class).where(Class.id == class_id))
    cls = result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="班级不存在")
    await db.delete(cls)
    return MessageResponse(message="删除成功")