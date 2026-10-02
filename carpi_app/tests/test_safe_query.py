import numpy as np
import pandas as pd
import pytest

from carpi_app.safe_query import QueryError, evaluate

DF = pd.DataFrame({"rpm": [800, 2000, 3500, np.nan], "coolant_c": [70, 90, 95, 95],
                   "lambda_err": [0.0, -0.06, 0.01, 0.1], "iat_c": [20, 35, 50, 40]})


@pytest.mark.parametrize("q, expect", [
    ("rpm > 1500", [False, True, True, False]),
    ("rpm > 1500 and coolant_c >= 90", [False, True, True, False]),
    ("rpm > 3000 or not coolant_c > 80", [True, False, True, False]),
    ("(rpm > 1000) & ~(iat_c > 40)", [False, True, False, False]),
    ("abs(lambda_err) > 0.05", [False, True, False, True]),
    ("lambda_err.abs() > 0.05", [False, True, False, True]),
    ("iat_c.between(30, 45)", [False, True, False, True]),
    ("1000 < rpm < 3000", [False, True, False, False]),
    ("rpm / 1000 + 1 >= 3", [False, True, True, False]),
    ("rpm.isna()", [False, False, False, True]),
    ("coolant_c == -(-90)", [False, True, False, False]),
])
def test_allowed(q, expect):
    assert evaluate(DF, q).tolist() == expect


@pytest.mark.parametrize("q", [
    "__import__('os').system('echo hi')",
    "rpm.__class__",
    "rpm.values",
    "open('x')",
    "rpm > 'a'",
    "[x for x in rpm]",
    "rpm[0] > 1",
    "lambda: 1",
    "rpm.between(a=1, b=2)",
    "rpm.apply(print)",
    "rpm ** 999999",
    "nope > 1",
    "rpm +",
    "rpm",
    "x" * 400,
])
def test_rejected(q):
    with pytest.raises(QueryError):
        evaluate(DF, q)
