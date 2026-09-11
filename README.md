# WorkBuddy & Gemini 反代工具调用崩溃修复方案与参数清洗中间件

> **项目说明**：本仓库针对 WorkBuddy（5.5.4 ~ 5.5.6）在使用自定义 OpenAI 兼容模型（经由反代包装的 Gemini 3.8 Flash / Gemini 2.0 等）时，偶发报 `Error Code: 10000 / Failed to run function tools: TypeError: Cannot read properties of undefined (reading 'split')` 导致整轮会话崩溃的问题，提供完整根因复盘、复现测试用例、以及立竿见影的**透明清洗中间件（Sanitizer Proxy）**。

---

## 一、为什么会碰到这个错误？（根因分析）

在 WorkBuddy 的 Agent 模式下，当向模型请求调用本地工具（如执行命令、操作文件）时，整个调用链如下：

```
[Gemini 模型 (网页/逆向)] 
       ↓ 
[反代服务 (gemini-web2api / antigravity-manager 等)]
       ↓  (转换为 OpenAI 格式的 tool_calls)
[WorkBuddy 客户端]
       ↓  (执行本地 PowerShell / Bash 工具)
💥 崩溃报错：TypeError: Cannot read properties of undefined (reading 'split')
```

### 责任界定与根本机制：
1. **客户端主责（WorkBuddy 防御性编程缺陷）**：
   WorkBuddy 在调用系统工具（`PowerShell` / `Bash`）时，直接对模型传来的参数执行了类似 `args.command.split(' ')` 的字符串分割逻辑。当参数对象中**缺失 `command` 字段**时，直接抛出未捕获的致命 JavaScript 异常，导致当前 Agent 轮次立刻中断。
2. **反代与模型诱因（Gemini 输出参数偶发残缺）**：
   Gemini 在处理极长对话或复杂任务时，偶发只输出了工具的描述（如 `{"description": "查看当前目录"}`），而**漏掉了 schema 中要求的必填字段 `command`**。反代程序没有对其进行合法性校验便直接透传给客户端，从而踩中了 WorkBuddy 的致命弱点。

---

## 二、复现 Payload（供开发者复现）

任意模型向客户端返回如下格式的残缺 `tool_calls`，即可 100% 触发崩溃：

```json
{
  "choices": [
    {
      "delta": {
        "tool_calls": [
          {
            "id": "call_test_undefined_split",
            "type": "function",
            "function": {
              "name": "PowerShell",
              "arguments": "{\"description\": \"列出目录内容\"}"
            }
          }
        ]
      }
    }
  ]
}
```

---

## 三、路线 B：透明清洗中间件（零等待、彻底解决）

不用等待客户端或反代服务官方发版，直接在本地或网关运行本仓库提供的 `sanitizer_proxy.py`。
它作为透明中间层，拦截上游返回的所有流式与非流式数据块：**一旦检测到 `PowerShell` 或 `Bash` 工具调用中缺少 `command`，立即自动注入安全合法占位符**，彻底消除 `undefined`，保护 WorkBuddy 不会自爆。

### 运行方式

1. **启动清洗服务**：
   ```bash
   # 默认监听 8046，上游指向本机 loopback 8045（同机部署）
   python sanitizer_proxy.py

   # 跨机部署：用环境变量指定反代实际地址与监听端口
   UPSTREAM_URL="http://192.168.31.123:8045" LISTEN_PORT=8046 python sanitizer_proxy.py
   ```
   默认监听 `8046` 端口，透明转发至目标反代端点（默认 `http://127.0.0.1:8045`，可用 `UPSTREAM_URL` 环境变量覆盖）。

2. **切换客户端配置**：
   在 WorkBuddy 的模型配置（`models.json`）中，将目标模型的请求端点修改为：
   ```text
   http://<清洗服务地址>:8046/v1/chat/completions
   # 同机示例：http://127.0.0.1:8046/v1/chat/completions
   # NAS 示例：http://192.168.31.123:8046/v1/chat/completions
   ```

---

## 四、双向反馈标准文案

### 1. 提交给 WorkBuddy 官方团队
> **标题**：【Bug报告】OpenAI兼容自定义模型遇到残缺tool_calls时整轮崩溃 (TypeError: undefined.split)  
> **问题定位**：客户端内置的 `PowerShell` / `Bash` 工具执行器在提取 `command` 参数时未做防御性空值校验，遇到缺失 `command` 的 arguments 时直接 `.split()` 导致崩溃。  
> **修复建议**：在提取 `command` 后增加前置判断：若 `typeof command !== 'string'`，直接抛出用户友好的参数缺失提示并请求重试，避免客户端硬崩溃。

### 2. 提交给反代项目官方（gemini-web2api / antigravity-manager）
> **标题**：[Bug/Enhancement] Ensure tool_calls arguments strictly contain schema-required fields for Gemini Flash  
> **问题描述**：Gemini Flash 在长上下文或复杂 prompt 场景下，返回的 function call arguments 偶发仅包含 `description` 而缺少必填的实际执行命令字段。  
> **建议改动**：反代服务在封装为 OpenAI `tool_calls` 结构时，应增加必填字段合规性校验或填充兜底占位值，避免下游兼容客户端抛出解析异常。

---

## License
MIT License
