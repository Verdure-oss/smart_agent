"""
长期记忆 — 基于向量数据库的持久化记忆
存储用户画像、历史工单、知识库文档等需要持久化的信息。
支持语义相似度检索，用于RAG知识检索Agent。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

try:
    import faiss
except ImportError:
    faiss = None

try:
    from sentence_transformers import SentenceTransformer
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    _HAS_SENTENCE_TRANSFORMERS = False


def _has_cuda() -> bool:
    """检查是否有CUDA可用"""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


class LongTermMemory:
    """
    长期记忆：基于FAISS的向量检索。

    特点：
    - 向量化存储，支持语义相似度检索
    - 持久化到磁盘，跨会话保持
    - 支持增量更新和批量导入
    - 生产环境可切换为Milvus/Pinecone

    文档分块策略：
    - 固定长度分块 (512 tokens) + 重叠窗口 (128 tokens)
    - 按段落自然分割优先
    """

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
        embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ):
        self.index_path = Path(index_path)
        self._embedding_model_name = embedding_model
        self._documents: list[dict[str, Any]] = []
        self._index = None
        self._embedder = None
        self._init_embedder()
        self.embedding_dim = self._embedder.get_sentence_embedding_dimension() if self._embedder else 384
        self._init_index()

    def _init_index(self):
        """初始化FAISS索引"""
        if faiss is None:
            self._index = None
            return

        metadata_path = self.index_path.with_suffix(".meta.json")
        if self.index_path.exists():
            try:
                self._index = faiss.read_index(str(self.index_path))
                if metadata_path.exists():
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        self._documents = json.load(f)
                # 如果维度不匹配，重新建索引
                d = self._index.d
                if d != self.embedding_dim:
                    logger.warning("[LongTerm] 索引维度 %d != embedder维度 %d，重建索引", d, self.embedding_dim)
                    self._index = faiss.IndexFlatIP(self.embedding_dim)
                    # 重新插入已有文档
                    for doc in self._documents:
                        vec = self.get_embedding(doc["content"])
                        self._index.add(vec.reshape(1, -1))
            except Exception as e:
                logger.warning("[LongTerm] 读取已有索引失败 (%s)，重建", e)
                self._index = faiss.IndexFlatIP(self.embedding_dim)
        else:
            self._index = faiss.IndexFlatIP(self.embedding_dim)

    def _init_embedder(self):
        """初始化本地嵌入模型（按需下载）"""
        if not _HAS_SENTENCE_TRANSFORMERS:
            logger.warning("[LongTerm] sentence-transformers 不可用，回退到哈希嵌入")
            return

        # 优先考虑环境变量指定的本地路径
        local_path = os.getenv("SENTENCE_TRANSFORMERS_MODEL_PATH", "")
        load_path = local_path if local_path else self._embedding_model_name

        # 优先从本地缓存/路径加载，失败则从 HuggingFace 下载
        try:
            # 若指定了 HF 镜像或 token，利用环境变量
            use_auth = os.getenv("HF_TOKEN")
            kwargs = {"device": "cuda" if _has_cuda() else "cpu"}
            if use_auth:
                kwargs["use_auth_token"] = use_auth
            self._embedder = SentenceTransformer(load_path, **kwargs)
            self.embedding_dim = self._embedder.get_sentence_embedding_dimension()
            logger.info("[LongTerm] 嵌入模型加载成功: %s (dim=%d)", load_path, self.embedding_dim)
        except Exception as e:
            logger.warning("[LongTerm] 嵌入模型加载失败 (%s)，回退到哈希嵌入", e)
            self._embedder = None

    def get_embedding(self, text: str) -> np.ndarray:
        """获取文本嵌入向量，优先使用本地模型，失败时回退到哈希"""
        if self._embedder is not None:
            return self._embedder.encode(text, normalize_embeddings=True).astype(np.float32)
        # fallback: hash-based deterministic embedding (for dev only)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        np.random.seed(int(text_hash[:8], 16) % (2**32))
        vec = np.random.randn(384).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def _simple_embedding(self, text: str) -> np.ndarray:
        """兼容旧接口，调用 get_embedding"""
        return self.get_embedding(text)

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        """添加文档到向量库"""

        # 生成唯一id
        doc_id = hashlib.md5(content.encode()).hexdigest()[:12]

        # 内容\来源\metadata
        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": metadata or {},
        }
        self._documents.append(doc)

        if self._index is not None:
            embedding = self.get_embedding(content)
            self._index.add(embedding.reshape(1, -1))

        return doc_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        """批量添加文档"""
        doc_ids = []
        for doc in documents:
            doc_id = self.add_document(
                content=doc.get("content", ""),
                source=doc.get("source", ""),
                metadata=doc.get("metadata", {}),
            )
            doc_ids.append(doc_id)
        return doc_ids

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """语义相似度检索"""
        if self._index is None or not self._documents:
            return self._fallback_search(query, top_k)

        query_vec = self.get_embedding(query).reshape(1, -1)
        scores, indices = self._index.search(query_vec, min(top_k, len(self._documents)))

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._documents):
                continue
            doc = self._documents[idx].copy()
            doc["score"] = float(score)
            results.append(doc)

        return results

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """当FAISS不可用时的关键词回退搜索"""
        scored = []
        query_terms = set(query.lower().split())

        for doc in self._documents:
            content_lower = doc["content"].lower()
            score = sum(1 for term in query_terms if term in content_lower)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]

    def save(self):
        """持久化索引到磁盘"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self._index is not None:
            faiss.write_index(self._index, str(self.index_path))

        metadata_path = self.index_path.with_suffix(".meta.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(self._documents, f, ensure_ascii=False, indent=2)

    def load_knowledge_base(self, kb_dir: str) -> int:
        """从目录批量加载知识库文档"""
        kb_path = Path(kb_dir)
        if not kb_path.exists():
            return 0

        count = 0
        for file_path in kb_path.glob("**/*.txt"):
            content = file_path.read_text(encoding="utf-8")
            chunks = self._chunk_text(content)
            for chunk in chunks:
                self.add_document(
                    content=chunk,
                    source=str(file_path.name),
                    metadata={"file": str(file_path)},
                )
                count += 1

        return count

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """
        文本分块：固定长度 + 重叠窗口。
        优先按段落分割，段落过长则按句子分割。
        """
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            # 判断段落长度是否超过目标
            # 否则写入current_chunk
            # 是则写入chunks列表中,待返回
            # 如果没有current_chunk说明段落太长,需要以句子切分,一句句切.
            # 以固定长度切分.
            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                    current_chunk = overlap_text + para + "\n\n"
                else:
                    sentences = para.replace("。", "。\n").replace(".", ".\n").split("\n")
                    for sentence in sentences:
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        if len(current_chunk) + len(sentence) <= chunk_size:
                            current_chunk += sentence
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = sentence

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text[:chunk_size]]
