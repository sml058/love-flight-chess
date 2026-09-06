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
            bv = body.get('boardVersion')
            if bv in (50, 100, 150):
                state['boardVersion'] = bv
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
