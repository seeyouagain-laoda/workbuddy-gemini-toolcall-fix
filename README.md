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
#### 意见 1：【Bug报告】OpenAI兼容自定义模型遇到残缺tool_calls时整轮崩溃 (TypeError: undefined.split)  
> **问题定位**：客户端内置的 `PowerShell` / `Bash` 工具执行器在提取 `command` 参数时未做防御性空值校验，遇到缺失 `command` 的 arguments 时直接 `.split()` 导致崩溃（Error Code 10000）。  
> **修复建议**：在提取 `command` 后增加前置防御性校验：若 `typeof command !== 'string'`，不要执行 `.split()`，而是直接向模型回传错误或补充默认空指令重试，避免前端硬崩溃中断会话。

#### 意见 2：【架构建议】长上下文压缩（Auto-Compact）导致 Thinking 块签名损坏与假死死锁
> **问题定位**：客户端在长会话触发 `auto_compact` 上下文压缩修剪时，对最新 assistant 历史消息中的思考内容（`thinking` 块）进行了总结或截断，破坏了上游加密签名（`thoughtSignature`）。上游服务（如 Gemini 3.7 / 3.8）严格校验思考签名，检测到篡改后直接返回 `400 INVALID_ARGUMENT (thinking blocks in the latest assistant message cannot be modified)`。客户端在收到 400 时误判为常规失败并死循环重试，导致界面无响应长久转圈（假死）。  
> **修复建议**：
> 1. 上下文压缩机制应遵循上游签名规范：对最新 assistant 消息中的 `thinking` 块做**只读保护（Read-only Bypass）**，禁止截断或摘要，保持原样字节回传；
> 2. 对非最新轮次的早期思考过程，在回传时应主动彻底剥离，而非半截篡改；
> 3. 增强单轮工具调用输入输出的截流保护（Trimming），避免单轮大文件读取直接打穿 1M 上限。

#### 意见 3：【死锁防护】工具调用漏参兜底不应诱发模型无限盲目重试
> **问题定位**：当模型偶发漏传必填参数（如漏发 `command`）时，若下游兜底直接返回无意义的占位符（如 `[Fallback]`），模型推理判定为未达预期，而在注意力分散的下一轮中往往再次漏参，形成「漏参 -> 兜底无意义 -> 模型再次重试漏参」的高频死锁死循环，导致界面卡顿并迅速消耗 Token。  
> **修复建议**：
> 1. 客户端在检测到 schema 参数缺失时，应在单轮内明确中断并告知模型具体缺失的字段，而非静默盲试；
> 2. 清洗层与客户端工具执行器应引入**重试熔断器（Max Retry Breaker）**，同一工具调用连续 2 次参数异常时强制截断并要求用户接入或退回纯文本回答。

### 2. 提交给反代项目官方（gemini-web2api / antigravity-manager）
> **标题**：[Bug/Enhancement] Ensure tool_calls arguments strictly contain schema-required fields for Gemini Flash  
> **问题描述**：Gemini Flash 在长上下文或复杂 prompt 场景下，返回的 function call arguments 偶发仅包含 `description` 而缺少必填的实际执行命令字段。  
> **建议改动**：反代服务在封装为 OpenAI `tool_calls` 结构时，应增加必填字段合规性校验或填充兜底占位值，避免下游兼容客户端抛出解析异常。

---

## 守护与自启动 (Systemd 守护)

为确保飞牛 NAS 重启或宿主机断电恢复后服务永不掉线，推荐通过 systemd 系统级托管：

```ini
# /etc/systemd/system/wb-sanitizer.service
[Unit]
Description=WorkBuddy ToolCall Sanitizer Proxy
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=半夏
WorkingDirectory=/home/半夏/wb_sanitizer
ExecStart=/usr/bin/python3 /home/半夏/wb_sanitizer/sanitizer_proxy.py
Restart=always
RestartSec=3
StandardOutput=append:/home/半夏/wb_sanitizer/sanitizer.log
StandardError=append:/home/半夏/wb_sanitizer/sanitizer.log

[Install]
WantedBy=multi-user.target
```

启停命令：
```bash
sudo systemctl daemon-reload
sudo systemctl enable wb-sanitizer.service
sudo systemctl start wb-sanitizer.service
```

---

## License
MIT License
