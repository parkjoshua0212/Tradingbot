"""AWS Lambda entry point. Wraps trading.main() without changing it.
The files trading.py keeps in its working dir (state.json, last_run.txt, KILL, results/) live in S3 between runs.
A DynamoDB conditional put on the KST date stops a second real run on the same day before any order is sent.
Without STATE_BUCKET it runs in local mode (no SSM/S3/DynamoDB), for the local container check."""
import logging
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")  # pykrx imports matplotlib; HOME is read-only on Lambda

from botocore.exceptions import ClientError  # noqa: E402

import messages  # noqa: E402
import trading  # noqa: E402
from notifier import send_alert  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logging.getLogger().setLevel(logging.INFO)  # Lambda pre-installs a root handler, so basicConfig alone is a no-op there
KST = ZoneInfo("Asia/Seoul")
HERE = os.path.dirname(os.path.abspath(__file__))
SYNCED = ["state.json", "last_run.txt", "KILL", "results/daily_results.csv", "results/trades.csv"]


def is_dry_run(env) -> bool:
    """Fail-safe: only DRY_RUN=0 sends paper orders. Unset or any other value is a dry run."""
    return env.get("DRY_RUN", "1") != "0"


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def load_secrets(ssm, path: str) -> None:
    """Every SecureString under path -> env var named after its last segment (.../KIS_MOCK_APP_KEY)."""
    kw = {"Path": path, "WithDecryption": True}
    while True:
        page = ssm.get_parameters_by_path(**kw)
        for p in page["Parameters"]:
            os.environ[p["Name"].rsplit("/", 1)[1]] = p["Value"]
        if not page.get("NextToken"):
            return
        kw["NextToken"] = page["NextToken"]


def claim_day(ddb, table: str, day: str, now: datetime) -> bool:
    """Atomic once-per-day guard. False if this KST date was already claimed (Scheduler is at-least-once)."""
    try:
        ddb.put_item(TableName=table, Item={"pk": {"S": f"RUN#{day}"}, "claimed_at": {"S": now.isoformat()}},
                     ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if _code(e) == "ConditionalCheckFailedException":
            return False
        raise


def save_orders(ddb, table: str, orders: dict) -> None:
    """One item per order, overwritten each run (idempotent): the latest booked status wins.
    shortcut: rewrites every order ever placed, a few per day; write only changed orders if this grows."""
    for oid, o in orders.items():
        item = {k: {"N": str(v)} if isinstance(v, (int, float)) else {"S": str(v)} for k, v in o.items()}
        ddb.put_item(TableName=table, Item={**item, "pk": {"S": f"ORDER#{oid}"}})


def download(s3, bucket: str) -> None:
    for key in SYNCED:
        try:
            s3.download_file(bucket, key, key)
        except ClientError as e:
            if _code(e) not in ("404", "NoSuchKey"):  # a missing file is normal (first run, no KILL)
                raise


def upload(s3, bucket: str) -> None:
    for key in SYNCED:
        if os.path.exists(key):
            s3.upload_file(key, bucket, key)


def run(env=os.environ, s3=None, ddb=None, ssm=None, main=trading.main, now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    dry = is_dry_run(env)
    bucket, table = env.get("STATE_BUCKET"), env.get("TABLE_NAME")
    work = env.get("BOT_WORKDIR", "/tmp/bot")
    shutil.rmtree(work, ignore_errors=True)  # a warm container must not reuse yesterday's files (e.g. a KILL deleted in S3)
    os.makedirs(os.path.join(work, "results"))
    shutil.copy(os.path.join(HERE, "config.toml"), work)
    os.chdir(work)  # trading.py uses relative paths; only /tmp is writable on Lambda

    if bucket:
        load_secrets(ssm, env["PARAM_PATH"])  # before the claim: an SSM failure must not use up the day
        if not dry and not claim_day(ddb, table, now.date().isoformat(), now):
            log.warning("%s already claimed by an earlier invocation; exiting without trading", now.date())
            return {"status": "already_ran", "dry_run": dry}
        download(s3, bucket)  # dry runs read the real state too, they just never write it back

    argv = ["trading.py"] + (["--dry-run"] if dry else [])
    log.info("calling trading.main(%s)", argv)
    try:
        main(argv, now)
    except Exception as e:
        log.exception("unhandled error")
        send_alert(messages.format_error(e))
        raise  # Lambda Errors metric -> alarm -> SNS
    finally:
        if bucket and not dry:
            upload(s3, bucket)  # state first: it is the ledger
            save_orders(ddb, table, (trading._load_state() or {}).get("orders", {}))
    return {"status": "ok", "dry_run": dry}


def handler(event, context):
    if not os.environ.get("STATE_BUCKET"):
        return run()
    import boto3
    return run(s3=boto3.client("s3"), ddb=boto3.client("dynamodb"), ssm=boto3.client("ssm"))
