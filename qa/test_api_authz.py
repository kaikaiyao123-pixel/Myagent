"""接口级鉴权/越权测试（无需 MySQL / Redis / LLM）。

被测对象是真实的 FastAPI 路由 + 真实的 WhitelistMiddleware + 真实的 get_login_user 依赖；
只对「数据访问层（DAO）」打桩，从而在不搭基础设施的前提下验证**授权判定逻辑**本身。

运行：<repo>/src/backend/.venv/Scripts/python.exe qa/test_api_authz.py -v
"""
import json
import os
import sys
import unittest
from datetime import timedelta

REPO = os.environ.get("AGENTCHAT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src", "backend"))

# 固定签名密钥，保证 token 可复现（生产由 AUTHJWT_SECRET_KEY 注入）
os.environ.setdefault("AUTHJWT_SECRET_KEY", "qa-fixed-secret-for-testing-only")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agentchat.api.JWT import Settings as AuthJwtSettings  # noqa: E402
from agentchat.auth import AuthJWT  # noqa: E402
from agentchat.auth.exceptions import AuthJWTException  # noqa: E402
from agentchat.middleware.white_list_middleware import WhitelistChecker, WhitelistMiddleware  # noqa: E402
from agentchat.settings import app_settings  # noqa: E402

# 生产 compose 使用的白名单值（docker/docker_config.yaml）
PROD_WHITELIST = ["/api/v1/mcp/*"]

# 加载仓库自带 config.yaml（只读，不建立任何连接），否则 app_settings.storage 为 None，
# 导入 services.storage 会直接 AttributeError。
import asyncio  # noqa: E402

from agentchat.settings import init_app_settings  # noqa: E402

asyncio.run(init_app_settings(os.path.join(REPO, "src", "backend", "agentchat", "config.yaml")))

AuthJWT.load_config(lambda: AuthJwtSettings())


def build_app():
    from agentchat.api.v1 import dialog, history, mcp_server, mcp_user_config, message, upload, user

    app = FastAPI()
    app.add_middleware(WhitelistMiddleware)

    @app.exception_handler(AuthJWTException)
    def _handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    for r in (dialog, history, mcp_server, mcp_user_config, message, upload, user):
        app.include_router(r.router, prefix="/api/v1")
    return app


def token_for(user_id, user_name="u", role=None, expires=timedelta(minutes=30)):
    payload = {"user_id": user_id, "user_name": user_name, "role": role}
    return AuthJWT().create_access_token(subject=json.dumps(payload), expires_time=expires)


class Base(unittest.TestCase):
    def setUp(self):
        app_settings.whitelist_paths = list(PROD_WHITELIST)
        # 普通用户角色查询打桩，避免真实 MySQL
        import agentchat.api.services.user as user_svc
        self._orig_roles = user_svc.UserRoleDao.get_user_roles
        user_svc.UserRoleDao.get_user_roles = classmethod(lambda cls, uid: [])
        self.client = TestClient(build_app())

    def tearDown(self):
        import agentchat.api.services.user as user_svc
        user_svc.UserRoleDao.get_user_roles = self._orig_roles


class TestWhitelistMatching(unittest.TestCase):
    """纯函数级：白名单前缀匹配语义"""

    def test_exact_and_prefix(self):
        c = WhitelistChecker(PROD_WHITELIST)
        self.assertTrue(c.is_whitelisted("/api/v1/mcp/register"))
        self.assertTrue(c.is_whitelisted("/api/v1/mcp"))

    def test_prefix_bleeds_into_sibling_routes(self):
        """前缀 /api/v1/mcp 会命中同前缀的兄弟路由（mcp_server / mcp_user_config）"""
        c = WhitelistChecker(PROD_WHITELIST)
        self.assertTrue(c.is_whitelisted("/api/v1/mcp_server"),
                        "mcp_server 被 /api/v1/mcp/* 前缀误命中")
        self.assertTrue(c.is_whitelisted("/api/v1/mcp_user_config"),
                        "mcp_user_config 被 /api/v1/mcp/* 前缀误命中")

    def test_unrelated_path_not_whitelisted(self):
        c = WhitelistChecker(PROD_WHITELIST)
        self.assertFalse(c.is_whitelisted("/api/v1/dialog/list"))


class TestAuthentication(Base):
    def test_baseline_protected_route_rejects_anonymous(self):
        r = self.client.get("/api/v1/dialog/list")
        self.assertEqual(r.status_code, 401, "非白名单路由应要求鉴权（基线对照）")

    def test_whitelisted_sibling_route_accepts_anonymous(self):
        """F-01：GET /api/v1/mcp_server 未携带任何凭据仍进入业务逻辑"""
        from agentchat.api.services.mcp_server import MCPService
        seen = {}

        async def fake_get_all_servers(user_id):
            seen["user_id"] = user_id
            return [{"server_id": "srv-1", "server_name": "leaked"}]

        orig = MCPService.get_all_servers
        MCPService.get_all_servers = fake_get_all_servers
        try:
            r = self.client.get("/api/v1/mcp_server")
        finally:
            MCPService.get_all_servers = orig

        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen.get("user_id"), "1",
                         "匿名请求被当成 Admin(user_id=1) 处理，且未经过 JWT 校验")

    def test_whitelisted_logo_endpoint_returns_200_anonymously(self):
        r = self.client.get("/api/v1/mcp_server/logo")
        self.assertEqual(r.status_code, 200, "匿名可读，说明鉴权被白名单短路")
        self.assertIn("data", r.json())

    def test_message_like_has_no_auth_dependency(self):
        """F-02：/message/like 完全未挂鉴权依赖，匿名即可写入"""
        from agentchat.api.services.message import MessageLikeService
        calls = []
        orig = MessageLikeService.create_message_like
        MessageLikeService.create_message_like = classmethod(
            lambda cls, user_input, agent_output: calls.append((user_input, agent_output)))
        try:
            r = self.client.post("/api/v1/message/like",
                                 json={"user_input": "x" * 5000, "agent_output": "y"})
        finally:
            MessageLikeService.create_message_like = orig
        self.assertEqual(r.status_code, 200)
        self.assertTrue(calls, "匿名请求成功触发了数据库写入")

    def test_tampered_signature_rejected(self):
        import jwt
        good = token_for("user-a")
        forged = jwt.encode({"sub": json.dumps({"user_id": "1", "user_name": "x", "role": "admin"}),
                             "type": "access", "exp": 9999999999},
                            "attacker-key", algorithm="HS256")
        self.assertEqual(self.client.get("/api/v1/dialog/list",
                                         headers={"Authorization": f"Bearer {forged}"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/dialog/list",
                                         headers={"Authorization": f"Bearer {good}"}).status_code != 401, True)

    def test_expired_token_rejected(self):
        t = token_for("user-a", expires=timedelta(seconds=-30))
        r = self.client.get("/api/v1/dialog/list", headers={"Authorization": f"Bearer {t}"})
        self.assertEqual(r.status_code, 401)

    def test_alg_none_rejected(self):
        import jwt
        t = jwt.encode({"sub": json.dumps({"user_id": "1", "user_name": "x", "role": "admin"}),
                        "type": "access", "exp": 9999999999}, key="", algorithm="none")
        r = self.client.get("/api/v1/dialog/list", headers={"Authorization": f"Bearer {t}"})
        self.assertEqual(r.status_code, 401, "alg=none 必须被拒绝")

    def test_refresh_token_cannot_access(self):
        t = AuthJWT().create_refresh_token(subject="user-a")
        r = self.client.get("/api/v1/dialog/list", headers={"Authorization": f"Bearer {t}"})
        self.assertEqual(r.status_code, 401, "refresh token 不可当 access token 使用")


class TestHorizontalPrivilegeEscalation(Base):
    """F-03/F-04：已登录用户 A 读取/删除用户 B 的资源"""

    def test_history_readable_across_users(self):
        from agentchat.database.dao.history import HistoryDao

        class Row:
            def to_dict(self):
                return {"role": "user", "content": "B 的私密提问", "dialog_id": "dialog-of-B"}

        async def fake_get(dialog_id):
            return [Row()]

        orig = HistoryDao.get_dialog_history
        HistoryDao.get_dialog_history = classmethod(lambda cls, d: fake_get(d))
        try:
            r = self.client.get("/api/v1/history?dialog_id=dialog-of-B",
                                headers={"Authorization": f"Bearer {token_for('user-A')}"})
        finally:
            HistoryDao.get_dialog_history = orig

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["data"][0]["content"], "B 的私密提问",
                         "用户 A 读到了用户 B 的对话历史（接口从未使用 login_user.user_id 做归属校验）")

    def test_mcp_user_config_readable_across_users(self):
        from agentchat.database.dao.mcp_user_config import MCPUserConfigDao

        class Row:
            def to_dict(self):
                return {"id": "cfg-of-B", "user_id": "user-B",
                        "config": [{"key": "Authorization", "value": "Bearer sk-B-secret"}]}

        orig = MCPUserConfigDao.get_mcp_user_config_from_id

        async def fake_get(config_id):
            return Row()

        MCPUserConfigDao.get_mcp_user_config_from_id = classmethod(lambda cls, cid: fake_get(cid))
        try:
            r = self.client.get("/api/v1/mcp_user_config/cfg-of-B",
                                headers={"Authorization": f"Bearer {token_for('user-A')}"})
        finally:
            MCPUserConfigDao.get_mcp_user_config_from_id = orig

        self.assertEqual(r.status_code, 200)
        self.assertIn("sk-B-secret", json.dumps(r.json(), ensure_ascii=False),
                      "用户 A 读到了用户 B 的 MCP 凭据")

    def test_mcp_user_config_deletable_across_users(self):
        from agentchat.database.dao.mcp_user_config import MCPUserConfigDao
        deleted = []

        async def fake_delete(config_id):
            deleted.append(config_id)

        orig = MCPUserConfigDao.delete_mcp_user_config
        MCPUserConfigDao.delete_mcp_user_config = classmethod(lambda cls, cid: fake_delete(cid))
        try:
            r = self.client.request("DELETE", "/api/v1/mcp_user_config/delete",
                                    headers={"Authorization": f"Bearer {token_for('user-A')}"},
                                    json={"config_id": "cfg-of-B"})
        finally:
            MCPUserConfigDao.delete_mcp_user_config = orig

        self.assertEqual(r.status_code, 200)
        self.assertEqual(deleted, ["cfg-of-B"], "用户 A 删除了用户 B 的配置")


class TestUserEndpointsUnauthenticated(Base):
    """/user/* 读写接口完全未挂鉴权依赖"""

    def _fake_user(self):
        from agentchat.database.models.user import UserTable
        return UserTable(
            user_id="user-B",
            user_name="victim",
            user_email="victim@example.com",
            user_avatar="http://x/a.png",
            user_password="5f4dcc3b5aa765d61d8327deb882cf99deadbeef",  # 伪造哈希占位
            delete=False,
        )

    def test_user_info_readable_anonymously(self):
        """F-05：匿名读取任意用户资料，且响应中含 user_password 字段"""
        from agentchat.database.dao.user import UserDao

        orig = UserDao.get_user
        UserDao.get_user = classmethod(lambda cls, uid: self._fake_user())
        try:
            r = self.client.get("/api/v1/user/info?user_id=user-B")
        finally:
            UserDao.get_user = orig

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["data"]["user_name"], "victim")
        self.assertIn("user_password", body["data"],
                      "未鉴权响应中直接返回了密码哈希（UserTable.hide_fields 为空）")

    def test_user_update_writable_anonymously(self):
        """F-06：匿名可改任意用户资料（user_id 来自请求体）"""
        from agentchat.api.services.user import UserService
        calls = []
        orig = UserService.update_user_info
        UserService.update_user_info = classmethod(
            lambda cls, uid, avatar, desc: calls.append((uid, avatar, desc)))
        try:
            r = self.client.put("/api/v1/user/update",
                                json={"user_id": "user-B", "user_avatar": "http://evil/x.png",
                                      "user_description": "pwned"})
        finally:
            UserService.update_user_info = orig

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls and calls[0][0], "user-B", "匿名请求直接改写了他人资料")


class TestResponseSemantics(Base):
    """响应语义：业务错误是否可被网关/监控识别"""

    def test_business_error_returns_http_200(self):
        from agentchat.api.services.mcp_server import MCPService

        async def boom(user_id):
            raise RuntimeError("db down")

        orig = MCPService.get_all_servers
        MCPService.get_all_servers = boom
        try:
            r = self.client.get("/api/v1/mcp_server")
        finally:
            MCPService.get_all_servers = orig

        self.assertEqual(r.status_code, 200, "服务端异常仍返回 HTTP 200（resp_500 只改 body）")
        self.assertEqual(r.json()["status_code"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
