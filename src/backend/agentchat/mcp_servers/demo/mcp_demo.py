"""Demo MCP Server —— 演示 / 工具型，零业务外部依赖。

提供三个工具，用于走通「写代码 → 启动 → 注册 → Agent 调用」全流程：
  - echo            : 原样回显，做连通性自测
  - get_current_time: 返回指定时区当前时间（ISO 8601）
  - calculate       : 安全计算四则运算表达式（仅 + - * / ( ) 与数字）

支持两种传输（--transport）：
  - stdio : 标准输入输出，供本地用 MCP Inspector / 脚本直连调试（默认）
  - sse   : Server-Sent Events，供 AgentChat 后端以 url 方式注册并调用

运行示例：
  # 本地 stdio 调试（用 MCP Inspector: npx @modelcontextprotocol/inspector python mcp_demo.py）
  python mcp_demo.py --transport stdio

  # 以 SSE 启动，供 AgentChat 注册（默认 127.0.0.1:8001，路径 /sse）
  python mcp_demo.py --transport sse --host 0.0.0.0 --port 8001
"""
import argparse
import ast
import operator
import zoneinfo
from datetime import datetime

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("MCP-Demo")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回用户输入的文本，用于 MCP 连通性自测。

    Args:
        text: 任意字符串
    """
    return text


@mcp.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """返回指定 IANA 时区的当前时间，格式为 ISO 8601。

    Args:
        timezone: IANA 时区名，例如 Asia/Shanghai、America/New_York、UTC。
                  若时区数据不可用则回退到 UTC。
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    return datetime.now(tz).isoformat()


@mcp.tool()
def calculate(expression: str) -> str:
    """安全地计算四则运算表达式，仅支持 + - * / ( ) 与整数/小数。

    不允许函数调用、变量、属性访问等，避免任意代码执行。

    Args:
        expression: 例如 "12 * (3 + 4) / 2"
    """
    return _safe_eval(expression)


def _safe_eval(expr: str) -> str:
    """仅用 ast 解析四则运算，杜绝 eval 的代码执行风险。"""
    allowed = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    try:
        node = ast.parse(expr, mode="eval")

        def _ev(n):
            if isinstance(n, ast.Expression):
                return _ev(n.body)
            if isinstance(n, ast.Constant):
                if isinstance(n.value, (int, float)):
                    return n.value
                raise ValueError("仅支持数字常量")
            if isinstance(n, ast.BinOp):
                return allowed[type(n.op)](_ev(n.left), _ev(n.right))
            if isinstance(n, ast.UnaryOp):
                return allowed[type(n.op)](_ev(n.operand))
            raise ValueError("不支持的语法")

        return str(_ev(node))
    except Exception as e:  # noqa: BLE001
        return f"计算失败：{e}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="传输方式：stdio（本地调试）或 sse（供 AgentChat 注册）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="sse 模式监听地址")
    parser.add_argument("--port", type=int, default=8001, help="sse 模式监听端口")
    args = parser.parse_args()

    if args.transport == "sse":
        # FastMCP 1.x：host/port 通过 settings 设置，run() 只接受 transport
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
