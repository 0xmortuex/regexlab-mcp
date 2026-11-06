# Changelog

## 0.1.0 - 2026-11-06

First release.

- Seven tools: `test_regex`, `explain_regex`, `check_backtracking`, `compare_engines`,
  `convert_pattern`, `replace_preview`, `suggest_tests`.
- Engines: Python `re` (always), JavaScript RegExp through `node` (when installed), the
  third-party `regex` module (when installed). Every match runs in its own child process
  with a hard timeout and a process-tree kill (POSIX process group, Windows Job Object plus
  `taskkill /T`); 1 GiB address-space cap on Linux and a Job Object memory cap on Windows.
- Own regex parser for the common Python/JavaScript syntax subset, feeding the explainer,
  the static ReDoS scan, the converter and the test generator. Unsupported constructs are
  reported, never guessed.
- ReDoS: static scan (nested repeats, ambiguous alternation in a loop, adjacent overlapping
  repeats, unanchored leading repeat) plus a timed probe on crafted input that is killed at
  the budget.
