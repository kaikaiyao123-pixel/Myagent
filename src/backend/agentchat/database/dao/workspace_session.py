from typing import List

from sqlmodel import select, and_, delete
from agentchat.database.session import async_session_getter
from agentchat.database.models.workspace_session import WorkSpaceSession


class WorkSpaceSessionDao:

    @classmethod
    async def get_workspace_sessions(cls, user_id):
        async with async_session_getter() as session:
            statement = select(WorkSpaceSession).where(WorkSpaceSession.user_id == user_id)
            result = await session.exec(statement)
            return result.all()

    @classmethod
    async def create_workspace_session(cls, workspace_session: WorkSpaceSession):
        async with async_session_getter() as session:
            session.add(workspace_session)
            await session.commit()
            await session.refresh(workspace_session)
        return workspace_session

    @classmethod
    async def delete_workspace_session(cls, session_ids: List[str], user_id):
        async with async_session_getter() as session:
            statement = delete(WorkSpaceSession).where(and_(WorkSpaceSession.session_id.in_(session_ids),
                                                            WorkSpaceSession.user_id == user_id))
            await session.exec(statement)
            await session.commit()

    @classmethod
    async def update_workspace_session_contexts(cls, session_id, user_id, session_context):
        async with async_session_getter() as session:
            statement = select(WorkSpaceSession).where(and_(
                WorkSpaceSession.session_id == session_id,
                WorkSpaceSession.user_id == user_id,
            ))
            result = await session.exec(statement)
            workspace_session = result.one_or_none()
            if workspace_session is None:
                return None
            new_contexts = workspace_session.contexts.copy()
            new_contexts.append(session_context)
            workspace_session.contexts = new_contexts  # 重新赋值

            await session.commit()
            await session.refresh(workspace_session)

        return workspace_session

    @classmethod
    async def get_workspace_session_from_id(cls, session_id, user_id=None):
        async with async_session_getter() as session:
            statement = select(WorkSpaceSession).where(WorkSpaceSession.session_id == session_id)
            if user_id is not None:
                statement = statement.where(WorkSpaceSession.user_id == user_id)
            result = await session.exec(statement)
            return result.one_or_none()

    @classmethod
    async def clear_workspace_session_contexts(cls, session_id, user_id=None):
        async with async_session_getter() as session:
            statement = select(WorkSpaceSession).where(WorkSpaceSession.session_id == session_id)
            if user_id is not None:
                statement = statement.where(WorkSpaceSession.user_id == user_id)
            result = await session.exec(statement)
            workspace_session = result.one_or_none()
            if workspace_session is None:
                return None
            new_contexts = []
            workspace_session.contexts = new_contexts  # 重新赋值

            await session.commit()
            await session.refresh(workspace_session)

        return workspace_session
