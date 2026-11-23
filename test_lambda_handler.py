import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

from botocore.exceptions import ClientError

import lambda_handler as lh

NOW = datetime(2026, 11, 24, 9, 5, tzinfo=lh.KST)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": ""}}, "op")


class FakeS3:
    def __init__(self, files=None, fail_on=None):
        self.files, self.fail_on, self.uploads = dict(files or {}), fail_on, []

    def download_file(self, bucket, key, filename):
        if key == self.fail_on:
            raise _err("AccessDenied")
        if key not in self.files:
            raise _err("404")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(self.files[key])

    def upload_file(self, filename, bucket, key):
        self.uploads.append(key)
        self.files[key] = open(filename, encoding="utf-8").read()


class FakeDDB:
    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item, ConditionExpression=None):
        pk = Item["pk"]["S"]
        if ConditionExpression and pk in self.items:
            raise _err("ConditionalCheckFailedException")
        self.items[pk] = Item


class FakeSSM:
    def get_parameters_by_path(self, Path, WithDecryption, NextToken=None):
        assert WithDecryption
        return {"Parameters": [{"Name": f"{Path}/LH_TEST_SECRET", "Value": "s3cret"}]}


class FakeMain:
    def __init__(self, write_state=None, raises=None):
        self.calls, self.write_state, self.raises = [], write_state, raises

    def __call__(self, argv, now):
        self.calls.append(argv)
        self.saw_kill = os.path.exists("KILL")
        if self.write_state:
            json.dump(self.write_state, open("state.json", "w", encoding="utf-8"))
        if self.raises:
            raise self.raises


def _run(env, **kw):
    """Run in a temp workdir and restore the cwd (other tests read config.toml relative to the repo)."""
    cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as d:
            return lh.run(env={"BOT_WORKDIR": os.path.join(d, "bot"), **env}, now=NOW, **kw)
    finally:
        os.chdir(cwd)
        os.environ.pop("LH_TEST_SECRET", None)


def _aws_env(dry):
    return {"STATE_BUCKET": "b", "TABLE_NAME": "t", "PARAM_PATH": "/tradingbot/test", "DRY_RUN": dry}


def test_dry_run_flag_reaches_trading_main():
    # trading.main decides with `"--dry-run" in argv`, so the flag must be in argv. Unset or unknown = dry (fail-safe).
    for dry_env, expect_dry in [({}, True), ({"DRY_RUN": "1"}, True), ({"DRY_RUN": "true"}, True), ({"DRY_RUN": "0"}, False)]:
        main = FakeMain()
        out = _run(dry_env, main=main)
        assert ("--dry-run" in main.calls[0]) == expect_dry, (dry_env, main.calls)
        assert out["dry_run"] == expect_dry


def test_dry_run_on_aws_never_claims_or_writes():
    s3, ddb, main = FakeS3({"state.json": '{"orders": {}}'}), FakeDDB(), FakeMain(write_state={"orders": {"1": {"qty": 1}}})
    _run(_aws_env("1"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=main)
    assert main.calls == [["trading.py", "--dry-run"]]
    assert ddb.items == {} and s3.files == {"state.json": '{"orders": {}}'}


def test_second_real_run_same_day_never_calls_main():
    s3, ddb = FakeS3(), FakeDDB()
    first, second = FakeMain(), FakeMain()
    assert _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=first)["status"] == "ok"
    assert _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=second)["status"] == "already_ran"
    assert first.calls == [["trading.py"]] and second.calls == []
    assert "RUN#2026-11-24" in ddb.items


REAL_STATE = '{"bot_cash": 123.0, "orders": {}}'


def test_exit_at_date_claim_uploads_nothing():
    s3, ddb, main = FakeS3({"state.json": REAL_STATE}), FakeDDB(), FakeMain()
    ddb.items["RUN#2026-11-24"] = {}  # an earlier invocation already claimed today
    assert _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=main)["status"] == "already_ran"
    assert main.calls == [] and s3.uploads == [] and s3.files == {"state.json": REAL_STATE}


def test_failed_download_uploads_nothing():
    # state.json downloads fine, then a later file fails: the half-downloaded workdir must never be written back
    s3, ddb, main = FakeS3({"state.json": REAL_STATE}, fail_on="last_run.txt"), FakeDDB(), FakeMain()
    try:
        _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=main)
        raise AssertionError("download error was swallowed")
    except ClientError as e:
        assert lh._code(e) == "AccessDenied"
    assert main.calls == [] and s3.uploads == [] and s3.files == {"state.json": REAL_STATE}
    assert list(ddb.items) == ["RUN#2026-11-24"]  # the day stays claimed: no orders, no retry without deleting it


def test_state_and_orders_saved_when_main_raises():
    order = {"ticker": "015760", "side": "buy", "qty": 3, "day": "20261124", "status": "pending",
             "booked_qty": 0, "booked_value": 0.0}
    s3, ddb = FakeS3(), FakeDDB()
    main = FakeMain(write_state={"bot_cash": 1.0, "orders": {"0001": order}}, raises=RuntimeError("boom"))
    try:
        _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=main)
        raise AssertionError("error was swallowed")
    except RuntimeError:
        pass
    assert json.loads(s3.files["state.json"])["orders"]["0001"]["ticker"] == "015760"
    assert ddb.items["ORDER#0001"]["qty"] == {"N": "3"} and ddb.items["ORDER#0001"]["side"] == {"S": "buy"}


def test_only_new_or_changed_orders_are_written():
    # provisioned 1 WCU: rewriting every order ever placed would eventually throttle
    o1, o2 = {"qty": 1, "status": "filled"}, {"qty": 2, "status": "pending"}
    s3, ddb = FakeS3({"state.json": json.dumps({"orders": {"1": o1, "2": o2}})}), FakeDDB()
    main = FakeMain(write_state={"orders": {"1": o1, "2": {**o2, "status": "filled"}, "3": {"qty": 3, "status": "pending"}}})
    _run(_aws_env("0"), s3=s3, ddb=ddb, ssm=FakeSSM(), main=main)
    assert sorted(ddb.items) == ["ORDER#2", "ORDER#3", "RUN#2026-11-24"]
    assert ddb.items["ORDER#2"]["status"] == {"S": "filled"}


def test_import_does_not_load_pandas():
    # Lambda's INIT phase (module import) has a 10 s limit; pandas alone exceeded it on a cold start
    heavy = "{'trading', 'notifier', 'data', 'pandas'}"
    code = f"import sys, lambda_handler; loaded = {heavy} & set(sys.modules); assert not loaded, loaded"
    subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.abspath(lh.__file__)), check=True)


def test_stale_kill_from_warm_container_is_cleared():
    with tempfile.TemporaryDirectory() as d:
        work = os.path.join(d, "bot")
        os.makedirs(work)
        open(os.path.join(work, "KILL"), "w").write("old")  # deleted from S3 since, so it must not survive
        main, cwd = FakeMain(), os.getcwd()
        try:
            lh.run(env={**_aws_env("0"), "BOT_WORKDIR": work}, s3=FakeS3(), ddb=FakeDDB(), ssm=FakeSSM(), main=main, now=NOW)
        finally:
            os.chdir(cwd)
            os.environ.pop("LH_TEST_SECRET", None)
        assert main.saw_kill is False


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
