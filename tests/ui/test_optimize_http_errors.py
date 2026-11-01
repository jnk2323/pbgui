"""Optimize presents FastAPI validation details as readable browser messages."""

import json
from pathlib import Path
import re

import pytest

from javascript_checks import run_javascript


@pytest.mark.parametrize("detail,expected", [
    ([{"loc": ["body", "config"], "msg": "Field required"}, {"msg": "Invalid date"}], "Field required; Invalid date"),
    ([], "HTTP 422"),
    ("Invalid config", "Invalid config"),
    ({"message": "Unsupported strategy"}, "Unsupported strategy"),
])
def test_validation_details_are_readable(detail, expected):
    """Exercise the actual frontend formatter in an isolated native browser."""
    source = (Path(__file__).resolve().parents[2] / "frontend/v7_optimize.html").read_text()
    function = re.search(r"^function buildHttpError\(.*?^\}", source, re.M | re.S).group()
    script = function + "\nconst error = buildHttpError({status:422}, " + json.dumps(json.dumps({"detail": detail})) + ");\n"
    script += "browserAssert.strictEqual(error.message, " + json.dumps(expected) + ");\n"
    script += "browserAssert.strictEqual(error.status, 422);"
    run_javascript(["-e", script], check=True)
