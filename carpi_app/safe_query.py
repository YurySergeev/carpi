"""A small, safe expression language for the Query box.

Only these are allowed, so it is safe to expose on a public server:
  column names (rpm, coolant_c, boost_psi, ...), numbers, True/False
  comparisons      <  <=  >  >=  ==  !=   (chains like 2000 < rpm < 3000 work)
  logic            and  or  not   (also &  |  ~)
  arithmetic       +  -  *  /  %  **
  functions        abs(x)  x.abs()  x.between(a, b)  x.isna()  x.notna()
Anything else (attribute access, imports, strings, indexing, other calls) is rejected.
"""
import ast
import operator as op

import numpy as np
import pandas as pd

MAX_LEN = 300
MAX_NODES = 120

_CMP = {ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge, ast.Eq: op.eq, ast.NotEq: op.ne}
_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Mod: op.mod,
        ast.Pow: op.pow, ast.BitAnd: op.and_, ast.BitOr: op.or_}
_METHODS = {"between": 2, "abs": 0, "isna": 0, "notna": 0}


class QueryError(ValueError):
    pass


def evaluate(df, text):
    """Return a boolean Series for `text` evaluated against df's columns. Raises QueryError."""
    text = (text or "").strip()
    if len(text) > MAX_LEN:
        raise QueryError(f"query is longer than {MAX_LEN} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise QueryError("couldn't parse it (use and / or / not, ==, >=, .between(a, b))") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise QueryError("query is too long")
    out = _eval(tree.body, df)
    if not isinstance(out, pd.Series):
        raise QueryError("the query must compare a column, e.g. rpm > 3000")
    if out.dtype != bool:
        if out.dropna().isin([True, False]).all():
            out = out.fillna(False).astype(bool)
        else:
            raise QueryError("the query must be a true/false condition, e.g. rpm > 3000")
    return out


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise QueryError("only numbers are allowed as values")
    if abs(v) > 1e9:
        raise QueryError("number too large")
    return v


def _bool(x):
    return x.fillna(False).astype(bool) if isinstance(x, pd.Series) else bool(x)


def _eval(n, df):
    if isinstance(n, ast.Constant):
        if isinstance(n.value, bool):
            return n.value
        return _num(n.value)
    if isinstance(n, ast.Name):
        if n.id in df.columns:
            return df[n.id]
        if n.id in ("True", "False"):
            return n.id == "True"
        raise QueryError(f"unknown column '{n.id}'")
    if isinstance(n, ast.BoolOp):
        vals = [_bool(_eval(v, df)) for v in n.values]
        res = vals[0]
        for v in vals[1:]:
            res = (res & v) if isinstance(n.op, ast.And) else (res | v)
        return res
    if isinstance(n, ast.UnaryOp):
        v = _eval(n.operand, df)
        if isinstance(n.op, (ast.Not, ast.Invert)):
            return ~_bool(v) if isinstance(v, pd.Series) else (not v)
        if isinstance(n.op, ast.USub):
            return -v
        if isinstance(n.op, ast.UAdd):
            return v
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        a, b = _eval(n.left, df), _eval(n.right, df)
        if isinstance(n.op, ast.Pow) and not isinstance(b, pd.Series) and abs(b) > 10:
            raise QueryError("exponent too large")
        if isinstance(n.op, (ast.BitAnd, ast.BitOr)):
            a, b = _bool(a), _bool(b)
        with np.errstate(all="ignore"):
            return _BIN[type(n.op)](a, b)
    if isinstance(n, ast.Compare):
        left, res = _eval(n.left, df), None
        for cop, comp in zip(n.ops, n.comparators):
            if type(cop) not in _CMP:
                raise QueryError("only < <= > >= == != comparisons are allowed")
            right = _eval(comp, df)
            part = _CMP[type(cop)](left, right)
            res = part if res is None else (_bool(res) & _bool(part))
            left = right
        return res
    if isinstance(n, ast.Call) and not n.keywords:
        f = n.func
        if isinstance(f, ast.Name) and f.id == "abs" and len(n.args) == 1:
            return abs(_eval(n.args[0], df))
        if isinstance(f, ast.Attribute) and f.attr in _METHODS and len(n.args) == _METHODS[f.attr]:
            target = _eval(f.value, df)
            if not isinstance(target, pd.Series):
                raise QueryError(f".{f.attr}() needs a column, e.g. rpm.{f.attr}(...)")
            args = [_num(_eval(a, df)) for a in n.args]
            return getattr(target, f.attr)(*args)
    raise QueryError(f"'{ast.unparse(n)}' isn't allowed here")
