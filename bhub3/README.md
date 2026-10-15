# bHub

    pip install -r requirements.txt
    python app.py

Everything you may want to change lives in **config.ini** (human-readable, every line commented). Edit it, restart, done:
ports, HTTPS + certificates, TURN/STUN networking, audio/video quality, upload limits, call sizes, sign-ups, database/uploads
location and performance tuning. If config.ini is missing it is re-created with defaults. Use another file with
`python app.py --config /path/to/file.ini`.

## First run / site admin
* Nothing is pre-loaded. **The first account you register becomes the site admin** (Site admin appears in the profile menu).
* Add/remove other admins in Site admin > Users. Locked out? `python app.py --admin <username>`.

## Camera, microphone, screen share, live (HTTPS)
Browsers block these on plain http. bHub also serves https on port 5443 ([https] in config.ini) with an auto-generated
self-signed certificate; open the https address shown at start-up, accept the one-time warning, log in again.
For a real certificate set cert_file/key_file in [https] (e.g. Let's Encrypt), or run behind Caddy/nginx with behind_proxy = true.

## Networking for calls and live
[ice] section: STUN is enough on one Wi-Fi. For people on mobile data / other networks add a TURN server (coturn): fill turn_urls and
either turn_username/turn_password or turn_secret (coturn static-auth-secret; bHub then issues short-lived logins).
The first person in a call is the host and everyone else connects to them; if they leave the next person takes over.

## Sound quality / latency (all in [audio] and [video])
Opus voice at 96 kbps with forward error correction, 20 ms packets (10 for the lowest delay), a 20 ms playout buffer, 48 kHz,
echo cancellation + noise suppression + auto gain (each user can switch these off, or enable Music / studio mode: stereo, raw, 192 kbps).
Per-stream bitrate caps for camera, screen share, live and relayed video protect the call host's upload.

## Capacity notes
One process with a SQLite (WAL) database. Raise [server] threads for more simultaneous users. In-memory call state means run a single
process (do not start several copies against the same database).
