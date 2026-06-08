# py-self-md5

`py-self-md5` wraps a Python script so the generated script prints
`MD5: <DIGEST>` before running the original program.

The generated script does **not** read its own source file at runtime.  Instead,
the inserted prefix stores a quine-style byte template and computes the digest
from the reconstructed bytes in memory.

This project is inspired by
[`zhuzilin/pdf-with-its-own-md5`](https://github.com/zhuzilin/pdf-with-its-own-md5),
where selectable MD5-collision carriers are used to display the final digest in
a PDF.  This tool targets executable Python programs and does not require
HashClash or precomputed collision blocks.

## Install

Requires Python 3.11 or newer.

```bash
python -m pip install py-self-md5
```

After installation, run the built-in check:

```bash
py-self-md5 --self-test
```

You can also run it from the project root without installing:

```bash
python -m py_self_md5 --self-test
```

## Usage

Create `input.self_md5.py`:

```bash
py-self-md5 input.py
```

Run the generated script:

```bash
python input.self_md5.py
```

The first output line is `MD5: <DIGEST>`, where `<DIGEST>` is the uppercase MD5
digest of `input.self_md5.py`; then the original program continues.

Write to a specific path:

```bash
py-self-md5 input.py -o output.py
```

Rewrite the input file in place:

```bash
py-self-md5 input.py --in-place
```

Replace an existing `py-self-md5` prefix in place:

```bash
py-self-md5 input.py --in-place --force
```

Overwrite an existing output file:

```bash
py-self-md5 input.py -o output.py --force
```

Generate and verify in one step:

```bash
py-self-md5 input.py --check
```

## What the wrapper preserves

- The tool preserves shebang lines, encoding cookies, module docstrings, and
  `from __future__ import ...` placement.
- By default, the input file is not modified.
- Existing `py-self-md5` prefixes are detected. Use `--force` to replace one.

## Limitations

- This is an executable self-hash wrapper, not a general MD5 collision
  generator.
- The generated prefix can be large because it embeds enough bytes to
  reconstruct the generated script in memory.
- `--check` executes the generated script, so only use it with programs you are
  willing to run.
