"""滚动摘要压缩验证 —— 严格按 api.main 的真实调用时机"""
import asyncio, os, sys
sys.path.insert(0, r"D:\code\smart-cs-multi-agent")
os.chdir(r"D:\code\smart-cs-multi-agent")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
from dotenv import load_dotenv
load_dotenv(r"D:\code\smart-cs-multi-agent\.env", override=True)

import logging
logging.basicConfig(level=logging.WARNING)

from memory.short_term import ShortTermMemory
import api.main as api

short_term = api.short_term_memory  # 复用 main.py 里的真实实例
RECENT_TURNS = api.RECENT_TURNS

async def main():
    session_id = "summary-test2"
    # 模拟 10 轮对话，每轮都走 main.py 的真实顺序：
    # add_message(user) → _maybe_compact_history → add_message(assistant)
    for i in range(1, 11):
        await short_term.add_message(session_id, "user", f"第{i}问：理财产品A基本情况？")
        await api._maybe_compact_history(session_id)  # 主链路时机
        await short_term.add_message(session_id, "assistant", f"年化3.5%-5.2%，最低1万元。（回复{i}）")

    summary, upto = await short_term.get_summary(session_id)
    full = await short_term.get_history(session_id)
    summary_text, recent = await short_term.get_injection_context(session_id, recent_turns=RECENT_TURNS)

    full_tokens = sum(len(f"{m['role']}: {m['content']}") // 2 for m in full)
    inject_tokens = sum(len(summary_text) // 2 for _ in [0] if summary_text) + \
                    sum(len(f"{m['role']}: {m['content']}") // 2 for m in recent)

    print("=" * 72)
    print(f"[会话统计] 总消息={len(full)} 条 | compacted_upto={upto} (阈值 COMPACT_THRESHOLD={api.COMPACT_THRESHOLD})")
    print(f"[滚动摘要] {summary[:300] if summary else '(无)'}")
    print(f"[注入统计] 全量={full_tokens} tokens → 摘要+近轮={inject_tokens} tokens")
    print(f"[节省] {max(0, 1-inject_tokens/full_tokens)*100:.1f}%")

asyncio.run(main())