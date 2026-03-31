# Plan: Minimal Test Fixture for DAR Pipeline Testing

## Problem
Current fixture (runtime-audit-fixture) is expensive to test against:
- 31 files, 18.7 KB total source code
- ~270K tokens per full pipeline run
- 50 worker calls (2 Reader + 28 Verifier + 20 IssueComposer)
- Takes ~5-10 minutes per run,- Costs ~$1-5 per run depending on model

Need a minimal fixture that:
1. Triggers ALL 25 DAR detection rules
2. Is cheap to run (~50-80K tokens, ~10-15 worker calls)
3. Exercises the full pipeline (Reader -> Verifier -> IssueComposer -> Report)
4. Is a valid git repo

## Design: Single-File Fixture

### Repository: `dar-test-fixture-mini/`

**One source file** (`app.py`) containing targeted vulnerabilities:

```python
"""Minimal vulnerable app for DAR pipeline testing."""
import hashlib
import pickle
import base64
import subprocess
import yaml
import marshal
import random
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Secret Exposure (3 rules) ---
SECRET_KEY = "default-secret-key-for-testing"
password = "admin123!@#"
api_key = "sk_live_key_abcdef0123456789"

# --- Configuration (2 rules) ---
DEBUG = True
CORS_ORIGINS = "*"

# --- Weak Cryptography (3 rules) ---
def hash_password(pwd):
    return hashlib.md5(pwd.encode()).hexdigest()

def hash_token(data):
    return hashlib.sha1(data.encode()).hexdigest()

def generate_code():
    return random.randint(100000, 999999)

# --- Dangerous Deserialization (3 rules) ---
def load_data(raw):
    return pickle.loads(base64.b64decode(raw))

def load_config(text):
    return yaml.load(text)

def load_cache(data):
    return marshal.loads(data)

# --- Code Execution (3 rules) ---
def run_custom(expr):
    return eval(expr)

def run_script(code):
    exec(code)

def shell_cmd(cmd):
    return subprocess.run(cmd, shell=True)

# --- SQL Injection (3 rules) ---
def get_user(uid):
    query = f"SELECT * FROM users WHERE id = {uid}"
    return query

def search(term):
    q = "SELECT * FROM items WHERE name = '" + term + "'"
    return q

def filter_users(role):
    return "SELECT * FROM users WHERE role = '%s'" % role

# --- Input Trust (1 rule) ---
@app.route("/debug")
def debug_env():
    return jsonify({"env": dict(__import__("os").environ)})
```

### Detection Rule Coverage Map

| Category | Rule | Trigger Line |
|----------|------|-------------|
| **Secret Exposure** | `hardcoded_password` | `password = "admin123!@#"` |
| | `default_secret_key` | `SECRET_KEY = "default-secret-key-for-testing"` |
| | `api_key_in_source` | `api_key = "sk_live_key_abcdef0123456789"` |
| **Configuration** | `flask_debug` | `app.run(debug=True)` (implicit from DEBUG) |
| | `cors_wildcard` | `CORS_ORIGINS = "*"` |
| **Weak Cryptography** | `weak_hash_md5` | `hashlib.md5(...)` |
| | `weak_hash_sha1` | `hashlib.sha1(...)` |
| | `weak_random` | `random.randint(...)` |
| **Deserialization** | `pickle_loads` | `pickle.loads(...)` |
| | `yaml_unsafe_load` | `yaml.load(text)` |
| | `marshal_loads` | `marshal.loads(...)` |
| **Code Execution** | `eval_usage` | `eval(expr)` |
| | `exec_usage` | `exec(code)` |
| | `subprocess_shell` | `subprocess.run(cmd, shell=True)` |
| **SQL Injection** | `sql_fstring` | `f"SELECT ... {uid}"` |
| | `sql_string_concat` | `"SELECT ... '" + term + "'"` |
| | `sql_format_string` | `"SELECT ... '%s'" % role` |
| **Input Trust** | `flask_debug` | `DEBUG = True` (also triggers via app.run) |
| | `base64_as_security` | `base64.b64decode(raw)` in pickle context |

### Why This is Cheaper

| Metric | Current Fixture | Minimal Fixture |
|--------|----------------|----------------|
| Source files | 31 | 1 |
| Source size | 18.7 KB | ~1.5 KB |
| Pattern matches | ~20 (misses ~5 rules) | ~25 (all rules) |
| Worker calls | 50 | ~15-20 |
| Token cost | ~270K | ~60-80K |
| Cost (Sonnet) | ~$1.07 | ~$0.25 |
| Runtime | ~5-10 min | ~2-4 min |

### Implementation Steps

1. Create `dar-test-fixture-mini/` directory
2. Initialize git repo with one commit
3. Create `app.py` with all 25 vulnerability triggers
4. Add a `.env.example` for secret exposure triggers
5. Verify: `python cli.py snapshot-target` succeeds
6. Verify: pattern scanner finds all 25 rules
7. Verify: full pipeline run produces report with findings for all categories

### Follow-up: Script Update

Update `run_codex_manual_auto_target.ps1` (or create a new script) to target the minimal fixture, reducing `--max-iterations` from 50 to 20.
