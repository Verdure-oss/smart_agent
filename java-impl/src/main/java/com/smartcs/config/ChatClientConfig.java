package com.smartcs.config;

import org.springframework.context.annotation.Configuration;

/**
 * Spring AI ChatClient配置说明。
 * 注意：不要在这里定义 ChatClient.Builder 的包装 Bean ——
 * Spring Boot 自动配置的原始 Builder Bean 名也是 chatClientBuilder，
 * 同名包装会造成循环依赖。
 *
 * 正确的做法：注入原始 ChatClient.Builder，build 前调用 .defaultAdvisors()
 * 以满足 Spring AI 1.1.x 的 "No CallAdvisors available" 校验。
 * 各 Agent/Controller 统一通过 chatClientBuilder.defaultAdvisors().build() 构建。
 */
@Configuration
public class ChatClientConfig {
    // ChatClient.Builder 由 Spring Boot 自动配置提供，这里不重复定义。
}