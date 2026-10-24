"""GitHub Actions' expression language, as much of it as the example workflows use, so a test
can read a value such as a job's `runs-on` and work out what GitHub would make of it.

    evaluate("vars.SWITCH == 'on' && 'a' || 'b'", {"vars": {"SWITCH": "on"}})  # 'a'

As GitHub does: `&&` and `||` give one of their operands, not a boolean; `false`, `0`, `''`
and `null` are falsy; `==` and `!=` ignore the case of strings, and so do `startsWith`,
`endsWith` and `contains`; a property that is not there is `null`. Anything else, an unknown
function or a stray character, is refused rather than guessed.
"""

import json
import re
from typing import Any

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>-?\d+(?:\.\d+)?)"
    r"|(?P<operator>==|!=|<=|>=|&&|\|\||[!<>().,\[\]])|(?P<name>[A-Za-z_][\w-]*))"
)


class ExpressionError(Exception):
    """The text is not an expression this evaluator knows."""


def _tokens(text: str) -> list[tuple[str, str]]:
    found, at = [], 0
    while at < len(text.rstrip()):
        match = _TOKEN.match(text, at)
        if not match:
            raise ExpressionError(f"cannot read {text[at:]!r}")
        kind = match.lastgroup
        assert kind
        found.append((kind, match[kind]))
        at = match.end()
    return found


def _truthy(value: Any) -> bool:
    return value not in (False, 0, "", None)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return left == right


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, list):
        return any(_equal(member, item) for member in container)
    return _text(item).casefold() in _text(container).casefold()


_FUNCTIONS = {
    "startswith": lambda text, start: _text(text).casefold().startswith(_text(start).casefold()),
    "endswith": lambda text, end: _text(text).casefold().endswith(_text(end).casefold()),
    "contains": _contains,
    "fromjson": lambda text: json.loads(text),
    "tojson": lambda value: json.dumps(value, indent=2),
    "format": lambda text, *values: re.sub(
        r"\{(\d+)\}", lambda found: _text(values[int(found[1])]), text
    ),
}


class _Parser:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = _tokens(text)
        self.at = 0
        self.context = context

    def peek(self) -> str | None:
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        if self.at >= len(self.tokens):
            raise ExpressionError(f"ends where {expected or 'a value'} is due")
        token = self.tokens[self.at]
        if expected is not None and token[1] != expected:
            raise ExpressionError(f"{expected!r} expected, not {token[1]!r}")
        self.at += 1
        return token

    def whole(self) -> Any:
        value = self.either()
        if self.at != len(self.tokens):
            raise ExpressionError(f"unexpected {self.peek()!r}")
        return value

    def either(self) -> Any:
        value = self.both()
        while self.peek() == "||":
            self.take()
            right = self.both()
            value = value if _truthy(value) else right
        return value

    def both(self) -> Any:
        value = self.comparison()
        while self.peek() == "&&":
            self.take()
            right = self.comparison()
            value = right if _truthy(value) else value
        return value

    def comparison(self) -> Any:
        value = self.unary()
        while self.peek() in ("==", "!=", "<", ">", "<=", ">="):
            operator = self.take()[1]
            right = self.unary()
            if operator == "==":
                value = _equal(value, right)
            elif operator == "!=":
                value = not _equal(value, right)
            else:
                value = {"<": value < right, ">": value > right, "<=": value <= right,
                         ">=": value >= right}[operator]
        return value

    def unary(self) -> Any:
        if self.peek() == "!":
            self.take()
            return not _truthy(self.unary())
        return self.member()

    def member(self) -> Any:
        value = self.primary()
        while self.peek() in (".", "["):
            if self.take()[1] == ".":
                kind, key = self.take()
                if kind != "name":
                    raise ExpressionError(f"a property name expected, not {key!r}")
            else:
                key = self.either()
                self.take("]")
            value = value.get(key) if isinstance(value, dict) else None
        return value

    def primary(self) -> Any:
        kind, token = self.take()
        if kind == "string":
            return token[1:-1].replace("''", "'")
        if kind == "number":
            return float(token) if "." in token else int(token)
        if token == "(":
            value = self.either()
            self.take(")")
            return value
        if kind != "name":
            raise ExpressionError(f"unexpected {token!r}")
        if token in ("true", "false"):
            return token == "true"
        if token == "null":
            return None
        if self.peek() == "(":
            self.take()
            arguments = []
            while self.peek() != ")":
                arguments.append(self.either())
                if self.peek() == ",":
                    self.take()
            self.take(")")
            function = _FUNCTIONS.get(token.lower())
            if function is None:
                raise ExpressionError(f"unknown function {token}")
            return function(*arguments)
        if token not in self.context:
            raise ExpressionError(f"unknown context {token}")
        return self.context[token]


def evaluate(text: str, context: dict[str, Any]) -> Any:
    """The value of one expression, without its `${{ }}`."""
    return _Parser(text, context).whole()


def value(text: str, context: dict[str, Any]) -> Any:
    """What GitHub makes of a workflow value: the expression's value when the whole text is one
    `${{ }}`, the text itself when it holds none."""
    text = text.strip()
    found = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    if found:
        return evaluate(found[1], context)
    if "${{" in text:
        raise ExpressionError(f"a value with text around its expression: {text!r}")
    return text
