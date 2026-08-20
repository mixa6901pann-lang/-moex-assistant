"""CodeAct — execute Python code written by LLM.

Inspired by hack-moex leader (Alexander-Panov/ai-trader).
Provides a safe(ish) sandbox for LLM-generated calculations.
"""

from __future__ import annotations

import ast
import contextlib
import io
import traceback
from typing import Any

# Persistent namespace so variables survive across calls in one session
_code_session: dict[str, Any] = {}


def execute_python(code: str) -> str:
    """Execute Python code and return captured output + last expression value.

    Safe-guards:
    - AST parsing to detect if last line is expression
    - Restricted builtins (no open, eval, exec, __import__)
    - Persistent namespace per session (variables survive across calls)
    """
    stdout, stderr = io.StringIO(), io.StringIO()

    # Allowed imports for TA calculations
    _ALLOWED_IMPORTS = {"math", "statistics", "random", "numpy", "scipy", "pandas"}

    def _safe_import(name, *args, **kwargs):
        if name in _ALLOWED_IMPORTS:
            return __import__(name, *args, **kwargs)
        raise ImportError(f"Module '{name}' is not allowed in CodeAct sandbox")

    # Build restricted globals
    safe_builtins = {
        "abs": abs, "all": all, "any": any,
        "bool": bool, "complex": complex, "dict": dict,
        "enumerate": enumerate, "filter": filter, "float": float,
        "format": format, "frozenset": frozenset, "hasattr": hasattr,
        "int": int, "isinstance": isinstance, "issubclass": issubclass,
        "iter": iter, "len": len, "list": list, "map": map,
        "max": max, "min": min, "next": next, "pow": pow,
        "range": range, "repr": repr, "reversed": reversed,
        "round": round, "set": set, "slice": slice, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "type": type,
        "zip": zip, "print": print, "chr": chr, "ord": ord,
        "divmod": divmod, "hex": hex, "bin": bin, "oct": oct,
        "__import__": _safe_import,
    }

    # Pre-inject allowed modules so LLM can use them directly
    try:
        import math
        import statistics
        import random
        safe_builtins["math"] = math
        safe_builtins["statistics"] = statistics
        safe_builtins["random"] = random
    except Exception:
        pass

    try:
        import numpy
        safe_builtins["numpy"] = numpy
        safe_builtins["np"] = numpy
    except Exception:
        pass

    try:
        import scipy
        safe_builtins["scipy"] = scipy
    except Exception:
        pass

    try:
        import pandas
        safe_builtins["pandas"] = pandas
        safe_builtins["pd"] = pandas
    except Exception:
        pass

    exec_ns = {"__builtins__": safe_builtins, **_code_session}

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            tree = ast.parse(code)
            last_is_expr = tree.body and isinstance(tree.body[-1], ast.Expr)

            if last_is_expr:
                lines = code.rstrip().split("\n")
                code_exec = "\n".join(lines[:-1] + [f"__result__ = {lines[-1]}"])
            else:
                code_exec = code

            exec(code_exec, exec_ns)

            # Persist non-dunder vars back to session
            for k, v in exec_ns.items():
                if not k.startswith("_"):
                    _code_session[k] = v

            return_value = exec_ns.get("__result__") if last_is_expr else None
            output = (stdout.getvalue() + stderr.getvalue()).strip()

            if return_value is not None:
                result = str(return_value)
                if output:
                    result = f"{output}\n{result}"
                return result
            return output if output else "[OK: no output]"

    except SyntaxError as e:
        return f"[SyntaxError: {e.msg} (line {e.lineno})]"
    except Exception as e:
        return f"[{type(e).__name__}: {e}]\n{traceback.format_exc()[-500:]}"


def reset_session() -> None:
    """Clear the persistent code session."""
    global _code_session
    _code_session.clear()
