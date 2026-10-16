# -*- coding: utf-8 -*-
"""局域网文件共享工具：把本地文件夹共享到局域网，浏览器访问可查看列表并下载。

支持双角色：访问密码（普通用户）+ 管理员密码（可进入设置页修改配置）。
"""

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import tomllib
import urllib.parse
import webbrowser
from pathlib import Path

from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

# 配置位置分两种模式：
# - 脚本直接运行（py share.py）：读取项目根目录的 config.toml
# - PyInstaller 打包成 exe 运行：读取 %APPDATA%\file-share-server\config.toml，exe 目录保持干净
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    CONFIG_DIR = Path(os.environ.get("APPDATA") or BASE_DIR) / "file-share-server"
else:
    BASE_DIR = Path(__file__).resolve().parent
    CONFIG_DIR = BASE_DIR
CONFIG_PATH = CONFIG_DIR / "config.toml"

# 托盘图标文件：exe 模式取打包内置的，脚本模式取项目目录的
if getattr(sys, "frozen", False):
    ICON_FILE = Path(sys._MEIPASS) / "icon.ico"
else:
    ICON_FILE = BASE_DIR / "icon.ico"

# 当前版本号（发布新版本时同步修改，并打同名 git tag）
VERSION = "1.0.4"

DEFAULT_CONFIG = {
    "share_dir": str(BASE_DIR),
    "port": 8000,
    "visitor_enabled": False,
    "visitor_password": "",
    "admin_password": "",
    "share_enabled": True,
}

USAGE_TEXT = """局域网文件共享工具 - 使用说明
================================

【这是什么】
本软件把「分享文件夹」共享到局域网，同一网络内的其他电脑、手机
用浏览器打开网页即可查看文件列表并下载文件。

【快速开始】
1. 把要共享的文件复制/移动到本文件夹（支持子文件夹）
2. 双击运行 share.py（或命令行执行：py share.py）
3. 启动后窗口会显示两个地址：
   - 本机访问：   http://127.0.0.1:8000
   - 局域网访问： http://192.168.x.x:8000  （把这个发给同网络的人）
4. 其他人用浏览器打开「局域网访问」地址即可查看和下载
5. 按 Ctrl+C 停止服务

【两种密码】
- 访问密码：普通用户登录后只能浏览和下载
- 管理员密码：额外拥有「设置」页面，可修改分享目录、访问密码、管理员密码
登录页输入哪个密码，就自动获得对应权限。

【默认密码】（首次运行自动生成，建议尽快在设置页修改）
- 访问密码：123456（默认关闭，可在设置页启用）
- 管理员密码：admin456

【常用命令】
   py share.py                              按保存的配置启动
   py share.py --password 123456            设置/更改访问密码
   py share.py --no-password                关闭访问密码
   py share.py --port 8080                  更改端口
   py share.py --dir D:\\资料               更改分享目录

【提示】
- 设置页里点「选择目录」按钮可从系统中直接挑选文件夹
- 脚本运行时配置为软件目录下的 config.toml；exe 运行时配置在系统目录 %APPDATA%\\file-share-server\\config.toml
- 请在内网/可信网络中使用
"""

# ---------------------------------------------------------------- 配置


def first_run_setup(cfg):
    """首次使用：创建默认配置（双密码 + 端口8000），并在桌面创建「分享文件夹」及使用说明。"""
    if CONFIG_PATH.exists():
        return False
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        desktop = Path.home()
    share = desktop / "分享文件夹"
    share.mkdir(parents=True, exist_ok=True)
    readme = share / "使用说明.txt"
    if not readme.exists():
        readme.write_text(USAGE_TEXT, encoding="utf-8-sig")
    cfg["share_dir"] = str(share)
    cfg["port"] = 8000
    cfg["visitor_enabled"] = False
    cfg["visitor_password"] = "123456"
    cfg["admin_password"] = "admin456"
    cfg["share_enabled"] = True
    save_config(cfg)
    return True


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in cfg})
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 局域网文件共享工具配置文件",
        "# 密码以明文保存（用于登录校验和生成分享链接），请妥善保管此文件",
        f"share_dir = {json.dumps(cfg['share_dir'], ensure_ascii=False)}",
        f"port = {int(cfg['port'])}",
        f"visitor_enabled = {'true' if cfg['visitor_enabled'] else 'false'}",
        f"visitor_password = {json.dumps(cfg['visitor_password'])}",
        f"admin_password = {json.dumps(cfg['admin_password'])}",
        f"share_enabled = {'true' if cfg['share_enabled'] else 'false'}",
        "",
    ]
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


def migrate_old_config():
    """把旧版 config.json 迁移转换为 config.toml（软件目录与配置目录都检查）。"""
    for old in (BASE_DIR / "config.json", CONFIG_DIR / "config.json"):
        if not CONFIG_PATH.exists() and old.exists():
            try:
                data = json.loads(old.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # 兼容旧版字段名；旧哈希密码无法还原，丢弃后需重新设置
                    if "password_enabled" in data:
                        data["visitor_enabled"] = data.pop("password_enabled")
                    data.pop("password_hash", None)
                    cfg = dict(DEFAULT_CONFIG)
                    cfg.update({k: v for k, v in data.items() if k in cfg})
                    save_config(cfg)
                old.unlink()
            except (json.JSONDecodeError, OSError):
                pass


# ---------------------------------------------------------------- 安全


def safe_join(root, rel):
    """把请求的相对路径拼到 root 下，并保证结果仍在 root 之内。"""
    root = Path(root).resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


# ---------------------------------------------------------------- 页面

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 - 文件共享</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Microsoft YaHei", system-ui, sans-serif;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #667eea, #764ba2);
  }
  .card {
    background: #fff; border-radius: 12px; padding: 40px 36px;
    width: 90%; max-width: 360px; box-shadow: 0 12px 40px rgba(0,0,0,.25);
  }
  h1 { font-size: 20px; margin-bottom: 8px; color: #333; }
  p.sub { font-size: 13px; color: #888; margin-bottom: 24px; }
  input[type=password] {
    width: 100%; padding: 12px 14px; font-size: 15px;
    border: 1px solid #ddd; border-radius: 8px; outline: none;
  }
  input[type=password]:focus { border-color: #667eea; }
  button {
    width: 100%; margin-top: 16px; padding: 12px; font-size: 15px;
    color: #fff; background: #667eea; border: none; border-radius: 8px;
    cursor: pointer;
  }
  button:hover { background: #5a6fd6; }
  .error { color: #e74c3c; font-size: 13px; margin-top: 12px; text-align: center; }
</style>
</head>
<body>
  <form class="card" method="post">
    <h1>文件共享</h1>
    <p class="sub">此共享已开启密码保护，请输入访问密码</p>
    <input type="password" name="password" placeholder="访问密码" autofocus required>
    <button type="submit">进入</button>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
  </form>
</body>
</html>
"""

LIST_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件共享</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Microsoft YaHei", system-ui, sans-serif;
    background: #f4f6fa; color: #333;
  }
  .wrap { max-width: 900px; margin: 0 auto; padding: 24px 16px; }
  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 16px;
  }
  h1 { font-size: 20px; }
  .toplinks a { font-size: 13px; color: #999; text-decoration: none; margin-left: 14px; }
  .toplinks a:hover { color: #667eea; }
  .toplinks a.settings { color: #667eea; }
  .breadcrumb {
    font-size: 14px; color: #666; margin-bottom: 14px;
    background: #fff; padding: 10px 14px; border-radius: 8px;
  }
  .breadcrumb a { color: #667eea; text-decoration: none; }
  table {
    width: 100%; border-collapse: collapse; background: #fff;
    border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,.05);
  }
  th, td { padding: 12px 14px; text-align: left; font-size: 14px; }
  th { background: #fafbfc; color: #888; font-weight: normal; font-size: 13px; }
  tr:not(:last-child) td { border-bottom: 1px solid #f0f0f0; }
  td.name a { color: #333; text-decoration: none; }
  td.name a:hover { color: #667eea; }
  td.size, td.mtime { color: #999; white-space: nowrap; }
  td.download { text-align: right; white-space: nowrap; }
  td.download a {
    color: #667eea; text-decoration: none; font-size: 13px;
    border: 1px solid #667eea; border-radius: 6px; padding: 4px 10px;
  }
  td.download a:hover { background: #667eea; color: #fff; }
  td.download a + a { margin-left: 8px; }
  .empty { text-align: center; color: #aaa; padding: 40px 0; }
  .icon { margin-right: 8px; }
  .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 24px; }
  @media (max-width: 600px) {
    th.mtime, td.mtime { display: none; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📂 文件共享</h1>
    <div class="toplinks">
      {% if is_admin %}
      <a class="settings" href="{{ url_for('settings') }}">⚙️ 设置</a>
      <a href="{{ url_for('logout') }}">退出登录</a>
      {% elif not password_on %}
      <a class="settings" href="{{ url_for('login', next=request.path) }}">管理员登录</a>
      {% else %}
      <a href="{{ url_for('logout') }}">退出登录</a>
      {% endif %}
    </div>
  </header>
  <div class="breadcrumb">当前位置：{{ breadcrumb | safe }}</div>
  {% if items %}
  <table>
    <tr><th>名称</th><th>大小</th><th class="mtime">修改时间</th><th></th></tr>
    {% for it in items %}
    <tr>
      <td class="name">
        {% if it.is_dir %}
        <a href="{{ it.url }}"><span class="icon">📁</span>{{ it.name }}</a>
        {% else %}
        <span class="icon">📄</span>{{ it.name }}
        {% endif %}
      </td>
      <td class="size">{{ it.size }}</td>
      <td class="mtime">{{ it.mtime }}</td>
      <td class="download">
        {% if not it.is_dir %}
        {% if share_on %}<a href="javascript:;" onclick="shareLink('{{ it.share_url }}')">分享</a>{% endif %}
        <a href="{{ it.download_url }}">下载</a>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">此文件夹为空</div>
  {% endif %}
  <div class="footer">file-share-server v{{ version }}</div>
</div>
<script>
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise(function(resolve, reject) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy') ? resolve() : reject(); }
    catch (e) { reject(e); }
    document.body.removeChild(ta);
  });
}
function shareLink(url) {
  copyText(url).then(
    function() { alert('分享链接已复制到剪贴板：\\n\\n' + url); },
    function() { prompt('自动复制失败，请手动复制：', url); }
  );
}
</script>
</body>
</html>
"""

SETTINGS_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>设置 - 文件共享</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Microsoft YaHei", system-ui, sans-serif;
    background: #f4f6fa; color: #333;
  }
  .wrap { max-width: 560px; margin: 0 auto; padding: 24px 16px; }
  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 16px;
  }
  h1 { font-size: 20px; }
  header a { font-size: 13px; color: #999; text-decoration: none; }
  header a:hover { color: #667eea; }
  .card {
    background: #fff; border-radius: 10px; padding: 28px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
  }
  .field { margin-bottom: 20px; }
  label.title { display: block; font-size: 14px; color: #333; margin-bottom: 8px; font-weight: bold; }
  label.hint { display: block; font-size: 12px; color: #999; margin-top: 6px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px; font-size: 14px;
    border: 1px solid #ddd; border-radius: 8px; outline: none;
  }
  input:focus { border-color: #667eea; }
  .dirrow { display: flex; gap: 8px; }
  .dirrow input { flex: 1; }
  .dirrow input:disabled { background: #f5f5f5; color: #999; }
  .check { display: flex; align-items: center; font-size: 14px; }
  .check input { margin-right: 8px; }
  button {
    width: 100%; padding: 12px; font-size: 15px;
    color: #fff; background: #667eea; border: none; border-radius: 8px;
    cursor: pointer;
  }
  button:hover { background: #5a6fd6; }
  button.pickbtn {
    width: 100px; margin: 0; padding: 0; white-space: nowrap; flex-shrink: 0;
    color: #667eea; background: #fff; border: 1px solid #667eea;
    font-size: 14px; line-height: 38px; text-align: center;
  }
  button.pickbtn:hover { color: #fff; background: #667eea; }
  button.pickbtn:disabled { opacity: .55; cursor: default; }
  .msg-ok { color: #27ae60; font-size: 13px; margin-bottom: 16px; }
  .msg-err { color: #e74c3c; font-size: 13px; margin-bottom: 16px; }
  .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 24px; }
  hr { border: none; border-top: 1px solid #f0f0f0; margin: 24px 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>⚙️ 设置</h1>
    <a href="{{ url_for('index') }}">← 返回文件列表</a>
  </header>
  <form class="card" method="post">
    {% if msg %}<div class="msg-{{ 'ok' if ok else 'err' }}">{{ msg }}</div>{% endif %}

    <div class="field">
      <label class="title">分享目录</label>
      <div class="dirrow">
        <input type="text" id="share_dir" name="share_dir" value="{{ share_dir }}" required>
        <button type="button" class="pickbtn" onclick="pickDir()">选择目录</button>
      </div>
      <label class="hint">本机文件夹的完整路径，保存后立即生效；也可点击「选择」从本机挑选文件夹</label>
    </div>

    <hr>

    <div class="field">
      <label class="title">访问密码（普通用户）</label>
      <label class="check">
        <input type="checkbox" name="visitor_enabled" {% if visitor_enabled %}checked{% endif %}>
        启用访问密码
      </label>
      <div style="height:10px"></div>
      <input type="password" name="visitor_password" placeholder="新访问密码（留空则不修改）">
      <label class="hint">启用后，普通用户必须输入访问密码才能查看和下载</label>
    </div>

    <hr>

    <div class="field">
      <label class="title">分享功能</label>
      <label class="check">
        <input type="checkbox" name="share_enabled" {% if share_enabled %}checked{% endif %}>
        启用分享功能
      </label>
      <label class="hint">关闭后列表页不再显示「分享」按钮，已发出的分享链接也会失效</label>
    </div>

    <hr>

    <div class="field">
      <label class="title">管理员密码</label>
      <input type="password" name="admin_password" placeholder="新管理员密码（留空则不修改）">
      <label class="hint">管理员可进入此设置页面</label>
    </div>

    <button type="submit">保存设置</button>
  </form>
  <div class="footer">file-share-server v{{ version }}</div>
</div>
<script>
async function pickDir() {
  var btn = document.querySelector('.pickbtn');
  var input = document.getElementById('share_dir');
  btn.disabled = true;
  btn.textContent = '选择中';
  input.disabled = true;
  try {
    var resp = await fetch("{{ url_for('pick_dir') }}", {method: 'POST'});
    var data = await resp.json();
    if (data.ok && data.path) {
      input.value = data.path;
    } else if (data.error) {
      alert(data.error);
    }
  } catch (e) {
    alert('打开选择窗口失败，请手动输入路径');
  } finally {
    btn.disabled = false;
    btn.textContent = '选择目录';
    input.disabled = false;
  }
}
</script>
</body>
</html>
"""

ERROR_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>出错</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px;">
  <h1>{{ code }}</h1>
  <p>{{ message }}</p>
  <p><a href="{{ url_for('index') }}">返回文件列表</a></p>
</body>
</html>
"""

SHARE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件分享</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Microsoft YaHei", system-ui, sans-serif;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #667eea, #764ba2);
  }
  .card {
    background: #fff; border-radius: 12px; padding: 40px 36px;
    width: 90%; max-width: 380px; box-shadow: 0 12px 40px rgba(0,0,0,.25);
    text-align: center;
  }
  h1 { font-size: 20px; margin-bottom: 10px; color: #333; }
  p.sub { font-size: 14px; color: #888; margin-bottom: 24px; line-height: 1.7; }
  input[type=password] {
    width: 100%; padding: 12px 14px; font-size: 15px;
    border: 1px solid #ddd; border-radius: 8px; outline: none;
  }
  input[type=password]:focus { border-color: #667eea; }
  button {
    width: 100%; margin-top: 16px; padding: 12px; font-size: 15px;
    color: #fff; background: #667eea; border: none; border-radius: 8px;
    cursor: pointer;
  }
  button:hover { background: #5a6fd6; }
  .error { color: #e74c3c; font-size: 13px; margin-top: 14px; }
</style>
</head>
<body>
  <div class="card">
    {% if mode == "missing" %}
    <h1>😢 文件不存在</h1>
    <p class="sub">该文件不存在，或已被分享者移除<br>请联系分享者确认链接是否有效</p>
    {% elif mode == "disabled" %}
    <h1>🚫 分享已失效</h1>
    <p class="sub">分享功能已被管理员关闭<br>请联系分享者重新开启或获取新的分享方式</p>
    {% else %}
    <h1>🔐 访问分享</h1>
    <p class="sub">此分享受密码保护<br>请输入访问密码继续</p>
    <form method="get" action="{{ path }}">
      <input type="password" name="pwd" placeholder="访问密码" autofocus required>
      <button type="submit">查看 / 下载</button>
      {% if error %}<div class="error">{{ error }}</div>{% endif %}
    </form>
    {% endif %}
  </div>
</body>
</html>
"""

# ---------------------------------------------------------------- 应用


def create_app(cfg):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)

    def visitor_protection_on():
        """访问密码是否开启（决定普通用户浏览是否需要登录）。"""
        return bool(cfg["visitor_enabled"] and cfg["visitor_password"])

    def admin_configured():
        """是否设置了管理员密码（设置页始终需要管理员登录）。"""
        return bool(cfg["admin_password"])

    def login_available():
        return visitor_protection_on() or admin_configured()

    def check_role(pwd):
        """根据输入的密码判定角色：admin / visitor / None（明文比对）。"""
        if cfg["admin_password"] and pwd == cfg["admin_password"]:
            return "admin"
        if (
            cfg["visitor_enabled"]
            and cfg["visitor_password"]
            and pwd == cfg["visitor_password"]
        ):
            return "visitor"
        return None

    seen_ips = set()

    @app.before_request
    def log_visit():
        """每个访客只记录第一次访问（按 IP 去重），打印时间、IP、角色、路径。"""
        if request.path == "/favicon.ico":
            return None
        ip = request.remote_addr
        if ip in seen_ips:
            return None
        seen_ips.add(ip)
        label = {"admin": "管理员", "visitor": "访问者"}.get(
            session.get("role"), "未登录"
        )
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[访问] {now}  IP:{ip}  {label}  {request.path}", flush=True)
        return None

    @app.before_request
    def require_login():
        if request.endpoint in ("login", "static", "share"):
            return None
        role = session.get("role")
        if request.endpoint == "settings":
            if role == "admin":
                return None
            if admin_configured():
                return redirect(url_for("login", next=request.path))
            abort(403)
        if visitor_protection_on():
            if role in ("admin", "visitor"):
                return None
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not login_available():
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            role = check_role(request.form.get("password", ""))
            if role:
                session["role"] = role
                target = request.args.get("next") or url_for("index")
                if not target.startswith("/"):
                    target = url_for("index")
                return redirect(target)
            error = "密码错误，请重试"
        return render_template_string(LOGIN_PAGE, error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login") if login_available() else url_for("index"))

    @app.route("/pick-dir", methods=["POST"])
    def pick_dir():
        """在服务器本机弹出原生「选择文件夹」对话框，返回所选路径。"""
        if session.get("role") != "admin":
            abort(403)
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(parent=root, title="选择要共享的文件夹")
            root.destroy()
        except Exception as e:
            return {"ok": False, "error": f"无法打开选择窗口，请手动输入路径（{e}）"}
        if not path:
            return {"ok": False}
        return {"ok": True, "path": str(Path(path))}

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if session.get("role") != "admin":
            abort(403)
        msg, ok = None, True
        if request.method == "POST":
            form = request.form
            d = form.get("share_dir", "").strip().strip('"')
            if d:
                p = Path(d).expanduser().resolve()
                if not p.is_dir():
                    msg, ok = f"分享目录不存在：{p}", False
                else:
                    cfg["share_dir"] = str(p)
            if ok:
                vp = form.get("visitor_password", "")
                ve = form.get("visitor_enabled") == "on"
                if ve and not vp and not cfg["visitor_password"]:
                    msg, ok = "启用访问密码前，请先设置一个访问密码", False
                else:
                    cfg["visitor_enabled"] = ve
                    if vp:
                        cfg["visitor_password"] = vp
                    ap = form.get("admin_password", "")
                    if ap:
                        cfg["admin_password"] = ap
                    cfg["share_enabled"] = form.get("share_enabled") == "on"
                    save_config(cfg)
                    # 密码变更后轮换会话密钥，使其他已登录会话（含分享链接带入的）立即失效；
                    # 同时刷新当前管理员的会话，避免其本人被登出
                    if vp or ap:
                        app.secret_key = secrets.token_hex(32)
                        session["role"] = "admin"
                    msg, ok = "设置已保存", True
        return render_template_string(
            SETTINGS_PAGE,
            msg=msg,
            ok=ok,
            share_dir=cfg["share_dir"],
            visitor_enabled=cfg["visitor_enabled"],
            share_enabled=cfg["share_enabled"],
            version=VERSION,
        )

    @app.route("/")
    @app.route("/browse/<path:rel>")
    def index(rel=""):
        root = Path(cfg["share_dir"]).resolve()
        folder = safe_join(root, rel)
        if folder is None or not folder.is_dir():
            abort(404)
        try:
            entries = sorted(
                folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            abort(403)

        items = []
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            sub_rel = str(p.relative_to(root)).replace(os.sep, "/")
            share_path = url_for("share", rel=sub_rel)
            if visitor_protection_on() and cfg["visitor_password"]:
                share_path += "?pwd=" + urllib.parse.quote(cfg["visitor_password"])
            items.append(
                {
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size": "" if p.is_dir() else human_size(st.st_size),
                    "mtime": time_str(st.st_mtime),
                    "url": url_for("index", rel=sub_rel),
                    "download_url": url_for("download_file", rel=sub_rel),
                    "share_url": request.host_url.rstrip("/") + share_path,
                }
            )

        # 面包屑
        parts = [part for part in rel.split("/") if part]
        crumbs = ['<a href="{}">根目录</a>'.format(url_for("index"))]
        acc = []
        for part in parts:
            acc.append(part)
            crumbs.append(
                ' / <a href="{}">{}</a>'.format(
                    url_for("index", rel="/".join(acc)), html_escape(part)
                )
            )

        return render_template_string(
            LIST_PAGE,
            items=items,
            breadcrumb="".join(crumbs),
            is_admin=session.get("role") == "admin",
            password_on=visitor_protection_on() or session.get("role") is not None,
            share_on=cfg["share_enabled"],
            version=VERSION,
        )

    @app.route("/s/<path:rel>")
    def share(rel):
        """分享链接：校验开关、路径与密码，通过后文件直接下载、文件夹跳转到浏览页。"""
        if not cfg["share_enabled"]:
            return (
                render_template_string(
                    SHARE_PAGE, mode="disabled", error=None, path=None
                ),
                410,
            )
        root = Path(cfg["share_dir"]).resolve()
        target = safe_join(root, rel)
        if target is None or not target.exists():
            return (
                render_template_string(SHARE_PAGE, mode="missing", error=None, path=None),
                404,
            )
        if visitor_protection_on():
            pwd = request.args.get("pwd", "")
            if pwd:
                # URL 显式携带密码：每次都严格校验（即使已有登录会话）
                r = check_role(pwd)
                if not r:
                    return (
                        render_template_string(
                            SHARE_PAGE,
                            mode="password",
                            error="密码错误，请重试",
                            path=request.path,
                        ),
                        401,
                    )
                session["role"] = r
            elif session.get("role") not in ("admin", "visitor"):
                return (
                    render_template_string(
                        SHARE_PAGE, mode="password", error=None, path=request.path
                    ),
                    401,
                )
        if target.is_dir():
            return redirect(url_for("index", rel=rel))
        return send_file(target, as_attachment=True)

    @app.route("/download/<path:rel>")
    def download_file(rel):
        root = Path(cfg["share_dir"]).resolve()
        target = safe_join(root, rel)
        if target is None or not target.is_file():
            abort(404)
        return send_file(target, as_attachment=True)

    @app.errorhandler(404)
    def not_found(e):
        return render_template_string(ERROR_PAGE, code=404, message="文件或目录不存在"), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template_string(ERROR_PAGE, code=403, message="没有权限访问"), 403

    return app


def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def time_str(ts):
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def html_escape(s):
    import html

    return html.escape(s)


# ---------------------------------------------------------------- 网络


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


# ---------------------------------------------------------------- 托盘

# 拦截控制台窗口「关闭」按钮的全局引用（防止被垃圾回收导致崩溃）
_wndproc_ref = None
_original_wndproc = None


def hook_console_close():
    """拦截控制台窗口的 WM_CLOSE（点 X）：改为隐藏窗口，退出只能走托盘菜单。"""
    global _wndproc_ref, _original_wndproc
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        GWL_WNDPROC = -4
        WM_CLOSE = 0x0010

        WNDPROC = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        _original_wndproc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

        def _wndproc(h, msg, wparam, lparam):
            if msg == WM_CLOSE:
                user32.ShowWindow(h, 0)  # SW_HIDE
                return 0
            return user32.CallWindowProcW(_original_wndproc, h, msg, wparam, lparam)

        _wndproc_ref = WNDPROC(_wndproc)
        user32.SetWindowLongPtrW(
            hwnd, GWL_WNDPROC, ctypes.cast(_wndproc_ref, ctypes.c_void_p)
        )
    except Exception:
        pass


def run_tray(cfg, port):
    """系统托盘图标：右键菜单「打开 / 查看 / 退出」。无法使用时静默跳过。"""
    try:
        import ctypes

        import pystray
        from PIL import Image
    except ImportError:
        print("  提示：未安装 pystray，托盘功能不可用（pip install pystray）")
        return

    url = f"http://{lan_ip()}:{port}/"

    def open_page(icon, item):
        webbrowser.open(url)

    def show_console(icon, item):
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    try:
        image = Image.open(ICON_FILE)
    except Exception:
        image = Image.new("RGB", (64, 64), (102, 126, 234))

    menu = pystray.Menu(
        pystray.MenuItem("打开", open_page, default=True),
        pystray.MenuItem("查看", show_console),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    tip = f"文件共享 {url}"
    pystray.Icon("file-share-server", image, tip, menu).run()


# ---------------------------------------------------------------- 入口


def main():
    parser = argparse.ArgumentParser(description="局域网文件共享工具")
    parser.add_argument("--dir", help="要共享的文件夹路径")
    parser.add_argument("--port", type=int, help="服务端口（默认 8000）")
    parser.add_argument("--password", help="设置访问密码并开启访问密码保护")
    parser.add_argument("--admin-password", help="设置管理员密码")
    parser.add_argument("--no-password", action="store_true", help="关闭访问密码保护")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-minimize", action="store_true", help="启动后不自动最小化控制台窗口")
    parser.add_argument("--no-tray", action="store_true", help="不启用系统托盘图标")
    args = parser.parse_args()

    migrate_old_config()
    cfg = load_config()
    if first_run_setup(cfg):
        print(f"首次使用：已在桌面创建分享文件夹：{cfg['share_dir']}")
    changed = False

    if args.dir:
        d = Path(args.dir).expanduser().resolve()
        if not d.is_dir():
            print(f"错误：文件夹不存在：{d}", file=sys.stderr)
            sys.exit(1)
        cfg["share_dir"] = str(d)
        changed = True
    if args.port:
        if not (1 <= args.port <= 65535):
            print("错误：端口必须在 1-65535 之间", file=sys.stderr)
            sys.exit(1)
        cfg["port"] = args.port
        changed = True
    if args.password:
        cfg["visitor_enabled"] = True
        cfg["visitor_password"] = args.password
        changed = True
    if args.admin_password:
        cfg["admin_password"] = args.admin_password
        changed = True
    if args.no_password:
        cfg["visitor_enabled"] = False
        cfg["visitor_password"] = ""
        changed = True

    if changed:
        save_config(cfg)
        print(f"配置已保存到 {CONFIG_PATH}")

    share_dir = Path(cfg["share_dir"])
    if not share_dir.is_dir():
        print(f"错误：共享文件夹不存在：{share_dir}", file=sys.stderr)
        sys.exit(1)

    port = cfg["port"]
    print("=" * 50)
    print(f"  版本：v{VERSION}")
    print(f"  共享目录：{share_dir}")
    print(f"  本机访问：http://127.0.0.1:{port}")
    ip = lan_ip()
    if ip != "127.0.0.1":
        print(f"  局域网访问：http://{ip}:{port}")
    print(f"  访问密码：{'已开启' if cfg['visitor_enabled'] else '未开启'}")
    print(f"  管理员密码：{'已设置' if cfg['admin_password'] else '未设置'}")
    print(f"  分享功能：{'已开启' if cfg['share_enabled'] else '已关闭'}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 50)

    # 启用托盘时：拦截点 X 并直接隐藏控制台窗口；否则按原逻辑仅 exe 最小化
    tray_on = not args.no_tray
    if tray_on:
        hook_console_close()
        try:
            import ctypes

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass
    elif not args.no_minimize and getattr(sys, "frozen", False):
        try:
            import ctypes

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        except Exception:
            pass

    # 启动后自动用默认浏览器打开局域网访问地址（可 --no-browser 关闭）
    if not args.no_browser:
        open_ip = lan_ip()
        url = f"http://{open_ip}:{port}/"
        def _open_browser():
            time.sleep(1.2)  # 等待 waitress 绑定端口
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open_browser, daemon=True).start()
        print(f"  已在默认浏览器打开：{url}")

    app = create_app(cfg)
    # 使用 waitress 生产级 WSGI 服务器（避免 Flask 开发服务器的警告）
    from waitress import serve

    if not args.no_tray:
        # 托盘消息循环跑在主线程，服务器跑在守护线程
        threading.Thread(
            target=serve, args=(app,), kwargs={"host": "0.0.0.0", "port": port, "threads": 16}, daemon=True
        ).start()
        run_tray(cfg, port)
    else:
        serve(app, host="0.0.0.0", port=port, threads=16)


if __name__ == "__main__":
    main()
