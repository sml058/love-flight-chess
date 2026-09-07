#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
恋爱飞行棋 · 局域网联机服务器
用法：python3 server.py
同 WiFi 下的设备用浏览器打开提示的地址即可加入。
零依赖，仅用 Python 标准库。
"""
import json
import os
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 云平台部署时端口由环境变量 PORT 决定（Render/Railway 等平台自动注入）
PORT = int(os.environ.get('PORT', 8765))

# ---------- 游戏状态 ----------
state = {
    'players': [],   # {id,name,gender,pos,steps,flying,hearts,shield,skip}
    'turn': 0,
    'phase': 'idle',
    'winner': None,
    'started': False,   # False=等待大厅，True=游戏进行中
    'hostId': -1,       # 房主（第一个加入的玩家）
    'boardVersion': 50, # 本局棋盘版本（50/100/150）
    'gameType': 'flight',  # 房间游戏：flight=飞行棋 / rps=猜拳 / tda=真心话大冒险
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
    'tda': {           # 联机真心话大冒险状态
        'turn': 0,       # 当前轮到抽卡的玩家
        'phase': 'idle', # idle=待抽卡 / show=已抽展示
        'card': None,    # {type:'truth'|'dare', text:'...'} 当前展示的卡
        'round': 0,      # 已轮数
    },
}
state_lock = threading.Lock()
sse_clients = []       # 已连接的 SSE 响应对象
sse_lock = threading.Lock()


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
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._serve_file('index.html', 'text/html; charset=utf-8')
        elif path == '/server.py':
            self._serve_file('server.py', 'text/plain; charset=utf-8')
        elif path == '/events':
            self._handle_sse()
        elif path == '/health':
            # 健康检查端点（云平台用它判断服务是否存活）
            self._json(200, {'status': 'ok', 'players': len(state['players']), 'time': int(time.time())})
        else:
            self.send_error(404)

    def _serve_file(self, fname, ctype):
        try:
            with open(fname, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
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
        # 保持连接
        try:
            while True:
                time.sleep(25)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with sse_lock:
                if self.wfile in sse_clients:
                    sse_clients.remove(self.wfile)

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
        else:
            self.send_error(404)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
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

    def _start(self, body):
        with state_lock:
            if not state['players']:
                self._json(400, {'error': '房间为空'})
                return
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以开始游戏'})
                return
            state['started'] = True
            state['phase'] = 'idle'
            state['turn'] = 0
            state['winner'] = None
            gt = body.get('gameType')
            if gt in ('flight', 'rps', 'tda'):
                state['gameType'] = gt
            bv = body.get('boardVersion')
            if bv in (50, 100, 150):
                state['boardVersion'] = bv
            # 初始化猜拳状态
            state['rps'] = {
                'pair': [], 'picks': {}, 'turn': -1, 'phase': 'wait',
                'winner': -1, 'loser': -1, 'draw': False, 'round': 0,
                'scores': {}, 'penalty': '',
            }
            # 初始化真心话大冒险状态
            state['tda'] = {
                'turn': 0, 'phase': 'idle', 'card': None, 'round': 0,
            }
            for p in state['players']:
                p.update(pos=-1, steps=0, flying=False, hearts=0, shield=False, skip=False)
            # 在锁内做深拷贝，锁外再广播（snapshot() 会再次获取同一把锁，不能在此调用）
            snap = json.loads(json.dumps(state))
            players_snap = json.loads(json.dumps(state['players']))
        broadcast('start', {'state': snap})
        broadcast('players', players_snap)
        self._json(200, {'state': snap})

    def _roll(self, body):
        with state_lock:
            if state['phase'] != 'idle':
                self._json(400, {'error': '不是掷骰时机'})
                return
            pid = body.get('id')
            if pid != state['turn'] or pid >= len(state['players']):
                self._json(400, {'error': '还没轮到你'})
                return
            state['phase'] = 'rolling'
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
        with state_lock:
            pid = body.get('id')
            was_started = state['started']
            state['players'] = [p for p in state['players'] if p['id'] != pid]
            if state['hostId'] == pid:
                state['hostId'] = state['players'][0]['id'] if state['players'] else -1
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
        self._json(200, {})

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
            if body.get('id') != state['hostId']:
                self._json(400, {'error': '只有房主可以抽取惩罚'})
                return
            state['rps']['penalty'] = (body.get('penalty') or '').strip()
            snap = self._rps_snapshot()
        broadcast('rps', snap)
        self._json(200, {'rps': snap})

    def _tda_snapshot(self):
        return json.loads(json.dumps(state['tda']))

    def _tda_draw(self, body):
        """当前轮到的玩家抽卡（客户端抽好卡后上报，广播给全员）"""
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
        broadcast('tda', snap)
        self._json(200, {'tda': snap})

    def _tda_next(self, body):
        """轮到下一位玩家抽卡（房主或当前抽卡者可触发）"""
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
