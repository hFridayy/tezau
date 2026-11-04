# Using the translator from Claude (MCP)

The `tezau-mcp` server gives Claude (or any MCP client) these tools:

| Tool | What it does |
|---|---|
| `estimate` | pages, tokens and cost before spending anything (memory hits are free) |
| `translate` | translates a .docx in the background and returns a job id |
| `job_status` | progress, then the result: pages, time, cost |
| `check` | quality report: automatic checks plus an optional AI judge |
| `translate_text` | quick translation of a sentence or paragraph, using memory and the glossary |
| `terms_list` / `term_set` | read the glossary / save your own decision (`заявки -> өтінімдер`) |

Resource: `glossary://terms`, the glossary as text.

## 1. Install

```bash
pip install -e ".[all]"          # from the project folder (includes the mcp library)
which tezau-mcp             # copy this full path, you need it below
```

## 2. Keys and settings

The server reads everything from one folder, **`TEZAU_HOME`** (default `~/.tezau`):

| File | Contents |
|---|---|
| `.env` | `GEMINI_API_KEY=...` (plus `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` if you have them) |
| `prices.json` | optional, for cost reports |
| `tezau.db` | the translation memory and glossary (created automatically) |

To share memory and terms with the command line, set `TEZAU_HOME` to your project folder.

Optional `.env` settings:
```
TEZAU_PROVIDERS=gemini        # default; "all" = every provider with a key; or "gemini,openai"
TEZAU_MODELS=claude=claude-haiku-5-5,openai=gpt-5-mini
```

## 3. Connect a client

**Claude Code (Linux, WSL, macOS):**
```bash
claude mcp add tezau --scope user \
  -e TEZAU_HOME=$HOME/translator_app -- $(which tezau-mcp)
claude mcp list                  # should show tezau as connected
```

**Claude Desktop on macOS:** Settings → Developer → Edit Config
(`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "tezau": {
      "command": "/Users/YOU/translator_app/venv/bin/tezau-mcp",
      "env": { "TEZAU_HOME": "/Users/YOU/translator_app" }
    }
  }
}
```

**Claude Desktop on Windows, code in WSL:**
```json
{
  "mcpServers": {
    "tezau": {
      "command": "wsl",
      "args": ["-e", "env", "TEZAU_HOME=/home/YOU/translator_app",
               "/home/YOU/translator_app/venv/bin/tezau-mcp"]
    }
  }
}
```
Windows paths you type (`C:\Users\YOU\Documents\file.docx`) are converted to `/mnt/c/...` automatically.

Restart Claude Desktop after editing the config.

## 4. Try it

> How much would it cost to translate /home/YOU/translator_app/rusfile.docx?
>
> Translate it into Kazakh, voting on terms first. Tell me when it's done.
>
> Check the translation.
>
> In our documents заявки is always өтінімдер. Save that.

For a free test without keys, ask for a dry run: *"translate it with the fake provider"*.

## Notes

- **Long documents:** `translate` returns immediately and runs in the background, so a 75-page
  document never hits a tool timeout. If the client closes mid-run, the finished pages are saved,
  and the same request continues where it stopped.
- **Privacy:** the Gemini free tier may use submitted content to improve Google's products.
  For confidential documents, use a paid key.
- **Logs** go to stderr (stdout is the MCP channel). Reports from `check` are saved in
  `TEZAU_HOME/results/`.
