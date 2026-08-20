"""Pydantic 请求/响应模型"""
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field


# ======== 学生 ========

class StudentBase(BaseModel):
    student_no: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=50)
    gender: str = Field(default="男", pattern="^(男|女)$")
    birth_date: Optional[date] = None
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = None
    class_id: Optional[str] = None
    status: str = Field(default="在读")
    remark: Optional[str] = None


class StudentCreate(StudentBase):
    pass


class StudentUpdate(BaseModel):
    student_no: Optional[str] = Field(None, min_length=1, max_length=20)
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    gender: Optional[str] = Field(None, pattern="^(男|女)$")
    birth_date: Optional[date] = None
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = None
    class_id: Optional[str] = None
    status: Optional[str] = None
    remark: Optional[str] = None


class StudentResponse(StudentBase):
    id: str
    created_at: datetime
    updated_at: datetime
    class_name: Optional[str] = None

    class Config:
        from_attributes = True


class StudentListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StudentResponse]


# ======== 班级 ========

class ClassBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    grade: int = Field(default=2024, ge=2000, le=2100)
    head_teacher: Optional[str] = Field(None, max_length=50)


class ClassCreate(ClassBase):
    pass


class ClassUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    grade: Optional[int] = Field(None, ge=2000, le=2100)
    head_teacher: Optional[str] = Field(None, max_length=50)


class ClassResponse(ClassBase):
    id: str
    student_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


# ======== 认证 ========

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=50)
    real_name: Optional[str] = None
    role: str = Field(default="viewer", pattern="^(admin|editor|viewer)$")


class UserResponse(BaseModel):
    id: str
    username: str
    real_name: Optional[str]
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class MessageResponse(BaseModel):
    message: str
