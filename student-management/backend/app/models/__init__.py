"""数据库模型"""
import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Date, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def gen_uuid():
    return str(uuid.uuid4())


class Student(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    student_no: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True, comment="学号")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="姓名")
    gender: Mapped[str] = mapped_column(String(4), nullable=False, default="男", comment="性别")
    birth_date: Mapped[datetime] = mapped_column(Date, nullable=True, comment="出生日期")
    phone: Mapped[str] = mapped_column(String(20), nullable=True, comment="联系电话")
    email: Mapped[str] = mapped_column(String(100), nullable=True, comment="邮箱")
    address: Mapped[str] = mapped_column(Text, nullable=True, comment="家庭地址")
    class_id: Mapped[str] = mapped_column(String(36), ForeignKey("classes.id"), nullable=True, comment="班级ID")
    status: Mapped[str] = mapped_column(String(10), default="在读", comment="状态: 在读/休学/退学/毕业")
    remark: Mapped[str] = mapped_column(Text, nullable=True, comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    class_: Mapped["Class"] = relationship("Class", back_populates="students")


class Class(Base):
    __tablename__ = "classes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="班级名称")
    grade: Mapped[int] = mapped_column(Integer, nullable=False, default=2024, comment="年级/入学年份")
    head_teacher: Mapped[str] = mapped_column(String(50), nullable=True, comment="班主任")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    students: Mapped[list["Student"]] = relationship("Student", back_populates="class_")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    real_name: Mapped[str] = mapped_column(String(50), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer", comment="admin/editor/viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
