#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
恋爱飞行棋 · 局域网联机服务器
用法：python3 server.py
同 WiFi 下的设备用浏览器打开提示的地址即可加入。
零依赖，仅用 Python 标准库。
"""
import base64
import gzip
import hashlib
import json
import os
import random
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# 云平台部署时端口由环境变量 PORT 决定（Render/Railway 等平台自动注入）
PORT = int(os.environ.get('PORT', 8765))

# ---------- 游戏状态 ----------
state = {
    'players': [],   # {id,name,gender,pos,steps,flying,hearts,shield,skip}
    'turn': 0,
    'phase': 'idle',
    'rollAt': 0,        # 本轮掷骰开始时间戳，用于 phase 卡住时的自愈
    'winner': None,
    'started': False,   # False=等待大厅，True=游戏进行中
    'hostId': -1,       # 房主（第一个加入的玩家）
    'boardVersion': 50, # 本局棋盘版本（50/100/150）
    'gameType': 'flight',  # 房间游戏：flight=飞行棋 / rps=猜拳 / tda=真心话大冒险 / mystery=盲盒奇遇
    'cells': [],           # 本局特殊格位置（开局由服务器统一生成，全员一致）
    'mystery': {       # 联机盲盒奇遇状态
        'phase': 'wait',   # wait=等待开局 / play=开盒中 / end=结算
        'order': [],       # [id, id...] 开盒顺序
        'turnIdx': 0,      # 当前该开盒的玩家在 order 中的下标
        'boxes': [None] * 9,  # 9 个盲盒：{who, kind, delta, text} 或 None
        'hearts': {},      # {id: 爱心数}
        'round': 0,        # 已开盒数
        'total': 0,        # 本局总开盒数 = 玩家数 * 3
    },
    'rps': {           # 联机猜拳状态
        'pair': [],        # [idA, idB] 本轮对决的两位玩家
        'picks': {},       # {id: 'rock'|'scissors'|'paper'}
        'turn': -1,        # 当前该出拳的玩家
        'phase': 'wait',   # wait=等待开局 / play=出拳中 / judge=判定完成
        'winner': -1,
        'loser': -1,
        'draw': False,
        'round': 0,
        'scores': {},      # {id: 胜场}
        'penalty': '',     # 惩罚内容（房主抽取后广播）
    },
    'penaltyCards': {'male': [], 'female': []},  # 房主自定义的惩罚卡池（{act,text}），空=用默认；仅房主可改
    'ready': {},          # {pid: True} 结算后「再来一局」的准备状态，房主看到所有人准备后点开始
    'tda': {           # 联机真心话大冒险状态
        'turn': 0,       # 当前轮到抽卡的玩家
        'phase': 'idle', # idle=待抽卡 / show=已抽展示
        'card': None,    # {type:'truth'|'dare', text:'...'} 当前展示的卡
        'round': 0,      # 已轮数
        'auto': 10,      # 抽卡后自动切换到下一位的秒数
    },
    'wheel': {       # 联机幸运转盘状态
        'phase': 'wait',   # wait=等待开局 / play=可转动 / show=展示结果 / end=结算
        'turn': 0,         # 当前该转的玩家下标
        'idx': -1,         # 最近一次转出的格索引 0-23
        'done': {},        # {pid: True} 本轮已转
        'round': 0,        # 已完成的轮数
        'total': 2,        # 总轮数（每人每轮转一次，共2轮）
        'last': None,      # {pid, name, idx}
    },
    'shoot': {       # 联机射击游戏状态
        'phase': 'wait',   # wait=等待开局 / play=射击中 / end=结算
        'turn': 0,         # 当前该射的玩家下标
        'mode': 'female',  # 当前靶性别：第一个男玩家射女靶
        'scores': {},      # {pid: 总分}
        'done': {},        # {pid: True} 本轮已射
        'last': None,      # {pid, part, score, x, y}
        'winner': -1,
        'round': 0,        # 已完成的轮数
        'total': 1,        # 每人 1 箭 = 1 轮
    },
    'race': {        # 联机赛马状态
        'phase': 'wait',   # wait=等待开局 / bet=下注中 / run=开跑 / judge=判定展示 / end=结算
        'bets': {},        # {pid: 马号1-5}
        'winner': 0,       # 冠军马号
        'ranking': [],     # 名次列表 [马号...]
        'scores': {},      # {pid: 猜中次数}
        'round': 0,        # 已完成的轮数
        'total': 3,        # 共 3 轮
        'losers': [],      # 本轮猜错玩家
        'penalty': '',     # 房主抽取的惩罚内容
        'winnerTxt': '',   # 冠军展示文字
    },
    'yoga': {        # 联机瑜伽抽卡状态
        'pack': False,     # 抽卡时是否已解锁进阶卡包
        'phase': 'wait',   # wait=等待开局 / draw=待抽卡 / hold=展示中 / end=结算
        'order': [],       # [pid...] 抽卡顺序
        'turnIdx': 0,      # 当前该抽的玩家在 order 中的下标
        'posIdx': -1,      # 最近抽到的姿势索引
        'done': {},        # {pid: 本轮已完成}
        'round': 0,        # 已完成的轮数
        'total': 0,        # 一轮 = 每人抽一张
    },
}
state_lock = threading.Lock()

# ---------- 用户上传的图片（自定义姿势卡图 / 自定义棋盘背景） ----------
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img', 'user')
UPLOAD_INDEX = os.path.join(UPLOAD_DIR, 'index.json')   # 元数据（文件名、类型、大小、时间）
UPLOAD_MAX_BYTES = 8 * 1024 * 1024                      # 单张上限 8MB（前端已压到 200KB 以内）
UPLOAD_KINDS = ('yoga', 'bg')
upload_lock = threading.Lock()


def _ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _load_upload_index():
    try:
        with open(UPLOAD_INDEX, 'r', encoding='utf-8') as f:
            v = json.load(f)
            return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_upload_index(items):
    _ensure_upload_dir()
    tmp = UPLOAD_INDEX + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, UPLOAD_INDEX)


def _list_builtin_files():
    """扫描 img/ 目录下的内置图片（不含 user/ 子目录），供图库页查看。"""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img')
    mimes = {'.webp', '.png', '.jpg', '.jpeg', '.gif', '.svg'}
    out = []
    try:
        for fn in sorted(os.listdir(root)):
            p = os.path.join(root, fn)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in mimes:
                continue
            try:
                stt = os.stat(p)
            except OSError:
                continue
            out.append({'name': fn, 'url': '/img/' + fn, 'size': stt.st_size,
                        'ts': int(stt.st_mtime)})
    except OSError:
        pass
    return out


def _list_upload_files():
    _ensure_upload_dir()
    out = []
    for fn in sorted(os.listdir(UPLOAD_DIR)):
        if fn.startswith('.') or not fn.lower().endswith(('.webp', '.png', '.jpg', '.jpeg', '.gif')):
            continue
        p = os.path.join(UPLOAD_DIR, fn)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        out.append({'name': fn, 'url': '/img/user/' + fn, 'size': stt.st_size,
                    'ts': int(stt.st_mtime)})
    return out
# ---------- 激活码（解锁进阶卡包） ----------
CODES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codes.json')
codes_lock = threading.Lock()
# 免费池上限：索引 >= FREE_COUNT 的姿势归入「进阶卡包」，需激活码解锁
FREE_COUNT = 8
FREE_POSE_NAMES = [
    '山式', '树式', '战士一式', '战士二式',
    '三角式', '下犬式', '猫牛式', '婴儿式',
]
PAID_POSE_NAMES = [
    '眼镜蛇式', '桥式', '蝴蝶式', '平板支撑', '鸽子式', '摊尸式', '新月式', '鸟王式',
    '侧角伸展式', '花环式', '骆驼式', '半鱼王式',
    '大猫伸展', '快乐婴儿式', '仰卧上举腿式', '站立前屈式',
    '坐角式', '侧卧举腿式', '侧卧拉伸式', '站立手拉脚趾式',
    # 🧳 旅行特辑
    '日出伸展式', '背包熊抱式', '观景台靠肩式', '地图平板式', '行李箱平衡式', '星空摊尸式',
    # 🏨 酒店场景
    '开门拥抱式', '床上晚安式', '枕头堡垒式', '晨起猫牛式', '浴缸放松式', '窗边合影式',
    # 💞 情侣配合
    '镜像模仿式', '背靠背传物式', '信任后倒式', '三脚同盟式', '对视呼吸式', '合十感恩式',
]
CODE_RE = re.compile(r'^LF-[A-Z0-9]{4}-[A-Z0-9]{4}$')


def _load_codes():
    try:
        with open(CODES_PATH, 'r', encoding='utf-8') as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {'codes': {}}
    except Exception:
        return {'codes': {}}


def _save_codes(db):
    tmp = CODES_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CODES_PATH)


def _gen_code(existing):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # 去掉易混的 I/O/0/1
    while True:
        code = 'LF-%s-%s' % (''.join(random.choice(alphabet) for _ in range(4)),
                             ''.join(random.choice(alphabet) for _ in range(4)))
        if code not in existing:
            return code


# 真心话大冒险抽卡后自动切换下一位的定时器（模块级，跨 Handler 实例共享）
tda_timer = None
TDA_AUTO_SECONDS = 10
sse_clients = []       # 已连接的 SSE 响应对象
sse_lock = threading.Lock()
sse_owner = {}         # SSE 连接(wfile) -> 玩家 id，用于掉线自动清理


def broadcast(event, data):
    """向所有 SSE 客户端广播事件"""
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode('utf-8')
    dead = []
    with sse_lock:
        for cli in sse_clients:
            try:
                cli.write(msg)
                cli.flush()
            except Exception:
                dead.append(cli)
        for d in dead:
            if d in sse_clients:
                sse_clients.remove(d)


def snapshot():
    with state_lock:
        return json.loads(json.dumps(state))


# ---------- HTTP 处理 ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    # ---- GET ----
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ('/', '/index.html'):
            self._serve_file('index.html', 'text/html; charset=utf-8')
        elif path == '/server.py':
            self._serve_file('server.py', 'text/plain; charset=utf-8')
        elif path == '/events':
            self._handle_sse()
        elif path == '/admin':
            self._admin_page()
        elif path == '/gallery':
            self._gallery_page()
        elif path == '/codes':
            self._codes_page()
        elif path == '/free-poses':
            self._json(200, {'freeCount': FREE_COUNT, 'names': FREE_POSE_NAMES,
                             'paidNames': PAID_POSE_NAMES, 'total': FREE_COUNT + len(PAID_POSE_NAMES)})
        elif path == '/health':
            # 健康检查端点（云平台用它判断服务是否存活）
            self._json(200, {'status': 'ok', 'players': len(state['players']), 'time': int(time.time())})
        elif path.startswith('/img/user/'):
            # 用户上传的图片：img/user/xxx.webp
            fn = os.path.basename(unquote(path))
            ext = os.path.splitext(fn)[1].lower()
            mimes = {'.webp': 'image/webp', '.png': 'image/png', '.jpg': 'image/jpeg',
                     '.jpeg': 'image/jpeg', '.gif': 'image/gif'}
            fp = os.path.join(UPLOAD_DIR, fn)
            if fn and ext in mimes and os.path.isfile(fp):
                self._serve_img(fp, mimes[ext])
            else:
                self.send_error(404)
        elif path == '/uploads':
            # 列出所有用户上传的图片（图库页用）
            self._json(200, {'items': _list_upload_files()})
        elif path.startswith('/img/'):
            # 外置图片：img/xxx.webp
            fn = unquote(os.path.basename(path))
            ext = os.path.splitext(fn)[1].lower()
            mimes = {'.webp': 'image/webp', '.png': 'image/png', '.jpg': 'image/jpeg',
                     '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.gif': 'image/gif'}
            if fn and ext in mimes and os.path.isfile(os.path.join('img', fn)):
                self._serve_img(os.path.join('img', fn), mimes[ext])
            else:
                self.send_error(404)
        elif path == '/state':
            # 只读：返回当前房间完整状态（用于断线恢复/大厅刷新）
            self._json(200, {'state': snapshot()})
        else:
            self.send_error(404)

    def _serve_file(self, fname, ctype):
        try:
            with open(fname, 'rb') as f:
                data = f.read()
            # 静态大文件（index.html 内嵌图片约 6MB）用 gzip 压缩后再发，体积约降 28%
            body = data
            encoding = None
            if len(data) > 8192 and 'text/' in ctype:
                accept = (self.headers.get('Accept-Encoding') or '').lower()
                if 'gzip' in accept:
                    if not hasattr(self, '_gzip_cache') or self._gzip_cache[0] != fname \
                            or self._gzip_cache[1] != len(data):
                        body = gzip.compress(data, 6)
                        self._gzip_cache = (fname, len(data), body)
                    else:
                        body = self._gzip_cache[2]
                    encoding = 'gzip'
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Type', ctype)
            if encoding:
                self.send_header('Content-Encoding', encoding)
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_img(self, fpath, ctype):
        try:
            with open(fpath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            # 图片加长缓存：手机二次进入无需重新下载
            self.send_header('Cache-Control', 'public, max-age=604800')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        # 解析 ?pid=N，用于掉线时自动清理对应玩家
        try:
            pid = int(parse_qs(urlparse(self.path).query).get('pid', ['-1'])[0])
        except Exception:
            pid = -1
        # 先发快照
        try:
            self.wfile.write(
                f"event: snapshot\ndata: {json.dumps(snapshot(), ensure_ascii=False)}\n\n".encode('utf-8')
            )
            self.wfile.flush()
        except Exception:
            return
        with sse_lock:
            sse_clients.append(self.wfile)
            if pid >= 0:
                sse_owner[self.wfile] = pid
        # 保持连接（keepalive 间隔短，掉线能较快被检测并自动清理）
        try:
            while True:
                time.sleep(3)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with sse_lock:
                if self.wfile in sse_clients:
                    sse_clients.remove(self.wfile)
                spid = sse_owner.pop(self.wfile, None)
            # 连接断开：若该玩家仍在房间，自动清理（意外退出/断网）
            if spid is not None:
                self._remove_player(spid)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8') if length else '{}'
        try:
            body = json.loads(raw)
        except Exception:
            body = {}

        if path == '/join':
            self._join(body)
        elif path == '/start':
            self._start(body)
        elif path == '/roll':
            self._roll(body)
        elif path == '/nextturn':
            self._nextturn(body)
        elif path == '/update':
            self._update(body)
        elif path == '/leave':
            self._leave(body)
        elif path == '/reset':
            self._reset()
        elif path == '/win':
            self._win(body)
        elif path == '/final-punish':
            self._final_punish(body)
        elif path == '/ready':
            self._ready(body)
        elif path == '/log':
            self._log(body)
        elif path == '/rps-roll':
            self._rps_roll(body)
        elif path == '/rps-next':
            self._rps_next(body)
        elif path == '/rps-punish':
            self._rps_punish(body)
        elif path == '/tda-draw':
            self._tda_draw(body)
        elif path == '/tda-next':
            self._tda_next(body)
        elif path == '/mystery-open':
            self._mystery_open(body)
        elif path == '/wheel-spin':
            self._wheel_spin(body)
        elif path == '/wheel-next':
            self._wheel_next(body)
        elif path == '/shoot-fire':
            self._shoot_fire(body)
        elif path == '/race-bet':
            self._race_bet(body)
        elif path == '/race-run':
            self._race_run(body)
        elif path == '/race-punish':
            self._race_punish(body)
        elif path == '/race-next':
            self._race_next(body)
        elif path == '/yoga-draw':
            self._yoga_draw(body)
        elif path == '/yoga-done':
            self._yoga_done(body)
        elif path == '/admin/clean':
            self._admin_clean()
        elif path == '/admin/reset':
            self._admin_reset()
        elif path == '/rps-cards':
            self._rps_cards(body)
        elif path == '/upload':
            self._upload(body)
        elif path == '/upload-delete':
            self._upload_delete(body)
        elif path == '/redeem':
            self._redeem(body)
        elif path == '/codes-gen':
            self._codes_gen(body)
        elif path == '/codes-list':
            self._codes_list(body)
        elif path == '/codes-toggle':
            self._codes_toggle(body)
        else:
            self.send_error(404)

    # ---------- 激活码 ----------
    def _is_admin(self, body):
        return self._client_ip() in self._gallery_authed or \
            str(body.get('pw') or '') == self.GALLERY_PASSWORD

    def _redeem(self, body):
        """POST /redeem {code:'LF-XXXX-XXXX', bind:'设备指纹'}
        返回 {ok, code, already}。同一设备重复校验不会报错（换机恢复用）。"""
        raw = str(body.get('code') or '').strip().upper()
        raw = re.sub(r'\s+', '', raw)
        if len(raw) == 10 and raw.startswith('LF') and '-' not in raw:   # 容错：LFXXXXXXXX
            raw = 'LF-' + raw[2:6] + '-' + raw[6:]
        if not CODE_RE.match(raw):
            return self._json(200, {'ok': False, 'error': '激活码格式不对（应为 LF-XXXX-XXXX）'})
        bind = str(body.get('bind') or '')[:64]
        with codes_lock:
            db = _load_codes()
            item = db['codes'].get(raw)
            if not item:
                return self._json(200, {'ok': False, 'error': '激活码不存在，请核对后重试'})
            if item.get('status') == 'disabled':
                return self._json(200, {'ok': False, 'error': '该激活码已被禁用，请联系卖家'})
            if item.get('status') == 'used':
                # 已绑定同一设备 → 认作本机恢复；否则拒绝二次核销
                if bind and item.get('bind') == bind:
                    return self._json(200, {'ok': True, 'code': raw, 'already': True})
                return self._json(200, {'ok': False, 'error': '该激活码已被使用，如为误用请联系卖家'})
            item['status'] = 'used'
            item['usedAt'] = int(time.time())
            item['bind'] = bind
            item['ip'] = self._client_ip()
            item['count'] = int(item.get('count') or 0) + 1
            _save_codes(db)
        self._json(200, {'ok': True, 'code': raw, 'already': False})

    def _codes_gen(self, body):
        """POST /codes-gen {pw, n, tag} —— 批量生成激活码"""
        if not self._is_admin(body):
            return self._json(403, {'ok': False, 'error': '需要管理密码'})
        try:
            n = max(1, min(200, int(body.get('n') or 10)))
        except Exception:
            n = 10
        tag = str(body.get('tag') or '')[:24]
        with codes_lock:
            db = _load_codes()
            made = []
            for _ in range(n):
                code = _gen_code(db['codes'])
                db['codes'][code] = {'status': 'unused', 'ts': int(time.time()),
                                     'tag': tag, 'bind': '', 'usedAt': 0, 'ip': '', 'count': 0}
                made.append(code)
            _save_codes(db)
        self._json(200, {'ok': True, 'codes': made})

    def _codes_list(self, body):
        """POST /codes-list {pw} —— 列出全部码 + 统计"""
        if not self._is_admin(body):
            return self._json(403, {'ok': False, 'error': '需要管理密码'})
        db = _load_codes()
        items = []
        for code, it in db['codes'].items():
            items.append({'code': code, 'status': it.get('status', 'unused'),
                          'ts': it.get('ts', 0), 'tag': it.get('tag', ''),
                          'usedAt': it.get('usedAt', 0), 'bind': it.get('bind', ''),
                          'ip': it.get('ip', '')})
        items.sort(key=lambda x: -x['ts'])
        stat = {'total': len(items),
                'used': sum(1 for i in items if i['status'] == 'used'),
                'unused': sum(1 for i in items if i['status'] == 'unused'),
                'disabled': sum(1 for i in items if i['status'] == 'disabled')}
        self._json(200, {'ok': True, 'items': items, 'stat': stat})

    def _codes_toggle(self, body):
        """POST /codes-toggle {pw, code, status:'unused'|'disabled'} —— 禁用/恢复"""
        if not self._is_admin(body):
            return self._json(403, {'ok': False, 'error': '需要管理密码'})
        code = str(body.get('code') or '').strip().upper()
        st = body.get('status')
        if st not in ('unused', 'disabled'):
            return self._json(400, {'ok': False, 'error': '状态不合法'})
        with codes_lock:
            db = _load_codes()
            it = db['codes'].get(code)
            if not it:
                return self._json(404, {'ok': False, 'error': '没有这个码'})
            if it.get('status') == 'used' and st == 'unused':
                return self._json(400, {'ok': False, 'error': '已使用的码不能恢复为未用'})
            it['status'] = st
            if st == 'unused':
                it['bind'], it['usedAt'], it['ip'] = '', 0, ''
            _save_codes(db)
        self._json(200, {'ok': True, 'code': code, 'status': st})

    # ---------- 用户图片上传 ----------
    def _upload(self, body):
        """POST /upload  {kind:'yoga'|'bg', dataUrl:'data:image/webp;base64,...', name:'可选'}
        图片落盘到 img/user/，返回可直接使用的 /img/user/xxx.webp 链接。
        """
        kind = body.get('kind') if body.get('kind') in UPLOAD_KINDS else 'yoga'
        data_url = body.get('dataUrl') or ''
        m = re.match(r'^data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$', data_url, re.S)
        if not m:
            return self._json(400, {'ok': False, 'error': '图片格式不支持，请用 png/jpg/webp'})
        ext = 'jpg' if m.group(1) in ('jpeg', 'jpg') else m.group(1)
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            return self._json(400, {'ok': False, 'error': '图片数据损坏'})
        if not raw:
            return self._json(400, {'ok': False, 'error': '图片是空的'})
        if len(raw) > UPLOAD_MAX_BYTES:
            return self._json(413, {'ok': False, 'error': '图片太大了（上限 8MB）'})
        _ensure_upload_dir()
        stamp = time.strftime('%Y%m%d-%H%M%S')
        digest = hashlib.sha1(raw).hexdigest()[:8]
        fn = '%s-%s-%s.%s' % (kind, stamp, digest, ext)
        with upload_lock:
            # 同一张图（内容 hash 相同、类型相同）直接复用，不重复占空间
            for old in os.listdir(UPLOAD_DIR):
                if old.startswith(kind + '-') and old.endswith('-' + digest + '.' + ext):
                    return self._json(200, {'ok': True, 'url': '/img/user/' + old, 'name': old,
                                            'size': len(raw), 'reused': True})
            with open(os.path.join(UPLOAD_DIR, fn), 'wb') as f:
                f.write(raw)
            items = _load_upload_index()
            items.append({'name': fn, 'kind': kind, 'label': str(body.get('name') or '')[:20],
                          'size': len(raw), 'ts': int(time.time())})
            _save_upload_index(items[-2000:])
        self._json(200, {'ok': True, 'url': '/img/user/' + fn, 'name': fn, 'size': len(raw)})

    def _upload_delete(self, body):
        """POST /upload-delete {name:'xxx.webp', pw:'图库密码'} —— 删除一张用户上传的图"""
        # 仅图库已认证的 IP 或带正确密码的请求可删除
        pw = str(body.get('pw') or '')
        if self._client_ip() not in self._gallery_authed and pw != self.GALLERY_PASSWORD:
            return self._json(403, {'ok': False, 'error': '需要图库管理密码'})
        fn = os.path.basename(str(body.get('name') or ''))
        if not fn or not re.match(r'^(yoga|bg)-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.(webp|png|jpg|gif)$', fn):
            return self._json(400, {'ok': False, 'error': '文件名不合法'})
        p = os.path.join(UPLOAD_DIR, fn)
        with upload_lock:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    return self._json(500, {'ok': False, 'error': '删除失败'})
            items = [i for i in _load_upload_index() if i.get('name') != fn]
            _save_upload_index(items)
        self._json(200, {'ok': True})

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _join(self, body):
        with state_lock:
            if len(state['players']) >= 4:
                self._json(400, {'error': '房间已满（最多4人）'})
                return
            if state['started']:
                self._json(400, {'error': '游戏已开始，请等待下一局'})
                return
            pid = len(state['players'])
            if pid == 0:
                state['hostId'] = pid  # 第一个加入的是房主
            player = {
                'id': pid,
                'name': (body.get('name') or f'宝贝{pid+1}').strip()[:12],
                'gender': body.get('gender') or ('male' if pid % 2 == 0 else 'female'),
                'pos': -1, 'steps': 0, 'flying': False,
                'hearts': 0, 'shield': False, 'skip': False,
            }
            state['players'].append(player)
            state['turn'] = 0
            state['phase'] = 'idle'
            state['winner'] = None
            state['started'] = False   # 新玩家加入时回到等待状态
            broadcast('players', state['players'])
        self._json(200, {'id': pid, 'state': snapshot()})

    def _gen_cells(self, n):
        """生成特殊格位置（奖励/惩罚/甜蜜/前进/后退/停一轮/护身符），与前端 buildCells 同分布。"""
        cells = [{'type': 'normal'} for _ in range(int(n))]
        def place(t, count):
            guard = 0
            placed = 0
            while placed < count and guard < 6000:
                guard += 1
                pos = random.randint(1, int(n) - 2)
                if cells[pos]['type'] != 'normal':
                    continue
                conflict = False
                for j in (pos - 1, pos, pos + 1):
                    if 0 <= j <= int(n) - 1 and cells[j]['type'] != 'normal':
                        conflict = True
                        break
                if conflict:
                    continue
                cells[pos]['type'] = t
                placed += 1
        n = int(n)
        ratio = n / 150.0
        place('reward', max(3, int(round(10 * ratio))))
        place('penalty', max(2, int(round(8 * ratio))))
        place('love', max(5, int(round(18 * ratio))))
        place('forward', max(2, int(round(8 * ratio))))
        place('backward', max(2, int(round(8 * ratio))))
        place('rest', max(1, int(round(4 * ratio))))
        place('safe', max(1, int(round(4 * ratio))))
        return cells

    def _start(self, body):
        with state_lock:
            if not state['players']:
                self._json(400, {'error': '房间为空'})
                return
            if len(state['players']) < 2:
                self._json(400, {'error': '至少 2 人才能开始游戏'})
                return
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以开始游戏'})
                return
            state['started'] = True
            state['phase'] = 'idle'
            state['turn'] = 0
            state['winner'] = None
            gt = body.get('gameType')
            if gt in ('flight', 'rps', 'tda', 'mystery', 'wheel', 'shoot', 'race', 'yoga'):
                state['gameType'] = gt
            bv = body.get('boardVersion')
            if bv in (50, 100, 150):
                state['boardVersion'] = bv
            # 开局由服务器统一生成特殊格位置（全员一致，每次随机）
            state['cells'] = self._gen_cells(state['boardVersion'])
            # 初始化猜拳状态
            state['rps'] = {
                'pair': [], 'picks': {}, 'turn': -1, 'phase': 'wait',
                'winner': -1, 'loser': -1, 'draw': False, 'round': 0,
                'scores': {}, 'penalty': '',
            }
            # 初始化真心话大冒险状态
            state['tda'] = {
                'turn': 0, 'phase': 'idle', 'card': None, 'round': 0, 'auto': TDA_AUTO_SECONDS,
            }
            # 初始化盲盒奇遇状态
            state['mystery'] = {
                'phase': 'play',
                'order': [p['id'] for p in state['players']],
                'turnIdx': 0,
                'boxes': [None] * 9,
                'hearts': {},
                'round': 0,
                'total': 9,
            }
            # 初始化幸运转盘状态
            state['wheel'] = {
                'phase': 'play', 'turn': 0, 'idx': -1,
                'done': {}, 'round': 0, 'total': 2, 'last': None,
            }
            # 初始化射击游戏状态（第一个男玩家射女靶，否则射男靶）
            first_p = state['players'][0]
            state['shoot'] = {
                'phase': 'play', 'turn': 0,
                'mode': 'male' if first_p['gender'] == 'female' else 'female',
                'scores': {}, 'done': {}, 'last': None,
                'winner': -1, 'round': 0, 'total': 1,
            }
            # 初始化赛马状态
            state['race'] = {
                'phase': 'bet', 'bets': {}, 'winner': 0, 'ranking': [],
                'scores': {}, 'round': 0, 'total': 3,
                'losers': [], 'penalty': '', 'winnerTxt': '',
            }
            # 初始化瑜伽抽卡状态
            state['yoga'] = {
                'phase': 'draw',
                'order': [p['id'] for p in state['players']],
                'turnIdx': 0, 'posIdx': -1,
                'done': {}, 'round': 0, 'total': len(state['players']),
            }
            for p in state['players']:
                p.update(pos=-1, steps=0, flying=False, hearts=0, shield=False, skip=False)
            state['ready'] = {}
            # 在锁内做深拷贝，锁外再广播（snapshot() 会再次获取同一把锁，不能在此调用）
            snap = json.loads(json.dumps(state))
            players_snap = json.loads(json.dumps(state['players']))
        broadcast('start', {'state': snap})
        broadcast('players', players_snap)
        self._json(200, {'state': snap})

    def _roll(self, body):
        with state_lock:
            now = time.time()
            pid = body.get('id')
            # 只有当前轮到（turn 匹配）的玩家能掷骰
            if pid != state['turn'] or pid >= len(state['players']):
                self._json(400, {'error': '还没轮到你'})
                return
            # phase 非 idle 时复位为 idle：覆盖两种稳定场景——
            # ① 抽到"再掷"奖励卡后前端允许本玩家再掷，但服务器 phase 仍停在 rolling；
            # ② 上一位 nextturn 丢失导致 phase 卡住（自动自愈）。
            # 并发安全性仍由上面的 turn 匹配保证，phase 仅作状态标记。
            if state['phase'] != 'idle':
                state['phase'] = 'idle'
            state['phase'] = 'rolling'
            state['rollAt'] = now
            n = random.randint(1, 6)
            broadcast('dice', {'player': pid, 'value': n})
        self._json(200, {'value': n})

    def _nextturn(self, body):
        with state_lock:
            n = len(state['players'])
            if n == 0:
                self._json(200, {})
                return
            for k in range(1, n + 1):
                nxt = (state['turn'] + k) % n
                if state['players'][nxt].get('skip'):
                    state['players'][nxt]['skip'] = False
                    broadcast('players', state['players'])
                    continue
                state['turn'] = nxt
                break
            state['phase'] = 'idle'
            broadcast('turn', state['turn'])
        self._json(200, {})

    def _update(self, body):
        with state_lock:
            pid = body.get('id')
            if pid is None or pid >= len(state['players']):
                self._json(400, {'error': '玩家不存在'})
                return
            for k, v in body.items():
                if k != 'id':
                    state['players'][pid][k] = v
            broadcast('players', state['players'])
        self._json(200, {})

    def _leave(self, body):
        self._remove_player(body.get('id'))
        self._json(200, {})

    def _remove_player(self, pid):
        """把玩家移出房间（正常退出 / 掉线自动清理 / 管理清理共用）。"""
        if pid is None or pid < 0:
            return
        with state_lock:
            was_started = state['started']
            before = len(state['players'])
            state['players'] = [p for p in state['players'] if p['id'] != pid]
            if len(state['players']) == before:
                return  # 该玩家本就不在房间，无需处理
            if state['hostId'] == pid:
                state['hostId'] = state['players'][0]['id'] if state['players'] else -1
            if pid in state['ready']:
                del state['ready'][pid]
            if state['turn'] >= len(state['players']):
                state['turn'] = 0
            state['phase'] = 'idle'
            if len(state['players']) < 2:
                state['started'] = False   # 人数不足时回到等待状态
            snap = json.loads(json.dumps(state))
            players_snap = json.loads(json.dumps(state['players']))
        # 游戏进行中有人退出 → 结束本局，全员回到主页面
        if was_started:
            broadcast('ended', {'reason': '有玩家退出了游戏，本局已结束'})
        broadcast('players', players_snap)
        broadcast('turn', snap['turn'])

    def _reset(self):
        with state_lock:
            for p in state['players']:
                p.update(pos=-1, steps=0, flying=False, hearts=0, shield=False, skip=False)
            state['turn'] = 0
            state['phase'] = 'idle'
            state['winner'] = None
            state['started'] = False
            broadcast('players', state['players'])
            broadcast('turn', 0)
        self._json(200, {})

    def _win(self, body):
        """联机对局有玩家率先抵达终点：裁定赢家并广播，全员统一进入结束界面。"""
        with state_lock:
            pid = body.get('id')
            if pid is None or pid >= len(state['players']):
                self._json(400, {'error': '玩家不存在'})
                return
            state['winner'] = pid
            state['started'] = False
            state['players'][pid]['hearts'] = state['players'][pid].get('hearts', 0) + 5
            snap = {'winner': pid, 'name': state['players'][pid]['name'],
                    'players': json.loads(json.dumps(state['players']))}
        broadcast('ended', snap)
        self._json(200, snap)

    def _ready(self, body):
        """结算后玩家点「准备好」，广播准备状态给全员。"""
        with state_lock:
            pid = body.get('id')
            if pid is None or pid >= len(state['players']):
                self._json(400, {'error': '玩家不存在'})
                return
            state['ready'][pid] = True
            snap = json.loads(json.dumps(state['ready']))
        broadcast('ready', {'ready': snap, 'id': pid})
        self._json(200, {'ready': snap})

    def _log(self, body):
        """联机足迹日志广播：掷骰者抽到奖励/惩罚卡后，把卡内容同步给全员，保证足迹一致。"""
        with state_lock:
            if not state['players']:
                self._json(400, {'error': '房间为空'})
                return
            msg = (body.get('msg') or '').strip()
            if not msg:
                self._json(400, {'error': '日志内容为空'})
                return
        broadcast('log', {'msg': msg})
        self._json(200, {'ok': True})

    def _final_punish(self, body):
        """赢家到终点后抽终点惩罚卡：只有赢家可抽，广播给全员。"""
        with state_lock:
            if state['winner'] is None or body.get('id') != state['winner']:
                self._json(400, {'error': '只有赢家可以抽终点惩罚卡'})
                return
            text = (body.get('text') or '').strip()
            if not text:
                self._json(400, {'error': '惩罚内容为空'})
                return
        broadcast('final-punish', {'text': text})
        self._json(200, {'text': text})

    # ---------- 服务器管理入口 ----------
    def _online_pids(self):
        with sse_lock:
            return set(sse_owner.values())

    # ---------- 用户上传图库（管理员页） ----------
    GALLERY_PASSWORD = os.environ.get('GALLERY_PASSWORD', 'loveyou')
    _gallery_authed = set()   # 已通过密码的客户端 IP

    def _client_ip(self):
        xff = self.headers.get('X-Forwarded-For', '')
        return (xff.split(',')[0].strip() if xff else self.client_address[0])

    def _gallery_page(self):
        ip = self._client_ip()
        pw = urlparse(self.path).query
        qs = parse_qs(pw)
        # 密码校验（?pw=xxx 或已验证过的 IP 自动放行）
        ok = ip in self._gallery_authed
        if not ok and qs.get('pw', [''])[0] == self.GALLERY_PASSWORD:
            self._gallery_authed.add(ip)
            ok = True
        if not ok:
            page = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>图库 · 验证</title><style>'
                    'body{font-family:-apple-system,sans-serif;background:#FFF5F9;color:#3A2E39;'
                    'display:flex;align-items:center;justify-content:center;min-height:90vh}'
                    'input,button{font-size:16px;padding:10px 14px;border-radius:10px;border:1.5px solid #F0C9D8}'
                    'button{background:#FF6E9C;color:#fff;border:none;cursor:pointer;font-weight:700}'
                    '</style></head><body><form method="get">'
                    '<div>🔐 请输入管理密码<br><br>'
                    '<input type="password" name="pw" autofocus>'
                    ' <button>进入</button></div></form></body></html>')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(page.encode('utf-8'))
            return
        items = _list_upload_files()
        meta = {i['name']: i for i in _load_upload_index()}
        builtin = _list_builtin_files()
        html = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>恋爱飞行棋 · 图片管理</title><style>'
                'body{font-family:-apple-system,sans-serif;background:#FFF5F9;color:#3A2E39;max-width:960px;margin:0 auto;padding:16px}'
                'h1{font-size:20px;color:#FF5E8A} .sub{font-size:13px;color:#A890A0;margin-bottom:14px}'
                '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}'
                '.cell{background:#fff;border-radius:14px;padding:10px;box-shadow:0 2px 10px rgba(255,94,138,.12);'
                'display:flex;flex-direction:column;gap:6px;align-items:center}'
                '.cell img{width:100%;height:150px;object-fit:cover;border-radius:10px;cursor:pointer;background:#f7f2f4}'
                '.nm{font-size:11px;color:#7E6BA8;word-break:break-all;text-align:center}'
                '.kb{font-size:10px;padding:1px 7px;border-radius:8px}'
                '.kb.yoga{background:#F3E8FF;color:#8B5CF6}.kb.bg{background:#DCFCE7;color:#16A34A}'
                '.del{font-size:11px;padding:5px 12px;border:none;border-radius:9px;background:#FFE8E8;color:#D63B3B;cursor:pointer}'
                '.empty{padding:40px;text-align:center;color:#A890A0}'
                '</style></head><body>']
        yoga_n = sum(1 for i in items if i['name'].startswith('yoga-'))
        bg_n = len(items) - yoga_n
        html.append(f'<h1>🖼 图片管理</h1>')
        html.append(f'<div class="sub">用户上传 {len(items)} 张（姿势卡图 {yoga_n} · 背景图 {bg_n}） · '
                    f'内置图片 {len(builtin)} 张 · <a href="/admin">→ 房间管理</a></div>')
        if not items:
            html.append('<div class="empty">还没有用户上传图片</div>')
        else:
            html.append('<div class="grid">')
            for it in reversed(items):  # 新的在前
                nm = it['name']
                kind = 'yoga' if nm.startswith('yoga-') else 'bg'
                label = meta.get(nm, {}).get('label', '')
                import datetime as _dt
                tstr = _dt.datetime.fromtimestamp(it['ts']).strftime('%m-%d %H:%M')
                html.append(
                    f'<div class="cell">'
                    f'<img src="{it["url"]}" loading="lazy" onclick="window.open(this.src)">'
                    f'<span class="kb {kind}">{"姿势卡" if kind=="yoga" else "背景"} · {tstr}</span>'
                    f'<div class="nm">{label or nm}</div>'
                    f'<button class="del" onclick="delImg(\'{nm}\')">删除</button>'
                    f'</div>')
            html.append('</div>')
        # ---- 内置图片区块 ----
        def _cat(fn):
            if fn.startswith('yp-'): return '瑜伽服'
            if fn.startswith('yoga-tight-'): return '超短款'
            if fn.startswith('yoga-swim-'): return '泳衣款'
            if fn.startswith('couple-'): return '夫妻姿势'
            if fn.startswith('shoot-'): return '射击靶'
            return '其他'
        cats = {}
        for it in builtin:
            cats.setdefault(_cat(it['name']), []).append(it)
        html.append('<h1 style="margin-top:28px">🎨 内置图片（共 %d 张）</h1>' % len(builtin))
        html.append('<div class="sub">游戏自带的所有图片素材，按分类展示；点击可看大图。</div>')
        for cat in sorted(cats.keys()):
            lst = cats[cat]
            html.append(f'<div class="bg-opt-head" style="margin:16px 0 8px;font-weight:800;color:#FF5E8A">{cat}（{len(lst)}）</div>')
            html.append('<div class="grid">')
            for it in lst:
                nm = it['name']
                kb = it['size'] // 1024
                html.append(
                    f'<div class="cell">'
                    f'<img src="{it["url"]}" loading="lazy" onclick="window.open(this.src)">'
                    f'<div class="nm">{nm}<br><span style="color:#C0AECB">{kb} KB</span></div>'
                    f'</div>')
            html.append('</div>')
        html.append('<script>'
                    'function delImg(n){if(!confirm("确定删除 "+n+" ?"))return;'
                    'fetch("/upload-delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:n})})'
                    '.then(function(r){return r.json()}).then(function(d){if(d.ok)location.reload();else alert(d.error||"删除失败")})'
                    '.catch(function(){alert("删除失败")});}'
                    '</script></body></html>')
        page = ''.join(html)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(page.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(page.encode('utf-8'))

    def _codes_page(self):
        """激活码管理：生成 / 查看核销 / 禁用 / 导出 CSV"""
        ip = self._client_ip()
        qs = parse_qs(urlparse(self.path).query)
        ok = ip in self._gallery_authed
        if not ok and qs.get('pw', [''])[0] == self.GALLERY_PASSWORD:
            self._gallery_authed.add(ip)
            ok = True
        if not ok:
            page = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>激活码 · 验证</title><style>'
                    'body{font-family:-apple-system,sans-serif;background:#FFF5F9;color:#3A2E39;'
                    'display:flex;align-items:center;justify-content:center;min-height:90vh}'
                    'input,button{font-size:16px;padding:10px 14px;border-radius:10px;border:1.5px solid #F0C9D8}'
                    'button{background:#FF6E9C;color:#fff;border:none;cursor:pointer;font-weight:700}'
                    '</style></head><body><form method="get">'
                    '<div>🔐 请输入管理密码<br><br>'
                    '<input type="password" name="pw" autofocus>'
                    ' <button>进入</button></div></form></body></html>')
            b = page.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        page = '''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>恋爱飞行棋 · 激活码管理</title><style>
body{font-family:-apple-system,sans-serif;background:#FFF5F9;color:#3A2E39;max-width:900px;margin:0 auto;padding:16px}
h1{font-size:20px;color:#FF5E8A;margin:0 0 4px}
.sub{font-size:13px;color:#A890A0;margin-bottom:16px}
.card{background:#fff;border-radius:16px;padding:14px 16px;box-shadow:0 2px 12px rgba(255,94,138,.12);margin-bottom:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select,button{font-size:14px;padding:8px 12px;border-radius:10px;border:1.5px solid #F0C9D8;font-family:inherit}
button{background:#FF6E9C;color:#fff;border:none;cursor:pointer;font-weight:700}
button.ghost{background:#fff;color:#FF5E8A;border:1.5px solid #F7B9CD}
button:disabled{opacity:.5;cursor:not-allowed}
.stat{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.st{flex:1;min-width:90px;background:#fff;border-radius:14px;padding:10px 12px;text-align:center;box-shadow:0 2px 10px rgba(255,94,138,.1)}
.st b{display:block;font-size:22px;color:#FF5E8A}
.st span{font-size:12px;color:#A890A0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 6px;text-align:left;border-bottom:1px solid #F8E7EE}
th{color:#A890A0;font-weight:600;font-size:12px}
code{background:#FFF0F5;padding:2px 6px;border-radius:6px;font-size:13px;letter-spacing:.5px}
.tag{font-size:11px;padding:2px 8px;border-radius:8px;font-weight:700}
.tag.unused{background:#E8F8EF;color:#1F9D5B}
.tag.used{background:#EEF0FF;color:#5566D9}
.tag.disabled{background:#FBE9E9;color:#D65A5A}
.newbox{display:none;background:#FFF7FA;border:1.5px dashed #F7B9CD;border-radius:12px;padding:12px;margin-top:10px;
font-family:ui-monospace,monospace;font-size:13px;white-space:pre-wrap;line-height:1.8}
.hint{font-size:12px;color:#A890A0;margin-top:8px;line-height:1.6}
</style></head><body>
<h1>🎫 激活码管理</h1>
<div class="sub">生成 → 发出 → 用户输入 → 这里自动显示核销状态。<a href="/gallery">← 回图片管理</a></div>

<div class="stat">
  <div class="st"><b id="s-total">-</b><span>总数</span></div>
  <div class="st"><b id="s-unused">-</b><span>未使用</span></div>
  <div class="st"><b id="s-used">-</b><span>已使用</span></div>
  <div class="st"><b id="s-off">-</b><span>已禁用</span></div>
</div>

<div class="card">
  <div class="row">
    <input id="g-n" type="number" value="10" min="1" max="200" style="width:80px">
    <input id="g-tag" type="text" placeholder="备注（如：小红书首波）" style="flex:1;min-width:140px">
    <button onclick="gen()">生成</button>
    <button class="ghost" onclick="copyNew()">复制新码</button>
    <button class="ghost" onclick="exportCsv()">导出 CSV</button>
  </div>
  <div class="newbox" id="newbox"></div>
</div>

<div class="card">
  <table><thead><tr><th>激活码</th><th>状态</th><th>备注</th><th>使用时间</th><th>操作</th></tr></thead>
  <tbody id="tb"><tr><td colspan="5" style="color:#C0AECB">加载中…</td></tr></tbody></table>
  <div class="hint">用户核销后「使用时间」会填上；同一设备重复输入不会报错（换机恢复）。</div>
</div>

<script>
var PWSET = location.search.indexOf('pw=') >= 0;
var PW = PWSET ? decodeURIComponent(location.search.split('pw=')[1].split('&')[0]) : '';
var ROWS = [], NEWCODES = '';
function api(path, body){
  if(PW) body.pw = PW;
  return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(r){ return r.json(); });
}
function fmt(t){ if(!t) return '-'; var d = new Date(t*1000);
  function p(n){ return n<10?'0'+n:''+n; }
  return (d.getMonth()+1)+'/'+d.getDate()+' '+p(d.getHours())+':'+p(d.getMinutes()); }
function load(){
  api('/codes-list', {}).then(function(d){
    if(!d.ok){ document.getElementById('tb').innerHTML = '<tr><td colspan="5" style="color:#D65A5A">'+(d.error||'加载失败')+'</td></tr>'; return; }
    ROWS = d.items;
    document.getElementById('s-total').textContent = d.stat.total;
    document.getElementById('s-unused').textContent = d.stat.unused;
    document.getElementById('s-used').textContent = d.stat.used;
    document.getElementById('s-off').textContent = d.stat.disabled;
    var tb = document.getElementById('tb');
    if(!ROWS.length){ tb.innerHTML = '<tr><td colspan="5" style="color:#C0AECB">还没有码，上面生成一批</td></tr>'; return; }
    tb.innerHTML = ROWS.map(function(r){
      var cls = r.status, lab = {unused:'未使用', used:'已使用', disabled:'已禁用'}[r.status] || r.status;
      var btn = r.status === 'used' ? '' :
        '<button class="ghost" style="padding:4px 10px;font-size:12px" onclick="toggle(\\''+r.code+'\\',\\''+(r.status==='unused'?'disabled':'unused')+'\\')">'+(r.status==='unused'?'禁用':'恢复')+'</button>';
      return '<tr><td><code>'+r.code+'</code></td><td><span class="tag '+cls+'">'+lab+'</span></td>'
        + '<td>'+(r.tag||'-')+'</td><td>'+fmt(r.usedAt)+'</td><td>'+btn+'</td></tr>';
    }).join('');
  }).catch(function(){ document.getElementById('tb').innerHTML = '<tr><td colspan="5" style="color:#D65A5A">网络错误，确认已用 ?pw= 密码进入</td></tr>'; });
}
function gen(){
  var n = parseInt(document.getElementById('g-n').value, 10) || 10;
  var tag = document.getElementById('g-tag').value.trim();
  api('/codes-gen', {n:n, tag:tag}).then(function(d){
    if(!d.ok){ alert(d.error||'生成失败'); return; }
    NEWCODES = d.codes.join('\\n');
    var box = document.getElementById('newbox');
    box.style.display = 'block';
    box.textContent = d.codes.join('\\n');
    load();
  });
}
function copyNew(){
  if(!NEWCODES){ alert('还没有新生成的码'); return; }
  navigator.clipboard.writeText(NEWCODES).then(function(){ alert('已复制 ' + NEWCODES.split('\\n').length + ' 个码'); },
    function(){ prompt('手动复制：', NEWCODES); });
}
function toggle(code, st){
  api('/codes-toggle', {code:code, status:st}).then(function(d){ if(!d.ok) alert(d.error||'操作失败'); load(); });
}
function exportCsv(){
  var head = 'code,status,tag,generated,usedAt,bind,ip\\n';
  var lines = ROWS.map(function(r){
    return [r.code, r.status, r.tag||'', fmt(r.ts), fmt(r.usedAt), r.bind||'', r.ip||''].join(',');
  });
  var blob = new Blob(['\\ufeff'+head+lines.join('\\n')], {type:'text/csv;charset=utf-8'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'love-flight-codes.csv';
  a.click();
}
load();
</script></body></html>'''
        b = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _admin_page(self):
        """管理页：查看房间状态，清理意外退出残留的玩家 / 重置房间。"""
        with state_lock:
            st = json.loads(json.dumps(state))
        online = self._online_pids()
        html = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>恋爱飞行棋 · 服务器管理</title>'
                '<style>body{font-family:-apple-system,sans-serif;background:#FFF5F9;color:#3A2E39;'
                'max-width:640px;margin:0 auto;padding:20px}'
                'h1{font-size:20px;color:#FF5E8A}'
                '.card{background:#fff;border-radius:14px;padding:16px;margin:12px 0;'
                'box-shadow:0 2px 10px rgba(255,94,138,.12)}'
                '.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f4e4ea}'
                '.row:last-child{border-bottom:none}'
                '.tag{font-size:12px;padding:2px 8px;border-radius:10px}'
                '.on{background:#E2F7E6;color:#1F8A3D}.off{background:#FFE8E8;color:#D63B3B}'
                '.btn{display:inline-block;margin:8px 8px 0 0;padding:10px 18px;border:none;border-radius:12px;'
                'cursor:pointer;font-size:15px;color:#fff}'
                '.clean{background:#FF8A65}.reset{background:#9B7BFF}'
                '.ok{background:#E2F7E6;color:#1F8A3D;padding:8px 12px;border-radius:10px;margin-top:10px;display:none}'
                '</style></head><body>']
        html.append('<h1>🎛 恋爱飞行棋 · 服务器管理</h1>')
        html.append(f'<div class="card"><div class="row"><span>房间状态</span><b>{("进行中" if st["started"] else "等待大厅")}</b></div>'
                    f'<div class="row"><span>游戏类型</span><b>{st["gameType"]}</b></div>'
                    f'<div class="row"><span>棋盘版本</span><b>{st["boardVersion"]} 格</b></div>'
                    f'<div class="row"><span>在线玩家（有连接）</span><b>{len(online)}</b></div>'
                    f'<div class="row"><span>房间记录玩家</span><b>{len(st["players"])}</b></div></div>')
        if st['players']:
            html.append('<div class="card"><div class="row" style="font-weight:700"><span>玩家</span><span>状态</span></div>')
            for p in st['players']:
                pid = p['id']
                ok = '在线' if pid in online else '离线(残留)'
                cls = 'on' if pid in online else 'off'
                tag = '👑房主' if pid == st['hostId'] else ''
                html.append(f'<div class="row"><span>#{pid} {p.get("name","")} {tag}</span>'
                            f'<span class="tag {cls}">{ok}</span></div>')
            html.append('</div>')
            html.append('<button class="btn clean" onclick="cleanup()">🧹 清理离线残留玩家</button>')
        html.append('<button class="btn reset" onclick="resetAll()">♻️ 重置房间（清空所有玩家）</button>')
        html.append('<div class="ok" id="msg"></div>')
        html.append('<script>'
                    'function show(m){var e=document.getElementById("msg");e.textContent=m;e.style.display="block";}'
                    'function cleanup(){fetch("/admin/clean",{method:"POST"}).then(function(r){return r.json()})'
                    '.then(function(d){show("已清理 "+d.removed+" 名离线玩家");setTimeout(function(){location.reload()},800)})'
                    '.catch(function(){show("操作失败")})}'
                    'function resetAll(){if(!confirm("确定重置房间？所有玩家将被清空"))return;'
                    'fetch("/admin/reset",{method:"POST"}).then(function(r){return r.json()})'
                    '.then(function(){show("房间已重置");setTimeout(function(){location.reload()},800)})'
                    '.catch(function(){show("操作失败")})}'
                    '</script></body></html>')
        page = ''.join(html)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(page.encode('utf-8'))

    def _admin_clean(self):
        """清理离线（无 SSE 连接）的残留玩家。"""
        online = self._online_pids()
        with state_lock:
            pids = [p['id'] for p in state['players'] if p['id'] not in online]
        removed = 0
        for pid in pids:
            before = len(state['players'])
            self._remove_player(pid)
            if len(state['players']) < before:
                removed += 1
        self._json(200, {'removed': removed, 'state': snapshot()})

    def _admin_reset(self):
        """重置房间：清空所有玩家，回到空房状态。"""
        with state_lock:
            pids = [p['id'] for p in state['players']]
        for pid in pids:
            self._remove_player(pid)
        self._json(200, {'state': snapshot()})

    # ---------- 联机猜拳 ----------
    def _rps_snapshot(self):
        return json.loads(json.dumps(state['rps']))

    def _rps_result(self, c1, c2):
        if c1 == c2:
            return 'draw'
        beat = {'rock': 'scissors', 'scissors': 'paper', 'paper': 'rock'}
        return 'p1' if beat[c1] == c2 else 'p2'

    def _rps_roll(self, body):
        with state_lock:
            if not state['started'] or state['gameType'] != 'rps':
                self._json(400, {'error': '当前不是猜拳对局'})
                return
            rps = state['rps']
            pid = body.get('id')
            if rps['phase'] != 'play':
                self._json(400, {'error': '还不是出拳时机'})
                return
            if pid != rps['turn']:
                self._json(400, {'error': '还没轮到你出拳'})
                return
            choice = random.choice(['rock', 'scissors', 'paper'])
            rps['picks'][pid] = choice
            pair = rps['pair']
            if pid == pair[0] and len(rps['picks']) < 2:
                rps['turn'] = pair[1]
            else:
                # 双方都出拳，判定胜负
                c1 = rps['picks'][pair[0]]
                c2 = rps['picks'][pair[1]]
                res = self._rps_result(c1, c2)
                rps['phase'] = 'judge'
                rps['turn'] = -1
                if res == 'draw':
                    rps['draw'] = True
                    rps['winner'] = rps['loser'] = -1
                else:
                    rps['draw'] = False
                    rps['winner'] = pair[0] if res == 'p1' else pair[1]
                    rps['loser'] = pair[1] if res == 'p1' else pair[0]
                    rps['scores'][str(rps['winner'])] = rps['scores'].get(str(rps['winner']), 0) + 1
            snap = self._rps_snapshot()
        broadcast('rps', snap)
        self._json(200, {'rps': snap})

    def _rps_next(self, body):
        with state_lock:
            if not state['started'] or state['gameType'] != 'rps':
                self._json(400, {'error': '当前不是猜拳对局'})
                return
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以开始下一轮'})
                return
            rps = state['rps']
            rps['round'] += 1
            rps['phase'] = 'play'
            rps['picks'] = {}
            rps['winner'] = rps['loser'] = -1
            rps['draw'] = False
            rps['penalty'] = ''
            ids = [p['id'] for p in state['players']]
            if len(ids) >= 2:
                a = random.choice(ids)
                rest = [x for x in ids if x != a]
                b = random.choice(rest)
                rps['pair'] = [a, b]
                rps['turn'] = a
            else:
                rps['phase'] = 'wait'
                rps['turn'] = -1
            snap = self._rps_snapshot()
        broadcast('rps', snap)
        self._json(200, {'rps': snap})

    def _rps_punish(self, body):
        with state_lock:
            if not state['started'] or state['gameType'] != 'rps':
                self._json(400, {'error': '当前不是猜拳对局'})
                return
            if state['rps']['winner'] < 0 or body.get('id') != state['rps']['winner']:
                self._json(400, {'error': '只有赢家可以抽取惩罚'})
                return
            state['rps']['penalty'] = (body.get('penalty') or '').strip()
            snap = self._rps_snapshot()
        broadcast('rps', snap)
        self._json(200, {'rps': snap})

    def _rps_cards(self, body):
        """房主自定义惩罚卡池，广播全员（只有房主可以修改添加）。"""
        with state_lock:
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以修改自定义惩罚卡'})
                return
            gender = body.get('gender')
            if gender not in ('male', 'female'):
                self._json(400, {'error': '性别参数无效'})
                return
            cards = body.get('cards')
            if not isinstance(cards, list):
                self._json(400, {'error': '卡片数据无效'})
                return
            cleaned = []
            for c in cards:
                if isinstance(c, dict):
                    text = str(c.get('text') or '').strip()
                    act = str(c.get('act') or '').strip()
                    if text and act in ('h-2', 'h-3'):
                        cleaned.append({'act': act, 'text': text})
            state['penaltyCards'][gender] = cleaned
            snap = json.loads(json.dumps(state['penaltyCards']))
        broadcast('rps-cards', snap)
        self._json(200, {'penaltyCards': snap})

    def _tda_snapshot(self):
        return json.loads(json.dumps(state['tda']))

    def _tda_draw(self, body):
        """当前轮到的玩家抽卡（客户端抽好卡后上报，广播给全员）"""
        global tda_timer
        with state_lock:
            if not state['started'] or state['gameType'] != 'tda':
                self._json(400, {'error': '当前不是真心话大冒险对局'})
                return
            pid = body.get('id')
            tda = state['tda']
            if tda['phase'] != 'idle':
                self._json(400, {'error': '当前卡片还没看完'})
                return
            if pid != tda['turn'] or pid >= len(state['players']):
                self._json(400, {'error': '还没轮到你抽卡'})
                return
            ctype = body.get('type')
            if ctype not in ('truth', 'dare'):
                self._json(400, {'error': '卡片类型不合法'})
                return
            text = (body.get('text') or '').strip()
            if not text:
                self._json(400, {'error': '卡片内容为空'})
                return
            tda['card'] = {'type': ctype, 'text': text}
            tda['phase'] = 'show'
            snap = self._tda_snapshot()
        # 展示结束后自动切到下一位
        if tda_timer:
            try: tda_timer.cancel()
            except Exception: pass
        tda_timer = threading.Timer(TDA_AUTO_SECONDS, self._tda_auto_next)
        tda_timer.daemon = True
        tda_timer.start()
        broadcast('tda', snap)
        self._json(200, {'tda': snap})

    def _tda_auto_next(self):
        """定时器触发：展示结束自动切到下一位"""
        global tda_timer
        tda_timer = None
        with state_lock:
            if not state['started'] or state['gameType'] != 'tda':
                return
            tda = state['tda']
            if tda['phase'] != 'show':
                return
            n = len(state['players'])
            if n == 0:
                return
            nxt = (tda['turn'] + 1) % n
            tda['turn'] = nxt
            tda['phase'] = 'idle'
            tda['card'] = None
            tda['round'] += 1
            snap = self._tda_snapshot()
        broadcast('tda', snap)

    def _tda_next(self, body):
        """轮到下一位玩家抽卡（房主或当前抽卡者可手动提前触发）"""
        global tda_timer
        if tda_timer:
            try: tda_timer.cancel()
            except Exception: pass
            tda_timer = None
        with state_lock:
            if not state['started'] or state['gameType'] != 'tda':
                self._json(400, {'error': '当前不是真心话大冒险对局'})
                return
            pid = body.get('id')
            tda = state['tda']
            n = len(state['players'])
            # 权限：房主 或 当前轮到/刚抽完卡的玩家
            if pid != state['hostId'] and pid != tda['turn']:
                self._json(400, {'error': '只有房主或当前玩家可以继续'})
                return
            if n == 0:
                self._json(200, {'tda': self._tda_snapshot()})
                return
            nxt = (tda['turn'] + 1) % n
            tda['turn'] = nxt
            tda['phase'] = 'idle'
            tda['card'] = None
            tda['round'] += 1
            snap = self._tda_snapshot()
        broadcast('tda', snap)
        self._json(200, {'tda': snap})

    def _mystery_snapshot(self):
        return json.loads(json.dumps(state['mystery']))

    def _mystery_open(self, body):
        """当前轮到的玩家开盒：客户端按自己性别抽好内容上报，广播给全员。"""
        with state_lock:
            if not state['started'] or state['gameType'] != 'mystery':
                self._json(400, {'error': '当前不是盲盒对局'})
                return
            pid = body.get('id')
            m = state['mystery']
            if m['phase'] != 'play':
                self._json(400, {'error': '当前不能开盒'})
                return
            if not m['order'] or m['turnIdx'] >= len(m['order']) or m['order'][m['turnIdx']] != pid:
                self._json(400, {'error': '还没轮到你开盒'})
                return
            bi = body.get('boxIdx')
            if not isinstance(bi, int) or bi < 0 or bi >= 9:
                self._json(400, {'error': '盲盒编号无效'})
                return
            if m['boxes'][bi] is not None:
                self._json(400, {'error': '这个盲盒已经开过了'})
                return
            kind = body.get('kind')
            if kind not in ('reward', 'penalty', 'jackpot'):
                self._json(400, {'error': '内容类型无效'})
                return
            try:
                delta = int(body.get('delta'))
            except Exception:
                self._json(400, {'error': '爱心数值无效'})
                return
            text = (body.get('text') or '').strip()
            if not text:
                self._json(400, {'error': '卡片内容为空'})
                return
            m['boxes'][bi] = {'who': pid, 'kind': kind, 'delta': delta, 'text': text}
            m['hearts'][str(pid)] = m['hearts'].get(str(pid), 0) + delta
            m['round'] += 1
            if m['round'] >= m['total']:
                m['phase'] = 'end'
            else:
                m['turnIdx'] = (m['turnIdx'] + 1) % len(m['order'])
            snap = self._mystery_snapshot()
        broadcast('mystery', snap)
        self._json(200, {'mystery': snap})


# ---------- 联机幸运转盘 ----------

    def _wheel_snapshot(self):
        with state_lock:
            return json.loads(json.dumps(state['wheel']))

    def _wheel_spin(self, body):
        with state_lock:
            w = state['wheel']
            if not state['started'] or state['gameType'] != 'wheel':
                self._json(400, {'error': '游戏未开始'}); return
            if w['phase'] != 'play':
                self._json(400, {'error': '现在不能转'}); return
            pid = body.get('id')
            if pid != state['players'][w['turn']]['id']:
                self._json(400, {'error': '还没轮到你转'}); return
            idx = random.randint(0, 23)
            w['idx'] = idx
            w['done'][pid] = True
            w['last'] = {'pid': pid, 'name': state['players'][w['turn']]['name'], 'idx': idx}
            w['phase'] = 'show'
            snap = json.loads(json.dumps(state['wheel']))
        broadcast('wheel', snap)
        self._json(200, {'wheel': snap})

    def _wheel_next(self, body):
        with state_lock:
            w = state['wheel']
            if state['gameType'] != 'wheel' or w['phase'] != 'show':
                self._json(400, {'error': '状态不对'}); return
            n = len(state['players'])
            for _ in range(n):
                w['turn'] = (w['turn'] + 1) % n
                if not w['done'].get(state['players'][w['turn']]['id']):
                    break
            if all(w['done'].get(p['id']) for p in state['players']):
                w['round'] += 1
                if w['round'] >= w['total']:
                    w['phase'] = 'end'
                else:
                    w['done'] = {}
                    w['turn'] = 0
                    w['phase'] = 'play'
            else:
                w['phase'] = 'play'
            snap = json.loads(json.dumps(state['wheel']))
        broadcast('wheel', snap)
        self._json(200, {'wheel': snap})


# ---------- 联机射击游戏 ----------

    def _shoot_hit_part(self, x, y):
        import math
        if math.hypot(x - 260, y - 342) < 42: return 'heart'
        if math.hypot(x - 260, y - 150) < 52: return 'face'
        if 208 <= x <= 312 and 212 <= y <= 342: return 'body'
        if (164 <= x <= 232 and 240 <= y <= 342) or (288 <= x <= 356 and 240 <= y <= 342): return 'arm'
        if 232 <= x <= 288 and 342 <= y <= 472: return 'leg'
        return None

    def _shoot_part_score(self, part):
        return {'heart': 10, 'face': 8, 'body': 6, 'arm': 4}.get(part, 3)

    def _shoot_fire(self, body):
        with state_lock:
            s = state['shoot']
            if not state['started'] or state['gameType'] != 'shoot':
                self._json(400, {'error': '游戏未开始'}); return
            if s['phase'] != 'play':
                self._json(400, {'error': '现在不能射击'}); return
            pid = body.get('id')
            if pid != state['players'][s['turn']]['id']:
                self._json(400, {'error': '还没轮到你射'}); return
            x = float(body.get('x', 260)); y = float(body.get('y', 290))
            part = self._shoot_hit_part(x, y)
            score = self._shoot_part_score(part) if part else 0
            s['scores'][str(pid)] = s['scores'].get(str(pid), 0) + score
            s['done'][pid] = True
            s['last'] = {'pid': pid, 'name': state['players'][s['turn']]['name'], 'part': part, 'score': score, 'x': x, 'y': y}
            # 推进下一位
            n = len(state['players'])
            all_done = all(s['done'].get(p['id']) for p in state['players'])
            if all_done:
                s['phase'] = 'end'
                s['round'] += 1
                best = -1; winner = -1
                for p in state['players']:
                    sc = s['scores'].get(str(p['id']), 0)
                    if sc > best:
                        best = sc; winner = p['id']
                s['winner'] = winner
            else:
                for _ in range(n):
                    s['turn'] = (s['turn'] + 1) % n
                    if not s['done'].get(state['players'][s['turn']]['id']):
                        break
                np = state['players'][s['turn']]
                s['mode'] = 'male' if np['gender'] == 'female' else 'female'
            snap = json.loads(json.dumps(state['shoot']))
        broadcast('shoot', snap)
        self._json(200, {'shoot': snap})


# ---------- 联机赛马 ----------

    def _race_snapshot(self):
        with state_lock:
            return json.loads(json.dumps(state['race']))

    def _race_bet(self, body):
        with state_lock:
            r = state['race']
            if not state['started'] or state['gameType'] != 'race':
                self._json(400, {'error': '游戏未开始'}); return
            if r['phase'] != 'bet':
                self._json(400, {'error': '现在不能下注'}); return
            pid = body.get('id')
            horse = body.get('horse')
            if horse not in (1, 2, 3, 4, 5):
                self._json(400, {'error': '请选择 1-5 号马'}); return
            r['bets'][pid] = horse
            snap = json.loads(json.dumps(state['race']))
        broadcast('race', snap)
        self._json(200, {'race': snap})

    def _race_run(self, body):
        with state_lock:
            r = state['race']
            if not state['started'] or state['gameType'] != 'race':
                self._json(400, {'error': '游戏未开始'}); return
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以开跑'}); return
            if r['phase'] != 'bet':
                self._json(400, {'error': '现在不能开跑'}); return
            if len(r['bets']) < len(state['players']):
                self._json(400, {'error': '还有玩家没下注'}); return
            horses = list(range(1, 6))
            random.shuffle(horses)
            r['ranking'] = horses
            r['winner'] = horses[0]
            r['winnerTxt'] = '🏆 冠军是 ' + str(horses[0]) + ' 号马！'
            r['losers'] = []
            for p in state['players']:
                if r['bets'].get(p['id']) == horses[0]:
                    r['scores'][str(p['id'])] = r['scores'].get(str(p['id']), 0) + 1
                else:
                    r['losers'].append(p['id'])
            r['phase'] = 'judge'
            snap = json.loads(json.dumps(state['race']))
        broadcast('race', snap)
        self._json(200, {'race': snap})

    def _race_punish(self, body):
        with state_lock:
            r = state['race']
            if state['gameType'] != 'race' or r['phase'] != 'judge':
                self._json(400, {'error': '状态不对'}); return
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以抽惩罚'}); return
            if not r['losers']:
                r['penalty'] = ''
            else:
                gender = state['players'][r['losers'][0]]['gender']
                cards = state['penaltyCards'].get(gender) or []
                if cards:
                    r['penalty'] = random.choice(cards)
                else:
                    default_m = ['做 5 个俯卧撑', '给对方讲个笑话', '学猫叫三声', '原地转 5 圈']
                    default_f = ['做 5 个深蹲', '给对方唱首歌', '学猫叫三声', '原地转 5 圈']
                    pool = default_f if gender == 'female' else default_m
                    r['penalty'] = random.choice(pool)
            snap = json.loads(json.dumps(state['race']))
        broadcast('race', snap)
        self._json(200, {'race': snap})

    def _race_next(self, body):
        with state_lock:
            r = state['race']
            if state['gameType'] != 'race' or r['phase'] != 'judge':
                self._json(400, {'error': '状态不对'}); return
            r['round'] += 1
            if r['round'] >= r['total']:
                r['phase'] = 'end'
            else:
                r['bets'] = {}
                r['penalty'] = ''
                r['winnerTxt'] = ''
                r['losers'] = []
                r['phase'] = 'bet'
            snap = json.loads(json.dumps(state['race']))
        broadcast('race', snap)
        self._json(200, {'race': snap})


# ---------- 联机瑜伽抽卡 ----------

    def _yoga_draw(self, body):
        with state_lock:
            y = state['yoga']
            if not state['started'] or state['gameType'] != 'yoga':
                self._json(400, {'error': '游戏未开始'}); return
            if y['phase'] != 'draw':
                self._json(400, {'error': '现在不能抽卡'}); return
            pid = body.get('id')
            if pid != y['order'][y['turnIdx']]:
                self._json(400, {'error': '还没轮到你抽'}); return
            # 抽卡池与服务端自由池一致：前 FREE_COUNT 个免费，解锁后并入进阶池
            y['pack'] = bool(body.get('pack'))
            total = FREE_COUNT + len(PAID_POSE_NAMES)
            pool = list(range(total)) if y['pack'] else list(range(FREE_COUNT))
            # 客户端可传主题筛选后的索引列表（仅允许合法范围内的白名单）
            try:
                sub = [int(x) for x in (body.get('pool') or [])]
            except Exception:
                sub = []
            if sub:
                sub = [x for x in sub if 0 <= x < total]
                if y['pack']:
                    sub = [x for x in sub if x >= FREE_COUNT] or sub
                else:
                    sub = [x for x in sub if x < FREE_COUNT]
                if sub:
                    pool = sub
            y['posIdx'] = random.choice(pool)
            y['phase'] = 'hold'
            snap = json.loads(json.dumps(state['yoga']))
        broadcast('yoga', snap)
        self._json(200, {'yoga': snap})

    def _yoga_done(self, body):
        with state_lock:
            y = state['yoga']
            if state['gameType'] != 'yoga' or y['phase'] != 'hold':
                self._json(400, {'error': '状态不对'}); return
            pid = body.get('id')
            y['done'][pid] = True
            n = len(y['order'])
            all_done = all(y['done'].get(oid) for oid in y['order'])
            if all_done:
                y['round'] += 1
                y['phase'] = 'end'
            else:
                y['turnIdx'] = (y['turnIdx'] + 1) % n
                while y['done'].get(y['order'][y['turnIdx']]):
                    y['turnIdx'] = (y['turnIdx'] + 1) % n
                y['phase'] = 'draw'
            snap = json.loads(json.dumps(state['yoga']))
        broadcast('yoga', snap)
        self._json(200, {'yoga': snap})


# ---------- 入口 ----------
def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print('=' * 50)
    print('  恋爱飞行棋 · 联机服务器已启动')
    print('=' * 50)
    print(f'  监听端口：    {PORT}')
    print(f'  健康检查：    http://localhost:{PORT}/health')
    print('-' * 50)
    # 检测是否在云平台运行
    if os.environ.get('PORT') or os.environ.get('RENDER') or os.environ.get('RAILWAY_ENVIRONMENT'):
        print('  云平台模式：服务已就绪，平台域名自动分配')
    else:
        ip = get_lan_ip()
        print(f'  本机访问：    http://localhost:{PORT}')
        print(f'  同局域网访问：http://{ip}:{PORT}')
        print('  让其他设备连同一个 WiFi，打开上面的「同局域网访问」地址')
    print('  按 Ctrl+C 停止服务器')
    print('=' * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务器已停止')
        server.server_close()
