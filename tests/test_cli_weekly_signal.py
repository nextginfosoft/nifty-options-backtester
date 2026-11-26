"""Regression: the CLI weekly signal compared TradeAction.value ("enter") with "ENTER", so the weekly track always
reported 'declined entry', and the risk-engine block used TradeAction without importing it.

run_signal_combined needs live data, so these guard the two facts it depends on."""
import ast
from pathlib import Path

import main
from strategies.base import TradeAction

MAIN = Path(main.__file__)


def test_main_imports_trade_action():
    assert main.TradeAction is TradeAction


def test_no_enum_value_is_compared_with_an_uppercase_literal():
    """TradeAction values are lowercase ('enter'); compare members instead of .value against "ENTER"."""
    lowered = {m.value for m in TradeAction}
    bad = []
    for node in ast.walk(ast.parse(MAIN.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) and node.left.attr == "value":
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.lower() in lowered and c.value not in lowered:
                    bad.append((node.lineno, c.value))
    assert bad == []
