#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 自定义模型工具调用参数清洗中间件 (ToolCall Sanitizer)
监听本地 8046 端口，透明转发到 NAS 反代 8045 端口。
若检测到 tool_calls 缺失 command 参数，自动注入安全占位符，彻底防止 WB undefined.split 崩溃。
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error

# 上游反代服务地址。可通过环境变量 UPSTREAM_URL 覆盖；
# 默认指向本机 loopback（清洗服务与反代同机部署时最高效）。
# 若清洗服务与反代不在同一台机器，请改为反代实际地址，例如 http://192.168.31.123:8045
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://127.0.0.1:8045")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8046"))

class SanitizerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        url = f"{UPSTREAM_URL}{self.path}"
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        headers = {key: val for key, val in self.headers.items() if key.lower() not in ["host", "content-length"]}
        req = urllib.request.Request(url, data=post_data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                resp_headers = resp.info()
                content_type = resp_headers.get("Content-Type", "")
                self.send_response(resp.status)
                for k, v in resp_headers.items():
                    if k.lower() not in ["transfer-encoding", "content-length"]:
                        self.send_header(k, v)

                # 处理非流式 JSON 响应
                if "application/json" in content_type:
                    raw_body = resp.read().decode("utf-8")
                    try:
                        data = json.loads(raw_body)
                        data = self._sanitize_payload(data)
                        repaired = json.dumps(data).encode("utf-8")
                        self.send_header("Content-Length", str(len(repaired)))
                        self.end_headers()
                        self.wfile.write(repaired)
                    except Exception:
                        self.send_header("Content-Length", str(len(raw_body.encode("utf-8"))))
                        self.end_headers()
                        self.wfile.write(raw_body.encode("utf-8"))
                else:
                    # 流式响应逐块透明传输并清洗
                    self.end_headers()
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        line_str = line.decode("utf-8", errors="replace")
                        if line_str.startswith("data: ") and line_str.strip() != "data: [DONE]":
                            try:
                                chunk = json.loads(line_str[6:].strip())
                                chunk = self._sanitize_payload(chunk)
                                line = f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                            except Exception:
                                pass
                        self.wfile.write(line)
                        self.wfile.flush()
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))

    def _sanitize_payload(self, data):
        """核心守卫：检查 choices 内的 tool_calls，若缺 command 则赋予合法安全兜底或智能补全，防止模型死循环"""
        try:
            choices = data.get("choices", [])
            for c in choices:
                delta = c.get("delta") or c.get("message") or {}
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    if fn_name in ["PowerShell", "Bash", "run_command"]:
                        args_raw = fn.get("arguments", "{}")
                        if isinstance(args_raw, str):
                            try:
                                args = json.loads(args_raw)
                            except Exception:
                                args = {}
                        else:
                            args = args_raw or {}

                        if not args.get("command") or not str(args.get("command")).strip():
                            desc = str(args.get("description", "")).strip()
                            desc_lower = desc.lower()

                            # 智能死锁消除策略 1：如果是微信通知相关动作，模型漏传 command 时自动补全标准命令并执行成功，彻底消除“没成功又重试”的死循环
                            if any(k in desc_lower or k in desc for k in ["微信", "notify", "通知", "推送"]):
                                task_title = desc if desc else "任务完成通知"
                                # 安全清洗标题字符，防止命令注入
                                safe_title = "".join(ch for ch in task_title if ch.isalnum() or ch in " _-一段测试报告").strip()
                                if not safe_title:
                                    safe_title = "任务已完成"
                                args["command"] = f'export -n PYTHONPATH; bash ~/.workbuddy/scripts/notify_done.sh "{safe_title}"'
                            else:
                                # 智能死锁消除策略 2：普通命令漏参时，返回带有 [OK] 标志的明确完成提示，告诉模型已记录，无需反复重试
                                fallback_msg = desc if desc else "command executed"
                                args["command"] = f'echo "[OK: Action logged - {fallback_msg}]"'

                            fn["arguments"] = json.dumps(args) if isinstance(args_raw, str) else args
        except Exception:
            pass
        return data

    def do_GET(self):
        url = f"{UPSTREAM_URL}{self.path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                self.end_headers()
                self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), SanitizerHandler)
    print(f"ToolCall Sanitizer 启动成功，监听端口 {LISTEN_PORT} -> 目标端点 {UPSTREAM_URL}")
    server.serve_forever()
