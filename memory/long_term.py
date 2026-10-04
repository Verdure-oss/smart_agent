"""
长期记忆 — 基于向量数据库的持久化记忆
存储用户画像、历史工单、知识库文档等需要持久化的信息。
支持语义相似度检索（FAISS）+ 关键词检索（BM25）+ RRF 混合召回，用于 RAG 知识检索 Agent。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
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

try:
    from rank_bm25 import BM25Okapi
    _HAS_RANK_BM25 = True
except ImportError:
    _HAS_RANK_BM25 = False

try:
    import jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False


def _tokenize_text(text: str) -> list[str]:
    """中文友好的分词：优先 jieba，回退到英文单词 + 汉字单字切分。"""
    text = (text or "").lower()
    if _HAS_JIEBA:
        tokens = [t.strip() for t in jieba.cut(text) if t.strip()]
        if tokens:
            return tokens
    # 兜底：英文/数字按词，中文按单字，避免无 jieba 时中文整句成为一个 token
    import re
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)


class _SimpleBM25:
    """纯 Python 的极简 BM25 实现，仅作为 rank_bm25 不可用时的兜底。"""

    def __init__(self, corpus: list[list[str]]):
        self.corpus = corpus
        self.n_docs = len(corpus)
        self.avgdl = (sum(len(d) for d in corpus) / self.n_docs) if self.n_docs else 0.0
        self.k1 = 1.5
        self.b = 0.75
        self._df: dict[str, int] = {}
        for doc in corpus:
            for term in set(doc):
                self._df[term] = self._df.get(term, 0) + 1

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log((self.n_docs - df + 0.5) / (df + 0.5) + 1.0)

    def get_scores(self, query: list[str]) -> list[float]:
        scores: list[float] = []
        for doc in self.corpus:
            doc_len = len(doc)
            tf: dict[str, int] = {}
            for t in doc:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for term in query:
                f = tf.get(term, 0)
                if f == 0:
                    continue
                dl = doc_len / (self.avgdl or 1.0)
                denom = f + self.k1 * (1 - self.b + self.b * dl)
                score += self._idf(term) * (f * (self.k1 + 1)) / denom
            scores.append(float(score))
        return scores


def _has_cuda() -> bool:
    """检查是否有CUDA可用"""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


class LongTermMemory:
    """
    长期记忆：基于FAISS的向量检索 + BM25 关键词检索。

    特点：
    - 向量化存储，支持语义相似度检索
    - BM25 关键词检索，补充向量检索对专有名词/精确术语的短板
    - 双路召回后 RRF 融合排序
    - 持久化到磁盘，跨会话保持
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
        self._bm25 = None
        self._bm25_dirty = True
        self._init_embedder()
        self.embedding_dim = self._get_embedder_dim()
        self._init_index()

    def _get_embedder_dim(self) -> int:
        """获取嵌入维度，兼容新旧版 sentence-transformers API"""
        if self._embedder is None:
            return 384
        # 新版本 API 优先（get_embedding_dimension），回退旧版本（get_sentence_embedding_dimension）
        getter = getattr(self._embedder, "get_embedding_dimension", None)
        if getter is None:
            getter = getattr(self._embedder, "get_sentence_embedding_dimension", None)
        try:
            return getter() if getter is not None else 384
        except Exception:
            return 384

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
            self.embedding_dim = self._get_embedder_dim()
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
        self._bm25_dirty = True

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

    def _rebuild_bm25(self) -> None:
        """（重建）BM25 索引，与 _documents 保持同步。"""
        corpus = [_tokenize_text(doc.get("content", "")) for doc in self._documents]
        if _HAS_RANK_BM25:
            self._bm25 = BM25Okapi(corpus)
        else:
            self._bm25 = _SimpleBM25(corpus)
        self._bm25_dirty = False

    def hybrid_search(self, query: str, top_k: int = 5, fusion_k: int = 60) -> list[dict]:
        """
        双路混合召回：向量检索（FAISS / 关键词兜底）+ BM25，RRF 融合排序。

        返回的每个文档附带：
        - score: RRF 融合分数
        - vector_score: 向量路分数（存在时）
        - bm25_score: BM25 分数（存在时）
        - matched_by: 命中的召回路列表 ["vector", "bm25"]
        """
        # 1) 向量召回
        vector_docs = self.search(query, top_k=top_k)

        # 2) BM25 召回
        if self._bm25 is None or self._bm25_dirty:
            self._rebuild_bm25()
        bm25_scores = self._bm25.get_scores(_tokenize_text(query))
        bm25_ranked = sorted(
            range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True
        )
        bm25_docs: list[dict] = []
        for i in bm25_ranked:
            if bm25_scores[i] <= 0:
                continue
            doc = dict(self._documents[i])
            doc["bm25_score"] = float(bm25_scores[i])
            bm25_docs.append(doc)
            if len(bm25_docs) >= top_k:
                break

        # 3) RRF 融合
        rrf: dict[str, float] = {}
        doc_pool: dict[str, dict] = {}

        for rank, d in enumerate(vector_docs):
            key = d.get("id") or d.get("content", "")
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (fusion_k + rank + 1)
            merged = dict(d)
            merged["vector_score"] = float(d.get("score", 0.0))
            merged["matched_by"] = ["vector"]
            doc_pool.setdefault(key, merged)

        for rank, d in enumerate(bm25_docs):
            key = d.get("id") or d.get("content", "")
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (fusion_k + rank + 1)
            if key in doc_pool:
                doc_pool[key]["bm25_score"] = d["bm25_score"]
                doc_pool[key]["matched_by"].append("bm25")
            else:
                merged = dict(d)
                merged["matched_by"] = ["bm25"]
                doc_pool[key] = merged

        ranked = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
        results: list[dict] = []
        for key, score in ranked[:top_k]:
            out = dict(doc_pool[key])
            out["score"] = float(score)
            results.append(out)

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