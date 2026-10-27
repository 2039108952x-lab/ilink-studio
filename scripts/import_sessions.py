"""把旧的 data/sessions.json 导入 mailbox.db。

    python scripts/import_sessions.py                 # 默认导入 data/sessions.json
    python scripts/import_sessions.py --path 别的文件  # 指定文件

重复执行是安全的：同一个文件导入两次不会多出行，源文件不会被改动。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.importer import FilterImportError, import_sessions_file  # noqa: E402
from gateway.mailbox import Mailbox, MailboxError  # noqa: E402
from gateway.paths import app_paths  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入旧版 sessions.json")
    parser.add_argument("--path", default=None, help="默认 <数据目录>/sessions.json")
    args = parser.parse_args(argv)

    paths = app_paths()
    source = Path(args.path) if args.path else paths.data_dir / "sessions.json"
    mailbox = Mailbox(paths.mailbox_db, backup_dir=paths.data_dir / "backups")
    try:
        report = import_sessions_file(mailbox, source)
    except (FilterImportError, MailboxError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        mailbox.close()
    print(json.dumps({"ok": True, **report.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
