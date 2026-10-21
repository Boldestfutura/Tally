# Tally

A small, self-hosted two-player scoreboard for Cameron and Paget. It tracks the
current month, lifetime points, and monthly winners, and keeps both phones in
sync—including taps made while offline.

The server is a single dependency-free Python file. The scoreboard data is kept
separately at `/data/tally.json`; it is never part of this repository.

## Running on TrueNAS

The current custom app runs `server.py` from a host-mounted app directory and
stores its data at `/data/tally.json`. Keep the data mount when updating the app
files so the scores and monthly history remain intact.

## Updating

The app checks this repository's `VERSION` file for newer releases. When one is
available, use **Check for updates** and **Install update** in Tally. The install
request is handled by a small root cron worker on TrueNAS: it downloads the
allow-listed app files from this repository, backs up the previous files, then
redeploys the app. Score data is not touched.

The host worker is `update-worker.sh`. Install its once-per-minute root cron job
with:

```sh
sh /mnt/SSDs/Applications/tally/app/install-worker.sh
```

Set the app's host directory to match if it is installed somewhere else.

## Icons

The PNG icons are used by the browser tab, home screen, and PWA manifest. The
in-app logo is the same tally-scoreboard mark.
