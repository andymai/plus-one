import json
import os
import subprocess
from types import SimpleNamespace

from .store import now

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash"
# rg, fd, and git grep each take a flag that runs a command (--pre, -x, -O), so searching goes through Grep and Glob.
ALLOWED = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff *)",
    "Bash(git show *)",
    "Bash(git log *)",
    "Bash(git blame *)",
    "Bash(git ls-files *)",
    "Bash(git merge-base *)",
    "Bash(ls *)",
    "Bash(wc *)",
]
DENIED = [
    "Bash(gh *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git fetch *)",
    "Bash(git checkout *)",
    "Bash(git reset *)",
    "Bash(git diff *--output*)",
    "Bash(git log *--output*)",
    "Bash(git show *--output*)",
    "Bash(snow *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(rm *)",
    "Bash(mv *)",
]


class ClaudeError(RuntimeError):
    pass


def _env(cfg):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE_CODE_", "CLAUDE_PID", "GH_", "GITHUB_", "DBUS_"))
    }
    gh_empty = cfg.state_dir / "no-gh"
    gh_empty.mkdir(parents=True, exist_ok=True)
    env.update(GH_CONFIG_DIR=str(gh_empty), GIT_TERMINAL_PROMPT="0")
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(cfg.claude_config_dir)
    return env


def call(cfg, role, prompt, schema, *, cwd=None, add_dirs=(), system=None, tools=True):
    model = getattr(cfg.models, role)
    cmd = [
        "claude",
        "-p",
        "--model",
        model.model,
        "--effort",
        model.effort,
        "--output-format",
        "json",
        "--no-session-persistence",
        # --restricted ignores every settings file, so allow rules in the owner's own Claude settings can't widen
        # what the reviewer may run. The login still comes from the config dir.
        "--permission-mode",
        "dontAsk",
        "--restricted",
        "--settings",
        '{"disableAllHooks":true}',
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--max-budget-usd",
        str(model.budget_usd),
        "--json-schema",
        json.dumps(schema),
        "--tools",
        READ_ONLY_TOOLS if tools else "",
    ]
    if tools:
        cmd += ["--allowedTools", ",".join(ALLOWED), "--disallowedTools", ",".join(DENIED)]
    for path in add_dirs:
        cmd += ["--add-dir", str(path)]
    if system:
        cmd += ["--append-system-prompt", system]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_env(cfg),
            timeout=model.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"{role} call timed out after {model.timeout_s}s") from exc
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"{role} call returned no JSON: {proc.stderr.strip()[:300]}") from exc
    if out.get("is_error") or out.get("structured_output") is None:
        raise ClaudeError(
            f"{role} call failed: {out.get('subtype')} {str(out.get('result'))[:300]}"
        )
    usage = out.get("usage") or {}
    return SimpleNamespace(
        data=out["structured_output"],
        cost=out.get("total_cost_usd", 0.0),
        turns=out.get("num_turns", 0),
        duration_s=(out.get("duration_ms") or 0) / 1000,
        model=next(iter(out.get("modelUsage") or {}), model.model),
        usage={
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
        },
    )


def timed(rec, cfg, role, stage, idx, prompt, schema, **kwargs):
    model = getattr(cfg.models, role)
    started = now()
    try:
        result = call(cfg, role, prompt, schema, **kwargs)
    except Exception as exc:
        if rec:
            rec.stage(stage, idx, model.model, model.effort, started, error=str(exc)[:500])
        raise
    if rec:
        rec.stage(stage, idx, result.model, model.effort, started, result=result)
    return result
