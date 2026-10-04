package com.smartcs.agent;

import java.util.List;

/**
 * Supervisor 拆解的结构化输出（LLM → BeanOutputConverter 反序列化目标）。
 * 走 prompt 内嵌 JSON Schema（BeanOutputConverter），不使用 response_format=json_schema，
 * 与 Python 版踩坑结论一致：对 OpenAI 兼容网关兼容性最好。
 */
public class SupervisorPlan {

    private List<SubTask> subTasks;

    public List<SubTask> getSubTasks() { return subTasks; }
    public void setSubTasks(List<SubTask> subTasks) { this.subTasks = subTasks; }
}
