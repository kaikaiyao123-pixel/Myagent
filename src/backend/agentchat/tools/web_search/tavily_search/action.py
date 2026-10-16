from typing import Type, Optional, Literal
from langchain.tools import tool
from tavily import TavilyClient

from agentchat.settings import app_settings

# ---------------------------------------------------------------------------
# 惰性初始化（勿改回模块顶层实例化）
#
# ⚠️ 原实现在模块顶层实例化：
#       tavily_client = TavilyClient(app_settings.tools.tavily.get("api_key"))
#    TavilyClient 会校验 api_key，为空即抛异常；而 agentchat/tools/__init__.py
#    在 import 期加载全部工具模块，因此空 Key 会导致【整个应用启动失败】，
#    与 google_search/action.py 是同一类问题。
# ---------------------------------------------------------------------------
_tavily_client = None


def _get_tavily_client():
    global _tavily_client
    if _tavily_client is None:
        api_key = ""
        if app_settings.tools is not None:
            api_key = (app_settings.tools.tavily or {}).get("api_key") or ""
        if not api_key:
            raise ValueError(
                "联网搜索工具未配置 api_key（config.yaml → tools.tavily.api_key），无法使用"
            )
        _tavily_client = TavilyClient(api_key)
    return _tavily_client


@tool("web_search", parse_docstring=True)
def tavily_search(query: str,
                  topic: Optional[str],
                  max_results: Optional[int],
                  time_range: Optional[Literal["day", "week", "month", "year"]]):
    """
    根据用户的问题以及查询参数进行联网搜索

    Args:
        query: 用户想要搜索的问题
        topic: 搜索主题领域，general为通用，news为新闻，finance为财经
        max_results: 最大返回结果数量，控制结果数量上限
        time_range: 时间范围，筛选过去一天、一周、一个月或一年的内容

    Returns:
        将联网搜索到的信息返回给用户
    """
    return _tavily_search(query, topic, max_results, time_range)

def _tavily_search(query, topic, max_results, time_range):
    """使用Tavily搜索工具给用户进行搜索"""
    response = _get_tavily_client().search(
        query=query,
        country="china",
        topic=topic,
        time_range=time_range,
        max_results=max_results
    )

    return "\n\n".join([f'网址:{result["url"]}, 内容: {result["content"]}' for result in response["results"]])
