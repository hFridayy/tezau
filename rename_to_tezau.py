"""Rename the project to tezau: package translator/ -> tezau/, commands tezau and tezau-mcp,
MCP server "tezau", settings TEZAU_HOME / TEZAU_PROVIDERS / ... (old TRANSLATOR_* names still work),
database tezau.db. Run once from the project folder: python rename_to_tezau.py"""
import os
import re
import subprocess
import sys

if not os.path.isdir("translator"):
    sys.exit("No translator/ folder here - run this from the project folder (or it is already renamed).")
if os.path.exists("tezau"):
    sys.exit("A tezau/ folder already exists - nothing changed.")

# 1. move the package (git mv keeps the file history)
if subprocess.run(["git", "mv", "translator", "tezau"], capture_output=True).returncode != 0:
    os.rename("translator", "tezau")
print("  OK   translator/ -> tezau/")

RULES = [  # (regex, replacement) applied to every file below
    (r"\bfrom translator\b", "from tezau"),
    (r"\bimport translator\b", "import tezau"),
    (r"\btranslator-mcp\b", "tezau-mcp"),
    (r"\bkazakh-translator\b", "tezau"),
    (r"\btranslator\.db\b", "tezau.db"),
    (r"\btranslator\.(cli|mcp_server|providers|engine|memory|glossary|docx_io|evaluation|fake)\b", r"tezau.\1"),
    (r"translator/", "tezau/"),
    (r'packages = \["translator"\]', 'packages = ["tezau"]'),
    (r'^translator = "tezau\.cli:main"', 'tezau = "tezau.cli:main"'),
    (r"TRANSLATOR_(HOME|DB|PROVIDERS|MODELS)", r"TEZAU_\1"),
    (r"mcp__kazakh-translator__", "mcp__tezau__"),
    (r"the `translator` command", "the `tezau` command"),
]
CLI_RULES = [  # only in tezau/cli.py: the command name in help texts
    (r"The `translator` command", "The `tezau` command"),
    (r"^(\s+)translator ", r"\1tezau "),
    (r"`translator <command>", "`tezau <command>"),
    (r'prog="translator"', 'prog="tezau"'),
    (r'getLogger\("translator"\)', 'getLogger("tezau")'),
]

files = ["pyproject.toml", "selfcheck.py", "README.md", ".claude/settings.json"]
for folder in ("tezau", "tests", "docs"):
    for root, _, names in os.walk(folder):
        files += [os.path.join(root, n) for n in names if n.endswith((".py", ".md"))]

for path in files:
    if not os.path.exists(path):
        continue
    code = original = open(path, encoding="utf-8").read()
    for pattern, new in RULES + (CLI_RULES if path.endswith(os.path.join("tezau", "cli.py")) else []):
        code = re.sub(pattern, new, code, flags=re.M)
    if path.endswith("test_cli.py"):
        code = code.replace("The `translator` command", "The `tezau` command")
    if code != original:
        open(path, "w", encoding="utf-8").write(code)
        print(f"  OK   {path}")

# 2. MCP server: read TEZAU_* settings, falling back to the old TRANSLATOR_* names
path = os.path.join("tezau", "mcp_server.py")
code = open(path, encoding="utf-8").read()
code = code.replace('''# ------------------------------------------------------------------ settings
''', '''# ------------------------------------------------------------------ settings

def setting(name: str, default: str = None) -> str:
    """TEZAU_<name> from the environment or .env; the old TRANSLATOR_<name> still works."""
    return os.environ.get(f"TEZAU_{name}") or os.environ.get(f"TRANSLATOR_{name}") or default

''', 1)
for old, new in [('os.environ.get("TEZAU_HOME", "~/.tezau")', 'setting("HOME", "~/.tezau")'),
                 ('os.environ.get("TEZAU_DB")', 'setting("DB")'),
                 ('os.environ.get("TEZAU_MODELS", "")', 'setting("MODELS", "")'),
                 ('os.environ.get("TEZAU_PROVIDERS")', 'setting("PROVIDERS")')]:
    print(f"  {'OK  ' if old in code else 'MISS'} mcp_server: {new}")
    code = code.replace(old, new)
open(path, "w", encoding="utf-8").write(code)

# 3. package description
init = os.path.join("tezau", "__init__.py")
code = open(init, encoding="utf-8").read().splitlines()
code[0] = ('"""tezau: translate 75+ page Word documents from Russian to Kazakh in minutes, for under $1, '
           'with consistent terminology."""')
open(init, "w", encoding="utf-8").write("\n".join(code) + "\n")

left = [p for p in files if os.path.exists(p) and re.search(r"from translator\b|import translator\b|translator-mcp",
                                                            open(p, encoding="utf-8").read())]
print("rename:", "OK" if not left else f"CHECK ME - old names left in {left}")
