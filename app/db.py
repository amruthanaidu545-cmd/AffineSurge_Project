"""SQLAlchemy models: documents, immutable versions, logical nodes, node versions, selections."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class Version(Base):
    """One ingestion of a PDF. Never mutated after creation (v1 survives v2)."""
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("document_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    number: Mapped[int] = mapped_column(Integer)
    source_file: Mapped[str] = mapped_column(String)
    source_sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    title: Mapped[str] = mapped_column(String)
    warnings: Mapped[list] = mapped_column(JSON, default=list)


class LogicalNode(Base):
    """Stable identity of a section across versions."""
    __tablename__ = "logical_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    first_version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))


class NodeVersion(Base):
    """A section as it appears in one version. This id is what APIs/selections use."""
    __tablename__ = "node_versions"
    __table_args__ = (UniqueConstraint("logical_node_id", "version_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_node_id: Mapped[int | None] = mapped_column(ForeignKey("logical_nodes.id"), index=True)  # set before commit in ingest
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("node_versions.id"), nullable=True, index=True)
    number: Mapped[str | None] = mapped_column(String, nullable=True)
    heading: Mapped[str] = mapped_column(String)
    level: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    references: Mapped[list] = mapped_column(JSON, default=list)
    # versioning metadata (relative to the previous version)
    change_status: Mapped[str] = mapped_column(String, default="new")  # new|unchanged|modified
    heading_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    number_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    match_method: Mapped[str | None] = mapped_column(String, nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_node_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[list["SelectionItem"]] = relationship(back_populates="selection", order_by="SelectionItem.id")


class SelectionItem(Base):
    """Pins a selection to an exact (node, version) pair via node_version_id."""
    __tablename__ = "selection_items"
    __table_args__ = (UniqueConstraint("selection_id", "node_version_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    selection_id: Mapped[int] = mapped_column(ForeignKey("selections.id"))
    node_version_id: Mapped[int] = mapped_column(ForeignKey("node_versions.id"))
    selection: Mapped[Selection] = relationship(back_populates="items")


def make_session_factory(url: str):
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    if url.endswith(":memory:"):
        from sqlalchemy.pool import StaticPool
        kw["poolclass"] = StaticPool
    engine = create_engine(url, **kw)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # SQLite ignores FKs unless asked
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
