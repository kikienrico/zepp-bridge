# Zepp to Apple Health HRV bridge

Gets the full minute-by-minute overnight HRV off an Amazfit/Zepp device into Apple
Health, so Bevel (or Athlytic, etc.) sees a real HRV curve instead of the 2-3
points Zepp bothers to sync.

The Zepp app only writes a few HRV points per night to Apple Health and no
temperature. The dense series is all there in Zepp's cloud, it just never gets
exported. This pulls it out and feeds it in.

Unofficial, uses a reverse-engineered Zepp API, and the token dies about every 30
days. Not affiliated with anyone. It might break whenever Zepp changes something.

## How it works

Something that's always on (I used a Pi 4, any old box / Mac / NAS works) pulls
from Zepp on a schedule and serves the data on a tiny local endpoint. The iPhone
runs a Shortcut that reads it and writes to Apple Health, because that write can
only happen on the phone.

```
strap -> Zepp cloud -> box (hourly pull, sqlite, local API) -> iOS Shortcut -> Apple Health -> Bevel
```

One thing worth knowing up front: HealthKit only has an SDNN field for HRV, and
Zepp's value is RMSSD. So the RMSSD ends up in the SDNN field, and you have to set
Bevel to mirror Apple Health (Step 7) instead of its default, which looks for
beat-to-beat data this source doesn't have. The absolute number won't match an
Apple Watch but the trend is right and Bevel builds its own baseline anyway.

## What you need

- An Amazfit that records overnight HRV. Built on a Helio Strap.
- A Zepp account you can log into with email + password (SSO-only accounts need to
  set a password first, or use the proxy method).
- An always-on box on the same network as the phone.
- iPhone with Shortcuts.

## 1. Get the app token

Two ways. Try the first, it needs no proxy.

### huami-token (email + password)

Use the Codeberg repo, the GitHub mirror is dead:

```bash
git clone https://codeberg.org/argrento/huami-token.git
cd huami-token
pip install -r requirements.txt
python main.py -m amazfit -e YOUR_EMAIL -p YOUR_PASSWORD -n
```

`-n` prints the app token, user id and region. Grab the app token (long string),
plus the user id and region.

If the login fails (Zepp keeps changing it) make sure you're on the latest version,
otherwise fall back to the proxy.

### Proxy capture (Proxyman / mitmproxy)

Sniff the Zepp app's own traffic and read the token out of it. More fiddly but
always works, and it also tells you your region host.

On the computer, install Proxyman (free tier is fine), it listens on port 9090.
Note the computer's local IP.

On the phone:
- Wi-Fi settings -> (i) on your network -> Configure Proxy -> Manual, server = the
  computer's IP, port = 9090.
- In Safari open `proxyman.io/mitmssl` (or `mitm.it` for mitmproxy) and install the
  cert profile. Then General -> VPN & Device Management -> install it, AND General
  -> About -> Certificate Trust Settings -> toggle it on. People miss that second
  toggle and then HTTPS won't decrypt.
- Open Zepp, go to the HRV / temperature screens, scroll around.

Back on the computer, filter by `zepp.com`, open a request to
`api-mifit-XXX.zepp.com`, look at the headers and copy the full `apptoken` value.
The user id is the number in the `/users/<id>/` path. The host is that domain.

Don't reopen the Zepp app before you use the token, reopening can rotate it. Turn
the phone proxy back off when you're done.

## 2. Region host

The host has to match your account or you get invalid-token / empty responses even
with a good token.

- Europe: `api-mifit-de2.zepp.com`
- Americas: `api-mifit-us2.zepp.com`
- other/global: `api-mifit.zepp.com`

Use whatever huami-token or the capture showed you, don't guess. If you got a token
but used the wrong host you'll see errors, just change `de2` to `us2` (or whatever)
in the config. A `401 invalid token` that still comes back usually means the host
is right and the token's the problem; no response usually means wrong host.

## 3. Zepp CLI

This rides on top of [zepp-health-cli](https://github.com/m4ary/zepp-health-cli),
which does the API calls. Clone the original, don't re-upload it:

```bash
git clone https://github.com/m4ary/zepp-health-cli.git zepp-health
cd zepp-health
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # fill in token / user_id / host
python3 zepp_health.py summary        # should print your training load
```

Paste the whole token. If you copied it from something that showed `abc…xyz`, the
`…` ends up in the string and it fails. No spaces, no line breaks.

## 4. The bridge

```bash
mkdir -p ~/zepp-bridge
cp zepp_bridge.py ~/zepp-bridge/
nano ~/zepp-bridge/zepp_bridge.py   # set paths, LOCAL_TZ, SHARED_SECRET
```

For `SHARED_SECRET` use letters and numbers only. A `!` or `&` in it breaks inside
the URL and you get "unauthorized" that looks like a wrong password (spent a while
on that one).

```bash
pip install requests
cd ~/zepp-bridge
~/zepp-health/.venv/bin/python3 zepp_bridge.py pull
~/zepp-health/.venv/bin/python3 zepp_bridge.py dump -n 10
```

`dump` should list recent samples.

## 5. Keep it running

Service (`zepp-bridge.service` is in the repo, fix the username/paths):

```bash
sudo cp zepp-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zepp-bridge
```

If you edit the script later, `sudo systemctl restart zepp-bridge`, otherwise the
old copy keeps running.

Hourly pull, `crontab -e`:

```
0 * * * * /home/pi/zepp-health/.venv/bin/python3 /home/pi/zepp-bridge/zepp_bridge.py pull >> /home/pi/zepp-bridge/pull.log 2>&1
```

Test from the phone browser:

```
http://BOX_IP:8765/hrv?since=1&secret=YOUR_SECRET
```

You should get JSON with a `samples` array. Each sample has `rmssd` and `local`.

## 6. The Shortcut

Build it by hand (if you share yours via iCloud, blank the IP and secret first).

Set two Text variables: `BaseURL` = `http://BOX_IP:8765`, and `Secret`.

- Get Contents of URL -> `[BaseURL]/hrv?secret=[Secret]`, GET
- Get Dictionary Value -> `samples`
- Repeat with Each:
  - Get Dictionary Value -> `rmssd`
  - Get Dictionary Value -> `local`
  - Log Health Sample -> Heart Rate Variability, value = rmssd (ms), date = local
- Get Dictionary Value -> `max_ts_ms`
- Get Contents of URL -> `[BaseURL]/ack`, POST, header `X-Secret` = Secret, JSON
  body `upto` = max_ts_ms

Use `local` for the date, not `iso`. `iso` is UTC and the Shortcut misreads the
timezone, stamping everything at the wrong hour (or "now"). `local` is already in
your timezone.

Automate it: Automation -> Time of Day (midday is safe, late enough that you've
opened Zepp and the box has pulled), Run Immediately on. Only fires when unlocked,
which is fine for last night's data.

## 7. App settings

Both matter:

- Apple Health -> Zepp: turn off HRV write permission, otherwise Zepp's sparse
  points mix in with the dense series.
- Bevel -> Customization -> Calculations -> HRV Method: Apple Health (SDNN).

Run the Shortcut once by hand. First run writes 1000+ samples and takes a minute or
two, after that it's just the new night. Check Health -> Heart Rate Variability ->
Show All Data, the points should sit at the right nighttime hours, then open Bevel.

## Troubleshooting

**invalid token / 0102** - expired (get a new one), or wrong token type (the one
from Zepp's web/watchface console is not the mobile apptoken and never works), or
truncated with a `…`, or you reopened the app after capturing.

**unauthorized** - secret mismatch (check `grep SHARED_SECRET zepp_bridge.py`),
a special character in the secret, or you forgot to restart the service after
editing.

**Shortcut finishes instantly, nothing written** - empty list because the ack
cursor already moved. Reset it:
`sqlite3 ~/zepp-bridge/hrv.db "DELETE FROM meta WHERE k='acked_upto_ms';"`
Or Health write permission is off.

**everything logged at "now" or the wrong hour** - you used `iso` instead of
`local`.

**empty response** - wrong region host.

While debugging, disable the `/ack` step so the cursor doesn't move on failed runs.

## Notes

- Token dies ~monthly. When pulls start failing with 0102, get a new token, update
  config.json, restart the service. Set NTFY_URL for a heads-up.
- The absolute HRV numbers aren't comparable to an Apple Watch, only the trend.
  That's fine for recovery scoring.
- Skin temperature is in the same API (`skinTempCalibrated`, a delta from your
  baseline), but there's no true absolute value, so anything you inject is
  baseline + delta. Left it out.
- The rmssd series can include daytime spot readings. Bevel filters to sleep, or
  you can filter server-side.

## License

MIT, no warranty.
