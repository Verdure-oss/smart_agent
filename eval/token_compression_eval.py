"""
Token 压缩效果评估 — 全量历史 vs 按需注入（滚动摘要 + 最近 N 轮）

可复现脚本：不依赖 LLM / Redis / 外部服务，
在"内存回退 + 截断式模拟摘要（保守下限）"下对比两种 Prompt 注入方案的 Token 消耗。

用法：
    python eval/token_compression_eval.py [--turns 20] [--recent 4] [--full 10]

输出：两种方案的累积 Prompt Token、每轮均值、节省百分比。
真实 LLM 摘要通常比模拟更精简，因此这里测出的是"保守下限"。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.short_term import ShortTermMemory  # noqa: E402


def estimate_tokens(text: str) -> int:
    """与 api/main.py 的 _estimate_tokens 保持同口径"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(len(text) // 2, 0)


def mock_compressed_summary(msgs: list[dict]) -> str:
    """模拟 LLM 摘要：把一段对话压缩成要点式短句（每条消息约 20-30 字符）。

    真实 LLM 摘要通常更精炼，因此这是偏保守的下限估计。
    """
    role_cn = {"user": "用户", "assistant": "客服"}
    compact = []
    for m in msgs:
        text = m["content"]
        ids = re.findall(r"(?:ORD-|TK-|P-)[A-Z0-9-]+", text)
        keywords = ("收益", "利率", "风险", "退", "开户", "测评", "限额", "订单", "工单")
        if ids:
            key = f"涉及单号{ids[0]}"
        elif any(k in text for k in keywords):
            key = text[:14].rstrip("，。！？") + "…"
        else:
            key = text[:12].rstrip("，。！？")
        compact.append(f"{role_cn[m['role']]}{key}")
    return "；".join(compact)


def build_session_turns(total_turns: int) -> list[tuple[str, str]]:
    """构造一个真实的客服多轮对话（含订单、退款、补充信息等）"""
    script = [
        ("我想查一下我的订单", "好的，请提供您的订单号。"),
        ("订单号是 ORD-20260620-002", "查询到您的订单：理财产品B，10万元，状态为待处理。"),
        ("这个订单什么时候能完成？", "预计3个工作日内完成，请留意通知。"),
        ("我想退款", "退款需要在7天内申请，请问您要退哪个订单？"),
        ("退 ORD-20260620-002 这个", "已为您创建退款工单 TK-20260620-ABC123，3-5个工作日原路退回。"),
        ("退款大概多久到账？", "退款将在3-5个工作日内原路退回至您的支付账户。"),
        ("那理财产品A的收益是多少？", "理财产品A年化收益率3.5%-5.2%，最低投资1万元。"),
        ("最低能投多少？", "最低投资金额为1万元，投资期限6个月起。"),
        ("我想买5万，怎么操作？", "已在您的账户页面生成购买确认，请完成风险测评后确认。"),
        ("好的，我测评过了", "已为您创建购买工单 TK-20260620-DEF456，将按流程为您处理。"),
        ("测评有效期多久？", "风险测评结果有效期为两年。"),
        ("那我两年后还要重新测？", "是的，到期后需重新完成风险评估问卷。"),
        ("理财有风险吗？", "理财非存款，产品有风险，投资须谨慎。"),
        ("知道了，谢谢", "不客气，如还有其他问题随时咨询。"),
        ("我想问一下退保的事情", "请问您是指哪份保险产品？"),
        ("就是理财B附带的那份", "退保在犹豫期内无损失，超过犹豫期按现金价值退还。"),
        ("那我现在退保能退多少？", "您的保单已超过犹豫期，具体以保险合同现金价值表为准。"),
        ("能给我算一下吗？", "请提供保单号，我为您查询现金价值。"),
        ("保单号是 P-8899", "已为您查询，当前现金价值约为 85000 元。"),
        ("好的，我知道了", "好的，如需办理退保请随时告诉我。"),
    ]
    if total_turns > len(script):
        script = script * (total_turns // len(script) + 1)
    return script[:total_turns]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--recent", type=int, default=4, help="按需注入保留最近几轮(一问一答=1轮)")
    parser.add_argument("--full", type=int, default=10, help="旧方案全量注入的轮数上限")
    parser.add_argument("--threshold", type=int, default=6, help="新增未压缩消息超过该值触发压缩")
    args = parser.parse_args()

    turns = build_session_turns(args.turns)
    mem = ShortTermMemory()  # redis 不可用 → 内存回退

    full_total = injected_total = 0
    last_n = args.recent * 2  # 一问一答 = 2 条消息
    session = "eval-session"

    print(f"{'轮次':<5}{'全量Token':<12}{'按需Token':<12}{'节省%':<10}{'状态'}")
    print("-" * 60)

    for i, (user_msg, assistant_msg) in enumerate(turns, start=1):
        await mem.add_message(session, "user", user_msg)
        await mem.add_message(session, "assistant", assistant_msg)

        # 旧方案：最近 full 轮全量注入
        full_history = await mem.get_history(session, last_n=args.full * 2)
        full_prompt = "\n".join(f"{m['role']}: {m['content']}" for m in full_history)

        # 新方案：滚动摘要 + 最近 recent 轮原文
        summary_text, _ = await mem.get_summary(session)
        recent_msgs = await mem.get_history(session, last_n=last_n)
        injected_parts = []
        if summary_text:
            injected_parts.append(f"【会话摘要】{summary_text}")
        injected_parts.extend(f"{m['role']}: {m['content']}" for m in recent_msgs)
        injected_prompt = "\n".join(injected_parts)

        full_tokens = estimate_tokens(full_prompt)
        injected_tokens = estimate_tokens(injected_prompt)
        full_total += full_tokens
        injected_total += injected_tokens

        # 增量压缩：未压缩消息超过阈值时，把最早一段压入摘要（与 main.py 同规则）
        summary_text, compacted_upto = await mem.get_summary(session)
        unconsumed = len(await mem.get_history(session)) - compacted_upto
        compressed_now = False
        if unconsumed > args.threshold:
            history = await mem.get_history(session)
            cutoff = max(compacted_upto, len(history) - last_n)
            old = history[compacted_upto:cutoff]
            if old:
                new_summary = mock_compressed_summary(old)
                final_text = f"{summary_text}；{new_summary}" if summary_text else new_summary
                await mem.set_summary(session, final_text, cutoff)
                compressed_now = True

        saved = (full_tokens - injected_tokens) / full_tokens * 100 if full_tokens else 0
        status = "压缩" if compressed_now else ""
        print(f"{i:<5}{full_tokens:<12}{injected_tokens:<12}{saved:>6.1f}%    {status}")

    print("-" * 60)
    total_saved = (full_total - injected_total) / full_total * 100 if full_total else 0
    avg_full = full_total // len(turns)
    avg_injected = injected_total // len(turns)
    print(f"\n累计 {args.turns} 轮 → 全量方案总 Token: {full_total} (均值 {avg_full}/轮)")
    print(f"累计 {args.turns} 轮 → 按需注入总 Token: {injected_total} (均值 {avg_injected}/轮)")
    print(f"Token 消耗降低: {total_saved:.1f}%  （保守下限，LLM 摘要通常更省）")


if __name__ == "__main__":
    asyncio.run(main())