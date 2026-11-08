import asyncio
import contextlib
import json
import time

import pytest
from mcp import Client

from scripthost.config import Config
from scripthost.server import build


def data(result):
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@contextlib.asynccontextmanager
async def host(tmp_path):
    config = Config(data_dir=tmp_path, auth_mode="none", default_test_timeout=30)
    mcp, store, engine = build(config)
    await engine.startup()
    try:
        async with Client(mcp) as client:
            yield client, store, engine
    finally:
        await engine.shutdown()


async def test_create_run_update_logs_files(tmp_path):
  async with host(tmp_path) as env:
      client, store, engine = env
      res = data(await client.call_tool("create_project", {
          "name": "hello",
          "description": "Greets the heat pump and writes a state file",
          "files": {"main.py": "import sys, pathlib\nprint('hello', *sys.argv[1:])\npathlib.Path('state.txt').write_text('ok')\n"},
      }))
      assert res["dependencies"] == "installed"

      run = data(await client.call_tool("run_once", {"name": "hello", "args": ["world"]}))
      assert run["status"] == "succeeded", run
      assert "hello world" in run["output"]

      f = data(await client.call_tool("read_file", {"name": "hello", "path": "state.txt"}))
      assert f["content"] == "ok"

      hits = data(await client.call_tool("search_projects", {"query": "heat pump"}))
      assert hits["results"][0]["name"] == "hello"

      upd = data(await client.call_tool("update_project", {
          "name": "hello", "files": {"main.py": "import sys\nprint('v2'); sys.exit(3)\n"}, "message": "v2",
          "env": {"FOO": "bar"}, "secret_env": {"TOKEN": "s3cret"},
      }))
      assert upd["project"]["revision"] == 2
      assert upd["project"]["secret_env_keys"] == ["TOKEN"]
      assert "s3cret" not in json.dumps(upd)

      run = data(await client.call_tool("run_once", {"name": "hello"}))
      assert run["status"] == "failed" and run["exit_code"] == 3 and "v2" in run["output"]

      rb = data(await client.call_tool("rollback_project", {"name": "hello", "revision": 1}))
      assert rb["revision"] == 3
      proj = data(await client.call_tool("get_project", {"name": "hello"}))
      assert "hello" in proj["files"]["main.py"]

      logs = data(await client.call_tool("get_logs", {"name": "hello", "grep": "v2"}))
      assert "v2" in logs["log"]


async def test_requirements_and_secret_env(tmp_path):
  async with host(tmp_path) as env:
      client, *_ = env
      res = data(await client.call_tool("create_project", {
          "name": "deps",
          "description": "uses a dependency",
          "files": {"main.py": "import os, six\nprint(six.__name__, os.environ['TOKEN'])\n"},
          "requirements": ["six"],
          "secret_env": {"TOKEN": "abc"},
      }))
      assert res["dependencies"] == "installed", res
      run = data(await client.call_tool("run_once", {"name": "deps"}))
      assert run["status"] == "succeeded", run
      assert "six abc" in run["output"]


async def test_service_restart_policy(tmp_path):
  async with host(tmp_path) as env:
      client, store, engine = env
      data(await client.call_tool("create_project", {
          "name": "flaky",
          "description": "crashes immediately",
          "files": {"main.py": "print('boom'); raise SystemExit(1)\n"},
          "restart_policy": "on-failure",
      }))
      data(await client.call_tool("start_service", {"name": "flaky"}))
      await asyncio.sleep(4)
      status = data(await client.call_tool("get_status", {"name": "flaky"}))
      assert status["service"]["restarts"] >= 1
      assert store.get("flaky").service_enabled
      data(await client.call_tool("stop_service", {"name": "flaky"}))
      assert not store.get("flaky").service_enabled

      data(await client.call_tool("create_project", {
          "name": "daemon",
          "description": "long running",
          "files": {"main.py": "import time\nwhile True:\n    print('tick', flush=True); time.sleep(0.2)\n"},
          "restart_policy": "always",
          "start_service": True,
      }))
      await asyncio.sleep(1.5)
      st = data(await client.call_tool("get_status", {"name": "daemon"}))
      assert st["service"]["state"] == "running"
      upd = data(await client.call_tool("update_project", {"name": "daemon", "env": {"X": "1"}}))
      assert upd["service_restarted"]
      data(await client.call_tool("stop_service", {"name": "daemon"}))
      runs = data(await client.call_tool("list_runs", {"name": "daemon"}))["runs"]
      assert all(r["status"] == "stopped" for r in runs), [(r["status"], r["exit_code"], r["note"]) for r in runs]


async def test_cron_and_timeout(tmp_path):
  async with host(tmp_path) as env:
      client, store, engine = env
      data(await client.call_tool("create_project", {
          "name": "periodic",
          "description": "cron job",
          "files": {"main.py": "print('cron ran')\n"},
          "cron": "* * * * *",
      }))
      # force the schedule to be due now
      engine._next_cron["periodic"] = time.time()
      await asyncio.sleep(2.5)
      runs = data(await client.call_tool("list_runs", {"name": "periodic"}))["runs"]
      assert runs and runs[0]["trigger"] == "cron"

      data(await client.call_tool("create_project", {
          "name": "slow", "description": "sleeps", "files": {"main.py": "import time; time.sleep(60)\n"},
      }))
      run = data(await client.call_tool("run_once", {"name": "slow", "timeout": 1}))
      assert run["status"] == "timeout"


async def test_path_safety_and_errors(tmp_path):
  async with host(tmp_path) as env:
      client, *_ = env
      data(await client.call_tool("create_project", {"name": "p", "description": "d", "files": {"main.py": "pass\n"}}))
      bad = await client.call_tool("read_file", {"name": "p", "path": "../../scripthost.db"})
      assert bad.is_error
      bad = await client.call_tool("create_project", {"name": "q", "description": "d", "files": {"x.py": ""}})
      assert bad.is_error and "entrypoint" in bad.content[0].text
      bad = await client.call_tool("create_project", {"name": "Bad Name", "description": "d", "files": {"main.py": ""}})
      assert bad.is_error
      gone = await client.call_tool("delete_project", {"name": "p", "confirm": True})
      assert not gone.is_error
