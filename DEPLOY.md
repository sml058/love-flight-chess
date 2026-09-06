# 恋爱飞行棋 · 联机服务器云平台部署指南

把 server.py 部署到免费云平台后，任何设备（手机/电脑）在任何网络下都能联机玩，不需要同一 WiFi，也不需要一台设备一直开着当服务器。

## 推荐平台对比

| 平台 | 免费层 | 休眠 | 部署难度 | 推荐度 |
|------|--------|------|----------|--------|
| **Render** | 永久免费 | 15分钟无请求休眠，唤醒约5秒 | ⭐ 简单 | ⭐⭐⭐⭐⭐ |
| PythonAnywhere | 永久免费 | 每日CPU限额，需手动配置 | ⭐⭐ 中等 | ⭐⭐⭐ |
| Replit | 免费层 | 休眠较快，需保持页面打开 | ⭐ 简单 | ⭐⭐⭐ |
| Railway | 免费额度5美元/月 | 不休眠 | ⭐⭐ 需绑卡 | ⭐⭐⭐⭐ |

**最推荐 Render**：真正免费、不需信用卡、部署最简单、自动HTTPS、有免费域名。

---

## 方案一：Render 部署（最推荐）

### 准备工作
1. 注册 [GitHub](https://github.com/) 账号（免费）
2. 注册 [Render](https://render.com/) 账号（免费，可用 GitHub 登录）

### 步骤1：上传代码到 GitHub
1. 在 GitHub 新建一个仓库（Repository），名字随便取，比如 `love-flight-chess`
2. 把以下文件上传到仓库根目录：
   - `server.py`
   - `index.html`
   - `requirements.txt`
   - `Procfile`
   - `render.yaml`
3. 确保文件都在仓库根目录（不要放在子文件夹里）

### 步骤2：在 Render 部署
1. 登录 [Render Dashboard](https://dashboard.render.com/)
2. 点击「New +」→ 「Web Service」
3. 选择你刚才创建的 GitHub 仓库（如果看不到，点「Configure account」授权 Render 访问你的仓库）
4. 配置页面填写：
   - **Name**: 随便取，比如 `love-flight-chess`（这个会成为你的域名前缀）
   - **Region**: 选 Singapore（新加坡，离国内近速度快）
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Instance Type**: 选 Free（免费）
5. 点击「Create Web Service」
6. 等待约2-3分钟，部署完成后页面顶部会显示你的域名，比如：
   `https://love-flight-chess.onrender.com`

### 步骤3：验证
1. 浏览器打开 `https://你的域名.onrender.com/health`
2. 看到 `{"status":"ok","players":0,...}` 就说明服务器正常运行了
3. 打开 `https://你的域名.onrender.com` 就能看到游戏页面

### 注意事项
- **免费层会休眠**：15分钟没有请求会自动休眠，第一次打开需要等5-10秒唤醒。唤醒后就正常了。
- **解决休眠延迟**：可以用 [UptimeRobot](https://uptimerobot.com/)（免费）每5分钟访问一次你的 `/health` 端点，保持服务唤醒。
- **域名**：免费域名是 `xxx.onrender.com`，也可以绑定自己的域名。

---

## 方案二：PythonAnywhere 部署

### 步骤
1. 注册 [PythonAnywhere](https://www.pythonanywhere.com/) 免费账号
2. 登录后点「Files」，上传 `server.py` 和 `index.html` 到根目录
3. 点「Web」→「Add a new web app」
4. 选择「Manual configuration」→「Python 3.10」
5. 创建完成后，在 Web 页面找到「WSGI configuration file」，点击编辑
6. 把 WSGI 文件内容替换为：
```python
import sys
import os
os.chdir('/home/你的用户名')
sys.path.insert(0, '/home/你的用户名')
from server import Handler
from http.server import ThreadingHTTPServer

# PythonAnywhere 需要用 WSGI 包装
# 注意：PythonAnywhere 免费层不支持 SSE 长连接，联机体验可能不如 Render
```
7. 实际上 PythonAnywhere 对 SSE 支持不好，**更推荐用 Render**。

---

## 方案三：Replit 部署（最简单，但需保持页面）

1. 注册 [Replit](https://replit.com/) 账号
2. 点「Create Repl」→ 选 Python 模板
3. 把 `server.py` 和 `index.html` 上传/粘贴进去
4. 点「Run」运行
5. Replit 会给你一个域名，比如 `https://xxx.replit.app`
6. **注意**：免费层关闭页面后服务会停止，需要保持 Replit 页面打开才能持续联机。

---

## 部署后怎么用

### 手机端（没有电脑也能玩）
1. 部署完成后，记住你的服务器域名，比如 `https://love-flight-chess.onrender.com`
2. 所有人（不管在什么网络、什么地方）用手机浏览器打开这个域名
3. 第一个人点「联机加入」→ 输入昵称 → 创建房间
4. 其他人点「联机加入」→ 输入昵称 → 自动加入同一个房间
5. 轮到谁谁掷骰子，所有人实时同步

### 电脑端
同样用浏览器打开域名即可，操作和手机一样。

---

## 常见问题

**Q: 免费层休眠了怎么办？**
A: 第一次打开等5-10秒就会自动唤醒。或者用 UptimeRobot 免费监控保持唤醒。

**Q: 最多支持多少人同时联机？**
A: 游戏设计最多4人。免费云平台的性能支持4人完全没问题。

**Q: 数据会保存吗？**
A: 服务器重启后游戏状态会重置（内存存储）。正常玩一局不需要持久化。

**Q: 可以自定义域名吗？**
A: Render 免费层支持绑定自己的域名，在设置里添加即可。

**Q: 部署后游戏页面打不开？**
A: 检查 `/health` 端点是否正常。如果正常但游戏页面打不开，确认 `index.html` 和 `server.py` 在同一个目录。

---

## 文件清单（部署需要上传的文件）

```
你的项目/
├── server.py          # 服务器主程序（已适配云平台）
├── index.html         # 游戏前端页面
├── requirements.txt   # Python依赖（零依赖，仅用于识别项目类型）
├── Procfile           # 启动命令（Render/Heroku用）
├── render.yaml        # Render部署配置（可选，有了更方便）
└── DEPLOY.md          # 本部署指南
```
