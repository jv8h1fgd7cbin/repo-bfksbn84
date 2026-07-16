import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(str, enum.Enum):
    DOG = "dog"
    CAT = "cat"
    DOG_AND_CAT = "dog_and_cat"
    UNDEFINED = "undefined"


class ChatStatus(str, enum.Enum):
    ACTIVE = "active"
    PENDING_ACCESS = "pending_access"
    BACKFILLING = "backfilling"
    ERROR = "error"


class PetOwner(Base):
    __tablename__ = "pet_owners"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[Category] = mapped_column(
        Enum(Category, name="category"), default=Category.UNDEFINED, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["UserMessage"]] = relationship(back_populates="owner")

    __table_args__ = (Index("ix_pet_owners_category_confidence", "category", "confidence"),)


class UserMessage(Base):
    __tablename__ = "user_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("pet_owners.user_id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_name: Mapped[str | None] = mapped_column(String(255))
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[PetOwner] = relationship(back_populates="messages")

    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_chat_message"),)


class MonitoredChat(Base):
    __tablename__ = "monitored_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    link: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ChatStatus] = mapped_column(
        Enum(ChatStatus, name="chat_status"), default=ChatStatus.ACTIVE, index=True
    )
    reason: Mapped[str | None] = mapped_column(String(255))
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    backfilled: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)  # error, floodwait, info
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
