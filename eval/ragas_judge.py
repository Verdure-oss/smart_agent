"""RAGAS 风格 LLM-as-judge 评测（Context Precision / Recall）

实现口径对齐 RAGAS：
- context_precision: 对检索到的每个 chunk，让 LLM 判定其与问题是否相关，
  按官方公式 Σ(P@k × v_k) / Σ(v_k) 计算；v_k 为相关性指示。
- context_recall: 将参考上下文（gold 文档）交给 LLM，判断检索上下文覆盖其中
  多少信息要点，按覆盖比例计算。

与 eval/rag_eval.py 的关系：
- rag_eval.py 是"离线 IR 口径"（字符串来源匹配，无需 LLM）
- 本脚本是"RAGAS 口径"（LLM-as-judge，与简历指标同口径）

用法：
    python eval/ragas_judge.py --dataset eval/dataset_v2.json --top_k 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE_PATH = ROOT / "eval" / "_ragas_cache.json"

from dotenv import load_dotenv  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from memory.long_term import LongTermMemory  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

RELEVANCE_PROMPT = """你是检索相关性评估员。判断检索到的知识片段是否与用户问题相关。

规则：
1. 片段只要包含能帮助回答问题的事实信息（数值、政策、流程、术语解释等），就判为相关
2. 片段完全无关（如问退款却检索到基金风险提示），判为不相关
3. 只输出一个 JSON：{{"relevant": true/false, "reason": "一句话理由"}}

用户问题: {question}

知识片段:
{chunk}
"""

COVERAGE_PROMPT = """你是检索覆盖度评估员。判断"检索到的上下文集合"覆盖了"参考答案"中的多少信息要点。

步骤：
1. 把参考答案拆成若干信息要点（每个要点是一个独立事实，如"7天内可无理由退款"）
2. 逐个要点判断：检索上下文中是否包含该信息（包含=1，不包含=0）
3. 只输出 JSON：{{"total_points": N, "covered_points": M, "points": [{{"point": "要点", "covered": true/false}}]}}

参考答案:
{reference}

检索上下文（可能有多段）:
{contexts}
"""


class RagasJudge:
    def __init__(self, llm: ChatOpenAI, cache_path: Path = CACHE_PATH):
        self.llm = llm
        self.cache_path = cache_path
        self._cache: dict = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def _cache_key(self, kind: str, question: str, text: str) -> str:
        import hashlib
        h = hashlib.md5((question + text).encode()).hexdigest()[:16]
        return f"{kind}:{h}"

    def _load_cached(self, kind: str, question: str, text: str):
        key = self._cache_key(kind, question, text)
        if key in self._cache:
            return self._cache[key]
        return None

    def _save_cached(self, kind: str, question: str, text: str, value):
        key = self._cache_key(kind, question, text)
        self._cache[key] = value
        try:
            self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    async def _ask_json(self, system: str, human: str) -> dict:
        resp = await self.llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=human),
        ])
        import json as _json
        try:
            return _json.loads(resp.content)
        except Exception:
            # 容错：提取第一个 { ... } 块
            import re
            m = re.search(r"\{.*\}", resp.content, re.S)
            if m:
                return _json.loads(m.group())
            return {}

    async def is_relevant(self, question: str, chunk: str) -> bool:
        cached = self._load_cached("rel", question, chunk)
        if cached is not None:
            return bool(cached)
        out = await self._ask_json(
            RELEVANCE_PROMPT.format(question=question, chunk=chunk),
            "请评估。",
        )
        val = bool(out.get("relevant"))
        self._save_cached("rel", question, chunk, val)
        return val

    async def coverage(self, question: str, reference: str, contexts: list[str]) -> float:
        cached = self._load_cached("cov", question, "\n".join(contexts)[:300])
        if cached is not None:
            return float(cached)
        out = await self._ask_json(
            COVERAGE_PROMPT.format(
                reference=reference,
                contexts="\n---\n".join(contexts),
            ),
            "请评估覆盖度。",
        )
        total = out.get("total_points") or 0
        covered = out.get("covered_points") or 0
        if total <= 0:
            return 0.0
        val = min(covered / total, 1.0)
        self._save_cached("cov", question, "\n".join(contexts)[:300], val)
        return val


def hybrid_topk(mem: LongTermMemory, query: str, top_k: int = 3) -> list[dict]:
    return mem.hybrid_search(query, top_k=top_k)


async def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAS 风格 LLM-judge 评测")
    parser.add_argument("--dataset", type=str, default="eval/dataset.json")
    parser.add_argument("--top_k", type=int, default=3)
    parser.add_argument("--samples", type=int, default=0, help="0=全部")
    parser.add_argument("--start", type=int, default=1, help="从第几个样本开始（1 起）")
    parser.add_argument("--end", type=int, default=0, help="到第几个样本（0=到最后）")
    parser.add_argument("--drop-cache", action="store_true", help="丢弃已有 LLM 判定缓存重新评测")
    args = parser.parse_args()

    if args.drop_cache and CACHE_PATH.exists():
        CACHE_PATH.unlink()
        print("[cache] 已清空 LLM 判定缓存")

    dataset_path = Path(args.dataset)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    docs = dataset["knowledge_docs"]
    pairs = dataset["qa_pairs"]
    if args.samples:
        pairs = pairs[: args.samples]
    elif args.end:
        pairs = pairs[args.start - 1 : args.end]
    elif args.start > 1:
        pairs = pairs[args.start - 1 :]

    mem = LongTermMemory(index_path=str(ROOT / "vector_store" / "_ragas_index"))
    for doc in docs:
        mem.add_document(doc["content"], source=doc["source"])
    print(f"知识库文档数: {len(mem._documents)}  评测样本数: {len(pairs)}  top_k={args.top_k}")

    llm = ChatOpenAI(model="gpt-6-luna", temperature=0)
    judge = RagasJudge(llm)

    # gold 文档内容索引：source → content
    gold_by_source = {d["source"]: d["content"] for d in docs}

    precisions: list[float] = []
    recalls: list[float] = []
    retrievals: list[str] = []

    for i, pair in enumerate(pairs, start=1):
        q = pair["question"]
        retr = hybrid_topk(mem, q, top_k=args.top_k)
        chunks = [d["content"] for d in retr]

        # RAGAS context_precision：逐 chunk 判定相关性
        rel_flags: list[bool] = []
        for c in chunks:
            rel = await judge.is_relevant(q, c)
            rel_flags.append(rel)
        relevant_counts = sum(rel_flags)
        precision = 0.0
        if relevant_counts > 0:
            weighted = 0.0
            for k, v in enumerate(rel_flags, start=1):
                p_at_k = sum(rel_flags[:k]) / k
                if v:
                    weighted += p_at_k
            precision = weighted / relevant_counts
        precisions.append(precision)

        # RAGAS context_recall：标准答案作为参考，判断检索上下文覆盖其中多少要点
        reference = pair.get("answer") or "\n".join(
            gold_by_source.get(s, "") for s in pair["relevant"]
        )
        recall = await judge.coverage(q, reference, chunks)
        recalls.append(recall)

        retrievals.append(
            f"  [{i}] {q}\n       precision={precision:.3f} recall={recall:.3f} | "
            + " | ".join(f"{'R' if f else '·'}{c[:18]}…" for f, c in zip(rel_flags, chunks))
        )

    mean_p = statistics.mean(precisions) * 100
    mean_r = statistics.mean(recalls) * 100
    print("\n" + "=" * 76)
    print(f"{'评测项':<28}{'均值':>10}")
    print("=" * 76)
    print(f"{'Context Precision (RAGAS)':<28}{mean_p:>9.2f}%")
    print(f"{'Context Recall (RAGAS)':<28}{mean_r:>9.2f}%")
    print("=" * 76)
    print("\n逐样本明细：")
    for line in retrievals:
        print(line)


if __name__ == "__main__":
    asyncio.run(main())