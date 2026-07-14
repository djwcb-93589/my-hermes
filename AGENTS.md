# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Repository scope

Modular Python implementation of "Hermes," an autonomous LLM agent with scheduled-task support. The agent lives in the `hermes/` package (~35 files). `main.py` is the thin entry point — argv dispatch plus the bare CLI REPL. `s15_scheduled_tasks.py` at the root is the **legacy single-file version** the package was split from; it's kept for reference but is not what runs.

Dependencies (in `pyproject.toml`): `openai`, `pyyaml` (plus `fastapi`/`uvicorn` declared but not yet used by the agent itself).

## Commands

All commands go through `main.py`:

```
python main.py            # CLI REPL (default): input → run_conversation → output
python main.py --gateway  # Async gateway + ConsoleAdapter
python main.py --simulate # Gateway + scripted SimulatedAdapter (demos batching/dedup)
python main.py --test     # Built-in unit tests (no pytest)
```

`main.py` only knows about argv dispatch and the raw REPL loop. Each non-default mode imports its module lazily inside the dispatch branch.

The `--test` mode is the only "test suite." Tests live in `hermes/tests.py` and assert on parser, `JobStore` CRUD + persistence, `JobScheduler` firing, `handle_cron_tool`, plus text-utils and `LocalBackend`. **Windows caveat:** the bash-dependent tests (`LocalBackend.execute` and downstream) fail because `subprocess.Popen(text=True)` decodes bash's UTF-8 output as GBK. This is a pre-existing platform issue, not a regression — the legacy `s15_scheduled_tasks.py` fails identically. All non-bash tests pass on Windows.

Required env (or set in `~/.hermes/.env`): `OPENAI_API_KEY`. Optional: `OPENAI_BASE_URL`, `MODEL`, `HERMES_HOME`, `DB_PATH`, plus `FALLBACK_*` for the fallback model.

## Package layout

Strict layering — each layer may only import from layers below it:

```
foundation      hermes/config.py        DEFAULT_CONFIG, load_env, load_config, save_config
                                        HERMES_HOME, all module constants, OpenAI client
                hermes/db.py            init_db, create_session, add_message, get_session_messages
                hermes/security.py      DANGEROUS_PATTERNS, detect/approve, allowlist

execution       hermes/backends/        BaseExecutionEnvironment + Local/Docker/SSH backends
                                        _SECRET_BLOCKLIST, create_backend, get_backend

tools           hermes/tools/           ToolRegistry + register_all()
                  terminal.py           run_terminal + approval gate
                  file.py               read_file / write_file
                  memory.py             memory store + handle_memory
                  skill.py              skill discovery + view/manage
                  delegate.py           child agent + handle_delegate

cron            hermes/cron/            (separate subpackage; tool registers via tools.register_all)
                  parser.py             parse_schedule, _parse_duration, _parse_cron_field
                  job.py                CronJob dataclass
                  store.py              JobStore + get_job_store/set_job_store singleton
                  scheduler.py          JobScheduler (background thread)
                  tool.py               handle_cron_tool + register

agent core      hermes/tokens.py        estimate_tokens, compress
                hermes/errors.py        classify_error, jittered_backoff, switch_to_fallback
                hermes/prompt.py        find_project_context, build_system_prompt
                hermes/conversation.py  run_conversation + ENABLED_TOOLSETS

transport       hermes/gateway/
                  types.py              MessageType, SessionSource, MessageEvent, build_session_key
                  text_utils.py         utf16_len, truncate_utf16, MessageDeduplicator, TextBatcher
                  cache.py              cache_image, cache_audio
                  runner.py             GatewayRunner
                  adapters/             BasePlatformAdapter + Console/Simulated adapters

entry modes     hermes/gateway_console.py     --gateway mode
                hermes/gateway_simulated.py   --simulate mode
                hermes/tests.py               --test mode
```

`main.py` sits at the top, calling into entry modes and `register_all()`.

## Architecture notes

**Module-load side effects:** importing `hermes.config` triggers `load_env()` and `load_config()` immediately — the OpenAI client is constructed with the resolved values. This matches the original s15 file's behavior. Don't import config at the top of a module unless you want this side effect on first import.

**Tool registration is explicit:** each tool module exposes `register(registry)`. `hermes/tools/__init__.py::register_all()` calls each one. `main.py::cli_loop()` calls `register_all()` before the REPL starts. If you add a tool module, you must (a) write a `register(registry)` function in it and (b) add a line to `register_all()`. Otherwise the tool silently won't appear.

**HERMES_HOME (`~/.hermes` by default):** profile directory holding runtime state. Layout: `SOUL.md` (system prompt prefix, capped at 20k chars), `config.yaml`, `.env`, `allowlist.json` (dangerous-command approvals), `memories/MEMORY.md` + `memories/USER.md`, `skills/<name>/SKILL.md`, `jobs.json` (cron jobs). `state.db` (SQLite) lives in CWD by default, not HERMES_HOME.

**Configuration precedence:** env vars win over `config.yaml`, which wins over `DEFAULT_CONFIG`. `config.yaml` can use `${VAR}` references that get expanded against the environment after merging.

**Secret blocklist:** `_SECRET_BLOCKLIST` (in `backends/__init__.py`) strips Hermes's own API keys from the subprocess env. Don't bypass `BaseExecutionEnvironment` when running shell commands.

**Scheduler thread vs. asyncio:** `JobScheduler` runs in a daemon thread so it works in both CLI (sync) and Gateway (async) modes. The bridge from the thread into the gateway's event loop is `asyncio.run_coroutine_threadsafe` (see `hermes/gateway_console.py::fire_gateway`).

**Session isolation:** CLI uses a random UUID as `session_id`; gateway uses the platform-scoped `session_key` (e.g. `agent:main:console:dm:console_user`) directly as the SQLite session id. The cron tool's `session_key` argument pins fired jobs to the originating session.

**SQLite schema:** `sessions(id, source, started_at)` and `messages(id, session_id, role, content, tool_calls, tool_call_id, timestamp)`. WAL mode. `GatewayRunner._run_agent` opens a fresh connection per inbound message — it does not share the CLI's `conn`.

**Persistence atomicity:** `JobStore._save` writes to a `.tmp` file then `os.replace()`s into place. (`Path.rename` would fail on Windows when the target exists; `os.replace` is atomic on both.)

## Things to keep in mind

- The OpenAI client targets OpenRouter by default (`anthropic/Codex-sonnet-4`), but any OpenAI-compatible endpoint works. Don't assume Anthropic SDK semantics.
- The legacy `s15_scheduled_tasks.py` (2,843 lines) is unmaintained — if you change behavior, change it in `hermes/` only.
- `run_conversation` (CLI) and `run_child_conversation` (in `tools/delegate.py`) share structure but are deliberately not unified. Don't dedupe without checking that child-agent restrictions (no recursion, no memory writes, no skill edits via `DELEGATE_BLOCKED_TOOLS`) still hold.
- The `--gateway` and `--simulate` modes use `asyncio.run(...)` at the top of `main.py`'s dispatch; the scheduler thread inside still uses synchronous callbacks.
- `pyproject.toml` declares `fastapi`/`uvicorn` but no code imports them yet — they're forward-declared for a future HTTP adapter, not currently wired up.

## Architecture and Code Quality

When writing or modifying code, follow these principles:

- Follow high cohesion and low coupling.
- Keep each module focused on one clear responsibility.
- Avoid mixing unrelated concerns in the same function, class, or file.
- Avoid unnecessary cross-module dependencies.
- Prefer explicit interfaces between modules instead of relying on hidden global state.
- When making larger changes, briefly explain whether the design keeps module boundaries clear.
- After code changes, check whether the change introduced unnecessary coupling.
- 代码注释必须使用中文。
- 不要把代码标识符翻译成中文。函数名、变量名、类名、文件名、API 名称、错误信息、命令行输出保持原文。