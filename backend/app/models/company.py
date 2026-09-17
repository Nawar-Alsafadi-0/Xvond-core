from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # Runtime/emergency switch only. Commercial/customer lifecycle is tracked
    # separately so onboarding users can access the portal before AI goes live.
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    lifecycle_status: Mapped[str] = mapped_column(
        String(30),
        default="onboarding",
        nullable=False,
        index=True,
    )

    # How this workspace entered Xvond. Existing/manual customers remain
    # managed; public signup creates self_service workspaces.
    onboarding_source: Mapped[str] = mapped_column(
        String(30),
        default="managed",
        nullable=False,
        index=True,
    )

    lifecycle_updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    users = relationship(
        "User",
        back_populates="company",
        cascade="all, delete-orphan",
    )

    modules = relationship(
        "CompanyModule",
        back_populates="company",
        cascade="all, delete-orphan",
    )
