"""守卫：**HTTP 接口面 ↔ 契约文档**必须一一对应（2026-10-01 新增）。

## 为什么需要它

`docs/手册/05-安卓通信协议.md` 是这个项目的**权威 HTTP 契约**（"§3 接口一览" + "§4 接口详情"）。
但 2026-10-01 实测发现：**它落后于实现** —— 一夜之间新增的
`/api/v1/messages`、`/api/v1/spo2*`、`/api/v1/screen`、`/api/v1/config`、`/api/v1/cloud/callback`
**在契约文档里根本查不到**（部分只散落在路线图与验收记录里）。

这在本项目里不只是"文档懒"：**"接口能改什么"本身就是安全边界**
（`POST /api/v1/config` 能改阈值与器件开关）—— **文档里看不见的接口 = 没人管的接口**。

项目对"报警码"早就有三方一致性守卫（`models.py` ↔ 报警规则表 ↔ 安卓文案表），
**HTTP 路由却一直没有**。这里按同一条纪律补上，且做成**双向**：

1. 代码里注册的每个接口 ⇒ **文档里必须查得到**（防"加了没写"）；
2. 文档 §3 表里列的每个接口 ⇒ **代码里必须真有**（防"写了没有/早就删了"）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # 仓库根（rpi/tests/integration → 3 层）
WEB_PY = ROOT / "rpi" / "health_monitor" / "net" / "web.py"
DOC = ROOT / "docs" / "手册" / "05-安卓通信协议.md"

#: 只检查 API 前缀（页面路由单独处理，见下面 `_page_routes`）。
_API_RE = re.compile(r'\("(GET|POST)",\s*"(/api/v1[^"]*)"\)')
#: 页面路由在 `handle()` 里是普通比较，不在路由表里。
_PAGE_EQ_RE = re.compile(r'parsed\.path == "(/[^"]*)"')
_PAGE_IN_RE = re.compile(r'parsed\.path in \(([^)]*)\)')
_QUOTED_RE = re.compile(r'"([^"]+)"')
#: 文档 §3 表的行：`| GET | \`/api/v1/health\` | ...`
_DOC_ROW_RE = re.compile(r"^\|\s*(GET|POST)\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)
#: 路由表的条目：`("GET", "/api/v1/health"): self._health,` —— 顺带把处理函数名抓出来
_ROUTE_ENTRY_RE = re.compile(r'\("(GET|POST)",\s*"(/api/v1[^"]*)"\):\s*self\.(\w+)')


def _code_api_routes() -> set[tuple[str, str]]:
    text = WEB_PY.read_text(encoding="utf-8")
    return {(m.group(1), m.group(2)) for m in _API_RE.finditer(text)}


def _code_page_routes() -> set[str]:
    """页面路由的**规范路径**（每个 `in (...)` 元组取第一个；那正是导航里用的那个）。"""
    text = WEB_PY.read_text(encoding="utf-8")
    pages = {m.group(1) for m in _PAGE_EQ_RE.finditer(text)}
    for m in _PAGE_IN_RE.finditer(text):
        quoted = _QUOTED_RE.findall(m.group(1))
        if quoted:
            pages.add(quoted[0])
    return pages


def _doc_api_routes() -> set[tuple[str, str]]:
    """文档 §3 表里**以 /api/ 开头**的那些行。

    ⚠️ 必须把页面路由（`/`、`/control`、`/panel`）排除在外：
    它们也在同一张表里（方便用户查），但**不在 API 路由字典**里（走的是普通比较）。
    第一版忘了排除，于是守卫报出两条**假红**（"文档有、代码没有"）——
    而恰好掩饰了同期那条**真红**（`/api/v1/cloud/callback`）。
    """
    return {(m.group(1), m.group(2)) for m in _DOC_ROW_RE.finditer(DOC.read_text(encoding="utf-8"))
            if m.group(2).startswith("/api/")}


def _doc_routes() -> set[tuple[str, str]]:
    """文档 §3 表里的**全部**行（含页面路由）—— 只用于"页面路由也要登记"那条断言。"""
    text = DOC.read_text(encoding="utf-8")
    return {(m.group(1), m.group(2)) for m in _DOC_ROW_RE.finditer(text)}


def _code_body_readers() -> set[tuple[str, str]]:
    """**真的会读请求体**的那些接口。

    ⚠️ 用 `ast` 而不是正则（`memory/30` §7 的教训："要按语法理解源码，就用 ast/tokenize"）：
    先找出所有调用了 `_current_body()` 的处理函数名，再回到路由表把函数名映射成 `(方法, 路径)`。
    """
    import ast

    source = WEB_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    readers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name == "_current_body":
                    readers.add(node.name)
    routes: dict[str, tuple[str, str]] = {}
    for match in _ROUTE_ENTRY_RE.finditer(source):
        routes[match.group(3)] = (match.group(1), match.group(2))
    return {routes[name] for name in readers if name in routes}


def _doc_body_readers() -> set[tuple[str, str]]:
    """文档 §2 "请求体"那一行里列出的接口（**这一行是机器可检的契约**）。

    ⚠️ 要把路径两侧的反引号去掉：第一版的 `(?P<path>`?/api/…)` 会把**结尾那个反引号
    一起吞进路径**，于是文档明明写对了、比对仍然是红的（假红）。
    """
    text = DOC.read_text(encoding="utf-8")
    for line in text.split("\n"):
        if line.startswith("| 请求体 |"):
            found = set()
            for method, path in re.findall(r"(GET|POST)\s+`?(/api/v1/[^`\s|]*)`?", line):
                found.add((method, path.rstrip("`")))
            return found
    return set()


class TestExtractionWorks(unittest.TestCase):
    """先证明"抽取器真的抽到了东西" —— 否则下面"没缺"只是因为它什么都没读到。"""

    def test_抽得到接口(self) -> None:
        api = _code_api_routes()
        self.assertGreaterEqual(len(api), 10, f"只抽到 {sorted(api)}，抽取器多半失效了")
        self.assertIn(("GET", "/api/v1/health"), api)

    def test_抽得到页面路由(self) -> None:
        pages = _code_page_routes()
        self.assertIn("/", pages)
        self.assertIn("/panel", pages)
        self.assertIn("/control", pages)

    def test_抽得到文档行(self) -> None:
        doc = _doc_routes()
        self.assertGreaterEqual(len(doc), 10, f"只抽到 {sorted(doc)}")
        self.assertIn(("GET", "/api/v1/health"), doc)


class TestBodyReaderListMatchesDocs(unittest.TestCase):
    """★ "哪些接口读请求体"这件事，**文档说的与代码做必须一致**（2026-10-01 加）。

    为什么值得单独一条：契约文档 §2 原来那句"本协议**不使用请求体**，参数一律走 query string
    （服务器不读 body）"在 2026-10-01 之后**已经不成立**（`POST /api/v1/config` 与
    `POST /api/v1/cloud/callback` 都读 body），而**没有任何东西会提醒你**。
    这条假话当场把代理自己坑了一次：我按文档把 `text` 放进播报接口的 **body**，
    得到的是 `{"error": "缺少 text 参数"}` —— 看起来像接口坏了，其实是**文档错了**。

    判据（可执行）：用 `ast` 找出所有**真的调用了 `_current_body()`** 的处理函数
    （不是靠人记得），映射回路由，与文档 §2 那一行列出的接口**双向比对**。
    """

    def test_抽取器真的抽到了东西(self) -> None:
        self.assertTrue(_code_body_readers(), "没找到任何读请求体的处理函数，抽取器多半失效了")
        self.assertTrue(_doc_body_readers(), "没从文档 §2 的'请求体'那行解析出接口")

    def test_读请求体的接口都在文档里列了(self) -> None:
        missing = sorted(_code_body_readers() - _doc_body_readers())
        self.assertEqual(
            missing, [],
            "这些接口**真的读了请求体**，但契约文档 §2 的'请求体'那行没列它们 ⇒ "
            f"调用方会照文档把参数放进 query string，然后收到'缺少参数'：{missing}",
        )

    def test_文档列的读body接口都真的读(self) -> None:
        extra = sorted(_doc_body_readers() - _code_body_readers())
        self.assertEqual(
            extra, [],
            f"文档说这些接口读请求体，但代码里没有 ⇒ 文档过期：{extra}",
        )

    def test_不读body的接口确实不是靠body传参(self) -> None:
        """反向钉子：把"不读 body"这件事也钉一下，免得文档那张表变成万能背锅侠。"""
        readers = _code_body_readers()
        self.assertNotIn(("POST", "/api/v1/speak"), readers,
                         "播报接口的 text 是 query 参数；若改成读 body，文档 §4.8 要同步改")
        self.assertNotIn(("POST", "/api/v1/sos"), readers)
        self.assertNotIn(("POST", "/api/v1/silence"), readers)


class TestApiSurfaceMatchesDocs(unittest.TestCase):
    def test_代码里注册的接口文档里都要有(self) -> None:
        missing = sorted(_code_api_routes() - _doc_api_routes())
        self.assertEqual(
            missing, [],
            "这些接口在代码里有、契约文档 §3 表里没有（**看不见的接口 = 没人管的接口**）："
            f"{missing}。请在 docs/手册/05-安卓通信协议.md 的 §3 表里补上、必要时补 §4 详情。",
        )

    def test_文档里列的接口代码里都要有(self) -> None:
        """★ 2026-10-01 这条抓到真问题：`/api/v1/cloud/callback` 文档里写着、
        代码里也有 handler，**却从来没注册进路由表** ⇒ 实测一直 404（死代码）。"""
        extra = sorted(_doc_api_routes() - _code_api_routes())
        self.assertEqual(
            extra, [],
            f"契约文档里列了这些接口，但代码里没有（文档过期、或者**从来没注册过**）：{extra}",
        )

    def test_页面路由也要出现在一览表里(self) -> None:
        """三块屏（数据/功能/配置）是用户直接打开的入口，不该只存在于代码里。"""
        text = DOC.read_text(encoding="utf-8")
        missing = sorted(p for p in _code_page_routes() if f"`{p}`" not in text)
        self.assertEqual(missing, [], f"这些页面在代码里有、文档里查不到：{missing}")

    def test_云端回调也在文档里(self) -> None:
        """`/api/v1/cloud/callback` 是服务器对服务器的口子，容易被漏（今晚就漏了）。"""
        self.assertIn(("POST", "/api/v1/cloud/callback"), _doc_routes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
