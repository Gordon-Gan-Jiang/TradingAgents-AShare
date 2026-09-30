"""provider 方法「是否真的实现了」的可判定事实。

## 为什么需要这个模块

`BaseMarketDataProvider` 的抽象方法在子类里常被写成**占位实现**——方法体只有一句
`raise NotImplementedError("... yet")`。这种写法能让 provider 通过 ABC 检查、成功
注册进 registry，却在被调用时必然失败。

事故案例（2026-09-24 排查）：`cn_baostock` 只实现了 `get_stock_data` 与
`get_indicators`，其余 7 个方法全是占位；而 `default_config.data_vendors` 却把它列进
了 `fundamental_data` 与 `news_data` 两条链。后果不是"偶尔报错"，而是**配置在说谎**：

1. 链上看起来是"多源覆盖"，实际只有一源能出数；
2. baostock 真正的故障原因（上游把服务从 `www.baostock.com:10030` 迁到
   `public-api.baostock.com:10030`）被 `NotImplementedError` 这个语义吞掉——
   "没实现"和"连不上"混成同一件事，运维完全无从下手。

所以"实现了没有"必须是**程序可判定**的事实。这里用 AST 判定方法体是否为纯占位，
而不是维护一份手写清单——手写清单正是最容易腐烂的东西，而且它腐烂时不会有任何
提示（这次事故就是配置与实现漂移的直接产物）。

## 判定口径

一个方法算**已实现**，当且仅当：它在本类（或 MRO 上某个非 `object` 的类）被定义，
且方法体在去掉 docstring 之后**不是**"全部语句都是 `raise NotImplementedError`"。

注意两点：

* **部分抛异常仍算已实现**。形如"取不到数据时 `raise NotImplementedError`"的
  分支写法在 `cn_akshare_provider` 里是常态（用于声明某个子维度未覆盖），
  判定必须按**整个方法体**看，不能按"是否出现该异常"看。
* **继承优先取最派生的一份**。子类用占位覆盖父类的真实现时，判定结果必须是
  "未实现"，否则又会造出一个说谎的 provider。
"""

from __future__ import annotations

from types import FunctionType
from typing import Any, Callable, Iterable

import ast
import inspect
import textwrap

#: 被视作"占位/不支持"的异常名。
#:
#: * `NotImplementedError` —— 历史写法，provider 用一句话方法体表达"没实现"。
#: * `SourceUnsupported` —— 新的显式语义（见 `providers/errors.py`）：该源**结构上
#:   不支持**这个能力。它与 NotImplementedError 是同一件事的两种写法，判定必须一致，
#:   否则"用新语义写的占位方法"会被算成已实现，能力矩阵立刻开始说谎。
#:
#: 其它异常（`SourceUnavailable`、`SourceEmpty`、TushareUnavailable、requests 的
#: ConnectionError）表达的是**运行时失败**，不是"不支持"。
_STUB_EXCEPTION_NAMES = frozenset({"NotImplementedError", "SourceUnsupported"})


def _is_not_implemented_raise(stmt: ast.stmt) -> bool:
    """该语句是否为 `raise NotImplementedError(...)` / `raise NotImplementedError`。"""
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    exc: ast.expr = stmt.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    if isinstance(exc, ast.Name):
        return exc.id in _STUB_EXCEPTION_NAMES
    if isinstance(exc, ast.Attribute):
        return exc.attr in _STUB_EXCEPTION_NAMES
    return False


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    """占位语句：`raise NotImplementedError`、`pass`、裸 `...`。"""
    if _is_not_implemented_raise(stmt):
        return True
    if isinstance(stmt, ast.Pass):
        return True
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return True
    return False


def _is_stub_function(fn: Callable[..., Any]) -> bool:
    """方法体去掉 docstring 后是否只有占位语句。

    取不到源码时返回 `False`（保守：宁可漏判为"已实现"，也不要把真实实现误判成占位，
    否则一致性测试会给出无法解释的失败）。
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):  # 动态生成/交互式定义，拿不到源码
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    node = None
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node = candidate
            break
    if node is None:
        return False

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # 去掉 docstring
    if not body:
        return True  # 只有 docstring / 空体，等同于未实现
    return all(_is_placeholder_stmt(stmt) for stmt in body)


def _method_functions(cls: type) -> dict[str, FunctionType]:
    """收集类上定义的公开可调用方法名 → 函数对象（MRO 上取最派生的一份）。

    跳过下划线开头（内部/魔法方法）与非函数属性（`name` 这类 property 不算接口方法）。
    staticmethod / classmethod 解包到底层函数后参与判定。
    """
    found: dict[str, FunctionType] = {}
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if name.startswith("_"):
                continue
            if name in found:
                continue  # 更派生的定义优先
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if isinstance(value, FunctionType):
                found[name] = value
    return found


def declared_methods(cls: type) -> frozenset[str]:
    """类上声明的全部公开接口方法名（含占位）。"""
    return frozenset(_method_functions(cls))


def stub_methods(cls: type) -> frozenset[str]:
    """类上**声明了但只是占位**的方法名。"""
    return frozenset(
        name for name, fn in _method_functions(cls).items() if _is_stub_function(fn)
    )


def implemented_methods(cls: type) -> frozenset[str]:
    """类上**真正实现**的方法名 = 声明 - 占位。"""
    return declared_methods(cls) - stub_methods(cls)


def provider_implemented_methods(provider: Any) -> frozenset[str]:
    """实例版：`implemented_methods(type(provider))`。"""
    return implemented_methods(type(provider))


def unsupported(methods: Iterable[str], cls: type) -> frozenset[str]:
    """给定方法名里，该类**没有真正实现**的那些（矩阵/链校验的常用问法）。"""
    impl = implemented_methods(cls)
    return frozenset(m for m in methods if m not in impl)
