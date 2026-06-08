#!/usr/bin/env python3
"""
Create a Python script that prints its own file MD5 before running user code,
without reading its own source file at runtime.

The referenced PDF construction uses MD5 collision choices to make static
content display the digest of the final file.  For executable Python, this
tool uses a quine-style carrier: the prepended shim stores a byte template for
the generated script and hashes the reconstructed bytes instead of opening
__file__.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import re
import subprocess
import sys
import tempfile
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path


BEGIN_MARKER = "# <py-self-md5:begin>"
END_MARKER = "# <py-self-md5:end>"
CODING_RE = re.compile(br"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class WrapResult:
    input_path: Path
    output_path: Path
    md5: str
    insertion_line: int
    already_wrapped: bool = False


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def detect_newline(data: bytes) -> bytes:
    first_lf = data.find(b"\n")
    if first_lf > 0 and data[first_lf - 1:first_lf] == b"\r":
        return b"\r\n"
    return b"\n"


def strip_eol(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def find_existing_shim_span(data: bytes) -> tuple[int, int] | None:
    begin_marker = BEGIN_MARKER.encode("ascii")
    end_marker = END_MARKER.encode("ascii")
    begin: int | None = None
    offset = 0

    for line in data.splitlines(keepends=True):
        if begin is None:
            if strip_eol(line) == begin_marker:
                begin = offset
        elif strip_eol(line) == end_marker:
            end = offset + len(line)
            while end < len(data) and data[end:end + 1] in (b"\r", b"\n"):
                end += 1
            return begin, end
        offset += len(line)

    return None


def has_coding_cookie(line: bytes) -> bool:
    return bool(CODING_RE.match(line))


def protected_header_line_count(lines: list[bytes]) -> int:
    """Return lines that must remain before executable code."""
    count = 0
    first = lines[0][len(UTF8_BOM):] if lines and lines[0].startswith(UTF8_BOM) else (lines[0] if lines else b"")
    if lines and first.startswith(b"#!"):
        count = 1
    if len(lines) > count and has_coding_cookie(lines[count]):
        count += 1
    elif count == 0 and len(lines) > 1 and has_coding_cookie(lines[1]):
        count = 2
    return count


def byte_offset_for_line(lines: list[bytes], line_no: int) -> int:
    """Return byte offset for the beginning of a 1-based line number."""
    if line_no <= 1:
        if lines and lines[0].startswith(UTF8_BOM):
            return len(UTF8_BOM)
        return 0
    if line_no > len(lines):
        return sum(len(line) for line in lines)
    return sum(len(line) for line in lines[: line_no - 1])


def decode_python_source(data: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def first_insert_line_from_ast(source: str, minimum_line: int) -> int:
    """Find the earliest legal line for executable shim code."""
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"input is not valid Python: {exc}") from exc

    insert_line = minimum_line
    body = module.body
    index = 0

    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            insert_line = max(insert_line, (body[0].end_lineno or body[0].lineno) + 1)
            index = 1

    while index < len(body):
        node = body[index]
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and node.level == 0
        ):
            insert_line = max(insert_line, (node.end_lineno or node.lineno) + 1)
            index += 1
            continue
        break

    return insert_line


def make_quine_template(
    head: bytes,
    *,
    function_name: str,
    template_marker: bytes,
    tail_marker: bytes,
    newline: bytes,
) -> bytes:
    lines = [
        BEGIN_MARKER,
        f"def {function_name}():",
        "    import hashlib as __py_self_md5_hashlib",
        "    import sys as __py_self_md5_sys",
        f"    __py_self_md5_template = {template_marker.decode('ascii')}",
        f"    __py_self_md5_tail = {tail_marker.decode('ascii')}",
        "    __py_self_md5_source = (",
        "        __py_self_md5_template",
        f"        .replace({tail_marker!r}, repr(__py_self_md5_tail).encode('ascii'), 1)",
        f"        .replace({template_marker!r}, repr(__py_self_md5_template).encode('ascii'), 1)",
        "        + __py_self_md5_tail",
        "    )",
        "    __py_self_md5_sys.stdout.write(",
        "        'MD5: ' + __py_self_md5_hashlib.md5(__py_self_md5_source).hexdigest().upper() + '\\n'",
        "    )",
        f"{function_name}()",
        f"del {function_name}",
        END_MARKER,
        "",
    ]
    return head + newline.join(line.encode("ascii") for line in lines)


def choose_markers(data: bytes, seed: str) -> tuple[bytes, bytes]:
    for counter in range(1000):
        suffix = f"{seed}_{counter}".encode("ascii")
        template_marker = b"__PY_SELF_MD5_TEMPLATE_" + suffix + b"__"
        tail_marker = b"__PY_SELF_MD5_TAIL_" + suffix + b"__"
        if template_marker not in data and tail_marker not in data:
            return template_marker, tail_marker
    raise RuntimeError("could not find marker names absent from input")


def render_quine(template: bytes, tail: bytes, template_marker: bytes, tail_marker: bytes) -> bytes:
    return (
        template
        .replace(tail_marker, repr(tail).encode("ascii"), 1)
        .replace(template_marker, repr(template).encode("ascii"), 1)
        + tail
    )


def build_wrapped_source(data: bytes, *, force: bool = False) -> tuple[bytes, int, bool]:
    existing_span = find_existing_shim_span(data)
    if existing_span is not None:
        if not force:
            return data, 1, True
        data = remove_existing_shim(data)

    lines = data.splitlines(keepends=True)
    minimum_line = protected_header_line_count(lines) + 1
    source = decode_python_source(data)
    insertion_line = first_insert_line_from_ast(source, minimum_line)
    offset = byte_offset_for_line(lines, insertion_line)

    seed = hashlib.sha1(data).hexdigest()[:16]
    function_name = f"__py_self_md5_{seed}"
    newline = detect_newline(data)

    head = data[:offset]
    tail = data[offset:]
    if head and not head.endswith((b"\n", b"\r")):
        head += newline

    template_marker, tail_marker = choose_markers(data, seed)
    template = make_quine_template(
        head,
        function_name=function_name,
        template_marker=template_marker,
        tail_marker=tail_marker,
        newline=newline,
    )
    wrapped = render_quine(template, tail, template_marker, tail_marker)

    return wrapped, insertion_line, False


def remove_existing_shim(data: bytes) -> bytes:
    span = find_existing_shim_span(data)
    if span is None:
        return data
    begin, end = span
    return data[:begin] + data[end:]


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.self_md5{input_path.suffix or '.py'}")


def wrap_file(input_path: Path, output_path: Path, *, force: bool = False) -> WrapResult:
    data = input_path.read_bytes()
    wrapped, insertion_line, already_wrapped = build_wrapped_source(data, force=force)
    if already_wrapped and input_path.resolve() != output_path.resolve():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(wrapped)
    elif not already_wrapped:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(wrapped)
    return WrapResult(
        input_path=input_path,
        output_path=output_path,
        md5=file_md5(output_path),
        insertion_line=insertion_line,
        already_wrapped=already_wrapped,
    )


def run_output_check(path: Path, timeout: float) -> tuple[bool, str, str]:
    expected = file_md5(path)
    proc = subprocess.run(
        [sys.executable, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    first_line = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else ""
    expected_line = f"MD5: {expected}"
    ok = first_line == expected_line
    detail = f"expected first line {expected_line}, got {first_line or '<no stdout>'}"
    if proc.returncode != 0:
        detail += f"; process exited with {proc.returncode}"
    return ok, detail, proc.stderr


def self_test() -> None:
    sample = textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        # coding: utf-8
        """sample module docstring"""
        from __future__ import annotations

        print("payload ran")
        '''
    ).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        source = tmp_dir / "sample.py"
        output = tmp_dir / "sample.self_md5.py"
        source.write_bytes(sample)
        result = wrap_file(source, output, force=False)
        generated = output.read_bytes()
        span = find_existing_shim_span(generated)
        if span is None:
            raise SystemExit("self-test failed: generated shim markers are missing")
        shim = generated[span[0]:span[1]]
        if b"open(" in shim or b"__file__" in shim:
            raise SystemExit("self-test failed: generated shim reads or references its source file")
        ok, detail, stderr = run_output_check(result.output_path, timeout=10)
        if not ok:
            raise SystemExit(f"self-test failed: {detail}\n{stderr}")
        print(f"self-test ok: {result.output_path.name} prints MD5: {result.md5}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepend a startup shim so a .py file prints its own MD5 before user code runs."
    )
    parser.add_argument("input", nargs="?", type=Path, help="input Python file")
    parser.add_argument("-o", "--output", type=Path, help="output path")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="rewrite the input file instead of creating *.self_md5.py",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite output and replace an existing py-self-md5 shim if present",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="execute the generated file and verify that its first stdout line is MD5: <digest>",
    )
    parser.add_argument(
        "--check-timeout",
        type=float,
        default=10.0,
        help="timeout in seconds for --check",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run an internal safe sample test",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        self_test()
        return 0

    if args.input is None:
        raise SystemExit("error: input file is required unless --self-test is used")

    input_path = args.input.resolve()
    if not input_path.exists():
        raise SystemExit(f"error: input file does not exist: {input_path}")
    if not input_path.is_file():
        raise SystemExit(f"error: input path is not a file: {input_path}")

    if args.in_place and args.output:
        raise SystemExit("error: --in-place and --output cannot be used together")

    output_path = input_path if args.in_place else (args.output or default_output_path(input_path)).resolve()
    if output_path.exists() and output_path.resolve() != input_path and not args.force:
        raise SystemExit(f"error: output exists, use --force to overwrite: {output_path}")

    result = wrap_file(input_path, output_path, force=args.force)
    if result.already_wrapped:
        print(f"already wrapped: {result.output_path}")
    else:
        print(f"wrote: {result.output_path}")
        print(f"inserted shim at line: {result.insertion_line}")
    print(f"file MD5: {result.md5}")

    if args.check:
        ok, detail, stderr = run_output_check(result.output_path, timeout=args.check_timeout)
        print(f"check: {'ok' if ok else 'failed'} ({detail})")
        if stderr:
            print(stderr, file=sys.stderr, end="")
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
