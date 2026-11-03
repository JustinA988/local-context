# Local Context

Local MCP server for Claude Desktop and Claude Code. It runs on your machine and can:

- list, search, and read files
- run one terminal command
- query SQLite
- run named tasks on a schedule, without calling Claude

Python 3.11+ is the only requirement. The server does not call the Claude API. Tool results are capped (4,000 characters by default, 16,000 max) so a read cannot dump a whole file into the chat.

## Claude Desktop

1. Install [Python 3.11+](https://www.python.org/downloads/). On Windows, `py -3 --version` should work.
2. Open **Settings → Developer → Edit Config**. That opens the config file this install actually reads.
3. Add a server. Use the full path to `run.cmd` on Windows so the Microsoft Store `python` stub is not used:

```json
{
  "mcpServers": {
    "local-context": {
      "command": "C:\\path\\to\\local-context\\run.cmd",
      "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "LOCAL_CONTEXT_ROOT": "C:\\path\\to\\folder\\claude\\may\\use"
      }
    }
  }
}
```

On macOS or Linux, set `command` to `python3` and `args` to `["/path/to/local-context/run.py"]`.

4. Quit Claude completely and open it again.
5. **Settings → Developer → Local MCP servers** should list `local-context`.

`LOCAL_CONTEXT_ROOT` is the folder Claude may read and use as the shell working directory.

## Claude Code

From this directory:

```powershell
claude --plugin-dir .
```

From GitHub, add this repository as a marketplace, then install `local-context@local-context-market`.

## Tools

| Tool | Returns |
| --- | --- |
| `list_dir` | Names in a directory |
| `search_text` | Matching lines |
| `read_file` | A text slice (`offset` continues a capped read) |
| `run_command` | One command, output capped |
| `query_sql` | SQLite rows, read-only unless you opt in |
| `list_sources` / `read_source` | A data source from config |
| `refresh_source` | Status only; command output stays on disk |
| `list_tasks` / `run_task` / `task_status` | Named local tasks |
| `server_status` | Roots, caps, and `api_calls=0` |

## Plug in data

Copy `config.example.json` to `config.json` (gitignored). Source types: `file`, `directory`, `json`, `csv`, `sqlite`, `command`.

A project can also ship `.local-context.json` with file, directory, json, csv, or sqlite sources inside that project. It cannot add shell access, tasks, or command sources.

`query_sql` runs one statement. Writes need `"allow_sql_writes": true` and a sqlite source with `"read_only": false`.

Run due tasks without Claude:

```powershell
py -3 run.py --runner --once
```

`every_seconds: 0` means manual only. Point Task Scheduler or cron at `--once`.

## Limits

The process runs as you. It is not a sandbox.

- Reads and the shell working directory stay under an allowed root, or a source you configured.
- Symlinks are followed only when the real file is still inside that area.
- `.claude`, `.claude.json`, and the Claude Desktop credential folders are blocked.
- Commands do not inherit Anthropic environment variables. Commands that name those credential files, the Anthropic API, encoded PowerShell, disk format, or shutdown are refused.
- A command can still open some other file by absolute path.
- Regex search does not allow `*`, `+`, `?`, or `{`.

## Develop

```powershell
py -3 -m pip install pytest
py -3 -m pytest
```
