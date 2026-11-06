# regexlab-mcp

**A regex lab for your AI agent: it RUNS the pattern in real engines instead of guessing.**

<!-- mcp-name: io.github.0xmortuex/regexlab-mcp -->

Models write subtly wrong regexes: a `$` that accepts a trailing newline, a `\d` that
matches Arabic digits, a `(a+)+` that hangs your server, a `(?P<name>...)` pasted into
JavaScript. regexlab-mcp is an [MCP](https://modelcontextprotocol.io) server that gives the
agent somewhere to check. It runs your pattern in Python `re`, JavaScript's RegExp (through
`node`) and, if installed, the third-party `regex` module, each in a throwaway child process
with a hard timeout, and reports what actually happened.

**What is different about it.** A few regex MCP servers already exist (for example
[regex-check-mcp](https://github.com/arhancanli/regex-check-mcp), which also covers PCRE2 and
RE2, and [rexact-mcp](https://github.com/Nicolasmar91/rexact-mcp)); I did not benchmark them
and they cover engines this one does not. The edge here is the combination: one server that
runs the *same* pattern side by side in several engines and flags disagreements, measures
ReDoS by timing adversarial input in a process that is killed at the budget (not only a static
guess), explains patterns with its own parser and says "not explained" instead of guessing,
translates between Python and JavaScript syntax and compiles the result in the target engine,
previews substitutions with each engine's own template rules, and generates test strings that
are verified by running them. It is pure Python with no dependency beyond `mcp`.

## What it looks like

Real output (Python 3.12, node 24, `regex` 2026.9):

```
> test_regex  pattern: ^\d+$   subject: "123\n"
case 1: '123\n'
  python: 1 match
    #1 [0:3] '123'
  javascript: no match
  regex: 1 match
    #1 [0:3] '123'
  => ENGINES DIFFER on this case (see compare_engines for why)

> check_backtracking  pattern: (a+)+$   engine: all
RISK: HIGH - catastrophic (exponential) backtracking CONFIRMED by running it.
  1. [nested] `(a+)+` repeats something that contains the repeat `a+`, and what follows that
     inner repeat can start with the same characters. ...
     probe: python: TIMED OUT after 3.0s and was killed, at n~28; last completed n=24 took 1113 ms.
     slow input: 'a' * 28 + '!'

> suggest_tests  pattern: ^[A-Z]{2}\d{3,4}$
SHOULD MATCH (4 confirmed):      'AA000'  'AA0000'  'ZZ000'  'ZZ0000'
SHOULD NOT MATCH (25 confirmed): 'A000'  'AA00000'  'AA!00'  'aa000' ...
SURPRISES - expected NO match but the engine matched (4):
  'AA000\n'   matches 'AA000' at [0:5]   ['AA000' with a trailing newline]

> convert_pattern  (?P<y>\d{4})-(?P=y)\Z   python -> javascript
  pattern: (?<y>\d{4})-\k<y>(?![\s\S])
  VERIFY: the converted pattern compiles in javascript v24.14.0.
```

## Tools

| Tool | What it does |
|------|--------------|
| `test_regex` | All matches with `[start:end)` spans (Unicode code points), numbered and named groups, per engine side by side, with disagreements flagged. Pass one text or a list of cases. |
| `explain_regex` | Token-by-token plain-English breakdown from the server's own parser: classes, greedy/lazy/possessive quantifiers, groups, lookaround, backreferences, anchors, flags, with engine-specific behaviour. Marks unsupported constructs `NOT EXPLAINED`. |
| `check_backtracking` | Static scan for nested/overlapping quantifiers plus a timed probe on crafted input, killed at the budget. Reports exponential vs polynomial, with an example slow input. |
| `compare_engines` | Compiles and runs one pattern everywhere, adds targeted variants (trailing newline, Unicode digits, non-ASCII word, emoji, Turkish case pairs) and lists the known semantic differences that apply. |
| `convert_pattern` | Python to JavaScript and back (named groups, `\A`/`\Z`/`$`, inline flags, verbose mode, `{,n}`, `[^]`...). Untranslatable constructs are explicit warnings; the result is compiled in the target engine. |
| `replace_preview` | Every substitution and the final text, using each engine's own template syntax (`\1` vs `$1`), warning when the replacement uses the wrong language's syntax. |
| `suggest_tests` | Should-match / should-not-match strings from the pattern's structure and mutations, all verified by running. Mutations that unexpectedly match are shown as SURPRISES. |

## Install

Requires Python 3.10+. For the JavaScript engine install [Node.js](https://nodejs.org); for the
`regex` engine add the extra (`regexlab-mcp[regex]`). Tools report which engines were skipped.

**Claude Code**

```bash
claude mcp add regexlab -- uvx regexlab-mcp
# with the optional regex module:
claude mcp add regexlab -- uvx --from "regexlab-mcp[regex]" regexlab-mcp
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "regexlab": { "command": "uvx", "args": ["regexlab-mcp"] }
  }
}
```

Or `pip install regexlab-mcp` and run `regexlab-mcp`.

## Try asking

- "Write a regex for ISO dates and prove it works on these 10 inputs, in both Python and JavaScript."
- "Is `^(\w+\s?)*$` safe to run on user input? Check it."
- "Explain `(?<=\$)\d+(?:\.\d{2})?` token by token."
- "This works in Python; make it work in JavaScript and tell me what changed."
- "Preview this `re.sub` on my log line before I run it on the file."
- "Give me should-match and should-not-match tests for my phone number regex."
- "Why does `^\d+$` accept `123\n` in Python but not in the browser?"

## Engines and limits

| Engine | Needs | Notes |
|--------|-------|-------|
| `python` | nothing | Python `re` of the interpreter that runs the server. |
| `javascript` | `node` on PATH | V8 irregexp; `g` and `d` are always on internally. |
| `regex` | `pip install regex` | Possessive/atomic, `\p{..}`, variable-width lookbehind. |

Spans are `[start:end)` in Unicode code points for every engine (JavaScript's UTF-16 indices
are converted), so results are comparable. Limits: pattern 5000 characters, 100 cases,
200,000 characters of subject in total, 500 matches per case, `timeout_s` 0.2 to 30.

PCRE2, RE2 and other engines are not included. The explainer covers the common subset; recursion,
conditionals and `\N{name}` are reported as not explained. The ReDoS static scan is a heuristic
(it can flag shapes that are harmless, and the probe then clears them); "no finding" is evidence,
not proof.

## Security

Regexes are untrusted input and can hang an engine for hours, which cannot be interrupted from
inside the same process. So:

- every run happens in its own child process (a fixed runner script, never `eval`/`exec` of user
  text); the pattern, flags and subject travel as JSON on stdin and are only ever passed to
  `re.compile` / `new RegExp` as data;
- no shell is used anywhere; the argument vector is fixed by the package;
- a hard timeout kills the whole process tree (POSIX process group, Windows Job Object plus
  `taskkill /T`); a test proves a grandchild process dies too;
- memory: 1 GiB address-space cap in the Python runner on Linux, a 1 GiB Job Object limit on
  Windows, `--max-old-space-size=256` for node. macOS has no enforceable address-space limit, so
  there only the timeout and the input caps protect you;
- subject size, case count, match count, pattern length and captured output are all capped.

The server reads no files and opens no network connections.

## Development

```bash
uv sync --extra test
uv run pytest tests -v
uv run --with ruff==0.16.0 ruff check src tests
uv run --with mypy==2.3.0 mypy --strict src
```

Tests run real engines in real child processes. JavaScript tests are skipped when `node` is
missing and `regex` tests when the module is missing.

Want to build one yourself? [regexlab-mcp-the-hard-way](https://github.com/0xmortuex/regexlab-mcp-the-hard-way)
is a seven-module tutorial that builds a minimal version of this server step by step.

## License

MIT
