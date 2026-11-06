"""Child-process runner for the Python engines ("python" = re, "regex" = third-party module).

Run as `python -I _runner_py.py`. Reads ONE JSON request from stdin and writes
newline-delimited JSON events to stdout, flushing after each so a parent that has to
kill us still sees everything finished so far. The pattern is only ever handed to
re.compile / regex.compile as data; nothing from the request is evaluated as code.
"""

import json
import sys
import time

MEM_CAP = None
if sys.platform == "linux":
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    MEM_CAP = resource.getrlimit(resource.RLIMIT_AS)[0]

TEXT_CAP = 300


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def clip(s):
    return s if len(s) <= TEXT_CAP else s[:TEXT_CAP] + "..."


FLAG_NAMES = {"i": "IGNORECASE", "m": "MULTILINE", "s": "DOTALL", "x": "VERBOSE", "a": "ASCII"}


def load_engine(name):
    if name == "regex":
        import regex as mod
    else:
        import re as mod
    return mod


def compile_pattern(mod, engine, pattern, flags):
    value = 0
    for f in flags:
        if f in FLAG_NAMES:
            value |= getattr(mod, FLAG_NAMES[f])
        elif f in ("g", "u"):
            continue  # g: we always return every match; u: str patterns are Unicode already
        else:
            out({"t": "error", "kind": "flag",
                 "message": "flag %r is not supported by %s (supported: i m s x a, and g/u as no-ops)"
                 % (f, engine)})
            return None
    try:
        return mod.compile(pattern, value)
    except Exception as e:  # re.error, regex.error, RecursionError, OverflowError
        out({"t": "error", "kind": "compile", "message": "%s: %s" % (type(e).__name__, e)})
        return None


def group_info(m):
    groups = []
    for k in range(1, (m.re.groups or 0) + 1):
        if m.start(k) < 0:
            groups.append(None)
        else:
            groups.append({"start": m.start(k), "end": m.end(k), "text": clip(m.group(k))})
    names = {}
    for name in m.re.groupindex:
        g = m.group(name)
        names[name] = None if g is None else clip(g)
    return groups, names


def op_match(req, mod, pat):
    cap = req["max_matches"]
    for i, subject in enumerate(req["subjects"]):
        matches = []
        truncated = False
        try:
            for m in pat.finditer(subject):
                if len(matches) >= cap:
                    truncated = True
                    break
                groups, names = group_info(m)
                matches.append({"start": m.start(), "end": m.end(), "text": clip(m.group(0)),
                                "groups": groups, "named": names})
        except MemoryError:
            out({"t": "error", "kind": "memory", "message": "out of memory while matching case %d" % i})
            return
        out({"t": "case", "i": i, "matches": matches, "truncated": truncated})


def op_sub(req, mod, pat):
    subject, repl = req["subject"], req["replacement"]
    cap = req["max_matches"]
    items = []
    truncated = False
    try:
        for m in pat.finditer(subject):
            if len(items) >= cap:
                truncated = True
                break
            items.append({"start": m.start(), "end": m.end(), "text": clip(m.group(0)),
                          "replacement": clip(m.expand(repl))})
        result = pat.sub(repl, subject)
    except MemoryError:
        out({"t": "error", "kind": "memory", "message": "out of memory"})
        return
    except Exception as e:  # bad group reference / bad escape in the replacement template
        out({"t": "error", "kind": "replacement", "message": "%s: %s" % (type(e).__name__, e)})
        return
    out({"t": "sub", "matches": items, "truncated": truncated, "result": result[:20000],
         "result_truncated": len(result) > 20000})


def op_probe(req, mod, pat):
    skipped_groups = set()
    for item in req["inputs"]:
        g = item["group"]
        if g in skipped_groups:
            continue
        subject = item["prefix"] + item["unit"] * item["n"] + item["suffix"]
        t0 = time.perf_counter()
        m = pat.search(subject)
        ms = (time.perf_counter() - t0) * 1000.0
        out({"t": "probe", "group": g, "n": item["n"], "length": len(subject),
             "ms": ms, "matched": m is not None})
        if ms > req["stop_ms"]:
            skipped_groups.add(g)


def main():
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    engine = req["engine"]
    try:
        mod = load_engine(engine)
    except ImportError as e:
        out({"t": "error", "kind": "unavailable", "message": "cannot import %s: %s" % (engine, e)})
        return
    pyver = ".".join(map(str, sys.version_info[:3]))
    version = mod.__version__ if engine == "regex" else pyver  # re.__version__ is a stale 2.2.1
    out({"t": "hello", "engine": engine, "version": version,
         "python": ".".join(map(str, sys.version_info[:3])), "mem_cap": MEM_CAP})
    pat = compile_pattern(mod, engine, req["pattern"], req["flags"])
    if pat is None:
        return
    out({"t": "compiled", "groups": pat.groups, "names": dict(pat.groupindex)})
    {"match": op_match, "sub": op_sub, "probe": op_probe}[req["op"]](req, mod, pat)
    out({"t": "done"})


main()
