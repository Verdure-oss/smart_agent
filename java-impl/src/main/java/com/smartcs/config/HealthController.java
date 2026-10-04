package com.smartcs.config;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 根路径健康检查 — 与 Python 版 API 契约对齐（前端 Vite 代理 /health 到后端根路径）。
 */
@RestController
public class HealthController {

    @GetMapping("/health")
    public Map<String, Object> health() {
        return Map.of("status", "healthy", "version", "1.0.0");
    }
}
