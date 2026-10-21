#!/bin/sh
# Run once on TrueNAS as root or truenas_admin to register the GitHub updater.
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_NAME=$(basename -- "$(dirname -- "$APP_DIR")")
CMD="python3 $APP_DIR/update-worker.py"

if midclt call cronjob.query 2>/dev/null | python3 -c '
import json,sys
cmd=sys.argv[1]
try: jobs=json.load(sys.stdin)
except Exception: jobs=[]
raise SystemExit(0 if any(j.get("command")==cmd for j in jobs) else 1)
' "$CMD"; then
  echo "Tally update worker is already installed."
  exit 0
fi

python3 - "$CMD" "$APP_NAME" <<'PY'
import json,subprocess,sys
command,name=sys.argv[1:]
job={"user":"root","command":command,
     "description":name+" GitHub updater","enabled":True,
     "stdout":False,"stderr":False,
     "schedule":{"minute":"*","hour":"*","dom":"*","month":"*","dow":"*"}}
r=subprocess.run(["midclt","call","cronjob.create",json.dumps(job)],capture_output=True,text=True)
if r.returncode:
    print(r.stderr or r.stdout,file=sys.stderr);raise SystemExit(r.returncode)
print("Tally GitHub update worker installed.")
PY
