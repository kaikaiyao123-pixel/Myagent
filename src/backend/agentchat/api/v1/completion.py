import json
import time
from typing import List, Callable
from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from loguru import logger

from agentchat.core.agents.general_agent import GeneralAgent, AgentConfig
from agentchat.api.services.history import HistoryService
from agentchat.api.services.dialog import DialogService
from agentchat.api.responses.streaming import WatchedStreamingResponse
from agentchat.api.services.user import UserPayload, get_login_user
from agentchat.prompts.completion import SYSTEM_PROMPT
from agentchat.schemas.completion import CompletionReq
from agentchat.services.memory.client import memory_client
from agentchat.services.semantic_cache import (
    cache_eligible,
    get_semantic_cache,
    prompt_hash as compute_prompt_hash,
)
from agentchat.services.langfuse_trace import message_to_text, trace_chat
from agentchat.utils.common import count_tokens_usage
from agentchat.utils.contexts import set_user_id_context, set_agent_name_context
from agentchat.utils.helpers import build_completion_system_prompt, build_completion_user_input

router = APIRouter(tags=["Completion"])

# 模型异常兜底文案（general_agent.astream 内）—— 这类响应不是有效答案，禁止写回缓存
_FALLBACK_MARKER = "知识盲区"


async def _respond_from_cache(answer: str, *, req: CompletionReq,
                             login_user: UserPayload,
                             agent_config: AgentConfig):
    """语义缓存命中：落库（与正常流式路径保持一致）+ 返回单 chunk SSE。"""
    await HistoryService.save_chat_history(
        role="user",
        content=req.user_input,
        events=[],
        dialog_id=req.dialog_id,
        token_usage=count_tokens_usage(req.user_input),
        memory_enable=agent_config.enable_memory,
    )
    await HistoryService.save_chat_history(
        role="assistant",
        content=answer,
        events=[],
        dialog_id=req.dialog_id,
        token_usage=count_tokens_usage(answer),
        memory_enable=agent_config.enable_memory,
    )
    await DialogService.update_dialog_summary(
        dialog_id=req.dialog_id,
        user_id=login_user.user_id,
    )

    async def cached_stream():
        # 事件格式与 chat_agent.astream 的 response_chunk 完全一致，前端无感
        yield f"data: {json.dumps({'type': 'response_chunk', 'timestamp': time.time(), 'data': {'chunk': answer, 'accumulated': answer}}, ensure_ascii=False)}\n\n"

    return WatchedStreamingResponse(
        content=cached_stream(),
        callback=lambda: None,
        media_type="text/event-stream",
    )

@router.post("/completion", description="对话接口")
async def completion(
    *,
    req: CompletionReq,
    login_user: UserPayload = Depends(get_login_user)
):
    """
    实时对话接口（SSE流式）
    """

    # Agent 初始化
    db_config = await DialogService.get_agent_by_dialog_id(req.dialog_id)
    agent_config = AgentConfig(**db_config)
    agent_config.user_id = login_user.user_id

    # 设置上下文信息
    set_user_id_context(login_user.user_id)
    set_agent_name_context(agent_config.name)

    chat_agent = GeneralAgent(agent_config)
    await chat_agent.init_agent()

    # 输入处理
    raw_input = req.user_input

    user_input = build_completion_user_input(
        file_url=req.file_url,
        user_input=raw_input
    )

    # Prompt 构建
    # prompt_template 是「模板级」系统提示词（不含历史摘要/记忆的运行时展开），
    # 语义缓存的 prompt_hash 用它 —— 同一 Agent 模板答案风格一致才可复用（方案 2.3）
    prompt_template = agent_config.system_prompt.strip() or SYSTEM_PROMPT
    cache_prompt_hash = compute_prompt_hash(prompt_template)

    short_history = await HistoryService.get_short_term_messages(
        req.dialog_id, login_user.user_id
    )
    history_summary = await DialogService.get_dialog_history_summary(req.dialog_id)

    long_memory = None
    if agent_config.enable_memory:
        memories = await memory_client.search(
            query=raw_input,
            run_id=req.dialog_id
        )
        long_memory = "\n".join(
            m.get("memory", "") for m in memories.get("results", [])
        )

    system_prompt = build_completion_system_prompt(
        prompt_template,
        history_summary,
        long_memory
    )

    messages: List[BaseMessage] = [
        SystemMessage(content=system_prompt),
        *short_history,
        HumanMessage(content=user_input),
    ]

    # ---- 语义缓存：门控 + 查询（方案 2.1/2.5；默认关闭，见配置模板）----
    cache = None
    if cache_eligible(
        file_url=req.file_url,
        enable_memory=agent_config.enable_memory,
        has_short_history=bool(short_history),
    ):
        cache = get_semantic_cache()
    if cache is not None:
        sim, cached_answer = await cache.get(raw_input, prompt_hash=cache_prompt_hash)
        if cached_answer is not None:
            logger.info(
                f"语义缓存命中：sim={sim:.4f} kb_version={cache.kb_version} "
                f"dialog={req.dialog_id}")
            return await _respond_from_cache(
                cached_answer,
                req=req,
                login_user=login_user,
                agent_config=agent_config,
            )

    # 事件 & 流式响应
    events: list = []

    async def stream():
        response_content = " "
        try:
            # Langfuse trace 根（方案 4.4）：模型调用在中间件内挂为子 generation
            with trace_chat(
                name="agentchat",
                user_id=login_user.user_id,
                session_id=req.dialog_id,
                input=message_to_text(messages),
            ):
                async for event in chat_agent.astream(messages):

                    if event.get("type") == "response_chunk":
                        chunk = event["data"].get("chunk", "")
                        response_content += chunk
                    else:
                        events.append(event)

                    yield f"data: {json.dumps(event)}\n\n"

        finally:
            if agent_config.enable_memory:
                await memory_client.add(
                    messages=[
                        {"role": "user", "content": raw_input},
                        {"role": "assistant", "content": response_content}
                    ],
                    run_id=req.dialog_id
                )

            # 语义缓存写回（方案 2.5）：只写完整、非兜底的答案
            if (cache is not None
                    and response_content.strip()
                    and _FALLBACK_MARKER not in response_content):
                try:
                    await cache.put(
                        raw_input,
                        response_content,
                        prompt_hash=cache_prompt_hash,
                        meta={"dialog_id": req.dialog_id},
                    )
                except Exception as e:
                    logger.warning(f"语义缓存写回失败（已忽略，不影响应答）：{e}")

            await HistoryService.save_chat_history(
                role="assistant",
                content=response_content,
                events=events,
                dialog_id=req.dialog_id,
                token_usage=count_tokens_usage(response_content),
                memory_enable=agent_config.enable_memory
            )

            await DialogService.update_dialog_summary(
                dialog_id=req.dialog_id,
                user_id=login_user.user_id,
            )

    # 用户消息先落库
    await HistoryService.save_chat_history(
        role="user",
        content=raw_input,
        events=events,
        dialog_id=req.dialog_id,
        token_usage=count_tokens_usage(raw_input),
        memory_enable=agent_config.enable_memory
    )

    # 返回流式响应
    return WatchedStreamingResponse(
        content=stream(),
        callback=chat_agent.stop_streaming_callback,
        media_type="text/event-stream"
    )
