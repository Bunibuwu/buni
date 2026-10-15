"""bHub server (Flask + SQLite). Run: pip install -r requirements.txt && python app.py"""
import base64, configparser, gzip, hashlib, hmac, json, logging, os, re, secrets, sqlite3, sys, time
from urllib.parse import urlparse
from flask import Flask, Response, g, jsonify, request, send_file, abort

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_TEMPLATE = r"""# ======================================================================
#  bHub configuration. Edit this file, then restart:  python app.py
#  Lines starting with # are comments. Delete a line to use its default.
# ======================================================================

[server]
host = 0.0.0.0              # 0.0.0.0 = reachable from other devices, 127.0.0.1 = this machine only
port = 5000                 # normal (http) port
threads = 64                # request threads. More = more simultaneous users (about 8 MB RAM each)
behind_proxy = false        # true if nginx / Caddy / cloudflared sits in front of bHub
public_url =                # optional, e.g. https://chat.example.com (shown in the start-up log)
allow_registration = true   # false = no new sign-ups (the very first account is always allowed)
database = bhub.db          # database file (use a full path to put it on another disk)
uploads = uploads           # folder for uploads (full path allowed)

[https]
# Browsers only allow camera, microphone and screen sharing on https (or localhost).
enabled = true
port = 5443
cert_file =                 # empty = auto-generate a self-signed certificate (browser shows a one-time warning)
key_file =                  # for a real certificate (Let's Encrypt) put the full paths to the .pem files here
redirect_http = false       # true = send everyone who opens the http port to https

[ice]
# Networking for calls and live streams. People on the same Wi-Fi work with STUN alone.
# People on different networks / mobile data need a TURN relay (coturn is free).
stun_servers = stun:stun.l.google.com:19302, stun:stun1.l.google.com:19302
turn_urls =                 # e.g. turn:turn.example.com:3478, turns:turn.example.com:5349
turn_username =             # fixed login for your TURN server ...
turn_password =
turn_secret =               # ... OR your coturn static-auth-secret (safer: bHub makes short-lived logins)
turn_ttl_hours = 12
force_relay = false         # true = always go through TURN (for testing / privacy)

[audio]
# Tuned for natural, clear voice with very little delay.
bitrate_kbps = 96           # Opus voice quality. 64 = good, 96 = excellent, 128 = studio
music_bitrate_kbps = 192    # used by the "Music / studio mode" switch in each user's audio settings
packet_ms = 20              # 10 = lowest delay (more bandwidth), 20 = balanced, 40 = least bandwidth
fec = true                  # forward error correction: keeps voice clean when packets are lost
dtx = false                 # silence suppression: saves bandwidth but can clip the start of words
jitter_buffer_ms = 20       # playback buffer. Lower = less delay, higher = steadier on bad connections
sample_rate = 48000

[video]
camera_height = 720         # 480 / 720 / 1080
camera_fps = 30
camera_kbps = 1500          # max bitrate per camera stream
screen_kbps = 3500          # max bitrate per screen share
screen_fps = 30
live_kbps = 3000            # max bitrate for a live stream
relay_kbps = 1500           # the call host re-sends other people's video at most this fast (protects the host's upload)
live_jitter_buffer_ms = 150 # live viewers buffer a little more for smoothness

[calls]
max_call_size = 25          # people allowed in one voice call / channel
max_viewers = 0             # viewers allowed per live stream (0 = unlimited)
poll_fast_ms = 250          # signalling speed while a call is connecting
poll_idle_ms = 1500         # signalling speed once everyone is connected

[limits]
chat_upload_mb = 10         # files in chats, posts, comments
video_upload_mb = 350
image_mb = 8                # avatars, banners, covers
store_file_mb = 1024        # store downloads uploaded by admins
login_attempts_per_minute = 8

[performance]
sqlite_cache_mb = 64        # database memory cache
sqlite_mmap_mb = 256        # memory-mapped database reads (faster)
gzip_level = 4              # 1 = fastest, 9 = smallest
connection_limit = 2000     # simultaneous open connections
backlog = 2048              # queued connections
channel_timeout = 60        # seconds before an idle connection is closed
"""
def load_config():
    path = os.environ.get('BHUB_CONFIG') or (sys.argv[sys.argv.index('--config') + 1] if '--config' in sys.argv else os.path.join(ROOT, 'config.ini'))
    if not os.path.exists(path):
        open(path, 'w', encoding='utf-8').write(CONFIG_TEMPLATE); print('Created', path, '(edit it to change ports, HTTPS, TURN, limits and audio quality)')
    cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'), interpolation=None)
    try: cp.read(path, encoding='utf-8')
    except configparser.Error as e: print('config.ini problem, using defaults where unreadable:', e)
    ref = configparser.ConfigParser(inline_comment_prefixes=('#', ';'), interpolation=None); ref.read_string(CONFIG_TEMPLATE)
    def get(sec, key, cast=str):
        raw = cp.get(sec, key, fallback=None)
        if raw is None or raw.strip() == '' and cast is not str: raw = ref.get(sec, key)
        raw = raw.strip()
        try: return (raw.lower() in ('1', 'true', 'yes', 'on')) if cast is bool else cast(raw)
        except ValueError: print(f'config.ini: bad value for [{sec}] {key} = {raw!r}, using default'); raw = ref.get(sec, key).strip(); return (raw.lower() in ('1', 'true', 'yes', 'on')) if cast is bool else cast(raw)
    return get, path
CFG, CFG_PATH = load_config()
def lst(v): return [x.strip() for x in v.split(',') if x.strip()]
def rel_path(p): return p if os.path.isabs(p) else os.path.join(ROOT, p)
DB, UP = rel_path(CFG('server', 'database')), rel_path(CFG('server', 'uploads')); PF = os.path.join(UP, 'pf')
os.makedirs(PF, exist_ok=True)
MB = 1024 * 1024
LIM = dict(chat=CFG('limits', 'chat_upload_mb', float) * MB, video=CFG('limits', 'video_upload_mb', float) * MB, image=CFG('limits', 'image_mb', float) * MB, product=CFG('limits', 'store_file_mb', float) * MB)
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = int(max(LIM.values()) + 8 * MB)
if CFG('server', 'behind_proxy', bool):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
COLORS = ['#7c5cff', '#5b8cff', '#22c55e', '#d97706', '#dc2626', '#a855f7']
PERMS = ['admin', 'channels', 'roles', 'kick', 'ban', 'send', 'connect', 'invite', 'messages']
PLATFORMS = ['windows', 'mac', 'linux', 'android', 'ios']
SAFE = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
        '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.flac': 'audio/flac'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT UNIQUE COLLATE NOCASE,salt,pw,display,bio DEFAULT '',color,seen REAL,settings TEXT,avatar,banner,admin INTEGER DEFAULT 0,banned INTEGER DEFAULT 0,ban_reason,created);
CREATE TABLE IF NOT EXISTS tokens(tok PRIMARY KEY,uid,ts);
CREATE TABLE IF NOT EXISTS notifs(id INTEGER PRIMARY KEY,uid,text,link,ts,read DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,kind,name,descr,tag,cover,version,ts);
CREATE TABLE IF NOT EXISTS achievements(id INTEGER PRIMARY KEY,pid,name,descr,ts);
CREATE TABLE IF NOT EXISTS pfiles(pid,platform,fn,name,size,PRIMARY KEY(pid,platform));
CREATE TABLE IF NOT EXISTS cart(uid,pid,PRIMARY KEY(uid,pid));
CREATE TABLE IF NOT EXISTS owned(uid,pid,ts,minutes DEFAULT 0,PRIMARY KEY(uid,pid));
CREATE TABLE IF NOT EXISTS orders(id PRIMARY KEY,uid,total,ts);
CREATE TABLE IF NOT EXISTS content(id INTEGER PRIMARY KEY,kind,title,descr,uid,dur,views DEFAULT 0,file,thumb,embed,viewers DEFAULT 0,ts);
CREATE TABLE IF NOT EXISTS votes(kind,tid,uid,v,PRIMARY KEY(kind,tid,uid));
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY,kind,tid,uid,body,ts,parent);
CREATE TABLE IF NOT EXISTS tweets(id INTEGER PRIMARY KEY,uid,body,ts,edited DEFAULT 0);
CREATE TABLE IF NOT EXISTS follows(a,b,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS boards(name PRIMARY KEY,descr,owner,ts,tags DEFAULT '');
CREATE TABLE IF NOT EXISTS bmods(board,uid,PRIMARY KEY(board,uid));
CREATE TABLE IF NOT EXISTS bbans(board,uid,reason,PRIMARY KEY(board,uid));
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY,board,uid,title,body,tags DEFAULT '',ts,edited DEFAULT 0);
CREATE TABLE IF NOT EXISTS friends(a,b,st,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS blocks(a,b,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS mutes(a,b,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS convs(id INTEGER PRIMARY KEY,kind,name,server,parent,owner,pos DEFAULT 0,private INTEGER DEFAULT 0,readonly INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(conv,uid,PRIMARY KEY(conv,uid));
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY,conv,uid,body,ts,edited DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(conv,uid,last,PRIMARY KEY(conv,uid));
CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY,name,owner,invite,color);
CREATE TABLE IF NOT EXISTS smembers(server,uid,PRIMARY KEY(server,uid));
CREATE TABLE IF NOT EXISTS roles(id INTEGER PRIMARY KEY,server,name,perms,color,base DEFAULT 0);
CREATE TABLE IF NOT EXISTS mroles(server,uid,role,PRIMARY KEY(server,uid,role));
CREATE TABLE IF NOT EXISTS bans(server,uid,PRIMARY KEY(server,uid));
CREATE TABLE IF NOT EXISTS chroles(conv,role,can_view DEFAULT 0,can_send DEFAULT 0,PRIMARY KEY(conv,role));
CREATE INDEX IF NOT EXISTS i_msgs ON msgs(conv,id);CREATE INDEX IF NOT EXISTS i_com ON comments(kind,tid);CREATE INDEX IF NOT EXISTS i_votes ON votes(kind,tid);
CREATE INDEX IF NOT EXISTS i_tw ON tweets(uid);CREATE INDEX IF NOT EXISTS i_posts ON posts(board);CREATE INDEX IF NOT EXISTS i_cont ON content(uid,kind);CREATE INDEX IF NOT EXISTS i_not ON notifs(uid);
CREATE INDEX IF NOT EXISTS i_cm ON cmembers(uid);CREATE INDEX IF NOT EXISTS i_fol ON follows(b);CREATE INDEX IF NOT EXISTS i_tok ON tokens(uid);CREATE INDEX IF NOT EXISTS i_conv ON convs(server);CREATE INDEX IF NOT EXISTS i_sm ON smembers(uid);
"""
MIGRATE = [('users', 'avatar'), ('users', 'banner'), ('users', 'admin INTEGER DEFAULT 0'), ('users', 'banned INTEGER DEFAULT 0'), ('users', 'ban_reason'), ('users', 'created'), ('comments', 'parent'), ('posts', 'tags DEFAULT ""'), ('posts', 'edited DEFAULT 0'),
           ('tweets', 'edited DEFAULT 0'), ('boards', 'tags DEFAULT ""'), ('convs', 'private INTEGER DEFAULT 0'), ('convs', 'readonly INTEGER DEFAULT 0'), ('content', 'embed'), ('content', 'thumb'), ('msgs', 'edited DEFAULT 0')]
class E(Exception):
    def __init__(s, m, c=400): super().__init__(m); s.c = c
def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=15); g.db.row_factory = sqlite3.Row; g.db.execute('PRAGMA synchronous=NORMAL'); g.db.execute('PRAGMA temp_store=MEMORY'); g.db.execute('PRAGMA cache_size=-%d' % (CFG('performance', 'sqlite_cache_mb', int) * 1024)); g.db.execute('PRAGMA mmap_size=%d' % (CFG('performance', 'sqlite_mmap_mb', int) * MB))
    return g.db
@app.teardown_appcontext
def _close(ex):
    d = g.pop('db', None)
    if d:
        try: d.rollback() if ex else d.commit()
        finally: d.close()
def Q(sql, *a): return [dict(r) for r in db().execute(sql, a).fetchall()]
def Q1(sql, *a): r = db().execute(sql, a).fetchone(); return dict(r) if r else None
def X(sql, *a): return db().execute(sql, a).lastrowid
def B(): return request.get_json(silent=True) or {}
def cl(s, n): return str(s or '').strip()[:n]
def hp(p, s): return hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(s), 100_000).hex()
def now(): return time.time()
def IN(ids): return ','.join(str(int(i)) for i in ids) or '0'
def me_name():
    if 'mn' not in g: g.mn = Q1('SELECT name FROM users WHERE id=?', g.u)['name']
    return g.mn
def notify(uid, text, link='', kind='system'):
    if uid == g.get('u'): return
    if g.get('u') and Q1('SELECT 1 FROM mutes WHERE a=? AND b=?', uid, g.u): return
    r = Q1('SELECT settings FROM users WHERE id=?', uid); st = json.loads((r or {}).get('settings') or '{}')
    if st.get('dnd') or st.get('n_' + kind) is False: return
    X('INSERT INTO notifs(uid,text,link,ts) VALUES(?,?,?,?)', uid, text, link, now())
def mentions(text, link):
    for n in set(re.findall(r'@([A-Za-z0-9_]{3,20})', text or '')):
        u = Q1('SELECT id FROM users WHERE name=?', n)
        if u and u['id'] != g.u and rel(g.u, u['id']) != 'blockedby': notify(u['id'], f"{me_name()} mentioned you: {text[:50]}", link, 'mention')
TOK, SEEN, FAIL = {}, {}, {}
def authed():
    t = request.headers.get('X-Token', ''); c = TOK.get(t)
    if c and now() - c[3] < 30: return c
    r = Q1('SELECT t.uid,u.admin,u.banned FROM tokens t JOIN users u ON u.id=t.uid WHERE t.tok=?', t)
    if not r: TOK.pop(t, None); return None
    c = TOK[t] = (r['uid'], r['admin'], r['banned'], now()); return c
def forget(uid):
    for t in [t for t, c in TOK.items() if c[0] == uid]: TOK.pop(t, None)
def route(p, m='GET', a=True, admin=False):
    def d(f):
        def w(**k):
            try:
                g.u, g.admin = None, False
                if a:
                    c = authed()
                    if not c: raise E('Please log in', 401)
                    if c[2]: raise E('Your account has been banned', 403)
                    g.u, g.admin = c[0], bool(c[1])
                    if admin and not g.admin: raise E('Admins only', 403)
                    if now() - SEEN.get(g.u, 0) > 20: SEEN[g.u] = now(); X('UPDATE users SET seen=? WHERE id=?', now(), g.u)
                return jsonify(f(**k))
            except E as e:
                db().rollback(); return jsonify(error=str(e)), e.c
        w.__name__ = f.__name__ + str(len(app.url_map._rules))
        app.route('/api' + p, methods=[m])(w); return f
    return d
def limit(kind):
    n = request.content_length or 0
    if n > LIM[kind] + 2 * MB: raise E(f'File too large (max {LIM[kind] // MB} MB)', 413)
def save_upload(f, kind, folder=UP, exts=None):
    ext = os.path.splitext(f.filename or '')[1].lower(); ext = ext if re.fullmatch(r'\.[a-z0-9]{1,8}', ext) else ''
    if exts and ext not in exts: raise E('Unsupported file type')
    fn = secrets.token_hex(10) + ext; p = os.path.join(folder, fn); f.save(p)
    if os.path.getsize(p) > LIM[kind]: os.remove(p); raise E(f'File too large (max {LIM[kind] // MB} MB)', 413)
    return fn, os.path.getsize(p)
def rmfile(fn, folder=UP):
    if fn:
        try: os.remove(os.path.join(folder, os.path.basename(fn)))
        except OSError: pass
@app.errorhandler(413)
def too_big(_): return jsonify(error='That upload is too large'), 413

# ---------------- accounts ----------------
def mk_user(n, p):
    s = secrets.token_hex(16); first = 0 if Q1('SELECT 1 FROM users') else 1
    return X('INSERT INTO users(name,salt,pw,display,color,seen,settings,admin,created) VALUES(?,?,?,?,?,?,?,?,?)', n, s, hp(p, s), n, COLORS[len(n) % 6], 0, '{}', first, now())
def tok(uid):
    t = secrets.token_urlsafe(32); X('INSERT INTO tokens VALUES(?,?,?)', t, uid, now()); return dict(token=t)
def throttle(key, add=False):
    n, t = FAIL.get(key, (0, 0))
    if now() - t > 60: n = 0
    if add: FAIL[key] = (n + 1, now())
    elif n >= CFG('limits', 'login_attempts_per_minute', int): raise E('Too many attempts. Wait a minute.', 429)
@route('/register', 'POST', False)
def register():
    b = B(); n, p = cl(b.get('name'), 20), str(b.get('password', ''))
    if not re.fullmatch(r'[A-Za-z0-9_]{3,20}', n): raise E('Username: 3-20 letters, numbers or _')
    if len(p) < 6: raise E('Password needs 6+ characters')
    if Q1('SELECT 1 FROM users WHERE name=?', n): raise E('Username already taken', 409)
    if not CFG('server', 'allow_registration', bool) and Q1('SELECT 1 FROM users'): raise E('New registrations are closed on this server', 403)
    return tok(mk_user(n, p))
@route('/login', 'POST', False)
def login():
    b = B(); n = cl(b.get('name'), 20); k = (request.remote_addr, n.lower()); throttle(k)
    u = Q1('SELECT * FROM users WHERE name=?', n)
    if not u or not secrets.compare_digest(u['pw'], hp(str(b.get('password', '')), u['salt'])): throttle(k, True); raise E('Wrong username or password', 401)
    if u['banned']: raise E('This account has been banned' + (': ' + u['ban_reason'] if u['ban_reason'] else ''), 403)
    return tok(u['id'])
@route('/logout', 'POST')
def logout(): X('DELETE FROM tokens WHERE tok=?', request.headers.get('X-Token')); TOK.pop(request.headers.get('X-Token'), None); return {}
def pub(u): return dict(id=u['id'], name=u['name'], display=u['display'], color=u['color'], bio=u['bio'], avatar=u['avatar'], banner=u['banner'])
@route('/me')
def me():
    u = Q1('SELECT * FROM users WHERE id=?', g.u)
    return dict(pub(u), admin=bool(u['admin']), settings=json.loads(u['settings'] or '{}'), unread=Q1('SELECT COUNT(*) n FROM notifs WHERE uid=? AND read=0', g.u)['n'],
                unread_dm=Q1('SELECT COUNT(*) n FROM msgs m JOIN cmembers c ON c.conv=m.conv AND c.uid=? WHERE m.uid!=? AND m.id>COALESCE((SELECT last FROM reads WHERE conv=m.conv AND uid=?),0)', g.u, g.u, g.u)['n'],
                muted=[r['name'] for r in Q('SELECT u.name FROM mutes m JOIN users u ON u.id=m.b WHERE m.a=?', g.u)])
@route('/me', 'PUT')
def me_put():
    b = B()
    if 'display' in b: X('UPDATE users SET display=? WHERE id=?', cl(b['display'], 30) or me_name(), g.u)
    if 'bio' in b: X('UPDATE users SET bio=? WHERE id=?', cl(b['bio'], 300), g.u)
    if b.get('color') in COLORS: X('UPDATE users SET color=? WHERE id=?', b['color'], g.u)
    if isinstance(b.get('settings'), dict):
        s = json.loads(Q1('SELECT settings s FROM users WHERE id=?', g.u)['s'] or '{}'); s.update(b['settings']); X('UPDATE users SET settings=? WHERE id=?', json.dumps(s), g.u)
    return me()
def check_pw(pw): 
    u = Q1('SELECT * FROM users WHERE id=?', g.u)
    if not secrets.compare_digest(u['pw'], hp(str(pw or ''), u['salt'])): raise E('Current password is wrong', 403)
@route('/me/username', 'POST')
def change_name():
    b = B(); n = cl(b.get('name'), 20); check_pw(b.get('password'))
    if not re.fullmatch(r'[A-Za-z0-9_]{3,20}', n): raise E('Username: 3-20 letters, numbers or _')
    if Q1('SELECT 1 FROM users WHERE name=? AND id!=?', n, g.u): raise E('Username already taken', 409)
    X('UPDATE users SET name=? WHERE id=?', n, g.u); return me()
@route('/me/password', 'POST')
def change_pw():
    b = B(); check_pw(b.get('password')); n = str(b.get('new', ''))
    if len(n) < 6: raise E('New password needs 6+ characters')
    s = secrets.token_hex(16); X('UPDATE users SET salt=?,pw=? WHERE id=?', s, hp(n, s), g.u)
    X('DELETE FROM tokens WHERE uid=? AND tok!=?', g.u, request.headers.get('X-Token')); forget(g.u); return {}
@route('/me/logout_all', 'POST')
def logout_all(): X('DELETE FROM tokens WHERE uid=? AND tok!=?', g.u, request.headers.get('X-Token')); forget(g.u); return {}
def set_image(col):
    limit('image'); f = request.files.get('file'); u = Q1('SELECT * FROM users WHERE id=?', g.u)
    if f and f.filename:
        fn, _ = save_upload(f, 'image', exts=('.png', '.jpg', '.jpeg', '.gif', '.webp')); rmfile(u[col]); X(f'UPDATE users SET {col}=? WHERE id=?', fn, g.u)
    else: rmfile(u[col]); X(f'UPDATE users SET {col}=NULL WHERE id=?', g.u)
    return me()
@route('/me/avatar', 'POST')
def avatar(): return set_image('avatar')
@route('/me/banner', 'POST')
def banner(): return set_image('banner')
@route('/me/export')
def export():
    u = Q1('SELECT id,name,display,bio,created FROM users WHERE id=?', g.u)
    return dict(account=u, tweets=Q('SELECT body,ts FROM tweets WHERE uid=?', g.u), posts=Q('SELECT board,title,body,tags,ts FROM posts WHERE uid=?', g.u), comments=Q('SELECT kind,body,ts FROM comments WHERE uid=?', g.u),
                uploads=Q('SELECT title,kind,ts FROM content WHERE uid=?', g.u), library=Q('SELECT p.name,o.minutes FROM owned o JOIN products p ON p.id=o.pid WHERE o.uid=?', g.u))
@route('/me/delete', 'POST')
def delete_me():
    check_pw(B().get('password'))
    if g.admin and Q1('SELECT COUNT(*) n FROM users WHERE admin=1')['n'] < 2: raise E('You are the only site admin. Make someone else admin first.')
    purge_user(g.u); return {}
@route('/notifs')
def notifs(): return Q('SELECT * FROM notifs WHERE uid=? ORDER BY id DESC LIMIT 50', g.u)
@route('/notifs/read', 'POST')
def notifs_read(): X('UPDATE notifs SET read=1 WHERE uid=?', g.u); return {}
@route('/notifs', 'DELETE')
def notifs_clear(): X('DELETE FROM notifs WHERE uid=?', g.u); return {}
def online(u):
    s = json.loads(u['settings'] or '{}'); return now() - (u['seen'] or 0) < 40 and s.get('show_online', True)
def udict(r): return dict(id=r['id'], name=r['name'], display=r['display'], color=r['color'], avatar=r['avatar'], online=online(r))
@route('/u/<name>')
def profile(name):
    u = Q1('SELECT * FROM users WHERE name=?', name)
    if not u: raise E('No such user', 404)
    d = dict(pub(u), followers=Q1('SELECT COUNT(*) n FROM follows WHERE b=?', u['id'])['n'], following=Q1('SELECT COUNT(*) n FROM follows WHERE a=?', u['id'])['n'],
             is_following=bool(Q1('SELECT 1 FROM follows WHERE a=? AND b=?', g.u, u['id'])), rel=rel(g.u, u['id']), tweets=tweet_rows('WHERE t.uid=?', u['id']), online=online(u),
             muted=bool(Q1('SELECT 1 FROM mutes WHERE a=? AND b=?', g.u, u['id'])), uploads=Q1('SELECT COUNT(*) n FROM content WHERE uid=?', u['id'])['n'])
    if g.admin: d.update(site_admin=bool(u['admin']), banned=bool(u['banned']))
    return d
@route('/users')
def users():
    q = re.sub(r'[%_]', '', cl(request.args.get('q'), 20)); return [pub(u) for u in Q('SELECT * FROM users WHERE name LIKE ? AND id!=? AND banned=0 ORDER BY name LIMIT 20', q + '%', g.u)]

# ---------------- store / cart / library (everything is free for now) ----------------
def prod_rows(where='', *a):
    return Q(f'''SELECT p.*,EXISTS(SELECT 1 FROM owned o WHERE o.pid=p.id AND o.uid=?) owned,EXISTS(SELECT 1 FROM cart c WHERE c.pid=p.id AND c.uid=?) incart,
      (SELECT group_concat(platform) FROM pfiles WHERE pid=p.id) platforms FROM products p {where}''', g.u, g.u, *a)
@route('/store')
def store():
    k, q, s = request.args.get('kind', 'all'), cl(request.args.get('q'), 40).lower(), request.args.get('sort', 'new')
    L = [p for p in prod_rows() if k in ('all', p['kind']) and (not q or q in p['name'].lower() or q in (p['tag'] or '').lower())]
    return sorted(L, key=(lambda p: p['name'].lower()) if s == 'name' else (lambda p: -p['ts']))
@route('/store/<int:i>')
def product(i):
    r = prod_rows('WHERE p.id=?', i)
    if not r: raise E('Not found', 404)
    r[0]['ach'] = Q('SELECT id,name,descr FROM achievements WHERE pid=? ORDER BY id', i); return r[0]
@route('/cart')
def cart(): return prod_rows('WHERE p.id IN (SELECT pid FROM cart WHERE uid=%d)' % g.u)
@route('/cart/add', 'POST')
def cart_add():
    pid = B().get('id')
    if not Q1('SELECT 1 FROM products WHERE id=?', pid): raise E('No such item', 404)
    if Q1('SELECT 1 FROM owned WHERE uid=? AND pid=?', g.u, pid): raise E('Already in your library')
    X('INSERT OR IGNORE INTO cart VALUES(?,?)', g.u, pid); return {}
@route('/cart/remove', 'POST')
def cart_rm(): X('DELETE FROM cart WHERE uid=? AND pid=?', g.u, B().get('id')); return {}
@route('/checkout', 'POST')
def checkout():
    items = cart()
    if not items: raise E('Your cart is empty')
    for p in items: X('INSERT OR IGNORE INTO owned(uid,pid,ts) VALUES(?,?,?)', g.u, p['id'], now())
    oid = 'BH-' + secrets.token_hex(4).upper(); X('INSERT INTO orders VALUES(?,?,?,?)', oid, g.u, 0, now()); X('DELETE FROM cart WHERE uid=?', g.u)
    notify(g.u, f'{len(items)} item(s) added to your library (order {oid})', '#/library'); return dict(order=oid, count=len(items))
@route('/library')
def library():
    L = Q('''SELECT p.*,o.minutes,o.ts bought,(SELECT group_concat(platform) FROM pfiles WHERE pid=p.id) platforms FROM owned o JOIN products p ON p.id=o.pid WHERE o.uid=? ORDER BY o.minutes DESC,o.ts DESC''', g.u)
    A = {}
    for a in Q(f'SELECT id,pid,name,descr FROM achievements WHERE pid IN ({IN(p["id"] for p in L)}) ORDER BY id'): A.setdefault(a['pid'], []).append(dict(id=a['id'], name=a['name'], descr=a['descr'], done=False))
    for p in L: p['ach'] = A.get(p['id'], [])
    return L
@route('/library/<int:pid>/play', 'POST')
def play(pid):
    o = Q1('SELECT minutes FROM owned WHERE uid=? AND pid=?', g.u, pid)
    if not o: raise E('Not in your library', 403)
    X('UPDATE owned SET minutes=minutes+1 WHERE uid=? AND pid=?', g.u, pid); return dict(minutes=o['minutes'] + 1)
@app.get('/api/library/<int:pid>/download/<plat>')
def download(pid, plat):
    c = authed(); tk = request.args.get('t')
    if not c and tk: TOK.pop(tk, None); r = Q1('SELECT uid FROM tokens WHERE tok=?', tk); c = (r['uid'], 0, 0, 0) if r else None
    if not c: abort(401)
    adm = Q1('SELECT admin FROM users WHERE id=?', c[0])['admin']
    if not adm and not Q1('SELECT 1 FROM owned WHERE uid=? AND pid=?', c[0], pid): abort(403)
    f = Q1('SELECT * FROM pfiles WHERE pid=? AND platform=?', pid, plat)
    if not f: abort(404)
    return send_file(os.path.join(PF, f['fn']), as_attachment=True, download_name=f['name'], conditional=True)

# ---------------- files + embeds ----------------
@route('/upload', 'POST')
def upload_file():
    limit('chat'); f = request.files.get('file')
    if not f or not f.filename: raise E('No file')
    fn, size = save_upload(f, 'chat'); name = re.sub(r'[^\w.\- ]', '_', f.filename)[:80]
    return dict(url=f'/f/{fn}?n={name.replace(" ", "_")}', name=name, size=size)
@app.get('/f/<fn>')
@app.get('/media/<fn>')
def serve(fn):
    fn = os.path.basename(fn); p = os.path.join(UP, fn)
    if not os.path.isfile(p): abort(404)
    mt = SAFE.get(os.path.splitext(fn)[1].lower())
    r = send_file(p, mimetype=mt or 'application/octet-stream', conditional=True, max_age=31536000, as_attachment=not mt, download_name=request.args.get('n') or fn)
    r.headers['X-Content-Type-Options'] = 'nosniff'; r.cache_control.immutable = True; return r
@route('/embed')
def embed():
    k, i = request.args.get('k', ''), cl(request.args.get('i'), 60)
    if k in ('watch', 'live'):
        c = Q1('SELECT c.id,c.title,c.kind,c.dur,c.views,c.thumb,c.embed,u.name author FROM content c JOIN users u ON u.id=c.uid WHERE c.id=?', i)
        if not c: return {}
        y = re.search(r'(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{11})', c['embed'] or '')
        th = '/media/' + c['thumb'] if c['thumb'] else (f'https://i.ytimg.com/vi/{y.group(1)}/hqdefault.jpg' if y else '')
        return dict(title=c['title'], sub=f"{c['author']} · {'LIVE now' if c['kind'] == 'live' else str(c['views']) + ' views'}", icon='radio' if c['kind'] == 'live' else 'play', thumb=th, short=c['kind'] == 'short')
    if k == 'post':
        p = Q1('SELECT p.title,p.board,u.name author FROM posts p JOIN users u ON u.id=p.uid WHERE p.id=?', i)
        return dict(title=p['title'], sub=f"b/{p['board']} · u/{p['author']}", icon='community') if p else {}
    if k == 'tweet':
        t = Q1('SELECT t.body,u.name author FROM tweets t JOIN users u ON u.id=t.uid WHERE t.id=?', i)
        return dict(title=t['body'][:120], sub=f"@{t['author']}", icon='community') if t else {}
    if k == 'b':
        b = Q1('SELECT name,descr FROM boards WHERE name=?', i.lower()); return dict(title='b/' + b['name'], sub=b['descr'] or 'Forum board', icon='hash') if b else {}
    if k in ('u', 'channel'):
        u = Q1('SELECT name,display,bio FROM users WHERE name=?', i); return dict(title=u['display'], sub='@' + u['name'] + (' · ' + u['bio'][:60] if u['bio'] else ''), icon='user') if u else {}
    if k == 'invite':
        s = Q1('SELECT id,name FROM servers WHERE invite=?', i)
        return dict(title='Join ' + s['name'], sub=f"{Q1('SELECT COUNT(*) n FROM smembers WHERE server=?', s['id'])['n']} members", icon='social', invite=i) if s else {}
    return {}

# ---------------- content (videos, shorts, live) ----------------
def content_rows(where='', *a):
    return Q(f'''SELECT c.*,u.name author,u.color,u.avatar,(SELECT COUNT(*) FROM votes WHERE kind='content' AND tid=c.id) likes,EXISTS(SELECT 1 FROM votes WHERE kind='content' AND tid=c.id AND uid=?) liked,
      EXISTS(SELECT 1 FROM follows WHERE a=? AND b=c.uid) following,(SELECT COUNT(*) FROM comments WHERE kind IN ('content','live') AND tid=c.id) ncomments FROM content c JOIN users u ON u.id=c.uid {where}''', g.u, g.u, *a)
LAST, REAPED = {}, [0]
def reap_live():
    if now() - REAPED[0] < 10: return
    REAPED[0] = now()
    for r in Q('SELECT id,ts FROM content WHERE kind="live"'):
        if now() - LAST.get(r['id'], r['ts']) > 75: drop_content(r['id'])
def valid_embed(u):
    u = cl(u, 500)
    if not u: return ''
    p = urlparse(u)
    if p.scheme not in ('http', 'https') or not p.netloc: raise E('Enter a full http(s) video link')
    return u
@route('/content')
def content():
    reap_live(); q = cl(request.args.get('q'), 60).lower(); k = request.args.get('kind')
    L = [c for c in content_rows('WHERE c.kind=? ORDER BY c.ts DESC LIMIT 300', k) if not q or q in (c['title'] + c['author']).lower()] if k else [c for c in content_rows('ORDER BY c.ts DESC LIMIT 300') if not q or q in (c['title'] + c['author']).lower()]
    return sorted(L, key=lambda c: not c['following'])
@route('/content', 'POST')
def upload():
    limit('video'); kind, title, f = request.form.get('kind', 'long'), cl(request.form.get('title'), 100), request.files.get('file'); em = valid_embed(request.form.get('embed'))
    if kind not in ('long', 'short') or not title: raise E('Title required')
    if not f and not em: raise E('Upload a video file or paste a video link')
    fn = save_upload(f, 'video', exts=('.mp4', '.webm', '.mov', '.m4v'))[0] if f and f.filename else ''
    th, t, d = '', request.files.get('thumb'), request.form.get('dur', '')
    if t and t.filename: th = save_upload(t, 'image', exts=('.jpg', '.jpeg', '.png', '.webp'))[0]
    if not re.fullmatch(r'\d{1,2}(:\d{2}){1,2}', d): d = ''
    i = X('INSERT INTO content(kind,title,descr,uid,dur,file,thumb,embed,ts) VALUES(?,?,?,?,?,?,?,?,?)', kind, title, cl(request.form.get('descr'), 1000), g.u, d, fn, th, em, now())
    mentions(request.form.get('descr', ''), f'#/content/watch/{i}'); return dict(id=i)
@route('/content/<int:i>')
def content_get(i):
    reap_live(); r = content_rows('WHERE c.id=?', i)
    if not r: raise E('Not found', 404)
    return r[0]
def drop_content(i):
    c = Q1('SELECT * FROM content WHERE id=?', i)
    if not c: return
    rmfile(c['file']); rmfile(c['thumb']); X("DELETE FROM comments WHERE kind IN ('content','live') AND tid=?", str(i)); X("DELETE FROM votes WHERE kind='content' AND tid=?", str(i)); X('DELETE FROM content WHERE id=?', i); LAST.pop(i, None)
@route('/content/<int:i>', 'DELETE')
def content_del(i):
    c = Q1('SELECT * FROM content WHERE id=?', i)
    if not c or (c['uid'] != g.u and not g.admin): raise E('Only the uploader can delete this', 403)
    drop_content(i); return {}
@route('/content/<int:i>', 'PUT')
def content_edit(i):
    c = Q1('SELECT * FROM content WHERE id=?', i); b = B()
    if not c or (c['uid'] != g.u and not g.admin): raise E('Only the uploader can edit this', 403)
    X('UPDATE content SET title=?,descr=? WHERE id=?', cl(b.get('title'), 100) or c['title'], cl(b.get('descr'), 1000), i); return {}
@route('/content/<int:i>/view', 'POST')
def view(i): X('UPDATE content SET views=views+1 WHERE id=?', i); return {}
@route('/channel/<name>')
def channel(name):
    u = Q1('SELECT * FROM users WHERE name=? AND banned=0', name)
    if not u: raise E('No such channel', 404)
    rows = content_rows('WHERE c.uid=? ORDER BY c.ts DESC', u['id'])
    return dict(pub(u), uid=u['id'], mine=u['id'] == g.u, followers=Q1('SELECT COUNT(*) n FROM follows WHERE b=?', u['id'])['n'], following=bool(Q1('SELECT 1 FROM follows WHERE a=? AND b=?', g.u, u['id'])), videos=rows)
@route('/live', 'POST')
def go_live():
    t = cl(B().get('title'), 100) or 'Live now'
    for r in Q('SELECT id FROM content WHERE kind="live" AND uid=?', g.u): drop_content(r['id'])
    i = X('INSERT INTO content(kind,title,uid,ts) VALUES("live",?,?,?)', t, g.u, now()); LAST[i] = now(); return dict(id=i)
@route('/live/<int:i>/end', 'POST')
def end_live(i):
    c = Q1('SELECT uid FROM content WHERE id=? AND kind="live"', i)
    if c and (c['uid'] == g.u or g.admin): drop_content(i)
    return {}

# ---------------- votes + comments (threaded) ----------------
@route('/vote', 'POST')
def vote():
    b = B(); k, t, v = b.get('kind'), str(b.get('tid')), int(b.get('v', 1))
    if k not in ('content', 'tweet', 'rt', 'post') or v not in (-1, 1): raise E('Bad vote')
    old = Q1('SELECT v FROM votes WHERE kind=? AND tid=? AND uid=?', k, t, g.u); X('DELETE FROM votes WHERE kind=? AND tid=? AND uid=?', k, t, g.u)
    if not old or old['v'] != v:
        X('INSERT INTO votes VALUES(?,?,?,?)', k, t, g.u, v)
        if k == 'tweet':
            o = Q1('SELECT uid FROM tweets WHERE id=?', t); o and notify(o['uid'], f'{me_name()} liked your post', f'#/community/tweet/{t}', 'like')
    return dict(score=Q1('SELECT COALESCE(SUM(v),0) s FROM votes WHERE kind=? AND tid=?', k, t)['s'], mine=0 if old and old['v'] == v else v)
def owners(kind, tid):
    if kind in ('content', 'live'): r = Q1('SELECT uid FROM content WHERE id=?', tid); return {r['uid']} if r else set()
    if kind == 'tweet': r = Q1('SELECT uid FROM tweets WHERE id=?', tid); return {r['uid']} if r else set()
    r = Q1('SELECT p.uid,b.owner,p.board FROM posts p LEFT JOIN boards b ON b.name=p.board WHERE p.id=?', tid)
    return ({r['uid'], r['owner']} | {m['uid'] for m in Q('SELECT uid FROM bmods WHERE board=?', r['board'])}) if r else set()
@route('/comments/<kind>/<tid>')
def comments(kind, tid):
    o = owners(kind, tid); after = int(request.args.get('after', 0))
    L = Q('SELECT c.id,c.uid,c.body,c.ts,c.parent,u.name author,u.color,u.avatar FROM comments c JOIN users u ON u.id=c.uid WHERE kind=? AND tid=? AND c.id>? ORDER BY c.id LIMIT 500', kind, tid, after)
    for c in L: c['can'] = c['uid'] == g.u or g.u in o or g.admin
    return L
@route('/comments/<kind>/<tid>', 'POST')
def comment(kind, tid):
    b = B(); t = cl(b.get('text'), 2000); par = b.get('parent')
    if kind not in ('content', 'tweet', 'post', 'live') or not t: raise E('Bad comment')
    if kind == 'post':
        p = Q1('SELECT board FROM posts WHERE id=?', tid)
        if not p: raise E('Post not found', 404)
        if Q1('SELECT 1 FROM bbans WHERE board=? AND uid=?', p['board'], g.u): raise E('You are banned from this board. You can still read it, but not comment.', 403)
    if par:
        pc = Q1('SELECT * FROM comments WHERE id=? AND kind=? AND tid=?', par, kind, tid)
        if not pc: raise E('That comment no longer exists', 404)
        par = pc['parent'] or pc['id']
        if pc['uid'] != g.u: notify(pc['uid'], f'{me_name()} replied to your comment: {t[:50]}', {'content': '#/content/watch/', 'live': '#/content/live/', 'tweet': '#/community/tweet/', 'post': '#/community/post/'}[kind] + tid, 'reply')
    if kind == 'tweet':
        o = Q1('SELECT uid FROM tweets WHERE id=?', tid); o and notify(o['uid'], f'{me_name()} replied to your post', f'#/community/tweet/{tid}', 'reply')
    if kind == 'post':
        o = Q1('SELECT uid FROM posts WHERE id=?', tid); o and notify(o['uid'], f'{me_name()} commented on your post', f'#/community/post/{tid}', 'reply')
    i = X('INSERT INTO comments(kind,tid,uid,body,ts,parent) VALUES(?,?,?,?,?,?)', kind, tid, g.u, t, now(), par or None)
    mentions(t, {'content': '#/content/watch/', 'live': '#/content/live/', 'tweet': '#/community/tweet/', 'post': '#/community/post/'}[kind] + tid); return dict(id=i)
@route('/comments/<int:i>', 'DELETE')
def comment_del(i):
    c = Q1('SELECT * FROM comments WHERE id=?', i)
    if not c: raise E('Not found', 404)
    if c['uid'] != g.u and g.u not in owners(c['kind'], c['tid']) and not g.admin: raise E('Not yours to delete', 403)
    X('DELETE FROM comments WHERE id=? OR parent=?', i, i); return {}

# ---------------- community: posts (tweets) + forum boards ----------------
def tweet_rows(where='', *a, limit=100):
    L = Q(f'''SELECT t.id,t.uid,t.body,t.ts,t.edited,u.name author,u.display,u.color,u.avatar,(SELECT COUNT(*) FROM votes WHERE kind='tweet' AND tid=t.id) likes,
      EXISTS(SELECT 1 FROM votes WHERE kind='tweet' AND tid=t.id AND uid={g.u}) liked,(SELECT COUNT(*) FROM votes WHERE kind='rt' AND tid=t.id) rts,EXISTS(SELECT 1 FROM votes WHERE kind='rt' AND tid=t.id AND uid={g.u}) rted,
      (SELECT COUNT(*) FROM comments WHERE kind='tweet' AND tid=t.id) replies FROM tweets t JOIN users u ON u.id=t.uid {where} ORDER BY t.id DESC LIMIT {limit}''', *a)
    for t in L: t['can'] = t['uid'] == g.u or g.admin
    return L
def followed(): return {r['b'] for r in Q('SELECT b FROM follows WHERE a=?', g.u)}
@route('/tweets')
def tweets():
    if request.args.get('mode') == 'following': return tweet_rows('WHERE t.uid IN (SELECT b FROM follows WHERE a=?) OR t.uid=?', g.u, g.u)
    f = followed(); L = tweet_rows(limit=200)
    return sorted(L, key=lambda t: -(t['ts'] + (43200 if t['uid'] in f else 0) + t['likes'] * 120 + t['rts'] * 240))[:100]
@route('/tweets', 'POST')
def tweet():
    t = cl(B().get('text'), 500)
    if not t: raise E('Write something first')
    i = X('INSERT INTO tweets(uid,body,ts) VALUES(?,?,?)', g.u, t, now()); mentions(t, f'#/community/tweet/{i}'); return dict(id=i)
@route('/tweets/<int:i>')
def tweet_get(i):
    r = tweet_rows('WHERE t.id=?', i)
    if not r: raise E('Post not found', 404)
    return r[0]
@route('/tweets/<int:i>', 'PUT')
def tweet_edit(i):
    t = cl(B().get('text'), 500)
    if not t or not Q1('SELECT 1 FROM tweets WHERE id=? AND (uid=? OR ?)', i, g.u, 1 if g.admin else 0): raise E('You cannot edit this post', 403)
    X('UPDATE tweets SET body=?,edited=1 WHERE id=?', t, i); return {}
def drop_tweet(i):
    for k in ('tweet', 'rt'): X('DELETE FROM votes WHERE kind=? AND tid=?', k, str(i))
    X("DELETE FROM comments WHERE kind='tweet' AND tid=?", str(i)); X('DELETE FROM tweets WHERE id=?', i)
@route('/tweets/<int:i>', 'DELETE')
def tweet_del(i):
    if not Q1('SELECT 1 FROM tweets WHERE id=? AND (uid=? OR ?)', i, g.u, 1 if g.admin else 0): raise E('Only the author can delete this post', 403)
    drop_tweet(i); return {}
@route('/follow', 'POST')
def follow():
    t = int(B().get('uid', 0))
    if t == g.u or not Q1('SELECT 1 FROM users WHERE id=?', t): raise E('Cannot follow')
    if Q1('SELECT 1 FROM follows WHERE a=? AND b=?', g.u, t): X('DELETE FROM follows WHERE a=? AND b=?', g.u, t); return dict(on=False)
    X('INSERT INTO follows VALUES(?,?)', g.u, t); notify(t, f'{me_name()} followed you', '#/u/' + me_name(), 'follow'); return dict(on=True)
def tags_of(s):
    out = []
    for t in (s if isinstance(s, list) else re.split(r'[,\n]', s or '')):
        t = re.sub(r'[^a-z0-9_\- ]', '', str(t).strip().lower()).strip().replace(' ', '-')[:20]
        if t and t not in out: out.append(t)
    return out[:12]
def is_bmod(b, uid=None):
    uid = uid or g.u; return g.admin or b['owner'] == uid or bool(Q1('SELECT 1 FROM bmods WHERE board=? AND uid=?', b['name'], uid))
@route('/boards')
def boards(): return Q('SELECT b.name,b.descr,b.tags,(b.owner=?) mine,(SELECT COUNT(*) FROM posts WHERE board=b.name) posts FROM boards b ORDER BY posts DESC,b.ts DESC', g.u)
@route('/boards', 'POST')
def board_new():
    b = B(); n = cl(b.get('name'), 21).lower().replace('b/', '')
    if not re.fullmatch(r'[a-z0-9_]{3,21}', n): raise E('Board name: 3-21 letters, numbers or _')
    if Q1('SELECT 1 FROM boards WHERE name=?', n): raise E('That board already exists', 409)
    X('INSERT INTO boards VALUES(?,?,?,?,?)', n, cl(b.get('descr'), 300), g.u, now(), ','.join(tags_of(b.get('tags')))); return dict(name=n)
def post_rows(where='', *a):
    L = Q(f'''SELECT p.id,p.board,p.title,p.body,p.tags,p.ts,p.edited,u.name author,u.id uid,u.color,u.avatar,COALESCE((SELECT SUM(v) FROM votes WHERE kind='post' AND tid=p.id),0) score,
      COALESCE((SELECT v FROM votes WHERE kind='post' AND tid=p.id AND uid={g.u}),0) mine,(SELECT COUNT(*) FROM comments WHERE kind='post' AND tid=p.id) ncomments FROM posts p JOIN users u ON u.id=p.uid {where}''', *a)
    mods = {r['name'] for r in Q('SELECT name FROM boards WHERE owner=? UNION SELECT board FROM bmods WHERE uid=?', g.u, g.u)}
    for p in L: p['can'] = g.admin or p['uid'] == g.u or p['board'] in mods; p['taglist'] = [t for t in (p['tags'] or '').split(',') if t]
    return L
@route('/boards/<n>')
def board(n):
    b = Q1('SELECT b.*,u.name ownername FROM boards b JOIN users u ON u.id=b.owner WHERE b.name=?', n.lower())
    if not b: raise E('No such board', 404)
    s, tag = request.args.get('sort', 'hot'), cl(request.args.get('tag'), 20); P = post_rows('WHERE board=?', b['name'])
    if tag: P = [p for p in P if tag in p['taglist']]
    P.sort(key=(lambda p: -p['ts']) if s == 'new' else (lambda p: -p['score']) if s == 'top' else (lambda p: -(p['score'] + 1) / (1 + (now() - p['ts']) / 86400) ** 1.3))
    mod = is_bmod(b); out = dict(name=b['name'], descr=b['descr'], tags=[t for t in b['tags'].split(',') if t], owner=b['ownername'], mine=b['owner'] == g.u or g.admin, mod=mod, banned=bool(Q1('SELECT 1 FROM bbans WHERE board=? AND uid=?', b['name'], g.u)), posts=P)
    if mod:
        out['mods'] = [r['name'] for r in Q('SELECT u.name FROM bmods m JOIN users u ON u.id=m.uid WHERE m.board=?', b['name'])]
        out['bans'] = Q('SELECT u.name,x.reason FROM bbans x JOIN users u ON u.id=x.uid WHERE x.board=?', b['name'])
    return out
@route('/boards/<n>', 'PUT')
def board_edit(n):
    b = Q1('SELECT * FROM boards WHERE name=?', n.lower())
    if not b or not is_bmod(b): raise E('Moderators only', 403)
    d = B(); X('UPDATE boards SET descr=?,tags=? WHERE name=?', cl(d.get('descr'), 300), ','.join(tags_of(d.get('tags'))), b['name']); return {}
@route('/boards/<n>/mods', 'POST')
def board_mods(n):
    b = Q1('SELECT * FROM boards WHERE name=?', n.lower()); d = B()
    if not b or not (g.admin or b['owner'] == g.u): raise E('Only the board creator can manage moderators', 403)
    u = Q1('SELECT id FROM users WHERE name=?', cl(d.get('name'), 20))
    if not u: raise E('No user with that name', 404)
    if d.get('on', True): X('INSERT OR IGNORE INTO bmods VALUES(?,?)', b['name'], u['id']); notify(u['id'], f"You are now a moderator of b/{b['name']}", f"#/community/b/{b['name']}")
    else: X('DELETE FROM bmods WHERE board=? AND uid=?', b['name'], u['id'])
    return {}
@route('/boards/<n>/ban', 'POST')
def board_ban(n):
    b = Q1('SELECT * FROM boards WHERE name=?', n.lower()); d = B()
    if not b or not is_bmod(b): raise E('Moderators only', 403)
    u = Q1('SELECT id FROM users WHERE name=?', cl(d.get('name'), 20))
    if not u: raise E('No user with that name', 404)
    if u['id'] == b['owner'] or (is_bmod(b, u['id']) and not (g.admin or b['owner'] == g.u)): raise E('You cannot ban the creator or another moderator', 403)
    if d.get('on', True): X('INSERT OR REPLACE INTO bbans VALUES(?,?,?)', b['name'], u['id'], cl(d.get('reason'), 100)); notify(u['id'], f"You were banned from posting in b/{b['name']}", f"#/community/b/{b['name']}")
    else: X('DELETE FROM bbans WHERE board=? AND uid=?', b['name'], u['id'])
    return {}
def drop_post(i): X("DELETE FROM comments WHERE kind='post' AND tid=?", str(i)); X("DELETE FROM votes WHERE kind='post' AND tid=?", str(i)); X('DELETE FROM posts WHERE id=?', i)
def drop_board(n):
    for p in Q('SELECT id FROM posts WHERE board=?', n): drop_post(p['id'])
    for t in ('bmods', 'bbans'): X(f'DELETE FROM {t} WHERE board=?', n)
    X('DELETE FROM boards WHERE name=?', n)
@route('/boards/<n>', 'DELETE')
def board_del(n):
    b = Q1('SELECT * FROM boards WHERE name=?', n.lower())
    if not b or not (b['owner'] == g.u or g.admin): raise E('Only the board creator can delete it', 403)
    drop_board(b['name']); return {}
def check_tags(b, raw):
    sel, allowed = tags_of(raw)[:5], [t for t in b['tags'].split(',') if t]
    if allowed and any(t not in allowed for t in sel): raise E('Tags must be from: ' + ', '.join(allowed))
    return ','.join(sel)
@route('/boards/<n>/posts', 'POST')
def post_new(n):
    d = B(); t = cl(d.get('title'), 200); b = Q1('SELECT * FROM boards WHERE name=?', n.lower())
    if not b: raise E('No such board', 404)
    if Q1('SELECT 1 FROM bbans WHERE board=? AND uid=?', b['name'], g.u): raise E(f"You are banned from b/{b['name']}. You can still read it, but not post.", 403)
    if not t: raise E('Title required')
    i = X('INSERT INTO posts(board,uid,title,body,tags,ts) VALUES(?,?,?,?,?,?)', b['name'], g.u, t, cl(d.get('body'), 10000), check_tags(b, d.get('tags')), now())
    mentions(t + ' ' + cl(d.get('body'), 10000), f'#/community/post/{i}'); return dict(id=i)
@route('/posts/<int:i>', 'PUT')
def post_edit(i):
    p = Q1('SELECT p.*,b.owner FROM posts p JOIN boards b ON b.name=p.board WHERE p.id=?', i); d = B()
    if not p: raise E('Not found', 404)
    b = Q1('SELECT * FROM boards WHERE name=?', p['board'])
    if p['uid'] != g.u and not is_bmod(b): raise E('You cannot edit this post', 403)
    X('UPDATE posts SET title=?,body=?,tags=?,edited=1 WHERE id=?', cl(d.get('title'), 200) or p['title'], cl(d.get('body'), 10000), check_tags(b, d.get('tags')) if 'tags' in d else p['tags'], i); return {}
@route('/posts/<int:i>')
def post_get(i):
    r = post_rows('WHERE p.id=?', i)
    if not r: raise E('No such post', 404)
    r[0]['banned'] = bool(Q1('SELECT 1 FROM bbans WHERE board=? AND uid=?', r[0]['board'], g.u)); return r[0]
@route('/posts/<int:i>', 'DELETE')
def post_del(i):
    p = Q1('SELECT * FROM posts WHERE id=?', i); b = p and Q1('SELECT * FROM boards WHERE name=?', p['board'])
    if not p or (p['uid'] != g.u and not is_bmod(b)): raise E('Only the author or a moderator can remove this', 403)
    drop_post(i); return {}
@route('/home/posts')
def home_posts():
    f = followed(); L = post_rows('WHERE 1=1 ORDER BY p.id DESC LIMIT 200')
    return sorted(L, key=lambda p: -(p['ts'] + (43200 if p['uid'] in f else 0) + p['score'] * 120))[:40]

# ---------------- social: friends / blocks / mutes ----------------
def rel(a, b):
    if Q1('SELECT 1 FROM blocks WHERE a=? AND b=?', a, b): return 'blocked'
    if Q1('SELECT 1 FROM blocks WHERE a=? AND b=?', b, a): return 'blockedby'
    f = Q1('SELECT * FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)', a, b, b, a)
    if not f: return 'none'
    return 'friends' if f['st'] == 'friends' else ('outgoing' if f['a'] == a else 'incoming')
@route('/friends')
def friends():
    out = dict(friends=[], incoming=[], outgoing=[], blocked=[]); rows = Q('SELECT * FROM friends WHERE a=? OR b=?', g.u, g.u); ids = {r['a'] if r['b'] == g.u else r['b'] for r in rows} | {r['b'] for r in Q('SELECT b FROM blocks WHERE a=?', g.u)}
    U = {u['id']: udict(u) for u in Q(f'SELECT * FROM users WHERE id IN ({IN(ids)})')}
    for f in rows:
        o = U.get(f['b'] if f['a'] == g.u else f['a'])
        if o: out['friends' if f['st'] == 'friends' else ('outgoing' if f['a'] == g.u else 'incoming')].append(o)
    out['blocked'] = [U[r['b']] for r in Q('SELECT b FROM blocks WHERE a=?', g.u) if r['b'] in U]; return out
@route('/friends/add', 'POST')
def f_add():
    u = Q1('SELECT id,name FROM users WHERE name=? AND banned=0', cl(B().get('name'), 20))
    if not u: raise E('No user with that name', 404)
    if u['id'] == g.u: raise E("You can't add yourself")
    r = rel(g.u, u['id'])
    if r in ('blocked', 'blockedby'): raise E('Cannot add this user')
    if r == 'friends': raise E('Already friends')
    if r == 'outgoing': raise E('Request already sent')
    if r == 'incoming': return f_accept_id(u['id'])
    X('INSERT INTO friends VALUES(?,?,?)', g.u, u['id'], 'pending'); notify(u['id'], f'{me_name()} sent you a friend request', '#/social/friends', 'friend'); return dict(sent=True)
def f_accept_id(i): X('UPDATE friends SET st="friends" WHERE a=? AND b=?', i, g.u); notify(i, f'{me_name()} accepted your friend request', '#/social/friends', 'friend'); return dict(ok=True)
@route('/friends/accept', 'POST')
def f_accept(): return f_accept_id(int(B().get('uid', 0)))
@route('/friends/remove', 'POST')
def f_remove(): i = int(B().get('uid', 0)); X('DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)', g.u, i, i, g.u); return {}
@route('/block', 'POST')
def block():
    i = int(B().get('uid', 0))
    if i == g.u: raise E('Nope')
    X('DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)', g.u, i, i, g.u); X('INSERT OR IGNORE INTO blocks VALUES(?,?)', g.u, i); return {}
@route('/unblock', 'POST')
def unblock(): X('DELETE FROM blocks WHERE a=? AND b=?', g.u, int(B().get('uid', 0))); return {}
@route('/mute', 'POST')
def mute():
    i = int(B().get('uid', 0))
    if i == g.u: raise E('You cannot mute yourself')
    if Q1('SELECT 1 FROM mutes WHERE a=? AND b=?', g.u, i): X('DELETE FROM mutes WHERE a=? AND b=?', g.u, i); return dict(on=False)
    X('INSERT INTO mutes VALUES(?,?)', g.u, i); return dict(on=True)

# ---------------- conversations: dm / group / server channels ----------------
def conv_get(cid):
    c = Q1('SELECT * FROM convs WHERE id=?', cid)
    if not c: raise E('No such conversation', 404)
    return c
def sperms(sid, uid):
    s = Q1('SELECT owner FROM servers WHERE id=?', sid)
    if not s or not Q1('SELECT 1 FROM smembers WHERE server=? AND uid=?', sid, uid): return set()
    if s['owner'] == uid: return set(PERMS)
    ps = set()
    for r in Q('SELECT perms FROM roles WHERE server=? AND (base=1 OR id IN (SELECT role FROM mroles WHERE server=? AND uid=?))', sid, sid, uid): ps |= set(filter(None, r['perms'].split(',')))
    return set(PERMS) if 'admin' in ps else ps
def need(sid, perm):
    if perm not in sperms(sid, g.u): raise E('You lack the "%s" permission' % perm, 403)
def server_chans(sid):
    chans = Q('SELECT * FROM convs WHERE server=? ORDER BY pos,id', sid); cr = {}
    for r in Q('SELECT * FROM chroles WHERE conv IN (SELECT id FROM convs WHERE server=?)', sid): cr.setdefault(r['conv'], []).append((r['role'], r['can_view'], r['can_send']))
    return chans, {c['id']: c for c in chans}, cr
def chan_ok(c, by, roles, adm, cr, mode):
    if adm: return True
    x = c
    while x:
        if mode == 'view' and x['private'] and not any(v and r in roles for r, v, s in cr.get(x['id'], [])): return False
        if mode == 'send' and x['readonly'] and not any(s and r in roles for r, v, s in cr.get(x['id'], [])): return False
        x = by.get(x['parent'])
    return True
def my_roles(sid, uid): return {r['role'] for r in Q('SELECT role FROM mroles WHERE server=? AND uid=?', sid, uid)}
def access(c, perm=None):
    if c['kind'] in ('dm', 'group'):
        if not Q1('SELECT 1 FROM cmembers WHERE conv=? AND uid=?', c['id'], g.u): raise E('Not in this chat', 403)
    elif c['kind'] in ('text', 'voice'):
        ps = sperms(c['server'], g.u)
        if not ps: raise E('Not in this server', 403)
        chans, by, cr = server_chans(c['server']); roles = my_roles(c['server'], g.u); adm = 'admin' in ps
        if not chan_ok(c, by, roles, adm, cr, 'view'): raise E('You cannot see this channel', 403)
        if perm and perm not in ps: raise E('You lack the "%s" permission' % perm, 403)
        if perm == 'send' and not chan_ok(c, by, roles, adm, cr, 'send'): raise E('This channel is read-only for you', 403)
    else: raise E('Not a chat channel')
@route('/convs')
def convs():
    cs = Q('SELECT c.* FROM convs c JOIN cmembers m ON m.conv=c.id WHERE m.uid=?', g.u)
    if not cs: return []
    ids = IN(c['id'] for c in cs); mem = {}
    for r in Q(f'SELECT m.conv,u.* FROM cmembers m JOIN users u ON u.id=m.uid WHERE m.conv IN ({ids})'): mem.setdefault(r['conv'], []).append(udict(r))
    last = {r['conv']: r for r in Q(f'SELECT conv,body,ts,id FROM msgs WHERE id IN (SELECT MAX(id) FROM msgs WHERE conv IN ({ids}) GROUP BY conv)')}
    un = {r['conv']: r['n'] for r in Q(f'SELECT m.conv,COUNT(*) n FROM msgs m WHERE m.conv IN ({ids}) AND m.uid!=? AND m.id>COALESCE((SELECT last FROM reads r WHERE r.conv=m.conv AND r.uid=?),0) GROUP BY m.conv', g.u, g.u)}
    out = []
    for c in cs:
        m = mem.get(c['id'], []); other = [x for x in m if x['id'] != g.u]; l = last.get(c['id'], {})
        out.append(dict(id=c['id'], kind=c['kind'], name=c['name'] if c['kind'] == 'group' else (other[0]['display'] if other else 'Empty'), members=m, other=other[0] if c['kind'] == 'dm' and other else None, last=(l.get('body', '') or '')[:80], lastid=l.get('id', 0), ts=l.get('ts', 0), unread=un.get(c['id'], 0)))
    return sorted(out, key=lambda c: -c['lastid'])
@route('/dm', 'POST')
def dm():
    o = int(B().get('uid', 0))
    if not Q1('SELECT 1 FROM users WHERE id=?', o) or o == g.u: raise E('Cannot message that user')
    if rel(g.u, o) in ('blocked', 'blockedby'): raise E('You cannot message this user')
    c = Q1("SELECT c.id FROM convs c WHERE c.kind='dm' AND EXISTS(SELECT 1 FROM cmembers WHERE conv=c.id AND uid=?) AND EXISTS(SELECT 1 FROM cmembers WHERE conv=c.id AND uid=?)", g.u, o)
    if c: return dict(id=c['id'])
    i = X("INSERT INTO convs(kind) VALUES('dm')"); X('INSERT INTO cmembers VALUES(?,?)', i, g.u); X('INSERT INTO cmembers VALUES(?,?)', i, o); return dict(id=i)
@route('/group', 'POST')
def group():
    b = B(); ids = {int(x) for x in b.get('uids', [])} - {g.u}
    if not ids: raise E('Pick at least one friend')
    if len(ids) + 1 > 10: raise E('Group chats hold up to 10 people')
    for i in ids:
        if rel(g.u, i) != 'friends': raise E('You can only add friends to a group')
    c = X("INSERT INTO convs(kind,name,owner) VALUES('group',?,?)", cl(b.get('name'), 40) or 'Group chat', g.u)
    for i in ids | {g.u}: X('INSERT INTO cmembers VALUES(?,?)', c, i)
    return dict(id=c)
@route('/group/<int:cid>/add', 'POST')
def group_add(cid):
    c = conv_get(cid); access(c); i = int(B().get('uid', 0))
    if c['kind'] != 'group': raise E('Not a group')
    if Q1('SELECT COUNT(*) n FROM cmembers WHERE conv=?', cid)['n'] >= 10: raise E('Group chats hold up to 10 people')
    if rel(g.u, i) != 'friends': raise E('You can only add friends')
    X('INSERT OR IGNORE INTO cmembers VALUES(?,?)', cid, i); return {}
@route('/group/<int:cid>/leave', 'POST')
def group_leave(cid): X('DELETE FROM cmembers WHERE conv=? AND uid=?', cid, g.u); return {}
@route('/convs/<int:cid>/msgs')
def msgs_get(cid):
    c = conv_get(cid); access(c)
    L = Q('SELECT m.id,m.body,m.ts,m.edited,u.id uid,u.name author,u.display,u.color,u.avatar FROM msgs m JOIN users u ON u.id=m.uid WHERE conv=? ORDER BY m.id DESC LIMIT 100', cid)[::-1]
    if L: X('INSERT OR REPLACE INTO reads VALUES(?,?,?)', cid, g.u, L[-1]['id'])
    mod = c['kind'] in ('text', 'voice') and 'messages' in sperms(c['server'], g.u)
    for m in L: m['can'] = m['uid'] == g.u or mod or g.admin
    return L
@route('/convs/<int:cid>/msgs', 'POST')
def msgs_post(cid):
    c = conv_get(cid); access(c, 'send' if c['kind'] == 'text' else None); t = cl(B().get('text'), 4000)
    if not t: raise E('Empty message')
    if c['kind'] == 'dm':
        o = Q1('SELECT uid FROM cmembers WHERE conv=? AND uid!=?', cid, g.u)
        if o and rel(g.u, o['uid']) in ('blocked', 'blockedby'): raise E('You cannot message this user', 403)
        o and notify(o['uid'], f'{me_name()}: {t[:60]}', f'#/social/dm/{cid}', 'dm')
    i = X('INSERT INTO msgs(conv,uid,body,ts) VALUES(?,?,?,?)', cid, g.u, t, now())
    mentions(t, f'#/social/dm/{cid}' if c['kind'] in ('dm', 'group') else f"#/social/s/{c['server']}/{cid}"); return dict(id=i)
@route('/msgs/<int:i>', 'DELETE')
def msg_del(i):
    m = Q1('SELECT * FROM msgs WHERE id=?', i)
    if not m: raise E('Not found', 404)
    c = Q1('SELECT * FROM convs WHERE id=?', m['conv'])
    if m['uid'] != g.u and not g.admin and not (c['server'] and 'messages' in sperms(c['server'], g.u)): raise E('Not yours to delete', 403)
    X('DELETE FROM msgs WHERE id=?', i); return {}
@route('/msgs/<int:i>', 'PUT')
def msg_edit(i):
    t = cl(B().get('text'), 4000)
    if not t or not Q1('SELECT 1 FROM msgs WHERE id=? AND uid=?', i, g.u): raise E('Cannot edit this message', 403)
    X('UPDATE msgs SET body=?,edited=1 WHERE id=?', t, i); return {}

# ---------------- servers, channels, roles ----------------
@route('/servers')
def servers_mine():
    L = Q('SELECT s.id,s.name,s.color,s.owner,s.invite FROM servers s JOIN smembers m ON m.server=s.id WHERE m.uid=? ORDER BY s.id', g.u)
    for s in L:
        ps = sperms(s['id'], g.u); s['perms'] = sorted(ps); s['mine'] = s['owner'] == g.u
        if 'invite' not in ps: s['invite'] = None
    return L
def base_role(sid): return X('INSERT INTO roles(server,name,perms,color,base) VALUES(?,?,?,?,1)', sid, '@everyone', 'send,connect', '#8b95a8')
@route('/servers', 'POST')
def server_new():
    n = cl(B().get('name'), 40)
    if not n: raise E('Name your server')
    s = X('INSERT INTO servers(name,owner,invite,color) VALUES(?,?,?,?)', n, g.u, secrets.token_urlsafe(6), COLORS[len(n) % 6]); X('INSERT INTO smembers VALUES(?,?)', s, g.u); base_role(s)
    cat = X("INSERT INTO convs(kind,name,server,owner) VALUES('category','Text Channels',?,?)", s, g.u); X("INSERT INTO convs(kind,name,server,parent,pos) VALUES('text','general',?,?,1)", s, cat)
    v = X("INSERT INTO convs(kind,name,server,owner,pos) VALUES('category','Voice Channels',?,?,1)", s, g.u); X("INSERT INTO convs(kind,name,server,parent,pos) VALUES('voice','Lounge',?,?,1)", s, v); return dict(id=s)
@route('/invite/<code>')
def invite_info(code):
    s = Q1('SELECT * FROM servers WHERE invite=?', code)
    if not s: raise E('This invite is invalid or expired', 404)
    return dict(id=s['id'], name=s['name'], color=s['color'], members=Q1('SELECT COUNT(*) n FROM smembers WHERE server=?', s['id'])['n'], joined=bool(Q1('SELECT 1 FROM smembers WHERE server=? AND uid=?', s['id'], g.u)), banned=bool(Q1('SELECT 1 FROM bans WHERE server=? AND uid=?', s['id'], g.u)))
@route('/servers/join', 'POST')
def server_join():
    s = Q1('SELECT * FROM servers WHERE invite=?', cl(B().get('code'), 40))
    if not s: raise E('Invalid invite code', 404)
    if Q1('SELECT 1 FROM bans WHERE server=? AND uid=?', s['id'], g.u): raise E('You are banned from this server', 403)
    X('INSERT OR IGNORE INTO smembers VALUES(?,?)', s['id'], g.u); return dict(id=s['id'])
@route('/servers/<int:s>')
def server_get(s):
    me_p = sperms(s, g.u)
    if not me_p: raise E('Not in this server', 403)
    sv = Q1('SELECT * FROM servers WHERE id=?', s); chans, by, cr = server_chans(s); roles = my_roles(s, g.u); adm = 'admin' in me_p
    vis = [c for c in chans if chan_ok(c, by, roles, adm, cr, 'view')]; manage = 'channels' in me_p
    for c in vis:
        if manage: c['view_roles'] = [r for r, v, x in cr.get(c['id'], []) if v]; c['send_roles'] = [r for r, v, x in cr.get(c['id'], []) if x]
    allr = Q('SELECT * FROM roles WHERE server=? ORDER BY id', s); mr = {}
    for r in Q('SELECT uid,role FROM mroles WHERE server=?', s): mr.setdefault(r['uid'], []).append(r['role'])
    mem = [dict(udict(u), roles=mr.get(u['id'], []), owner=u['id'] == sv['owner']) for u in Q('SELECT u.* FROM users u JOIN smembers m ON m.uid=u.id WHERE m.server=?', s)]
    vc = {c['id']: live_room(f"c{c['id']}") for c in vis if c['kind'] == 'voice'}; names = {u['id']: u for u in Q(f"SELECT id,name,display,color,avatar FROM users WHERE id IN ({IN({u for l in vc.values() for u in l})})")}
    return dict(id=s, name=sv['name'], owner=sv['owner'], invite=sv['invite'] if 'invite' in me_p else None, channels=vis, roles=allr, members=mem, perms=sorted(me_p), voice={k: [dict(id=u, name=names.get(u, {}).get('name', '?'), display=names.get(u, {}).get('display'), color=names.get(u, {}).get('color'), avatar=names.get(u, {}).get('avatar')) for u in v] for k, v in vc.items()},
                bans=(Q('SELECT u.id,u.name FROM bans b JOIN users u ON u.id=b.uid WHERE b.server=?', s) if 'ban' in me_p else []))
@route('/servers/<int:s>/channels', 'POST')
def chan_new(s):
    need(s, 'channels'); b = B(); n, k, p = cl(b.get('name'), 40), b.get('kind'), b.get('parent')
    if k not in ('text', 'voice', 'category') or not n: raise E('Name and type required')
    if p and not Q1('SELECT 1 FROM convs WHERE id=? AND server=? AND kind="category"', p, s): raise E('Parent must be a category in this server')
    if k == 'text': n = re.sub(r'\s+', '-', n.lower())
    return dict(id=X('INSERT INTO convs(kind,name,server,parent,owner,pos) VALUES(?,?,?,?,?,?)', k, n, s, p or None, g.u, 999))
@route('/channels/<int:cid>', 'DELETE')
def chan_del(cid):
    c = conv_get(cid); need(c['server'], 'channels')
    def rm(i):
        for ch in Q('SELECT id FROM convs WHERE parent=?', i): rm(ch['id'])
        X('DELETE FROM msgs WHERE conv=?', i); X('DELETE FROM chroles WHERE conv=?', i); X('DELETE FROM convs WHERE id=?', i)
    rm(cid); return {}
@route('/channels/<int:cid>', 'PUT')
def chan_rename(cid):
    c = conv_get(cid); need(c['server'], 'channels'); n = cl(B().get('name'), 40)
    if not n: raise E('Name required')
    X('UPDATE convs SET name=? WHERE id=?', re.sub(r'\s+', '-', n.lower()) if c['kind'] == 'text' else n, cid); return {}
@route('/channels/<int:cid>/perms', 'POST')
def chan_perms(cid):
    c = conv_get(cid); need(c['server'], 'channels'); b = B(); valid = {r['id'] for r in Q('SELECT id FROM roles WHERE server=? AND base=0', c['server'])}
    vr, sr = {int(x) for x in b.get('view_roles', [])} & valid, {int(x) for x in b.get('send_roles', [])} & valid
    X('UPDATE convs SET private=?,readonly=? WHERE id=?', 1 if b.get('private') else 0, 1 if b.get('readonly') else 0, cid); X('DELETE FROM chroles WHERE conv=?', cid)
    for r in vr | sr: X('INSERT INTO chroles VALUES(?,?,?,?)', cid, r, 1 if r in vr else 0, 1 if r in sr else 0)
    return {}
@route('/servers/<int:s>/move', 'POST')
def chan_move(s):
    need(s, 'channels'); b = B(); i, p = int(b.get('id', 0)), b.get('parent') or None
    if not Q1('SELECT 1 FROM convs WHERE id=? AND server=?', i, s): raise E('No such channel', 404)
    if p:
        p = int(p)
        if not Q1("SELECT 1 FROM convs WHERE id=? AND server=? AND kind='category'", p, s): raise E('Target must be a category')
        x = p
        while x:
            if x == i: raise E('A category cannot go inside itself')
            x = Q1('SELECT parent FROM convs WHERE id=?', x)['parent']
    sib = [r['id'] for r in Q('SELECT id FROM convs WHERE server=? AND parent IS ? AND id!=? ORDER BY pos,id', s, p, i)]
    sib.insert(max(0, min(int(b.get('index', 9999)), len(sib))), i)
    for n, cid in enumerate(sib): X('UPDATE convs SET parent=?,pos=? WHERE id=?', p, n, cid)
    return {}
def role_perms(s, b):
    ps = [p for p in b.get('perms', []) if p in PERMS]
    if 'admin' in ps and 'admin' not in sperms(s, g.u): ps.remove('admin')
    return ','.join(ps)
@route('/servers/<int:s>/roles', 'POST')
def role_new(s):
    need(s, 'roles'); b = B(); n = cl(b.get('name'), 30)
    if not n: raise E('Role name required')
    return dict(id=X('INSERT INTO roles(server,name,perms,color) VALUES(?,?,?,?)', s, n, role_perms(s, b), cl(b.get('color'), 9) or '#7c5cff'))
@route('/roles/<int:rid>', 'PUT')
def role_edit(rid):
    r = Q1('SELECT * FROM roles WHERE id=?', rid); need(r['server'], 'roles'); b = B()
    X('UPDATE roles SET name=?,perms=?,color=? WHERE id=?', r['name'] if r['base'] else (cl(b.get('name'), 30) or r['name']), role_perms(r['server'], b), cl(b.get('color'), 9) or r['color'], rid); return {}
@route('/roles/<int:rid>', 'DELETE')
def role_del(rid):
    r = Q1('SELECT * FROM roles WHERE id=?', rid); need(r['server'], 'roles')
    if r['base']: raise E('Cannot delete @everyone')
    X('DELETE FROM mroles WHERE role=?', rid); X('DELETE FROM chroles WHERE role=?', rid); X('DELETE FROM roles WHERE id=?', rid); return {}
@route('/servers/<int:s>/assign', 'POST')
def assign(s):
    need(s, 'roles'); b = B(); r = Q1('SELECT * FROM roles WHERE id=? AND server=?', b.get('role'), s)
    if not r or r['base']: raise E('Bad role')
    if 'admin' in r['perms'] and 'admin' not in sperms(s, g.u): raise E('Only admins can hand out admin roles', 403)
    if b.get('on'): X('INSERT OR IGNORE INTO mroles VALUES(?,?,?)', s, int(b['uid']), r['id'])
    else: X('DELETE FROM mroles WHERE server=? AND uid=? AND role=?', s, int(b['uid']), r['id'])
    return {}
@route('/servers/<int:s>/kick', 'POST')
def kick(s):
    need(s, 'kick'); i = int(B().get('uid', 0))
    if i == Q1('SELECT owner FROM servers WHERE id=?', s)['owner']: raise E('You cannot kick the owner', 403)
    X('DELETE FROM smembers WHERE server=? AND uid=?', s, i); X('DELETE FROM mroles WHERE server=? AND uid=?', s, i); return {}
@route('/servers/<int:s>/ban', 'POST')
def ban(s):
    need(s, 'ban'); i = int(B().get('uid', 0))
    if i == Q1('SELECT owner FROM servers WHERE id=?', s)['owner'] or i == g.u: raise E('You cannot ban that user', 403)
    X('INSERT OR IGNORE INTO bans VALUES(?,?)', s, i); X('DELETE FROM smembers WHERE server=? AND uid=?', s, i); X('DELETE FROM mroles WHERE server=? AND uid=?', s, i); return {}
@route('/servers/<int:s>/unban', 'POST')
def unban(s): need(s, 'ban'); X('DELETE FROM bans WHERE server=? AND uid=?', s, int(B().get('uid', 0))); return {}
@route('/servers/<int:s>/leave', 'POST')
def s_leave(s):
    if Q1('SELECT owner FROM servers WHERE id=?', s)['owner'] == g.u: raise E('Owners cannot leave. Delete the server instead.')
    X('DELETE FROM smembers WHERE server=? AND uid=?', s, g.u); X('DELETE FROM mroles WHERE server=? AND uid=?', s, g.u); return {}
@route('/servers/<int:s>', 'PUT')
def server_rename(s):
    need(s, 'admin'); n = cl(B().get('name'), 40)
    if n: X('UPDATE servers SET name=? WHERE id=?', n, s)
    return {}
def drop_server(s):
    for c in Q('SELECT id FROM convs WHERE server=?', s): X('DELETE FROM msgs WHERE conv=?', c['id']); X('DELETE FROM chroles WHERE conv=?', c['id'])
    for t in ('convs', 'smembers', 'roles', 'mroles', 'bans'): X(f'DELETE FROM {t} WHERE server=?', s)
    X('DELETE FROM servers WHERE id=?', s)
@route('/servers/<int:s>', 'DELETE')
def s_del(s):
    sv = Q1('SELECT owner FROM servers WHERE id=?', s)
    if not sv or (sv['owner'] != g.u and not g.admin): raise E('Only the owner can delete this server', 403)
    drop_server(s); return {}

# ---------------- calls / live: WebRTC signalling. First joiner is the host; everyone else connects to the host ----------------
R, JT, SIG = {}, {}, {}
def live_room(room): return [u for u, t in R.get(room, {}).items() if now() - t < 15]
def room_check(room):
    if room.startswith('live'):
        if not Q1('SELECT 1 FROM content WHERE id=? AND kind="live"', int(room[4:])): raise E('This stream has ended', 404)
        return None
    c = conv_get(int(room[1:])); access(c, 'connect' if c['kind'] == 'voice' else None); return c
def room_host(room):
    if room.startswith('live'): c = Q1('SELECT uid FROM content WHERE id=?', int(room[4:])); return c['uid'] if c else None
    act = live_room(room); return min(act, key=lambda u: JT.get((room, u), now())) if act else None
def peers(room):
    ids = [u for u in live_room(room) if u != g.u]; names = {u['id']: u for u in Q(f'SELECT id,name,display,color,avatar FROM users WHERE id IN ({IN(ids)})')}
    return [dict(id=u, **{k: names.get(u, {}).get(k) or ('?' if k == 'name' else None) for k in ('name', 'display', 'color', 'avatar')}) for u in ids]
@route('/rtc/<room>/join', 'POST')
def rtc_join(room):
    c = room_check(room)
    if room.startswith('live'):
        lv = Q1('SELECT uid FROM content WHERE id=?', int(room[4:]))
        if lv['uid'] == g.u: LAST[int(room[4:])] = now()
    P = peers(room)
    if c and c['kind'] in ('dm', 'group') and not P:
        for m in Q('SELECT uid FROM cmembers WHERE conv=? AND uid!=?', c['id'], g.u): notify(m['uid'], f'{me_name()} is calling you', f'#/social/dm/{c["id"]}?call=1', 'call')
    act = live_room(room)
    if g.u not in act:
        cap = CFG('calls', 'max_viewers', int) if room.startswith('live') else CFG('calls', 'max_call_size', int)
        if cap and len(act) >= cap + (1 if room.startswith('live') else 0): raise E('This call is full' if not room.startswith('live') else 'This stream is at its viewer limit', 403)
        JT[(room, g.u)] = now()
    R.setdefault(room, {})[g.u] = now(); SIG[(room, g.u)] = []
    return dict(me=g.u, host=room_host(room), peers=P)
@route('/rtc/<room>/poll', 'POST')
def rtc_poll(room):
    R.setdefault(room, {})[g.u] = now(); s = SIG.get((room, g.u), []); SIG[(room, g.u)] = []
    if room.startswith('live'):
        i = int(room[4:]); c = Q1('SELECT uid FROM content WHERE id=?', i)
        if not c: return dict(sigs=[], peers=[], host=None, ended=True)
        if c['uid'] == g.u: LAST[i] = now()
        X('UPDATE content SET viewers=? WHERE id=?', max(0, len(live_room(room)) - 1), i)
    return dict(sigs=s, peers=peers(room), host=room_host(room))
@route('/rtc/<room>/signal', 'POST')
def rtc_sig(room): b = B(); SIG.setdefault((room, int(b['to'])), []).append(dict(frm=g.u, data=b['data'])); return {}
@route('/rtc/<room>/leave', 'POST')
def rtc_leave(room): R.get(room, {}).pop(g.u, None); JT.pop((room, g.u), None); return {}

# ---------------- site admin ----------------
def drop_user_files(u):
    rmfile(u['avatar']); rmfile(u['banner'])
def purge_user(uid):
    u = Q1('SELECT * FROM users WHERE id=?', uid)
    if not u: return
    for c in Q('SELECT id FROM content WHERE uid=?', uid): drop_content(c['id'])
    for t in Q('SELECT id FROM tweets WHERE uid=?', uid): drop_tweet(t['id'])
    for p in Q('SELECT id FROM posts WHERE uid=?', uid): drop_post(p['id'])
    for b in Q('SELECT name FROM boards WHERE owner=?', uid): drop_board(b['name'])
    for s in Q('SELECT id FROM servers WHERE owner=?', uid): drop_server(s['id'])
    X('DELETE FROM comments WHERE parent IN (SELECT id FROM comments WHERE uid=?)', uid); X('DELETE FROM comments WHERE uid=?', uid); X('DELETE FROM msgs WHERE uid=?', uid)
    for c in Q("SELECT c.id FROM convs c JOIN cmembers m ON m.conv=c.id WHERE c.kind='dm' AND m.uid=?", uid): X('DELETE FROM msgs WHERE conv=?', c['id']); X('DELETE FROM cmembers WHERE conv=?', c['id']); X('DELETE FROM convs WHERE id=?', c['id'])
    for t, cols in (('cmembers', ('uid',)), ('smembers', ('uid',)), ('mroles', ('uid',)), ('bans', ('uid',)), ('bmods', ('uid',)), ('bbans', ('uid',)), ('follows', ('a', 'b')), ('friends', ('a', 'b')), ('blocks', ('a', 'b')), ('mutes', ('a', 'b')),
                    ('votes', ('uid',)), ('notifs', ('uid',)), ('cart', ('uid',)), ('owned', ('uid',)), ('orders', ('uid',)), ('tokens', ('uid',)), ('reads', ('uid',))):
        X(f'DELETE FROM {t} WHERE ' + ' OR '.join(f'{c}=?' for c in cols), *([uid] * len(cols)))
    drop_user_files(u); X('DELETE FROM users WHERE id=?', uid); forget(uid)
@route('/admin/stats', admin=True)
def adm_stats(): return {k: Q1(f'SELECT COUNT(*) n FROM {t}')['n'] for k, t in dict(users='users', videos='content', posts='posts', tweets='tweets', boards='boards', servers='servers', products='products').items()}
@route('/admin/users', admin=True)
def adm_users():
    q = re.sub(r'[%_]', '', cl(request.args.get('q'), 20)); return Q('SELECT id,name,display,admin,banned,ban_reason,created,seen FROM users WHERE name LIKE ? ORDER BY created DESC LIMIT 100', '%' + q + '%')
@route('/admin/ban', 'POST', admin=True)
def adm_ban():
    b = B(); u = Q1('SELECT * FROM users WHERE id=?', b.get('uid'))
    if not u: raise E('No such user', 404)
    if u['id'] == g.u: raise E('You cannot ban yourself')
    if b.get('banned', True):
        X('UPDATE users SET banned=1,ban_reason=? WHERE id=?', cl(b.get('reason'), 100), u['id']); X('DELETE FROM tokens WHERE uid=?', u['id'])
        for c in Q('SELECT id FROM content WHERE uid=? AND kind="live"', u['id']): drop_content(c['id'])
    else: X('UPDATE users SET banned=0,ban_reason=NULL WHERE id=?', u['id'])
    forget(u['id']); return {}
@route('/admin/admin', 'POST', admin=True)
def adm_admin():
    b = B(); u = Q1('SELECT * FROM users WHERE name=? OR id=?', cl(b.get('name'), 20), b.get('uid') or 0)
    if not u: raise E('No user with that name', 404)
    on = 1 if b.get('on', True) else 0
    if not on and Q1('SELECT COUNT(*) n FROM users WHERE admin=1')['n'] < 2: raise E('There must be at least one site admin')
    X('UPDATE users SET admin=? WHERE id=?', on, u['id']); forget(u['id']); return {}
@route('/admin/users/<int:uid>', 'PUT', admin=True)
def adm_user_edit(uid):
    b = B(); u = Q1('SELECT * FROM users WHERE id=?', uid)
    if not u: raise E('No such user', 404)
    if 'name' in b:
        n = cl(b['name'], 20)
        if not re.fullmatch(r'[A-Za-z0-9_]{3,20}', n) or Q1('SELECT 1 FROM users WHERE name=? AND id!=?', n, uid): raise E('Invalid or taken username')
        X('UPDATE users SET name=? WHERE id=?', n, uid)
    if 'display' in b: X('UPDATE users SET display=? WHERE id=?', cl(b['display'], 30) or u['name'], uid)
    if 'bio' in b: X('UPDATE users SET bio=? WHERE id=?', cl(b['bio'], 300), uid)
    if b.get('clear_avatar'): rmfile(u['avatar']); X('UPDATE users SET avatar=NULL WHERE id=?', uid)
    if b.get('clear_banner'): rmfile(u['banner']); X('UPDATE users SET banner=NULL WHERE id=?', uid)
    if b.get('password'):
        if len(str(b['password'])) < 6: raise E('Password needs 6+ characters')
        s = secrets.token_hex(16); X('UPDATE users SET salt=?,pw=? WHERE id=?', s, hp(str(b['password']), s), uid); X('DELETE FROM tokens WHERE uid=?', uid); forget(uid)
    return {}
@route('/admin/users/<int:uid>', 'DELETE', admin=True)
def adm_user_del(uid):
    u = Q1('SELECT * FROM users WHERE id=?', uid)
    if not u: raise E('No such user', 404)
    if u['id'] == g.u: raise E('Use Delete account in your settings')
    purge_user(uid); return {}
@route('/admin/list/<what>', admin=True)
def adm_list(what):
    q = '%' + re.sub(r'[%_]', '', cl(request.args.get('q'), 30)) + '%'
    sql = dict(content='SELECT c.id,c.title,u.name author,c.kind sub,c.ts FROM content c JOIN users u ON u.id=c.uid WHERE c.title LIKE ?1 OR u.name LIKE ?1 ORDER BY c.ts DESC LIMIT 100',
               tweets='SELECT t.id,t.body title,u.name author,"post" sub,t.ts FROM tweets t JOIN users u ON u.id=t.uid WHERE t.body LIKE ?1 OR u.name LIKE ?1 ORDER BY t.ts DESC LIMIT 100',
               posts='SELECT p.id,p.title,u.name author,p.board sub,p.ts FROM posts p JOIN users u ON u.id=p.uid WHERE p.title LIKE ?1 OR u.name LIKE ?1 ORDER BY p.ts DESC LIMIT 100',
               boards='SELECT b.name id,b.name title,u.name author,"board" sub,b.ts FROM boards b JOIN users u ON u.id=b.owner WHERE b.name LIKE ?1 ORDER BY b.ts DESC LIMIT 100',
               servers='SELECT s.id,s.name title,u.name author,"server" sub,0 ts FROM servers s JOIN users u ON u.id=s.owner WHERE s.name LIKE ?1 LIMIT 100').get(what)
    if not sql: raise E('Unknown list')
    return Q(sql, q)
def prod_fields(b):
    n = cl(b.get('name'), 80)
    if not n: raise E('Name required')
    return n, 'app' if b.get('kind') == 'app' else 'game', cl(b.get('tag'), 40), cl(b.get('descr'), 2000), cl(b.get('version'), 20)
@route('/admin/products', 'POST', admin=True)
def adm_prod_new(): n, k, t, d, v = prod_fields(B()); return dict(id=X('INSERT INTO products(kind,name,descr,tag,version,ts) VALUES(?,?,?,?,?,?)', k, n, d, t, v, now()))
@route('/admin/products/<int:i>', 'PUT', admin=True)
def adm_prod_edit(i): n, k, t, d, v = prod_fields(B()); X('UPDATE products SET kind=?,name=?,descr=?,tag=?,version=? WHERE id=?', k, n, d, t, v, i); return {}
@route('/admin/products/<int:i>', 'DELETE', admin=True)
def adm_prod_del(i):
    for f in Q('SELECT fn FROM pfiles WHERE pid=?', i): rmfile(f['fn'], PF)
    p = Q1('SELECT cover FROM products WHERE id=?', i); p and rmfile(p['cover'])
    for t in ('pfiles', 'cart', 'owned', 'achievements'): X(f'DELETE FROM {t} WHERE pid=?', i)
    X('DELETE FROM products WHERE id=?', i); return {}
@route('/admin/products/<int:i>/cover', 'POST', admin=True)
def adm_cover(i):
    limit('image'); f = request.files.get('file'); p = Q1('SELECT cover FROM products WHERE id=?', i)
    if not p: raise E('Not found', 404)
    rmfile(p['cover'])
    X('UPDATE products SET cover=? WHERE id=?', save_upload(f, 'image', exts=('.png', '.jpg', '.jpeg', '.webp', '.gif'))[0] if f and f.filename else None, i); return {}
@route('/admin/products/<int:i>/file', 'POST', admin=True)
def adm_pfile(i):
    limit('product'); plat, f = request.form.get('platform'), request.files.get('file')
    if plat not in PLATFORMS or not f or not f.filename: raise E('Pick a platform and a file')
    if not Q1('SELECT 1 FROM products WHERE id=?', i): raise E('Not found', 404)
    old = Q1('SELECT fn FROM pfiles WHERE pid=? AND platform=?', i, plat); old and rmfile(old['fn'], PF)
    fn, size = save_upload(f, 'product', PF); X('INSERT OR REPLACE INTO pfiles VALUES(?,?,?,?,?)', i, plat, fn, re.sub(r'[^\w.\- ]', '_', f.filename)[:100], size); return {}
@route('/admin/products/<int:i>/file/<plat>', 'DELETE', admin=True)
def adm_pfile_del(i, plat):
    old = Q1('SELECT fn FROM pfiles WHERE pid=? AND platform=?', i, plat); old and rmfile(old['fn'], PF); X('DELETE FROM pfiles WHERE pid=? AND platform=?', i, plat); return {}
@route('/admin/products/<int:i>/achievements', 'POST', admin=True)
def adm_ach_add(i):
    b = B(); n = cl(b.get('name'), 60)
    if not n: raise E('Give the achievement a name')
    if not Q1('SELECT 1 FROM products WHERE id=?', i): raise E('Not found', 404)
    return dict(id=X('INSERT INTO achievements(pid,name,descr,ts) VALUES(?,?,?,?)', i, n, cl(b.get('descr'), 200), now()))
@route('/admin/achievements/<int:i>', 'DELETE', admin=True)
def adm_ach_del(i): X('DELETE FROM achievements WHERE id=?', i); return {}
@route('/admin/products/<int:i>/files', admin=True)
def adm_pfiles(i): return Q('SELECT platform,name,size FROM pfiles WHERE pid=?', i)

# ---------------- static, compression, startup ----------------
STATIC = {}
GZ = CFG('performance', 'gzip_level', int)
def index_bytes():
    p = os.path.join(ROOT, 'static', 'index.html'); m = os.path.getmtime(p)
    if STATIC.get('m') != m:
        raw = open(p, 'rb').read(); STATIC.update(m=m, raw=raw, gz=gzip.compress(raw, 6), tag='"%s"' % hashlib.md5(raw).hexdigest()[:16])
    return STATIC
@app.get('/')
def index():
    s = index_bytes()
    if request.headers.get('If-None-Match') == s['tag']: return Response(status=304)
    gz = 'gzip' in request.headers.get('Accept-Encoding', ''); r = Response(s['gz'] if gz else s['raw'], mimetype='text/html')
    r.headers.update({'ETag': s['tag'], 'Cache-Control': 'no-cache', 'Vary': 'Accept-Encoding'});
    if gz: r.headers['Content-Encoding'] = 'gzip'
    return r
@app.after_request
def squeeze(r):
    r.headers.setdefault('X-Content-Type-Options', 'nosniff')
    if r.status_code < 400 and not r.direct_passthrough and 'gzip' in request.headers.get('Accept-Encoding', '') and 'Content-Encoding' not in r.headers and r.mimetype == 'application/json':
        d = r.get_data()
        if len(d) > 700: r.set_data(gzip.compress(d, GZ)); r.headers['Content-Encoding'] = 'gzip'; r.headers['Vary'] = 'Accept-Encoding'
    return r
def init():
    with app.app_context():
        c = sqlite3.connect(DB); c.execute('PRAGMA journal_mode=WAL'); c.close()
        d = db(); legacy = 'price' in [r['name'] for r in Q('PRAGMA table_info(products)')]
        if legacy:
            for t in ('products', 'owned', 'cart', 'orders'): d.execute(f'DROP TABLE IF EXISTS {t}')
        d.executescript(SCHEMA)
        for t, col in MIGRATE:
            if col.split()[0] not in [r['name'] for r in Q(f'PRAGMA table_info({t})')]: d.execute(f'ALTER TABLE {t} ADD COLUMN {col}')
        if legacy:   # remove the old demo content
            for n in ('aria', 'kai', 'rin', 'nova', 'ghost', 'chef'):
                u = Q1('SELECT id FROM users WHERE name=?', n); u and purge_user(u['id'])
            for c in Q('SELECT id FROM content WHERE uid IS NULL'): drop_content(c['id'])
        d.commit()
def make_admin(name):
    with app.app_context():
        u = Q1('SELECT id FROM users WHERE name=?', name)
        if not u: print('No such user:', name); return
        X('UPDATE users SET admin=1 WHERE id=?', u['id']); db().commit(); print(name, 'is now a site admin')
def lan_ips():
    import socket
    ips = {'127.0.0.1'}
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); k.connect(('8.8.8.8', 80)); ips.add(k.getsockname()[0]); k.close()
    except OSError: pass
    try: ips |= set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError: pass
    return sorted(ips)
def make_cert():
    """Self-signed certificate for localhost + this machine's LAN addresses (regenerated if the addresses change)."""
    cert, key, meta = (os.path.join(ROOT, f) for f in ('cert.pem', 'key.pem', 'cert.ips'))
    ips = ','.join(lan_ips())
    if os.path.exists(cert) and os.path.exists(key) and os.path.exists(meta) and open(meta).read() == ips: return cert, key
    import datetime, ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048); name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'bHub')])
    san = [x509.DNSName('localhost')] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips.split(',')]
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
         .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1825)).add_extension(x509.SubjectAlternativeName(san), False).sign(k, hashes.SHA256()))
    open(key, 'wb').write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())); open(cert, 'wb').write(c.public_bytes(serialization.Encoding.PEM)); open(meta, 'w').write(ips)
    return cert, key
HTTPS_PORT = [0]
def start_https(port):
    try:
        import threading
        from werkzeug.serving import make_server
        cert, key = (CFG('https', 'cert_file'), CFG('https', 'key_file')) if CFG('https', 'cert_file') and CFG('https', 'key_file') else make_cert()
        srv = make_server('0.0.0.0', port, app, threaded=True, ssl_context=(cert, key)); threading.Thread(target=srv.serve_forever, daemon=True).start(); HTTPS_PORT[0] = port
        print(f'HTTPS (camera, mic, screenshare work here): https://localhost:{port}   LAN: ' + '  '.join(f'https://{i}:{port}' for i in lan_ips() if i != '127.0.0.1') + '\n  (accept the one-time browser certificate warning)')
    except Exception as e: print('HTTPS disabled:', e, '| install it with: pip install cryptography')
@app.get('/api/config')
def config(): return jsonify(https_port=HTTPS_PORT[0] or None, register_open=CFG('server', 'allow_registration', bool), limits=dict(chat_mb=CFG('limits', 'chat_upload_mb', float), video_mb=CFG('limits', 'video_upload_mb', float)))
@route('/rtc_config')
def rtc_config():
    ice, turn = [], lst(CFG('ice', 'turn_urls'))
    if lst(CFG('ice', 'stun_servers')): ice.append(dict(urls=lst(CFG('ice', 'stun_servers'))))
    if turn:
        secret = CFG('ice', 'turn_secret')
        if secret:
            u = '%d:%d' % (now() + CFG('ice', 'turn_ttl_hours', float) * 3600, g.u); ice.append(dict(urls=turn, username=u, credential=base64.b64encode(hmac.new(secret.encode(), u.encode(), hashlib.sha1).digest()).decode()))
        elif CFG('ice', 'turn_username'): ice.append(dict(urls=turn, username=CFG('ice', 'turn_username'), credential=CFG('ice', 'turn_password')))
        else: ice.append(dict(urls=turn))
    A, V = (lambda k, c=int: CFG('audio', k, c)), (lambda k, c=int: CFG('video', k, c))
    return dict(ice=ice, relay=CFG('ice', 'force_relay', bool),
                audio=dict(kbps=A('bitrate_kbps'), music_kbps=A('music_bitrate_kbps'), ptime=A('packet_ms'), fec=A('fec', bool), dtx=A('dtx', bool), jitter=A('jitter_buffer_ms'), sample_rate=A('sample_rate')),
                video=dict(camera_height=V('camera_height'), camera_fps=V('camera_fps'), camera_kbps=V('camera_kbps'), screen_kbps=V('screen_kbps'), screen_fps=V('screen_fps'), live_kbps=V('live_kbps'), relay_kbps=V('relay_kbps'), live_jitter=V('live_jitter_buffer_ms')),
                calls=dict(poll_fast=CFG('calls', 'poll_fast_ms', int), poll_idle=CFG('calls', 'poll_idle_ms', int)))
@app.before_request
def maybe_redirect():
    if HTTPS_PORT[0] and CFG('https', 'redirect_http', bool) and not request.is_secure and request.method == 'GET' and not request.path.startswith('/api/'):
        from flask import redirect
        return redirect(f"https://{request.host.split(':')[0]}:{HTTPS_PORT[0]}{request.full_path.rstrip('?')}", 301)
init()
if __name__ == '__main__':
    if '--admin' in sys.argv: make_admin(sys.argv[sys.argv.index('--admin') + 1]); sys.exit()
    host, port = os.environ.get('HOST', CFG('server', 'host')), int(os.environ.get('PORT', CFG('server', 'port')))
    print(f'bHub  config: {CFG_PATH}\n  http : http://localhost:{port}' + (f'   public: {CFG("server", "public_url")}' if CFG('server', 'public_url') else ''))
    if CFG('https', 'enabled', bool) and '--no-https' not in sys.argv: start_https(int(os.environ.get('HTTPS_PORT', CFG('https', 'port'))))
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=int(os.environ.get('THREADS', CFG('server', 'threads', int))), connection_limit=CFG('performance', 'connection_limit', int), backlog=CFG('performance', 'backlog', int), channel_timeout=CFG('performance', 'channel_timeout', int), max_request_body_size=app.config['MAX_CONTENT_LENGTH'], ident='bHub')
    except ImportError: app.run(host=host, port=port, threaded=True)
