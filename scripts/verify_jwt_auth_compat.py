#!/usr/bin/env python3
"""校验 JWT 鉴权模块与当前 Pydantic 版本是否兼容。

背景（为什么需要这个脚本）
--------------------------
`fastapi-jwt-auth` 0.5.0 的 `LoadConfig` 是 Pydantic v1 写法，在 Pydantic 2
环境下会报错。本仓库已把该库**内联**为 `src/backend/agentchat/auth/`，
因此不再依赖 site-packages 里的 `fastapi_jwt_auth`，也**不需要**
"改 site-packages" 式的热补丁。

历史遗留：仓库里曾有 `scripts/fix_fastapi_jwt_auth.py`，它按
`pydantic.__file__` 定位并【覆盖】`site-packages/fastapi_jwt_auth/config.py`。
该脚本在本仓库已失效且有害：
  * 本仓库未安装 `fastapi_jwt_auth`，脚本只会打印"配置文件不存在"并返回 1；
    若照搬进 Dockerfile（RUN 该脚本），构建会直接失败；
  * 即便在别的环境生效，它改写的是镜像里的 site-packages，重建即丢失，
    且覆盖第三方包文件属于不可审计的隐式变更。

因此改为本脚本：只做【校验】，不改任何文件。可放进 CI 或部署前自检。

用法
----
    python scripts/verify_jwt_auth_compat.py            # 校验，失败返回 1
    python scripts/verify_jwt_auth_compat.py -v         # 额外打印环境信息

退出码：0 兼容；1 不兼容（会把具体断言打出来）
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "src" / "backend"


def _prepare_import_path() -> None:
    if not BACKEND_DIR.is_dir():
        print(f"[FAIL] 找不到后端目录：{BACKEND_DIR}")
        raise SystemExit(1)
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def _check(name: str, fn) -> bool:
    """执行一条断言；异常视为失败但不中断后续检查。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 需要把任意失败都汇报出来
        print(f"[FAIL] {name}\n       {type(exc).__name__}: {exc}")
        return False
    print(f"[ OK ] {name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 JWT 鉴权模块与 Pydantic 的兼容性")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印环境信息")
    args = parser.parse_args()

    _prepare_import_path()

    import pydantic

    # 捕获导入期告警：若有人把 @field_validator 改回 v1 的 @validator，
    # 这里会立刻出现 PydanticDeprecatedSince20，从而被判定为不通过。
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from agentchat.auth import AuthJWT  # noqa: F401  仅为触发导入
        from agentchat.auth.config import LoadConfig

    results: list[bool] = []

    def _deprecations() -> None:
        hits = [
            str(w.message) for w in caught
            if issubclass(w.category, DeprecationWarning)
        ]
        if hits:
            raise AssertionError(
                f"导入期出现 {len(hits)} 条 Pydantic 弃用告警（应迁移到 v2 写法）："
                f"{hits[0][:120]}"
            )

    def _vendored() -> None:
        module_file = Path(sys.modules["agentchat.auth"].__file__).resolve()
        if BACKEND_DIR.resolve() not in module_file.parents:
            raise AssertionError(f"鉴权模块未解析到仓库内联副本：{module_file}")

    def _basic() -> None:
        cfg = LoadConfig(authjwt_secret_key="k" * 32)
        if cfg.authjwt_secret_key != "k" * 32:
            raise AssertionError("secret_key 未按预期写入")
        if cfg.authjwt_token_location != {"headers"}:
            raise AssertionError(f"token_location 默认值异常：{cfg.authjwt_token_location!r}")

    def _csrf_normalized() -> None:
        cfg = LoadConfig(authjwt_csrf_methods=["post", "get"])
        if cfg.authjwt_csrf_methods != ["POST", "GET"]:
            raise AssertionError(f"csrf_methods 未转为大写：{cfg.authjwt_csrf_methods!r}")

    def _reject_bool_expires() -> None:
        from pydantic import ValidationError

        try:
            LoadConfig(authjwt_access_token_expires=True)
        except ValidationError:
            return
        raise AssertionError("authjwt_access_token_expires=True 应被拒绝")

    def _reject_empty_secret() -> None:
        from pydantic import ValidationError

        try:
            LoadConfig(authjwt_secret_key="")
        except ValidationError:
            return
        raise AssertionError("空字符串 secret_key 应被拒绝（str_min_length=1 未生效）")

    def _reject_bad_location() -> None:
        from pydantic import ValidationError

        try:
            LoadConfig(authjwt_token_location=["body"])
        except ValidationError:
            return
        raise AssertionError("authjwt_token_location=['body'] 应被拒绝")

    print("=== JWT 鉴权模块兼容性校验 ===")
    results.append(_check("鉴权模块解析到仓库内联副本（非 site-packages）", _vendored))
    results.append(_check("导入期无 Pydantic 弃用告警", _deprecations))
    results.append(_check("LoadConfig 基本构造与默认值", _basic))
    results.append(_check("csrf_methods 逐项转大写", _csrf_normalized))
    results.append(_check("拒绝 authjwt_access_token_expires=True", _reject_bool_expires))
    results.append(_check("拒绝空字符串 secret_key", _reject_empty_secret))
    results.append(_check("拒绝非法 token_location", _reject_bad_location))

    if args.verbose:
        print()
        print(f"python      : {sys.version.split()[0]}")
        print(f"pydantic    : {pydantic.VERSION}")
        print(f"auth 模块    : {Path(sys.modules['agentchat.auth'].__file__).resolve()}")

    print()
    if all(results):
        print(f"结论：兼容（{len(results)}/{len(results)} 项通过），无需任何 site-packages 补丁")
        return 0
    failed = results.count(False)
    print(f"结论：不兼容（{failed}/{len(results)} 项失败）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
