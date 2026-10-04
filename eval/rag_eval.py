"""
RAG 检索质量评测脚本（可复现）

实现了两类指标：
1. 离线可复现的 IR 指标（无需 LLM / RAGAS）：
   - Context Precision@k  |召回结果里真正相关的占比|
   - Context Recall@k     |相关文档被找回的比例|
   - MRR (Mean Reciprocal Rank)
   - Hit@k
   支持对 [向量单路 / BM25 单路 / 混合(RRF)] 三种召回路做横向对比。

2. （可选）官方 RAGAS 指标：当安装 `ragas` 且配置 LLM 时，
   可对「检索上下文 + 参考上下文」计算 ragascore.context_precision / context_recall。

用法：
    # 1) 离线 IR 指标对比（默认，无需 LLM）
    python eval/rag_eval.py

    # 2) 输出结果到 JSON
    python eval/rag_eval.py --output eval/results.json

    # 3) 官方 RAGAS 指标（需 pip install ragas，且 base_url/模型 配置可用）
    python eval/rag_eval.py --ragas
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.long_term import LongTermMemory, _tokenize_text  # noqa: E402

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"


def load_dataset(path: Path | None = None) -> dict:
    dataset_path = path or DATASET_PATH
    with open(dataset_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_memory(index_path: str, dataset_path: Path | None = None) -> LongTermMemory:
    dataset = load_dataset(dataset_path)
    mem = LongTermMemory(index_path=index_path)
    for doc in dataset["knowledge_docs"]:
        mem.add_document(doc["content"], source=doc["source"])
    return mem


def bm25_topk(mem: LongTermMemory, query: str, top_k: int = 3) -> list[dict]:
    mem._rebuild_bm25()
    scores = mem._bm25.get_scores(_tokenize_text(query))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    out = []
    for i in ranked:
        if scores[i] <= 0:
            continue
        d = dict(mem._documents[i])
        d["score"] = float(scores[i])
        out.append(d)
        if len(out) >= top_k:
            break
    return out


def vector_topk(mem: LongTermMemory, query: str, top_k: int = 3) -> list[dict]:
    return mem.search(query, top_k=top_k)


def hybrid_topk(mem: LongTermMemory, query: str, top_k: int = 3) -> list[dict]:
    return mem.hybrid_search(query, top_k=top_k)


def evaluate_recall_fn(recall_fn, mem: LongTermMemory, top_k: int = 3, dataset_path: Path | None = None) -> dict:
    dataset = load_dataset(dataset_path)
    pairs = dataset["qa_pairs"]

    precisions: list[float] = []
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    hits: list[int] = []

    for pair in pairs:
        question = pair["question"]
        relevant = set(pair["relevant"])
        docs = recall_fn(question, top_k=top_k)
        retrieved_sources = [d.get("source", "") for d in docs]

        retrieved_rel = set(retrieved_sources) & relevant
        precision = len(retrieved_rel) / len(retrieved_sources) if retrieved_sources else 0.0
        recall = len(retrieved_rel) / len(relevant) if relevant else 0.0

        hit = 0
        rr = 0.0
        for i, src in enumerate(retrieved_sources, start=1):
            if src in relevant:
                hit = hit or 1
                rr = 1.0 / i
                break

        precisions.append(precision)
        recalls.append(recall)
        reciprocal_ranks.append(rr)
        hits.append(hit)

    n = len(pairs)
    return {
        "context_precision": round(statistics.mean(precisions), 4),
        "context_recall": round(statistics.mean(recalls), 4),
        "mrr": round(statistics.mean(reciprocal_ranks), 4),
        "hit_rate": round(sum(hits) / n, 4),
        "n_queries": n,
    }


def per_query_report(recall_fn, mem: LongTermMemory, top_k: int = 3, dataset_path: Path | None = None) -> list[dict]:
    dataset = load_dataset(dataset_path)
    rows = []
    for pair in dataset["qa_pairs"]:
        docs = recall_fn(pair["question"], top_k=top_k)
        rows.append({
            "question": pair["question"],
            "relevant": pair["relevant"],
            "retrieved": [d.get("source", "") for d in docs],
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 检索质量评测")
    parser.add_argument("--top_k", type=int, default=3)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--dataset", type=str, default=None, help="评测数据集 json 路径（默认 eval/dataset.json）")
    parser.add_argument("--ragas", action="store_true", help="使用官方 RAGAS 指标（需安装 ragas）")
    args = parser.parse_args()

    dataset_path = Path(args.dataset) if args.dataset else None

    mem = build_memory(index_path=str(ROOT / "vector_store" / "_eval_index"), dataset_path=dataset_path)

    print(f"知识库文档数: {len(mem._documents)}")
    print(f"评测样本数: {len(load_dataset(dataset_path)['qa_pairs'])}  top_k={args.top_k}\n")

    methods = {
        "向量单路(FAISS)": lambda q, top_k=args.top_k: vector_topk(mem, q, top_k),
        "BM25单路": lambda q, top_k=args.top_k: bm25_topk(mem, q, top_k),
        "混合(RRF融合)": lambda q, top_k=args.top_k: hybrid_topk(mem, q, top_k),
    }

    print("=" * 72)
    print(f"{'召回方法':<18}{'ContextP':>10}{'ContextR':>10}{'MRR':>8}{'Hit@k':>8}")
    print("=" * 72)

    results = {}
    for name, fn in methods.items():
        m = evaluate_recall_fn(fn, mem, top_k=args.top_k, dataset_path=dataset_path)
        results[name] = m
        print(
            f"{name:<16}{m['context_precision'] * 100:>8.2f}%"
            f"{m['context_recall'] * 100:>10.2f}%"
            f"{m['mrr']:>8.4f}{m['hit_rate'] * 100:>7.2f}%"
        )

    print("=" * 72)
    print("\n各问题检索明细（混合召回）：")
    hybrid_fn = lambda q, top_k=args.top_k: hybrid_topk(mem, q, top_k)  # noqa: E731
    for row in per_query_report(hybrid_fn, mem, args.top_k, dataset_path=dataset_path):
        ok = set(row["relevant"]) & set(row["retrieved"])
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {row['question']}")
        print(f"        相关: {row['relevant']}  检索: {row['retrieved']}")

    if args.ragas:
        run_ragas(mem, args.top_k)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {out_path}")


def run_ragas(mem: LongTermMemory, top_k: int, dataset_path: Path | None = None) -> None:
    """官方 RAGAS Context Precision / Recall（需要 ragas + LLM 配置）。"""
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import context_precision, context_recall
        from langchain_openai import ChatOpenAI
        import os
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError as e:
        print(f"\n[RAGAS] 依赖缺失，跳过：{e}")
        print("[RAGAS] 请先: pip install ragas langchain-openai python-dotenv")
        return

    dataset = load_dataset(dataset_path)
    questions, contexts, reference = [], [], []

    # ragas 需要真实语料的全量参考上下文：这里用整份知识库作为 reference
    all_contexts = [doc["content"] for doc in dataset["knowledge_docs"]]

    for pair in dataset["qa_pairs"]:
        docs = hybrid_topk(mem, pair["question"], top_k=top_k)
        questions.append(pair["question"])
        contexts.append([d.get("content", "") for d in docs])
        reference.append(" ".join(all_contexts))

    from langchain_core.messages import HumanMessage  # noqa

    llm = ChatOpenAI(
        model=os.getenv("MODEL_NAME", "gpt-4o"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
    )

    ds = Dataset.from_dict({
        "question": questions,
        "contexts": contexts,
        "reference": reference,
    })

    result = evaluate(ds, metrics=[context_precision, context_recall], llm=llm)
    print("\n[RAGAS] 官方指标:")
    print(result)


if __name__ == "__main__":
    main()