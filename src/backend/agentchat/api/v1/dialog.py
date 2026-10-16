from loguru import logger
from fastapi import APIRouter, Depends, Body
from agentchat.api.services.agent import AgentService
from agentchat.api.services.dialog import DialogService
from agentchat.api.services.user import UserPayload, get_login_user
from agentchat.schemas.dialog import DialogCreateRequest
from agentchat.api.responses.builder import resp_200, resp_500, UnifiedResponseModel

router = APIRouter(tags=["Dialog"])


@router.get("/dialog/list", response_model=UnifiedResponseModel)
async def get_dialog(
    login_user: UserPayload = Depends(get_login_user)
):
    try:
        messages = await DialogService.get_list_dialog(user_id=login_user.user_id)
        results = []

        # 仅用智能体的「展示字段」补充会话卡片（头像、描述、模型、工具等）。
        # 注意：绝不能让智能体的字段覆盖会话自身的身份/时间字段，
        # 否则会话会显示成「智能体的创建时间」（如默认智能体 2026-09-16），
        # 造成“今天创建的会话却显示 9 月 16 日”的 bug。
        agent_display_fields = (
            "id", "logo_url", "description", "llm_id", "mcp_ids",
            "agent_skill_ids", "knowledge_ids", "system_prompt",
            "enable_memory", "tool_ids", "is_custom", "name",
        )

        for message in messages:
            message_agent = await AgentService.select_agent_by_id(
                agent_id=message["agent_id"]
            )
            if message_agent:
                for key in agent_display_fields:
                    if key in message_agent:
                        message[key] = message_agent[key]
            results.append(message)

        return resp_200(data=results)
    except Exception as err:
        logger.error(err)
        return resp_500(message=str(err))


@router.post("/dialog", response_model=UnifiedResponseModel)
async def create_dialog(
    dialog_req: DialogCreateRequest,
    login_user: UserPayload = Depends(get_login_user)
):
    try:
        dialog = await DialogService.create_dialog(
            name=dialog_req.name,
            agent_id=dialog_req.agent_id,
            agent_type=dialog_req.agent_type,
            user_id=login_user.user_id
        )
        return resp_200(dialog)
    except Exception as err:
        logger.error(err)
        return resp_500(message=str(err))


@router.delete("/dialog", response_model=UnifiedResponseModel)
async def delete_dialog(
    dialog_id: str = Body(description="对话ID", embed=True),
    login_user: UserPayload = Depends(get_login_user)
):
    try:
        # 验证用户权限
        await DialogService.verify_user_permission(dialog_id, login_user.user_id)

        await DialogService.delete_dialog(dialog_id=dialog_id)
        return resp_200()
    except Exception as err:
        logger.error(err)
        return resp_500(message=str(err))
