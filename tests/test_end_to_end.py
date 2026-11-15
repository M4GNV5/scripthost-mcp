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
          "env": {"FOO": "bar"},
      }))
      assert upd["project"]["revision"] == 2

      run = data(await client.call_tool("run_once", {"name": "hello"}))
      assert run["status"] == "failed" and run["exit_code"] == 3 and "v2" in run["output"]

      rb = data(await client.call_tool("rollback_project", {"name": "hello", "revision": 1}))
      assert rb["revision"] == 3
      proj = data(await client.call_tool("get_project", {"name": "hello"}))
      assert "hello" in proj["files"]["main.py"]

      logs = data(await client.call_tool("get_logs", {"name": "hello", "grep": "v2"}))
      assert "v2" in logs["log"]


async def test_requirements(tmp_path):
  async with host(tmp_path) as env:
      client, *_ = env
      res = data(await client.call_tool("create_project", {
          "name": "deps",
          "description": "uses a dependency",
          "files": {"main.py": "import six\nprint(six.__name__, 'abc')\n"},
          "requirements": ["six"],
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


async def test_secrets_from_dotenv(tmp_path):
    (tmp_path / "secrets.env").write_text(
        "# smart home\nHA_TOKEN=eyJhbGciOiJIUzI1NiJ9.secretvalue\nexport MQTT_PASSWORD='p@ss word'\nSHORT=ab\n"
    )
    async with host(tmp_path) as (client, store, engine):
        listed = data(await client.call_tool("list_secrets", {}))
        assert [s["name"] for s in listed["secrets"]] == ["HA_TOKEN", "MQTT_PASSWORD", "SHORT"]
        assert "secretvalue" not in json.dumps(listed)

        bad = await client.call_tool("create_project", {
            "name": "x", "description": "d", "files": {"main.py": "pass"}, "secrets": ["NOPE"]})
        assert bad.is_error and "HA_TOKEN" in bad.content[0].text  # lists what is available

        script = (
            "import os, pathlib\n"
            "print('token', os.environ['HA_TOKEN'])\n"
            "print('pw', os.environ['MQTT_PASSWORD'])\n"
            "print('unattached', os.environ.get('SHORT'))\n"
            "pathlib.Path('out.txt').write_text(os.environ['HA_TOKEN'])\n"
        )
        res = data(await client.call_tool("create_project", {
            "name": "ha", "description": "talks to home assistant", "files": {"main.py": script},
            "secrets": ["HA_TOKEN", "MQTT_PASSWORD"],
        }))
        assert res["created"]["secrets"] == ["HA_TOKEN", "MQTT_PASSWORD"]

        run = data(await client.call_tool("run_once", {"name": "ha", "env": {"HA_TOKEN": "override"}}))
        assert run["status"] == "succeeded", run
        out = run["output"]
        assert "token ***HA_TOKEN***" in out and "pw ***MQTT_PASSWORD***" in out
        assert "secretvalue" not in out and "p@ss word" not in out
        assert "unattached None" in out  # only attached secrets are injected; run env can't shadow them

        # the real value really is in the process env / on disk, just masked on the way out
        assert (store.work_dir("ha") / "out.txt").read_text() == "eyJhbGciOiJIUzI1NiJ9.secretvalue"
        f = data(await client.call_tool("read_file", {"name": "ha", "path": "out.txt"}))
        assert f["content"] == "***HA_TOKEN***"
        logs = data(await client.call_tool("get_logs", {"name": "ha"}))
        assert "secretvalue" not in logs["log"]

        # file is re-read on change; removing a secret makes runs fail with a clear error
        (tmp_path / "secrets.env").write_text("MQTT_PASSWORD=x1234\n")
        import os, time as _t
        os.utime(tmp_path / "secrets.env", (_t.time() + 5, _t.time() + 5))
        bad = await client.call_tool("run_once", {"name": "ha"})
        assert bad.is_error and "HA_TOKEN" in bad.content[0].text
        listed = data(await client.call_tool("list_secrets", {}))
        assert listed["missing_but_referenced"] == [{"name": "HA_TOKEN", "used_by": ["ha"]}]


def test_parse_dotenv():
    from scripthost.secrets import parse_dotenv
    assert parse_dotenv(
        "A=1\n# c\n\nexport B = two words # comment\nC=\"quoted # not a comment\"\nD='x=y'\nbad line\n"
    ) == {"A": "1", "B": "two words", "C": "quoted # not a comment", "D": "x=y"}
