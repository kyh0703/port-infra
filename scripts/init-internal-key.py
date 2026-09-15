"""Initialize an owner-readable local service key without printing its value."""

import argparse
import os
import re
import secrets
import tempfile
from pathlib import Path


def initialize(path: Path) -> bool:
    path = path.resolve()
    template = Path(__file__).resolve().parents[1] / ".env.example"
    content = path.read_text() if path.exists() else template.read_text()
    lines = content.splitlines(keepends=True)
    pattern = re.compile(r"^\s*(?:export\s+)?INTERNAL_SERVER_KEY\s*=(.*)$")
    matches = [(index, pattern.match(line.rstrip("\r\n"))) for index, line in enumerate(lines)]
    matches = [(index, match) for index, match in matches if match]
    if len(matches) > 1:
        raise ValueError("Duplicate INTERNAL_SERVER_KEY entries; no settings were changed")
    value = matches[0][1].group(1).strip() if matches else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if value:
        if re.fullmatch(r"[\x21-\x7e]{32,256}", value) is None:
            raise ValueError("Invalid INTERNAL_SERVER_KEY; no settings were changed")
        os.chmod(path, 0o600)
        return False
    key_line = f"INTERNAL_SERVER_KEY={secrets.token_hex(32)}\n"
    if matches:
        lines[matches[0][0]] = key_line
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(key_line)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".internal-key-", delete=False) as file:
            temporary = Path(file.name)
            file.write("".join(lines))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    arguments = parser.parse_args()
    try:
        created = initialize(arguments.env_file)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    print("Internal server key initialized" if created else "Internal server key already configured")
