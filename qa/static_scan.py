"""静态缺陷扫描：AST 级别，覆盖语法、异常吞噬、可变默认参数、危险调用、SQL 拼接。

用法：python qa/static_scan.py <项目根目录>
"""
import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
BACKEND = TARGET / "src/backend/agentchat"

SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "test"}

DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen",
    "subprocess.call", "subprocess.run", "subprocess.Popen",
    "pickle.loads", "yaml.load",
}


def iter_py(root: Path):
    for p in sorted(root.rglob("*.py")):
        parts = set(p.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        yield p


class Visitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.findings = []

    def add(self, node, kind, msg):
        self.findings.append({
            "file": str(self.path.relative_to(TARGET)).replace("\\", "/"),
            "line": getattr(node, "lineno", 0),
            "kind": kind,
            "msg": msg,
        })

    # --- 裸 except / 吞异常 ---
    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.add(node, "bare-except", "except: 捕获所有异常（含 KeyboardInterrupt/SystemExit）")
        else:
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                self.add(node, "silent-except", "except ...: pass 静默吞掉异常，故障不可观测")
        self.generic_visit(node)

    # --- 可变默认参数 ---
    def visit_FunctionDef(self, node):
        for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.add(node, "mutable-default", f"函数 {node.name} 使用可变对象作默认参数（跨调用共享）")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    # --- 危险调用 ---
    def visit_Call(self, node):
        f = node.func
        name = ""
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name):
                name = f"{base.id}.{f.attr}"
            else:
                name = f.attr
        if name in DANGEROUS_CALLS:
            if name in ("subprocess.call", "subprocess.run", "subprocess.Popen"):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        self.add(node, "shell-true", f"{name}(shell=True) 命令注入面")
                        break
                else:
                    self.add(node, "subprocess", f"{name} 外部进程调用（需确认入参可控性）")
            else:
                self.add(node, "dangerous-call", f"{name}() 动态执行/反序列化")
        self.generic_visit(node)

    # --- SQL 字符串拼接 ---
    def visit_BinOp(self, node):
        if isinstance(node.op, (ast.Add, ast.Mod)):
            s = ast.dump(node)
            if re.search(r"(SELECT|INSERT|UPDATE|DELETE|WHERE)", s, re.I) and (
                "FormattedValue" in s or "JoinedStr" in s or isinstance(node.op, ast.Mod)
            ):
                self.add(node, "sql-concat", "疑似用字符串拼接构造 SQL")
        self.generic_visit(node)


def check_fstrings_with_sql(path: Path, src: str, out: list):
    for i, line in enumerate(src.splitlines(), 1):
        if re.search(r"f['\"].*\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b", line, re.I):
            out.append({"file": str(path.relative_to(TARGET)).replace("\\", "/"), "line": i,
                        "kind": "sql-fstring", "msg": "f-string 拼 SQL"})


def check_hardcoded_secret(path: Path, src: str, out: list):
    pat = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKID[A-Za-z0-9]{10,}|LTAI[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,})")
    for i, line in enumerate(src.splitlines(), 1):
        if pat.search(line):
            out.append({"file": str(path.relative_to(TARGET)).replace("\\", "/"), "line": i,
                        "kind": "hardcoded-secret", "msg": "疑似硬编码密钥"})


def check_requests_verify(path: Path, src: str, out: list):
    for i, line in enumerate(src.splitlines(), 1):
        if re.search(r"verify\s*=\s*False", line):
            out.append({"file": str(path.relative_to(TARGET)).replace("\\", "/"), "line": i,
                        "kind": "tls-verify-off", "msg": "关闭 TLS 校验"})


def main():
    findings = []
    syntax_errors = []
    files = list(iter_py(BACKEND))
    for p in files:
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src, filename=str(p))
        except SyntaxError as e:
            syntax_errors.append({"file": str(p), "line": e.lineno, "msg": str(e)})
            continue
        v = Visitor(p)
        v.visit(tree)
        findings.extend(v.findings)
        check_fstrings_with_sql(p, src, findings)
        check_hardcoded_secret(p, src, findings)
        check_requests_verify(p, src, findings)

    by_kind = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)

    print(f"扫描文件数: {len(files)}")
    print(f"语法错误: {len(syntax_errors)}")
    for e in syntax_errors:
        print("  ", e)
    print(f"缺陷命中: {len(findings)}")
    for k in sorted(by_kind, key=lambda x: -len(by_kind[x])):
        items = by_kind[k]
        print(f"\n[{k}] {len(items)} 处")
        for it in items[:8]:
            print(f"   {it['file']}:{it['line']}  {it['msg']}")
        if len(items) > 8:
            print(f"   ... 其余 {len(items)-8} 处")

    Path(__file__).resolve().parent.joinpath("static_findings.json").write_text(
        json.dumps({"syntax_errors": syntax_errors, "findings": findings}, ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
