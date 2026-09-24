# Running the engine 24/7 on a small cloud server

A laptop sleeps, loses Wi-Fi and reboots for updates. Every minute the engine is down is a missed
opportunity, and a crash while holding quotes is a risk. The setup below costs about $5–20/month.

## 1. Server
- Any small Linux VM: AWS Lightsail / EC2 `t3.small`, DigitalOcean, or Hetzner. 2 GB RAM is plenty.
- Region **US East (N. Virginia, `us-east-1`)**. Most US exchange and data APIs are hosted there, so this
  is the lowest-latency choice until Novig confirms where its servers are.
- Ubuntu 24.04, Python 3.11+. Turn on automatic security updates.

## 2. Install
```bash
sudo adduser --disabled-password trader
sudo -iu trader
git clone <your repo> Sales-Dashboard && cd Sales-Dashboard/trading
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                        # must be all green before anything else
```

## 3. Secrets
- Create `/home/trader/live.env` (NOT inside the repo), using `config/live.env.example` as the template.
- `chmod 600 /home/trader/live.env`. Only the `trader` user can read it.
- Kalshi private key: `/home/trader/kalshi.pem`, also `chmod 600`, with `KALSHI_PRIVATE_KEY_PATH` pointing to it.
- Check without connecting to anything:
  `python main_supervisor.py --env-file /home/trader/live.env --check-config`

## 4. Always-on service (restarts on crash and on reboot)
`/etc/systemd/system/trading-engine.service`:
```ini
[Unit]
Description=Novig/Kalshi trading engine
After=network-online.target
Wants=network-online.target

[Service]
User=trader
WorkingDirectory=/home/trader/Sales-Dashboard/trading
ExecStart=/home/trader/Sales-Dashboard/trading/.venv/bin/python main_supervisor.py --env-file /home/trader/live.env --log-dir /home/trader/logs
Restart=always
RestartSec=10
# a stuck process gets 30s to pull quotes and exit cleanly
TimeoutStopSec=30
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading-engine
sudo systemctl status trading-engine        # is it running?
journalctl -u trading-engine -f             # live console output
```
On a restart, the startup sync restores open positions from Novig before any order is sent. The daily
loss stop and today's P&L come back from the ledger.

## 5. Alerts to your phone
- Free option: install the **ntfy** app, pick a hard-to-guess topic name, and set
  `ALERT_WEBHOOK_URL=https://ntfy.sh/<your-topic>` in `live.env`.
- Or use a Slack or Discord incoming-webhook URL.
- Every CRITICAL event is pushed (kill switch, daily loss stop, Novig socket down, unconfirmed or unknown
  fills, failed cancels, position mismatches). Identical alerts are sent at most once per 5 minutes.
- Test it once: `python -c "import logging, alerts, time; l=logging.getLogger('t'); h=alerts.AlertHandler('<url>'); l.addHandler(h); l.critical('test alert'); time.sleep(3)"`

## 6. Dashboard (read-only)
Run it on the server and view it through an SSH tunnel. Never expose it to the internet:
```bash
ssh -L 8501:127.0.0.1:8501 trader@<server>     # from your computer
nice -n 10 streamlit run dashboard.py          # on the server
```
Then open http://127.0.0.1:8501 on your computer.

## 7. Backups
`logs/live_ledger.jsonl` is the audit trail and the research folder is the edge measurement. Copy both
off the server daily, for example with a cron job that uploads to S3 or rsyncs to your computer.

## 8. Updating
```bash
cd ~/Sales-Dashboard && git pull && cd trading && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q && sudo systemctl restart trading-engine
```
Update between games, not while positions are filling.
