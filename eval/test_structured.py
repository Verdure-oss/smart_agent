import asyncio, os, sys
sys.path.insert(0, r"D:\code\smart-cs-multi-agent")
os.chdir(r"D:\code\smart-cs-multi-agent")
from dotenv import load_dotenv
load_dotenv(r"D:\code\smart-cs-multi-agent\.env", override=True)

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

class Demo(BaseModel):
    """测试结构化输出"""
    answer: str = Field(description="简答")
    confidence: float = Field(description="置信度")

llm = ChatOpenAI(model=os.getenv("MODEL_NAME"), temperature=0)

async def t(method):
    try:
        m = llm.with_structured_output(Demo, method=method)
        r = await m.ainvoke([SystemMessage(content="你是助手"), HumanMessage(content="什么是复利？")])
        print(f"[{method}] OK:", r)
    except Exception as e:
        print(f"[{method}] FAIL:", str(e)[:200])

async def main():
    await t("json_schema")
    await t("function_calling")
    await t("json_mode")

asyncio.run(main())