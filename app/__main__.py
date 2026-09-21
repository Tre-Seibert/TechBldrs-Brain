from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Repo-root .env (secrets stay local; .env is gitignored).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.main import run  # noqa: E402

if __name__ == "__main__":
    run()
