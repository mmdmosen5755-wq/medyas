import os, sys, pathlib
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("ADMIN_PASSWORD", "testpass123")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/agent_test.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
