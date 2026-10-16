# Demo MCP Server（演示 / 工具型）

最小可跑的 MCP Server 示例，零业务外部依赖，用于走通
「写代码 → 启动 → 注册 → Agent 调用」全流程。

## 暴露的工具

| 工具 | 说明 | 参数 |
|------|------|------|
| `echo` | 原样回显，做连通性自测 | `text: str` |
| `get_current_time` | 返回指定时区当前时间（ISO 8601） | `timezone: str = "Asia/Shanghai"` |
| `calculate` | 安全计算四则运算（+ - * / ( ) 与数字），用 `ast` 解析、不调用 `eval` | `expression: str` |

## 运行

```bash
# 进入目录
cd src/backend/agentchat/mcp_servers/demo

# 1) stdio 模式（默认）—— 本地用 MCP Inspector 调试
python mcp_demo.py --transport stdio
#   或用 Inspector：npx @modelcontextprotocol/inspector python mcp_demo.py

# 2) sse 模式 —— 供 AgentChat 后端以 url 注册并调用
python mcp_demo.py --transport sse --host 0.0.0.0 --port 8001
#   默认路径 /sse，即 http://<host>:8001/sse
```

> 依赖：需要 `mcp`（FastMCP）。项目后端环境已包含，若本地缺依赖：
> `pip install mcp`；`get_current_time` 的时区依赖 `tzdata`（Windows 上
> 没装时自动回退到 UTC，可 `pip install tzdata` 启用真实时区）。

## 在 AgentChat 中注册并让 Agent 调用

**重要：** 当前代码里 Agent 的执行链路（`core/agents/mcp_agent.py` 的
`MCPConfig` 只有 `url`/`type`/`tools`；`utils/convert.py::convert_mcp_config`
对 `stdio` 直接 `pass` 返回 `None`）**只支持 SSE / streamable_http 类型的
MCP Server，不支持 stdio**。因此要让 Agent 真正调用本服务，请按下面 SSE 方式注册。

### 方式 A：加入系统 MCP（`config/mcp_server.json`）

在 `src/backend/agentchat/config/mcp_server.json` 数组追加一项，然后重启后端：

```json
{
  "server_name": "Demo MCP",
  "url": "http://host.docker.internal:8001/sse",
  "type": "sse",
  "config": {},
  "params": {},
  "config_enabled": false,
  "logo_url": ""
}
```

- 后端在 Docker 内运行时，`host.docker.internal` 指向宿主机；本机直接跑后端则用 `http://127.0.0.1:8001/sse`。
- 该文件在启动时写入 `mcp_server` 表，成为「系统 MCP」，Agent 编辑器里可选。

### 方式 B：通过注册接口

`POST /api/v1/register_mcp`，`transport` 目前仅接受 `sse` / `streamable_http`，
`url` 填上面的 SSE 地址即可。

### 绑定到 Agent

在 Agent 编辑器的 MCP 列表里勾选「Demo MCP」，对话时主 Agent 会把它当作
子 Agent（sub-agent）调用其下的 `echo` / `get_current_time` / `calculate` 工具。

## 扩展自己的工具

照着 `@mcp.tool()` 装饰的函数加即可，函数 docstring 会成为工具描述，
类型注解会变成入参 schema：

```python
@mcp.tool()
def my_tool(arg1: str, arg2: int) -> str:
    """工具一句话描述，写清楚何时用、参数是什么。"""
    return do_something(arg1, arg2)
```
