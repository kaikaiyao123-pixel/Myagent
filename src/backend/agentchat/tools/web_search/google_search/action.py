from langchain.tools import tool
from langchain_community.utilities import SerpAPIWrapper
from agentchat.settings import app_settings

# ---------------------------------------------------------------------------
# 惰性初始化（勿改回模块顶层实例化）
#
# ⚠️ 原实现在模块顶层直接实例化：
#       search = SerpAPIWrapper(serpapi_api_key=app_settings.tools.google.get('api_key'))
#    而 agentchat/tools/__init__.py 会在 import 期加载【全部】工具模块，
#    于是只要 tools.google.api_key 为空，SerpAPIWrapper 的 pydantic 校验就会抛
#    ValidationError，导致【整个应用启动失败】——即便用户完全不使用网页搜索功能。
#    实测在多 worker 生产部署下表现为 lifespan 启动失败、容器反复重启、接口 502。
#    改为惰性初始化后，缺 Key 仅在使用该工具时报错，不影响服务启动。
# ---------------------------------------------------------------------------
_search = None


def _get_search():
    global _search
    if _search is None:
        api_key = ""
        if app_settings.tools is not None:
            api_key = (app_settings.tools.google or {}).get("api_key") or ""
        if not api_key:
            raise ValueError(
                "网页搜索工具未配置 api_key（config.yaml → tools.google.api_key），无法使用"
            )
        _search = SerpAPIWrapper(serpapi_api_key=api_key)
    return _search


@tool("web_search", parse_docstring=True)
def google_search(query: str):
    """
    根据用户的问题进行网上搜索信息。

    Args:
        query (str): 用户的问题。

    Returns:
        str: 搜索到的信息。
    """
    return _google_search(query)

def _google_search(query: str):
    """使用搜索工具给用户进行搜索"""
    result = _get_search().run(query)
    return result
