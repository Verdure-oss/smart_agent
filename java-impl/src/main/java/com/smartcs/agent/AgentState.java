package com.smartcs.agent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Agent全局状态 —— 在Supervisor编排流程中流转的上下文对象（"数据总线"）。
 * 所有子Agent读写同一个State实例；子Agent结果写入subResults / taskResults不同key，避免互相覆盖。
 *
 * 对齐 Python 版 AgentState 的编排字段：
 * sub_tasks / task_results / completed_task_ids / messages（多轮上下文，修复历史不回填的bug）。
 */
public class AgentState {
    private String userId;
    private String sessionId;
    private String userMessage;
    private String intent;
    private Map<String, Object> subResults = new LinkedHashMap<>();
    private boolean compliancePassed = true;
    private String finalResponse;
    private String currentAgent;
    private int retryCount = 0;

    // ===== 多轮对话上下文（由 Controller 从短期记忆回填）=====
    private List<Map<String, String>> messages = new ArrayList<>();
    private String historySummary;      // 滚动摘要（按需注入）
    private String workingInfo;         // 工作记忆跨轮补充信息

    // ===== 子任务编排（Supervisor decompose 产物）=====
    private List<SubTask> subTasks = new ArrayList<>();
    private Map<String, Object> taskResults = new LinkedHashMap<>();   // stepKey(taskId) -> 结果
    private Set<String> completedTaskIds = new LinkedHashSet<>();
    private String currentSubTaskId;
    private String currentSubTaskDescription;
    private int iterationCount = 0;

    public AgentState() {}

    public AgentState(String userId, String sessionId, String userMessage) {
        this.userId = userId;
        this.sessionId = sessionId;
        this.userMessage = userMessage;
    }

    public String getUserId() { return userId; }
    public void setUserId(String userId) { this.userId = userId; }

    public String getSessionId() { return sessionId; }
    public void setSessionId(String sessionId) { this.sessionId = sessionId; }

    public String getUserMessage() { return userMessage; }
    public void setUserMessage(String userMessage) { this.userMessage = userMessage; }

    public String getIntent() { return intent; }
    public void setIntent(String intent) { this.intent = intent; }

    public Map<String, Object> getSubResults() { return subResults; }
    public void setSubResults(Map<String, Object> subResults) { this.subResults = subResults; }

    public boolean isCompliancePassed() { return compliancePassed; }
    public void setCompliancePassed(boolean compliancePassed) { this.compliancePassed = compliancePassed; }

    public String getFinalResponse() { return finalResponse; }
    public void setFinalResponse(String finalResponse) { this.finalResponse = finalResponse; }

    public String getCurrentAgent() { return currentAgent; }
    public void setCurrentAgent(String currentAgent) { this.currentAgent = currentAgent; }

    public int getRetryCount() { return retryCount; }
    public void setRetryCount(int retryCount) { this.retryCount = retryCount; }

    public void addSubResult(String agentName, Object result) {
        this.subResults.put(agentName, result);
    }

    // ===== 新增编排字段 =====

    public List<Map<String, String>> getMessages() { return messages; }
    public void setMessages(List<Map<String, String>> messages) {
        this.messages = messages != null ? messages : new ArrayList<>();
    }

    public String getHistorySummary() { return historySummary; }
    public void setHistorySummary(String historySummary) { this.historySummary = historySummary; }

    public String getWorkingInfo() { return workingInfo; }
    public void setWorkingInfo(String workingInfo) { this.workingInfo = workingInfo; }

    public List<SubTask> getSubTasks() { return subTasks; }
    public void setSubTasks(List<SubTask> subTasks) {
        this.subTasks = subTasks != null ? subTasks : new ArrayList<>();
    }

    public Map<String, Object> getTaskResults() { return taskResults; }
    public void setTaskResults(Map<String, Object> taskResults) {
        this.taskResults = taskResults != null ? taskResults : new LinkedHashMap<>();
    }

    public Set<String> getCompletedTaskIds() { return completedTaskIds; }
    public void setCompletedTaskIds(Set<String> completedTaskIds) {
        this.completedTaskIds = completedTaskIds != null ? completedTaskIds : new LinkedHashSet<>();
    }

    public String getCurrentSubTaskId() { return currentSubTaskId; }
    public void setCurrentSubTaskId(String currentSubTaskId) { this.currentSubTaskId = currentSubTaskId; }

    public String getCurrentSubTaskDescription() { return currentSubTaskDescription; }
    public void setCurrentSubTaskDescription(String currentSubTaskDescription) {
        this.currentSubTaskDescription = currentSubTaskDescription;
    }

    public int getIterationCount() { return iterationCount; }
    public void setIterationCount(int iterationCount) { this.iterationCount = iterationCount; }

    /**
     * 找到下一个可执行的子任务：未完成且依赖（dependsOn）均已满足。
     */
    public SubTask findNextRunnable() {
        for (SubTask t : subTasks) {
            if (t.getId() == null || completedTaskIds.contains(t.getId())) {
                continue;
            }
            List<String> deps = t.getDependsOn();
            boolean depsOk = (deps == null) || completedTaskIds.containsAll(deps);
            if (depsOk) {
                return t;
            }
        }
        return null;
    }
}
