"""
Langfuse 全链路埋点（《稳健型三件套实施方案》第 4 章）

设计原则 —— 双重降级，任何一步失败只记日志、绝不影响主链路：
  1. 包缺失（环境没装 langfuse）          -> 全部 no-op
  2. 配置关闭 / 公私钥缺失 / 客户端初始化失败 -> 全部 no-op

接入方式：
  - trace_chat()        ：包住一次 /completion 的 agent 流式过程，建 trace 根
  - trace_generation()  ：包住单次模型调用，建 generation 观测
  - 两者的 observation 都经过 _SafeObservation 包装，update 失败被吞掉

API 说明（langfuse 5.x，与方案原稿的 v2 骨架不同）：
  方案里的 lf.generation(...) 是 v2 API，5.x 已移除；
  5.x 用 client.start_as_current_observation(as_type="generation") +
  observation.update(..., usage_details={...}, cost_details={...})。
"""
from contextlib import contextmanager
from typing import Optional

from loguru import logger

from agentchat.settings import app_settings

try:
    from langfuse import Langfuse as _Langfuse
    _HAS_LANGFUSE = True
except ImportError:  # 包不存在是合法状态（可选依赖）
    _HAS_LANGFUSE = False

_UNSET = object()
_client = _UNSET


def get_langfuse():
    """返回 Langfuse 客户端单例；未装包/未启用/缺 Key/初始化失败一律 None。"""
    global _client
    if _client is not _UNSET:
        return _client
    _client = None

    if not _HAS_LANGFUSE:
        logger.info("未安装 langfuse 包，可观测埋点降级为关闭（pip install langfuse 可启用）")
        return None

    cfg = getattr(app_settings, "langfuse", None) or {}
    if not cfg.get("enabled"):
        return None
    public_key, secret_key = cfg.get("public_key"), cfg.get("secret_key")
    if not public_key or not secret_key:
        logger.warning("langfuse.enabled=true 但公私钥缺失，埋点降级为关闭")
        return None
    try:
        _client = _Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=cfg.get("host") or "https://cloud.langfuse.com",
        )
        logger.info(f"Langfuse 埋点已启用：host={cfg.get('host')}")
    except Exception as e:
        logger.error(f"Langfuse 客户端初始化失败，埋点降级为关闭：{e}")
        _client = None
    return _client


class _SafeObservation:
    """observation 代理：update 失败只记日志。
    否则一次上报异常会把已经成功的模型调用变成 500。"""

    def __init__(self, obs):
        self._obs = obs

    def update(self, **kwargs):
        try:
            self._obs.update(**kwargs)
        except Exception as e:
            logger.warning(f"Langfuse observation update 失败（已忽略）：{e}")


def _resolve(client):
    return client if client is not None else get_langfuse()


@contextmanager
def trace_chat(*, name: str, user_id=None, session_id=None,
               input=None, metadata: dict = None, client=None):
    """一次对话的 trace 根（方案 4.4）。模型调用在其内部自动挂为子观测。"""
    lf = _resolve(client)
    cfg = getattr(app_settings, "langfuse", None) or {}
    if lf is None or not cfg.get("trace_chat", True):
        yield None
        return
    meta = dict(metadata or {})
    if user_id is not None:
        meta["user_id"] = user_id
    if session_id is not None:
        meta["session_id"] = session_id
    try:
        with lf.start_as_current_observation(
            as_type="span",
            name=name,
            trace_context={"trace_id": lf.create_trace_id()},
            input=input,
            metadata=meta,
        ) as root:
            yield _SafeObservation(root)
    except Exception as e:
        # 建 trace 失败不该影响对话；日志即可
        logger.warning(f"Langfuse trace 建立失败（已忽略）：{e}")
        yield None


@contextmanager
def trace_generation(*, name: str, model: str = None, input=None,
                     metadata: dict = None, client=None):
    """单次模型调用的 generation 观测（方案 4.3）。
    yield 出的对象若非 None，调用方应在拿到响应后 update(output/usage_details)。"""
    lf = _resolve(client)
    if lf is None:
        yield None
        return
    try:
        obs_cm = lf.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            input=input,
            metadata=dict(metadata or {}),
        )
        # 必须真正进入上下文管理器：否则观测不会开始/结束，langfuse 什么都收不到
        obs = obs_cm.__enter__()
    except Exception as e:
        logger.warning(f"Langfuse generation 建立失败（已忽略）：{e}")
        yield None
        return
    try:
        yield _SafeObservation(obs)
    except Exception as e:
        try:
            obs.update(level="ERROR", status_message=str(e))
        except Exception:
            pass
        try:
            obs_cm.__exit__(type(e), e, e.__traceback__)
        except Exception as exit_err:
            logger.warning(f"Langfuse observation 退出失败（已忽略）：{exit_err}")
        raise
    else:
        try:
            obs_cm.__exit__(None, None, None)
        except Exception as exit_err:
            logger.warning(f"Langfuse observation 退出失败（已忽略）：{exit_err}")


def message_to_text(messages) -> str:
    """把 langchain 消息列表压成可观测的文本（截断，避免把整个上下文灌进面板）。"""
    parts = []
    for m in messages or []:
        role = getattr(m, "type", None) or m.__class__.__name__
        content = getattr(m, "content", None)
        if content is None:
            content = str(m)
        parts.append(f"[{role}] {content}")
    text = "\n".join(parts)
    return text[:4000] + ("…<截断>" if len(text) > 4000 else "")
