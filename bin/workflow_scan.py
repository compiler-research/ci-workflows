"""Find the recipe cells a repository's GitHub workflows pull.

    import workflow_scan
    workflow_scan.ci_workflows_refs(checkout)   # does it use us at all?
    workflow_scan.scan_repo(checkout)           # which cells, from which rows

A consumer never names its cell outright: it hands matrix values to
setup-llvm / setup-recipe (or to a composite that calls them) through
`${{ }}` expressions, and each consumer wires those differently --
clad's `flavor: ${{ matrix.use-recipe != 'true' && (matrix.flavor ||
'system') || '' }}`, CppInterOp's bare `${{ matrix.flavor }}`,
CARTopiaX's setup-biodynamo. Rather than knowing every consumer's
convention, this evaluates the consumer's own `with:` against each
expanded matrix row, the way the runner would, and then applies the
one piece of logic that lives in shell rather than in expressions:
setup-llvm's flavor -> recipe table.

Stdlib-only, like bin/repro and bin/start, hence the YAML-subset parser
below. It covers what workflow files use -- block and flow collections,
quoted and block scalars, anchors, aliases and merge keys -- and
nothing else (no tags, no multi-document streams, no complex keys).
"""

from __future__ import annotations

import itertools
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

COORD_KEYS = ("recipe", "version", "os", "arch")

#: `uses:` of one of our actions: owner/repo/actions/<name>@ref.
_CI_ACTION_RE = re.compile(
    r"^compiler-research/ci-workflows/actions/([A-Za-z0-9_.-]+)@")
#: Anything of ours, including reusable workflows.
_CI_ANY_RE = re.compile(r"compiler-research/ci-workflows/[^\s'\"]+@")

#: setup-llvm's "Resolve flavor -> recipe" step. The only mapping that
#: happens in shell rather than in an expression, so the only one that
#: has to be restated here. system -> None: apt/brew, no cell.
FLAVOR_TO_RECIPE = {
    "":      "llvm-release",
    "asan":  "llvm-asan",
    "msan":  "llvm-msan",
    "cling": "llvm-root",
    "debug": "llvm-debug",
    "system": None,
}


def os_to_arch(os_slug: str) -> str:
    """setup-llvm's arch-from-os derivation."""
    if os_slug.startswith("macos-"):
        return "x86_64" if os_slug.endswith("-intel") else "arm64"
    if os_slug.endswith("-arm"):
        return "arm64"
    return "x86_64"


# ------------------------------------------------------------------ YAML


class YAMLError(ValueError):
    pass


_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")


def _plain(s: str) -> Any:
    """Type a plain scalar per the YAML 1.2 core schema."""
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _strip_comment(line: str) -> str:
    """Drop a trailing `# comment`, respecting quoted scalars.

    A quote only opens a string at a token start, so the apostrophe in
    `run: echo don't` does not swallow the rest of the line.
    """
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and line[i + 1:i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif c in "'\"" and (i == 0 or line[i - 1] in " \t:[{,-"):
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _unquote(s: str) -> str:
    if s.startswith("'"):
        return s[1:-1].replace("''", "'")
    body = s[1:-1]
    esc = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "/": "/", "0": "\0"}
    return re.sub(r"\\(.)", lambda m: esc.get(m.group(1), m.group(1)), body)


def _quoted_end(s: str, start: int = 0) -> int:
    """Index just past the quoted scalar opening at s[start]."""
    q = s[start]
    i = start + 1
    while i < len(s):
        if q == '"' and s[i] == "\\":
            i += 2
            continue
        if s[i] == q:
            if q == "'" and s[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    raise YAMLError(f"unterminated quoted scalar: {s!r}")


def _split_key(content: str) -> Optional[Tuple[str, str]]:
    """Split `key: rest` -> (key, rest), or None if not a mapping entry."""
    if not content or content[0] in "[{|>&*!%@`":
        return None
    if content[0] in "'\"":
        end = _quoted_end(content)
        after = content[end:].lstrip()
        if after == ":" or after.startswith(": "):
            return _unquote(content[:end]), after[1:].strip()
        return None
    m = re.search(r":(\s|$)", content)
    if not m:
        return None
    return content[:m.start()].strip(), content[m.end():].strip()


class _Parser:
    def __init__(self, text: str):
        self.raw = text.splitlines()
        self.i = 0
        # Virtual rewrites of a line, used to re-read `- key: v` as a
        # mapping starting at the column after the dash.
        self.over: Dict[int, Tuple[int, str]] = {}
        self.anchors: Dict[str, Any] = {}

    # -- line access
    def _line(self, i: int) -> Optional[Tuple[int, str]]:
        if i in self.over:
            return self.over[i]
        raw = self.raw[i]
        content = _strip_comment(raw).strip()
        if not content or content in ("---", "...") or content.startswith("%"):
            return None
        return len(raw) - len(raw.lstrip(" ")), content

    def peek(self) -> Optional[Tuple[int, str]]:
        while self.i < len(self.raw):
            got = self._line(self.i)
            if got is not None:
                return got
            self.i += 1
        return None

    @staticmethod
    def _is_seq(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    # -- structure
    def parse(self) -> Any:
        top = self.peek()
        return None if top is None else self.node(top[0])

    def node(self, indent: int) -> Any:
        ind, content = self.peek()
        if self._is_seq(content):
            return self.seq(ind)
        if _split_key(content) is None:
            self.i += 1
            return self.inline(content, ind - 1)
        return self.map(ind)

    def seq(self, indent: int) -> List[Any]:
        out: List[Any] = []
        while True:
            nxt = self.peek()
            if nxt is None or nxt[0] != indent or not self._is_seq(nxt[1]):
                return out
            content = nxt[1]
            rest = content[1:].lstrip()
            if not rest:
                self.i += 1
                after = self.peek()
                if after is not None and after[0] > indent:
                    out.append(self.node(after[0]))
                else:
                    out.append(None)
                continue
            col = indent + len(content) - len(rest)
            if self._is_seq(rest) or _split_key(rest) is not None:
                # `- key: v` / `- - x`: the item is a collection whose
                # first line starts after the dash.
                self.over[self.i] = (col, rest)
                out.append(self.node(col))
                continue
            self.i += 1
            out.append(self.inline(rest, indent))

    def map(self, indent: int) -> Dict[Any, Any]:
        out: Dict[Any, Any] = {}
        while True:
            nxt = self.peek()
            if nxt is None or nxt[0] < indent:
                return out
            if nxt[0] > indent:
                raise YAMLError(f"line {self.i + 1}: unexpected indent")
            if self._is_seq(nxt[1]):
                return out
            kv = _split_key(nxt[1])
            if kv is None:
                raise YAMLError(f"line {self.i + 1}: expected `key: value`")
            key, rest = kv
            self.over.pop(self.i, None)
            self.i += 1
            value = self.value(rest, indent)
            if key == "<<":
                for src in (value if isinstance(value, list) else [value]):
                    if isinstance(src, dict):
                        for k, v in src.items():
                            out.setdefault(k, v)
            else:
                out[key] = value

    def value(self, rest: str, indent: int) -> Any:
        """Value after `key:` (or `- `) whose key sits at `indent`."""
        anchor = None
        if rest.startswith("&"):
            anchor, _, rest = rest[1:].partition(" ")
            rest = rest.strip()
        if rest.startswith("*"):
            name = rest[1:].strip()
            if name not in self.anchors:
                raise YAMLError(f"unknown alias *{name}")
            return self.anchors[name]
        if rest == "":
            nxt = self.peek()
            if nxt is not None and (nxt[0] > indent or
                                    (nxt[0] == indent and
                                     self._is_seq(nxt[1]))):
                v = self.node(nxt[0])
            else:
                v = None
        else:
            v = self.inline(rest, indent)
        if anchor:
            self.anchors[anchor] = v
        return v

    def inline(self, rest: str, indent: int) -> Any:
        """A value that starts on the current line (already consumed)."""
        if rest[0] in "|>":
            return self.block_scalar(rest, indent)
        if rest[0] in "[{":
            text = rest
            while not _balanced(text):
                nxt = self.peek()
                if nxt is None:
                    raise YAMLError("unterminated flow collection")
                text += " " + nxt[1]
                self.i += 1
            return _flow(text, 0, self.anchors)[0]
        if rest[0] in "'\"":
            text = rest
            while True:
                try:
                    end = _quoted_end(text)
                    break
                except YAMLError:
                    if self.i >= len(self.raw):
                        raise
                    text += " " + self.raw[self.i].strip()
                    self.i += 1
            return _unquote(text[:end])
        # Plain scalar, possibly continued on deeper-indented lines.
        parts = [rest]
        while True:
            nxt = self.peek()
            if nxt is None or nxt[0] <= indent or self.i in self.over:
                break
            parts.append(nxt[1])
            self.i += 1
        return _plain(" ".join(parts))

    def block_scalar(self, header: str, indent: int) -> str:
        style = header[0]
        chomp = "-" if "-" in header else "+" if "+" in header else ""
        lines: List[str] = []
        content_indent = None
        while self.i < len(self.raw):
            raw = self.raw[self.i]
            if raw.strip():
                ind = len(raw) - len(raw.lstrip(" "))
                if content_indent is None:
                    if ind <= indent:
                        break
                    content_indent = ind
                elif ind < content_indent:
                    break
                lines.append(raw[content_indent:])
            else:
                lines.append("")
            self.i += 1
        while lines and lines[-1] == "" and chomp != "+":
            lines.pop()
        if style == "|":
            body = "\n".join(lines)
        else:
            body, prev_blank = "", True
            for ln in lines:
                if ln == "":
                    body += "\n"
                    prev_blank = True
                else:
                    body += ("" if prev_blank else " ") + ln
                    prev_blank = False
        return body if chomp == "-" or not body else body + "\n"


def _balanced(text: str) -> bool:
    depth, i = 0, 0
    while i < len(text):
        c = text[i]
        if c in "'\"" and (i == 0 or text[i - 1] in " [{,:"):
            try:
                i = _quoted_end(text, i)
            except YAMLError:
                return False
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return True
        i += 1
    return depth == 0


def _flow(s: str, i: int,
          anchors: Dict[str, Any]) -> Tuple[Any, int]:
    """Parse the flow node at s[i]; return (value, index after it)."""
    while s[i] == " ":
        i += 1
    if s[i] == "*":
        m = re.compile(r"\*([^\s,\]}]+)").match(s, i)
        if m.group(1) not in anchors:
            raise YAMLError(f"unknown alias *{m.group(1)}")
        return anchors[m.group(1)], m.end()
    if s[i] == "&":
        m = re.compile(r"&([^\s,\]}]+)\s*").match(s, i)
        v, end = _flow(s, m.end(), anchors)
        anchors[m.group(1)] = v
        return v, end
    c = s[i]
    if c in "[{":
        close = "]" if c == "[" else "}"
        out: Any = [] if c == "[" else {}
        i += 1
        while True:
            while s[i] in " ,":
                i += 1
            if s[i] == close:
                return out, i + 1
            if c == "[":
                v, i = _flow(s, i, anchors)
                out.append(v)
            else:
                k, i = _flow_scalar(s, i, key=True)
                while s[i] == " ":
                    i += 1
                if s[i] == ":":
                    i += 1
                    v, i = _flow(s, i, anchors)
                else:
                    v = None
                out[k] = v
    return _flow_scalar(s, i)


def _flow_scalar(s: str, i: int, key: bool = False) -> Tuple[Any, int]:
    while s[i] == " ":
        i += 1
    if s[i] in "'\"":
        end = _quoted_end(s, i)
        return _unquote(s[i:end]), end
    j = i
    while j < len(s):
        if s[j] in ",]}":
            break
        if s[j] == ":" and (j + 1 == len(s) or s[j + 1] in " ,]}"):
            break
        j += 1
    text = s[i:j].strip()
    return (text if key else _plain(text)), j


def parse_yaml(text: str) -> Any:
    return _Parser(text).parse()


def load_yaml(path: Path) -> Any:
    return parse_yaml(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------ expressions


class ExprError(ValueError):
    pass


_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<str>'(?:[^']|'')*')
    | (?P<num>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))
    | (?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\],.*])
    | (?P<id>[A-Za-z_][A-Za-z0-9_-]*)
    )""", re.X)


def _tokens(src: str) -> List[Tuple[str, str]]:
    out, i = [], 0
    while i < len(src):
        if src[i:].strip() == "":
            break
        m = _TOKEN_RE.match(src, i)
        if not m or m.end() == i:
            raise ExprError(f"cannot tokenize {src[i:]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        i = m.end()
    return out


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
        except ValueError:
            return math.nan
    return math.nan


def truthy(v: Any) -> bool:
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return not (v == 0 or math.isnan(v))
    return True


def to_str(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if type(a) is type(b) and isinstance(a, (dict, list)):
        return a is b
    return _num(a) == _num(b)


def _get(obj: Any, key: Any) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        if isinstance(key, str):
            for k, v in obj.items():
                if isinstance(k, str) and k.casefold() == key.casefold():
                    return v
        return None
    if isinstance(obj, list) and isinstance(key, (int, float)):
        k = int(key)
        return obj[k] if 0 <= k < len(obj) else None
    return None


def _fmt(template: str, args: List[Any]) -> str:
    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        n = int(m.group(1))
        return to_str(args[n]) if n < len(args) else ""
    return re.sub(r"\{\{|\}\}|\{(\d+)\}", sub, template)


_FUNCS = {
    "format":     lambda a: _fmt(to_str(a[0]), a[1:]),
    "contains":   lambda a: (any(_eq(x, a[1]) for x in a[0])
                             if isinstance(a[0], list)
                             else to_str(a[1]).casefold()
                             in to_str(a[0]).casefold()),
    "startswith": lambda a: to_str(a[0]).casefold().startswith(
                                to_str(a[1]).casefold()),
    "endswith":   lambda a: to_str(a[0]).casefold().endswith(
                                to_str(a[1]).casefold()),
    "join":       lambda a: (to_str(a[1]) if len(a) > 1 else ",").join(
                                to_str(x) for x in a[0])
                            if isinstance(a[0], list) else to_str(a[0]),
    "tojson":     lambda a: json.dumps(a[0]),
    "fromjson":   lambda a: json.loads(to_str(a[0])),
    "hashfiles":  lambda a: "",
    "success":    lambda a: True,
    "always":     lambda a: True,
    "failure":    lambda a: False,
    "cancelled":  lambda a: False,
}


class _Expr:
    def __init__(self, src: str, ctx: Dict[str, Any]):
        self.t = _tokens(src)
        self.p = 0
        self.ctx = ctx

    def run(self) -> Any:
        v = self.or_()
        if self.p != len(self.t):
            raise ExprError(f"trailing tokens: {self.t[self.p:]}")
        return v

    def _peek(self) -> Optional[str]:
        return self.t[self.p][1] if self.p < len(self.t) else None

    def _take(self, want: Optional[str] = None) -> Tuple[str, str]:
        if self.p >= len(self.t):
            raise ExprError("unexpected end of expression")
        tok = self.t[self.p]
        if want is not None and tok[1] != want:
            raise ExprError(f"expected {want!r}, got {tok[1]!r}")
        self.p += 1
        return tok

    def or_(self) -> Any:
        v = self.and_()
        while self._peek() == "||":
            self._take()
            rhs = self.and_()
            v = v if truthy(v) else rhs
        return v

    def and_(self) -> Any:
        v = self.eq()
        while self._peek() == "&&":
            self._take()
            rhs = self.eq()
            v = rhs if truthy(v) else v
        return v

    def eq(self) -> Any:
        v = self.cmp()
        while self._peek() in ("==", "!="):
            op = self._take()[1]
            rhs = self.cmp()
            v = _eq(v, rhs) if op == "==" else not _eq(v, rhs)
        return v

    def cmp(self) -> Any:
        v = self.unary()
        while self._peek() in ("<", ">", "<=", ">="):
            op = self._take()[1]
            rhs = self.unary()
            if isinstance(v, str) and isinstance(rhs, str):
                a, b = v.casefold(), rhs.casefold()
            else:
                a, b = _num(v), _num(rhs)
            v = {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        return v

    def unary(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not truthy(self.unary())
        return self.postfix()

    def postfix(self) -> Any:
        v = self.primary()
        while self._peek() in (".", "["):
            if self._take()[1] == ".":
                kind, name = self._take()
                if name == "*":
                    v = list(v.values()) if isinstance(v, dict) else v
                else:
                    v = _get(v, name)
            else:
                key = self.or_()
                self._take("]")
                v = _get(v, key)
        return v

    def primary(self) -> Any:
        kind, text = self._take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return _num(text)
        if text == "(":
            v = self.or_()
            self._take(")")
            return v
        if kind != "id":
            raise ExprError(f"unexpected {text!r}")
        if text in ("true", "false"):
            return text == "true"
        if text == "null":
            return None
        if self._peek() == "(":
            self._take()
            args: List[Any] = []
            while self._peek() != ")":
                args.append(self.or_())
                if self._peek() == ",":
                    self._take()
            self._take(")")
            fn = _FUNCS.get(text.lower())
            if fn is None:
                return None
            try:
                return fn(args)
            except (IndexError, TypeError, ValueError):
                return None
        return _get(self.ctx, text)


def evaluate(expr: str, ctx: Dict[str, Any]) -> Any:
    """Evaluate one GitHub Actions expression (the part inside ${{ }})."""
    return _Expr(expr, ctx).run()


_INTERP_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def render(value: Any, ctx: Dict[str, Any]) -> Any:
    """Substitute every `${{ }}` in a workflow value.

    A value that is exactly one expression keeps the expression's type,
    as the runner does; anything else is string interpolation.
    """
    if not isinstance(value, str) or "${{" not in value:
        return value
    whole = _INTERP_RE.fullmatch(value.strip())
    if whole:
        return evaluate(whole.group(1), ctx)
    return _INTERP_RE.sub(lambda m: to_str(evaluate(m.group(1), ctx)), value)


# ---------------------------------------------------------------- matrix


def expand_matrix(matrix: Any) -> Optional[List[Dict[str, Any]]]:
    """Expand `strategy.matrix` into its rows, GitHub's way.

    Returns [{}] for a job with no matrix, and None for one computed at
    run time (`${{ fromJSON(...) }}`), which cannot be known statically.
    """
    if matrix is None:
        return [{}]
    if not isinstance(matrix, dict):
        return None
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list):
        return None
    base: Dict[str, List[Any]] = {}
    for k, v in matrix.items():
        if k in ("include", "exclude"):
            continue
        if isinstance(v, str) and "${{" in v:
            return None
        base[k] = v if isinstance(v, list) else [v]
    rows: List[Dict[str, Any]] = []
    if base:
        keys = list(base)
        rows = [dict(zip(keys, combo))
                for combo in itertools.product(*base.values())]
    rows = [r for r in rows
            if not any(isinstance(e, dict) and
                       all(k in r and _eq(r[k], v) for k, v in e.items())
                       for e in exclude)]
    # An include merges into every original combination it does not
    # contradict, and becomes a row of its own if there is none; rows
    # added that way are never merge targets for later includes.
    originals = [dict(r) for r in rows]
    added: List[Dict[str, Any]] = []
    for inc in include:
        if not isinstance(inc, dict):
            continue
        matched = False
        for row, orig in zip(rows, originals):
            if all(_eq(orig[k], v) for k, v in inc.items() if k in orig):
                row.update(inc)
                matched = True
        if not matched:
            added.append(dict(inc))
    return rows + added


# --------------------------------------------------------------- scanning


def ci_workflows_refs(checkout: Path) -> List[Tuple[str, str]]:
    """(file, uses) for every reference to ci-workflows under .github/.

    The cheap "is this repo ci-workflows enabled" check: a textual scan,
    so it also counts reusable workflows and actions scan_repo cannot
    evaluate.
    """
    out: List[Tuple[str, str]] = []
    gh = checkout / ".github"
    if not gh.is_dir():
        return out
    for f in sorted(gh.rglob("*")):
        if f.suffix not in (".yml", ".yaml") or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _CI_ANY_RE.finditer(text):
            out.append((str(f.relative_to(checkout)), m.group(0)))
    return out


def _runner_ctx(runs_on: Any) -> Dict[str, str]:
    label = to_str(runs_on[0] if isinstance(runs_on, list) and runs_on
                   else runs_on).lower()
    if "windows" in label:
        os_name = "Windows"
    elif "macos" in label:
        os_name = "macOS"
    else:
        os_name = "Linux"
    if "arm" in label or (os_name == "macOS" and "intel" not in label):
        arch = "ARM64"
    else:
        arch = "X64"
    return {"os": os_name, "arch": arch, "temp": "/tmp",
            "tool_cache": "/opt/hostedtoolcache"}


def _action_inputs(action: Dict[str, Any], given: Dict[str, Any],
                   ctx: Dict[str, Any]) -> Dict[str, Any]:
    """An action's `inputs` context: defaults overlaid with `with:`."""
    out: Dict[str, Any] = {}
    for name, spec in (action.get("inputs") or {}).items():
        default = spec.get("default") if isinstance(spec, dict) else None
        out[name] = to_str(render(default, ctx)) if default is not None else ""
    for k, v in given.items():
        out[k] = to_str(v)
    return out


def _coord(recipe: Optional[str], version: Any, os_slug: Any,
           arch: Any) -> Optional[Dict[str, str]]:
    if not recipe:
        return None
    os_s, ver = to_str(os_slug), to_str(version)
    arch_s = to_str(arch) or (os_to_arch(os_s) if os_s else "")
    coord = {"recipe": recipe, "version": ver, "os": os_s, "arch": arch_s}
    return coord if all(coord.values()) else None


def _load_action(ref: str, checkout: Path) -> Optional[Dict[str, Any]]:
    """action.yml for a `uses:` we can see locally, or None.

    Ours resolve against this ci-workflows tree, not a fetch of @ref:
    the scan answers "which cells", and cells.yaml is read from here
    too.
    """
    m = _CI_ACTION_RE.match(ref)
    if m:
        base = REPO_ROOT / "actions" / m.group(1)
    elif ref.startswith("./"):
        base = checkout / ref[2:]
    else:
        return None
    for name in ("action.yml", "action.yaml"):
        if (base / name).is_file():
            try:
                doc = load_yaml(base / name)
            except (YAMLError, OSError):
                return None
            if isinstance(doc, dict):
                return doc
    return None


#: Contexts whose value is known before a job runs. A condition that
#: reads anything else (github.event_name, steps.*, needs.*, env, ...)
#: depends on how the run was triggered or went, so it is assumed true:
#: a cell a row *might* fetch is still one to offer.
_STATIC_CONTEXTS = {"matrix", "inputs", "runner", "strategy"}


def step_runs(cond: Any, ctx: Dict[str, Any]) -> bool:
    """Would a step/job with this `if:` run for this row?"""
    if cond is None:
        return True
    if isinstance(cond, bool):
        return cond
    text = to_str(cond).strip()
    whole = _INTERP_RE.fullmatch(text)
    expr = whole.group(1) if whole else text
    if "${{" in expr:
        return True
    try:
        toks = _tokens(expr)
    except ExprError:
        return True
    for n, (kind, val) in enumerate(toks):
        if (kind == "id" and val.lower() not in _STATIC_CONTEXTS
                and n + 1 < len(toks) and toks[n + 1][1] == "."
                and (n == 0 or toks[n - 1][1] != ".")):
            return True
    try:
        return truthy(evaluate(expr, ctx))
    except ExprError:
        return True


def _step_cells(steps: Any, ctx: Dict[str, Any], checkout: Path,
                depth: int = 0) -> Iterator[Tuple[str, Dict[str, str]]]:
    """Yield (action, coord) for every cell these steps would fetch."""
    if depth > 5 or not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = to_str(step.get("uses"))
        if not uses or not step_runs(step.get("if"), ctx):
            continue
        try:
            given = {k: render(v, ctx)
                     for k, v in (step.get("with") or {}).items()}
        except ExprError:
            continue
        m = _CI_ACTION_RE.match(uses)
        name = m.group(1) if m else None
        action = _load_action(uses, checkout)
        inputs = (_action_inputs(action, given, ctx) if action
                  else {k: to_str(v) for k, v in given.items()})
        if name == "setup-llvm":
            # setup-llvm reaches setup-recipe through a shell step's
            # outputs, which no expression can see -- resolve it here.
            flavor = inputs.get("flavor", "")
            if flavor not in FLAVOR_TO_RECIPE:
                continue
            coord = _coord(FLAVOR_TO_RECIPE[flavor],
                           inputs.get("flavor-version") or
                           inputs.get("version"),
                           inputs.get("os"), inputs.get("arch"))
            if coord:
                yield name, coord
            continue
        if name == "setup-recipe":
            coord = _coord(inputs.get("recipe"), inputs.get("version"),
                           inputs.get("os"), inputs.get("arch"))
            if coord:
                yield name, coord
            continue
        runs = (action or {}).get("runs") or {}
        if runs.get("using") == "composite":
            sub = dict(ctx, inputs=inputs, steps={})
            for _inner, coord in _step_cells(runs.get("steps"), sub,
                                             checkout, depth + 1):
                yield name or uses, coord


def _row_label(job_id: str, job: Dict[str, Any], row: Dict[str, Any],
               ctx: Dict[str, Any]) -> str:
    if row.get("name"):
        return to_str(row["name"])
    try:
        rendered = to_str(render(job.get("name"), ctx))
    except ExprError:
        rendered = ""
    if rendered:
        return rendered
    if row:
        return f"{job_id} (" + ", ".join(f"{k}={to_str(v)}"
                                         for k, v in row.items()) + ")"
    return job_id


def scan_repo(checkout: Path) -> List[Dict[str, str]]:
    """Every (row, cell) the checkout's workflows would fetch.

    One entry per distinct (workflow, job, row, cell), in file order,
    with keys: workflow, job, row, action, recipe, version, os, arch.
    Files that do not parse are skipped; this is a best-effort map,
    and the caller validates each cell against cells.yaml.
    """
    out: List[Dict[str, str]] = []
    seen = set()
    wf_dir = checkout / ".github" / "workflows"
    if not wf_dir.is_dir():
        return out
    files = sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")))
    for wf in files:
        try:
            doc = load_yaml(wf)
        except (YAMLError, OSError, IndexError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            continue
        for job_id, job in doc["jobs"].items():
            if not isinstance(job, dict):
                continue
            rows = expand_matrix((job.get("strategy") or {}).get("matrix")
                                 if isinstance(job.get("strategy"), dict)
                                 else None)
            for row in rows or []:
                ctx: Dict[str, Any] = {
                    "matrix": row, "inputs": {}, "env": {}, "vars": {},
                    "secrets": {}, "steps": {}, "needs": {},
                    "github": {"event_name": "push",
                               "repository": checkout.name,
                               "workspace": "/github/workspace"},
                }
                try:
                    ctx["runner"] = _runner_ctx(render(job.get("runs-on"),
                                                       ctx))
                except ExprError:
                    ctx["runner"] = _runner_ctx("")
                label = _row_label(str(job_id), job, row, ctx)
                for action, coord in _step_cells(job.get("steps"), ctx,
                                                 checkout):
                    key = (wf.name, job_id, label) + tuple(
                        coord[k] for k in COORD_KEYS)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(dict(coord, workflow=f".github/workflows/"
                                    f"{wf.name}", job=str(job_id),
                                    row=label, action=action))
    return out
