from typing import List

from agentchat.database.dao.workspace_session import WorkSpaceSession, WorkSpaceSessionDao
from agentchat.database.models.workspace_session import WorkSpaceSessionCreate
from agentchat.core.models.manager import ModelManager
from agentchat.core.callbacks import usage_metadata_callback
from agentchat.prompts.completion import GenerateTitlePrompt


class WorkSpaceSessionService:

    @classmethod
    async def create_workspace_session(cls, session_create: WorkSpaceSessionCreate):
        workspace_session = WorkSpaceSession(**session_create.model_dump())
        return await WorkSpaceSessionDao.create_workspace_session(workspace_session)

    @classmethod
    async def get_workspace_sessions(cls, user_id):
        results = await WorkSpaceSessionDao.get_workspace_sessions(user_id)
        results.sort(key=lambda x: x.update_time, reverse=True)
        return [result.to_dict() for result in results]

    @classmethod
    async def delete_workspace_session(cls, session_ids, user_id):
        await WorkSpaceSessionDao.delete_workspace_session(session_ids, user_id)

    @classmethod
    async def update_workspace_session_contexts(cls, session_id, user_id, session_context):
        return await WorkSpaceSessionDao.update_workspace_session_contexts(session_id, user_id, session_context)

    @classmethod
    async def clear_workspace_session_contexts(cls, session_id, user_id=None):
        return await WorkSpaceSessionDao.clear_workspace_session_contexts(session_id, user_id)

    @classmethod
    async def get_workspace_session_from_id(cls, session_id, user_id):
        result = await WorkSpaceSessionDao.get_workspace_session_from_id(session_id, user_id)
        if result is None:
            return None
        return result.to_dict()

    @classmethod
    async def generate_session_title(cls, user_query):
        prompt = GenerateTitlePrompt.format(query=user_query)
        response = await ModelManager.get_conversation_model().ainvoke(
            prompt, config={"callbacks": [usage_metadata_callback]}
        )
        title = str(response.content).strip()
        return title or user_query.strip()[:30]
