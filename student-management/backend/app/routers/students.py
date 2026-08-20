"""学生管理接口: CRUD + 搜索筛选 + 导入导出"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from ..database import get_db
from ..models import Student, Class
from ..schemas import (
    StudentCreate, StudentUpdate, StudentResponse,
    StudentListResponse, MessageResponse
)
from ..utils.dependencies import get_current_user, require_editor, require_admin
from ..utils import export_students_to_excel, parse_from_excel

router = APIRouter(prefix="/api/students", tags=["学生管理"])


@router.get("", response_model=StudentListResponse)
async def list_students(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None, description="搜索: 学号/姓名/手机"),
    class_id: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    query = select(Student, Class.name.label("class_name")).outerjoin(Class, Student.class_id == Class.id)

    conditions = []
    if keyword:
        conditions.append(or_(Student.student_no.like(f"%{keyword}%"), Student.name.like(f"%{keyword}%"), Student.phone.like(f"%{keyword}%")))
    if class_id:
        conditions.append(Student.class_id == class_id)
    if gender:
        conditions.append(Student.gender == gender)
    if status:
        conditions.append(Student.status == status)
    if conditions:
        query = query.where(*conditions)

    # 总数
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar()
    # 分页
    query = query.order_by(Student.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = []
    for student, class_name in rows:
        item = StudentResponse.model_validate(student)
        item.class_name = class_name
        items.append(item)
    return StudentListResponse(total=total, page=page, page_size=page_size, items=items)


@router.get("/{student_id}", response_model=StudentResponse)
async def get_student(student_id: str, db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    result = await db.execute(
        select(Student, Class.name.label("class_name"))
        .outerjoin(Class, Student.class_id == Class.id)
        .where(Student.id == student_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="学生不存在")
    student, class_name = row
    item = StudentResponse.model_validate(student)
    item.class_name = class_name
    return item


@router.post("", response_model=StudentResponse, status_code=status.HTTP_201_CREATED)
async def create_student(req: StudentCreate, db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    existing = await db.execute(select(Student).where(Student.student_no == req.student_no))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="学号已存在")
    student = Student(**req.model_dump(exclude_none=True))
    db.add(student)
    await db.flush()
    await db.refresh(student)
    return student


@router.put("/{student_id}", response_model=StudentResponse)
async def update_student(student_id: str, req: StudentUpdate, db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="学生不存在")
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(student, field, value)
    student.updated_at = datetime.now()
    await db.flush()
    await db.refresh(student)
    return student


@router.delete("/{student_id}", response_model=MessageResponse)
async def delete_student(student_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="学生不存在")
    await db.delete(student)
    return MessageResponse(message="删除成功")


@router.post("/batch-delete", response_model=MessageResponse)
async def batch_delete(ids: list[str], db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    await db.execute(Student.__table__.delete().where(Student.id.in_(ids)))
    return MessageResponse(message=f"已删除 {len(ids)} 名学生")


@router.get("/export/excel")
async def export_excel(
    keyword: Optional[str] = Query(None),
    class_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    query = select(Student, Class.name.label("class_name")).outerjoin(Class, Student.class_id == Class.id)
    if keyword:
        query = query.where(or_(Student.student_no.like(f"%{keyword}%"), Student.name.like(f"%{keyword}%"), Student.phone.like(f"%{keyword}%")))
    if class_id:
        query = query.where(Student.class_id == class_id)
    rows = (await db.execute(query.order_by(Student.student_no))).all()

    data = []
    for student, class_name in rows:
        d = StudentResponse.model_validate(student).model_dump()
        d["class_name"] = class_name
        data.append(d)

    excel = export_students_to_excel(data)
    return StreamingResponse(excel, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=students.xlsx"})


@router.post("/import/excel", response_model=MessageResponse)
async def import_excel(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), _=Depends(require_editor)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="请上传 .xlsx 或 .xls 文件")
    content = await file.read()
    students_data, parse_errors = parse_from_excel(content)

    # 班级名称 -> id 映射(PRD FR-06: 班级按稳定标识匹配, 不存在时不得静默丢弃)
    classes = (await db.execute(select(Class))).scalars().all()
    class_id_by_name = {c.name: c.id for c in classes}

    imported, errors = 0, list(parse_errors)
    # 先查全部已有学号, 避免逐行触发 autoflush 导致失败行残留
    all_no = {r[0] for r in (await db.execute(select(Student.student_no))).all()}
    pending: list[Student] = []
    for stu in students_data:
        if stu["student_no"] in all_no:
            errors.append(f"学号 {stu['student_no']} 已存在,已跳过")
            continue
        if stu["class_name"]:
            if stu["class_name"] not in class_id_by_name:
                errors.append(f"学号 {stu['student_no']}: 班级 {stu['class_name']!r} 不存在,已跳过")
                continue
            stu["class_id"] = class_id_by_name[stu["class_name"]]
        stu.pop("class_name", None)
        pending.append(Student(**stu))
        all_no.add(stu["student_no"])  # 同一文件内学号去重

    imported = len(pending)
    if pending:
        db.add_all(pending)
        await db.flush()

    msg = f"成功导入 {imported} 名"
    if errors:
        msg += f"，{len(errors)} 条失败: {'; '.join(errors[:5])}"
    return MessageResponse(message=msg)
