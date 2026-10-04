package com.smartcs.agent;

import java.util.ArrayList;
import java.util.List;

/**
 * 子任务 — Supervisor 拆解产物。
 * description 必须自包含（结合对话历史还原代词），agent 为执行方：
 * knowledge_rag | ticket_handler | auto（由 dispatch 依据 intent 决定）。
 */
public class SubTask {

    private String id;
    private String description;
    private String agent;
    private List<String> dependsOn = new ArrayList<>();

    public SubTask() {}

    public SubTask(String id, String description, String agent) {
        this.id = id;
        this.description = description;
        this.agent = agent;
    }

    public static SubTask of(String id, String description, String agent) {
        return new SubTask(id, description, agent);
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }

    public String getAgent() { return agent; }
    public void setAgent(String agent) { this.agent = agent; }

    public List<String> getDependsOn() { return dependsOn; }
    public void setDependsOn(List<String> dependsOn) {
        this.dependsOn = dependsOn != null ? dependsOn : new ArrayList<>();
    }
}
