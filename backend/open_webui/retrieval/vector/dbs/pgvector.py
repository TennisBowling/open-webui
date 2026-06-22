from typing import Any, Dict, List, Optional
import json
import logging

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    Text,
    cast,
    column,
    func,
    literal,
    select,
    text,
    values,
)
from sqlalchemy.dialects.postgresql import JSONB, array, insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import true

from open_webui.config import (
    PGVECTOR_CREATE_EXTENSION,
    PGVECTOR_DB_URL,
    PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH,
    PGVECTOR_PGCRYPTO,
    PGVECTOR_PGCRYPTO_KEY,
    PGVECTOR_POOL_MAX_OVERFLOW,
    PGVECTOR_POOL_RECYCLE,
    PGVECTOR_POOL_SIZE,
    PGVECTOR_POOL_TIMEOUT,
)
from open_webui.env import DATABASE_URL, SRC_LOG_LEVELS
from open_webui.retrieval.vector.main import (
    GetResult,
    SearchResult,
    VectorDBBase,
    VectorItem,
)
from open_webui.retrieval.vector.utils import process_metadata

VECTOR_LENGTH = PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH
Base = declarative_base()

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


def pgcrypto_decrypt(col, key, outtype="text"):
    return func.cast(func.pgp_sym_decrypt(col, literal(key)), outtype)


class DocumentChunk(Base):
    __tablename__ = "document_chunk"

    id = Column(Text, primary_key=True)
    vector = Column(Vector(dim=VECTOR_LENGTH), nullable=True)
    collection_name = Column(Text, nullable=False)

    if PGVECTOR_PGCRYPTO:
        text = Column(LargeBinary, nullable=True)
        vmetadata = Column(LargeBinary, nullable=True)
    else:
        text = Column(Text, nullable=True)
        vmetadata = Column(MutableDict.as_mutable(JSONB), nullable=True)


class PgvectorClient(VectorDBBase):
    def __init__(self) -> None:
        db_url = PGVECTOR_DB_URL or DATABASE_URL
        engine_kwargs = {"pool_pre_ping": True}
        if isinstance(PGVECTOR_POOL_SIZE, int):
            if PGVECTOR_POOL_SIZE > 0:
                engine_kwargs.update(
                    {
                        "pool_size": PGVECTOR_POOL_SIZE,
                        "max_overflow": PGVECTOR_POOL_MAX_OVERFLOW,
                        "pool_timeout": PGVECTOR_POOL_TIMEOUT,
                        "pool_recycle": PGVECTOR_POOL_RECYCLE,
                    }
                )
            else:
                engine_kwargs["poolclass"] = NullPool

        self.engine = create_async_engine(db_url, **engine_kwargs)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self.engine.begin() as conn:
            if PGVECTOR_CREATE_EXTENSION:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            if PGVECTOR_PGCRYPTO:
                if not PGVECTOR_PGCRYPTO_KEY:
                    raise ValueError(
                        "PGVECTOR_PGCRYPTO_KEY must be set when PGVECTOR_PGCRYPTO is enabled."
                    )
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_document_chunk_collection_name "
                    "ON document_chunk (collection_name)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_document_chunk_vector "
                    "ON document_chunk USING ivfflat (vector vector_cosine_ops) WITH (lists = 100)"
                )
            )
        self._initialized = True

    @staticmethod
    def _item(item: VectorItem | dict, key: str):
        if isinstance(item, dict):
            return item[key]
        return getattr(item, key)

    def adjust_vector_length(self, vector: List[float | int]) -> List[float]:
        out = [float(v) for v in vector]
        current_length = len(out)
        if current_length < VECTOR_LENGTH:
            out += [0.0] * (VECTOR_LENGTH - current_length)
        elif current_length > VECTOR_LENGTH:
            out = out[:VECTOR_LENGTH]
        return out

    def _rows(self, collection_name: str, items: List[VectorItem]) -> list[dict[str, Any]]:
        return [
            {
                "id": self._item(item, "id"),
                "vector": self.adjust_vector_length(self._item(item, "vector")),
                "collection_name": collection_name,
                "text": self._item(item, "text"),
                "vmetadata": process_metadata(self._item(item, "metadata")),
            }
            for item in items
        ]

    async def insert(self, collection_name: str, items: List[VectorItem]) -> None:
        await self.initialize()
        rows = self._rows(collection_name, items)
        if not rows:
            return
        async with self.session_factory() as session:
            if PGVECTOR_PGCRYPTO:
                for row in rows:
                    await session.execute(
                        text(
                            """
                            INSERT INTO document_chunk (id, vector, collection_name, text, vmetadata)
                            VALUES (:id, :vector, :collection_name,
                                    pgp_sym_encrypt(:text, :key),
                                    pgp_sym_encrypt(:metadata_text, :key))
                            ON CONFLICT (id) DO NOTHING
                            """
                        ),
                        {
                            **row,
                            "metadata_text": json.dumps(row["vmetadata"]),
                            "key": PGVECTOR_PGCRYPTO_KEY,
                        },
                    )
            else:
                await session.execute(
                    pg_insert(DocumentChunk)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=[DocumentChunk.id])
                )
            await session.commit()

    async def upsert(self, collection_name: str, items: List[VectorItem]) -> None:
        await self.initialize()
        rows = self._rows(collection_name, items)
        if not rows:
            return
        async with self.session_factory() as session:
            if PGVECTOR_PGCRYPTO:
                for row in rows:
                    await session.execute(
                        text(
                            """
                            INSERT INTO document_chunk (id, vector, collection_name, text, vmetadata)
                            VALUES (:id, :vector, :collection_name,
                                    pgp_sym_encrypt(:text, :key),
                                    pgp_sym_encrypt(:metadata_text, :key))
                            ON CONFLICT (id) DO UPDATE SET
                                vector = EXCLUDED.vector,
                                collection_name = EXCLUDED.collection_name,
                                text = EXCLUDED.text,
                                vmetadata = EXCLUDED.vmetadata
                            """
                        ),
                        {
                            **row,
                            "metadata_text": json.dumps(row["vmetadata"]),
                            "key": PGVECTOR_PGCRYPTO_KEY,
                        },
                    )
            else:
                stmt = pg_insert(DocumentChunk).values(rows)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[DocumentChunk.id],
                        set_={
                            "vector": stmt.excluded.vector,
                            "collection_name": stmt.excluded.collection_name,
                            "text": stmt.excluded.text,
                            "vmetadata": stmt.excluded.vmetadata,
                        },
                    )
                )
            await session.commit()

    async def search(
        self,
        collection_name: str,
        vectors: List[List[float | int]],
        limit: Optional[int] = None,
    ) -> Optional[SearchResult]:
        await self.initialize()
        if not vectors:
            return None
        vectors = [self.adjust_vector_length(vector) for vector in vectors]
        num_queries = len(vectors)

        def vector_expr(vector):
            return cast(array(vector), Vector(VECTOR_LENGTH))

        qid_col = column("qid", Integer)
        q_vector_col = column("q_vector", Vector(VECTOR_LENGTH))
        query_vectors = (
            values(qid_col, q_vector_col)
            .data([(idx, vector_expr(vector)) for idx, vector in enumerate(vectors)])
            .alias("query_vectors")
        )

        result_fields = [DocumentChunk.id]
        if PGVECTOR_PGCRYPTO:
            result_fields.append(
                pgcrypto_decrypt(DocumentChunk.text, PGVECTOR_PGCRYPTO_KEY, Text).label("text")
            )
            result_fields.append(
                pgcrypto_decrypt(DocumentChunk.vmetadata, PGVECTOR_PGCRYPTO_KEY, JSONB).label("vmetadata")
            )
        else:
            result_fields.extend([DocumentChunk.text, DocumentChunk.vmetadata])
        result_fields.append(
            DocumentChunk.vector.cosine_distance(query_vectors.c.q_vector).label("distance")
        )

        subq = (
            select(*result_fields)
            .where(DocumentChunk.collection_name == collection_name)
            .order_by(DocumentChunk.vector.cosine_distance(query_vectors.c.q_vector))
        )
        if limit is not None:
            subq = subq.limit(limit)
        subq = subq.lateral("result")

        stmt = (
            select(query_vectors.c.qid, subq.c.id, subq.c.text, subq.c.vmetadata, subq.c.distance)
            .select_from(query_vectors)
            .join(subq, true())
            .order_by(query_vectors.c.qid, subq.c.distance)
        )

        try:
            async with self.session_factory() as session:
                results = (await session.execute(stmt)).all()
        except Exception as e:
            log.exception(f"Error during search: {e}")
            return None

        ids = [[] for _ in range(num_queries)]
        distances = [[] for _ in range(num_queries)]
        documents = [[] for _ in range(num_queries)]
        metadatas = [[] for _ in range(num_queries)]
        for row in results:
            qid = int(row.qid)
            ids[qid].append(row.id)
            distances[qid].append((2.0 - row.distance) / 2.0)
            documents[qid].append(row.text)
            metadatas[qid].append(row.vmetadata)
        return SearchResult(ids=ids, distances=distances, documents=documents, metadatas=metadatas)

    async def query(
        self, collection_name: str, filter: Dict[str, Any], limit: Optional[int] = None
    ) -> Optional[GetResult]:
        await self.initialize()
        if PGVECTOR_PGCRYPTO:
            where_clauses = [DocumentChunk.collection_name == collection_name]
            for key, value in filter.items():
                where_clauses.append(
                    pgcrypto_decrypt(DocumentChunk.vmetadata, PGVECTOR_PGCRYPTO_KEY, JSONB)[key].astext
                    == str(value)
                )
            stmt = select(
                DocumentChunk.id,
                pgcrypto_decrypt(DocumentChunk.text, PGVECTOR_PGCRYPTO_KEY, Text).label("text"),
                pgcrypto_decrypt(DocumentChunk.vmetadata, PGVECTOR_PGCRYPTO_KEY, JSONB).label("vmetadata"),
            ).where(*where_clauses)
        else:
            stmt = select(DocumentChunk).where(DocumentChunk.collection_name == collection_name)
            for key, value in filter.items():
                stmt = stmt.where(DocumentChunk.vmetadata[key].astext == str(value))
        if limit is not None:
            stmt = stmt.limit(limit)
        try:
            async with self.session_factory() as session:
                results = (await session.execute(stmt)).all()
        except Exception as e:
            log.exception(f"Error during query: {e}")
            return None
        return self._get_result_from_rows(results)

    async def get(self, collection_name: str, limit: Optional[int] = None) -> Optional[GetResult]:
        await self.initialize()
        if PGVECTOR_PGCRYPTO:
            stmt = select(
                DocumentChunk.id,
                pgcrypto_decrypt(DocumentChunk.text, PGVECTOR_PGCRYPTO_KEY, Text).label("text"),
                pgcrypto_decrypt(DocumentChunk.vmetadata, PGVECTOR_PGCRYPTO_KEY, JSONB).label("vmetadata"),
            ).where(DocumentChunk.collection_name == collection_name)
        else:
            stmt = select(DocumentChunk).where(DocumentChunk.collection_name == collection_name)
        if limit is not None:
            stmt = stmt.limit(limit)
        try:
            async with self.session_factory() as session:
                results = (await session.execute(stmt)).all()
        except Exception as e:
            log.exception(f"Error during get: {e}")
            return None
        return self._get_result_from_rows(results)

    @staticmethod
    def _get_result_from_rows(results) -> Optional[GetResult]:
        if not results:
            return None
        rows = [row[0] if len(row) == 1 else row for row in results]
        return GetResult(
            ids=[[row.id for row in rows]],
            documents=[[row.text for row in rows]],
            metadatas=[[row.vmetadata for row in rows]],
        )

    async def delete(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.initialize()
        wheres = [DocumentChunk.collection_name == collection_name]
        if ids:
            wheres.append(DocumentChunk.id.in_(ids))
        if filter:
            for key, value in filter.items():
                if PGVECTOR_PGCRYPTO:
                    wheres.append(
                        pgcrypto_decrypt(DocumentChunk.vmetadata, PGVECTOR_PGCRYPTO_KEY, JSONB)[key].astext
                        == str(value)
                    )
                else:
                    wheres.append(DocumentChunk.vmetadata[key].astext == str(value))
        async with self.session_factory() as session:
            result = await session.execute(DocumentChunk.__table__.delete().where(*wheres))
            await session.commit()
            log.info(f"Deleted {result.rowcount} items from collection '{collection_name}'.")

    async def reset(self) -> None:
        await self.initialize()
        async with self.session_factory() as session:
            result = await session.execute(DocumentChunk.__table__.delete())
            await session.commit()
            log.info(f"Reset complete. Deleted {result.rowcount} document chunks.")

    async def close(self) -> None:
        await self.engine.dispose()

    async def has_collection(self, collection_name: str) -> bool:
        await self.initialize()
        async with self.session_factory() as session:
            result = await session.execute(
                select(DocumentChunk.id)
                .where(DocumentChunk.collection_name == collection_name)
                .limit(1)
            )
            return result.first() is not None

    async def delete_collection(self, collection_name: str) -> None:
        await self.delete(collection_name)
