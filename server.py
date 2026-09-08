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
from urllib.parse import urlparse, parse_qs

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
    'gameType': 'flight',  # 房间游戏：flight=飞行棋 / rps=猜拳 / tda=真心话大冒险
    'cells': [],           # 本局特殊格位置（开局由服务器统一生成，全员一致）
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
    'tda': {           # 联机真心话大冒险状态
        'turn': 0,       # 当前轮到抽卡的玩家
        'phase': 'idle', # idle=待抽卡 / show=已抽展示
        'card': None,    # {type:'truth'|'dare', text:'...'} 当前展示的卡
        'round': 0,      # 已轮数
        'auto': 10,      # 抽卡后自动切换到下一位的秒数
    },
}
state_lock = threading.Lock()
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
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._serve_file('index.html', 'text/html; charset=utf-8')
        elif path == '/server.py':
            self._serve_file('server.py', 'text/plain; charset=utf-8')
        elif path == '/events':
            self._handle_sse()
        elif path == '/admin':
            self._admin_page()
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
        elif path == '/admin/clean':
            self._admin_clean()
        elif path == '/admin/reset':
            self._admin_reset()
        elif path == '/rps-cards':
            self._rps_cards(body)
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
            if gt in ('flight', 'rps', 'tda'):
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

    # ---------- 服务器管理入口 ----------
    def _online_pids(self):
        with sse_lock:
            return set(sse_owner.values())

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
