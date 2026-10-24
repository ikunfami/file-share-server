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
    render_template,
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

# 当前版本号（发布新版本时同步修改，并打同名 git tag）
VERSION = "1.2.0"

# 支持点击文件名直接预览的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico"}

DEFAULT_CONFIG = {
    "share_dir": str(BASE_DIR),
    "port": 8000,
    "visitor_enabled": False,
    "visitor_password": "",
    "admin_password": "",
    "share_enabled": True,
    "zip_enabled": True,
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
        f"zip_enabled = {'true' if cfg['zip_enabled'] else 'false'}",
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






# ---------------------------------------------------------------- 应用


def create_app(cfg):
    # 静态资源与模板：脚本模式读项目目录，exe 模式读 PyInstaller 打包的临时目录
    resource_dir = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    app = Flask(
        __name__,
        static_folder=str(resource_dir / "assets"),
        static_url_path="/assets",
        template_folder=str(resource_dir / "templates"),
    )
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

    @app.after_request
    def no_cache_html(resp):
        """HTML 页面不缓存，避免服务端更新后浏览器展示旧页面（静态资源仍缓存）。"""
        if resp.content_type.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

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
                session["notice"] = "管理员登录成功" if role == "admin" else "用户登录成功"
                target = request.args.get("next") or url_for("index")
                if not target.startswith("/"):
                    target = url_for("index")
                return redirect(target)
            error = "密码错误，请重试"
        return render_template(
            "login.html",
            error=error,
            visitor_off=not visitor_protection_on(),
            version=VERSION,
        )

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
        toasts = []
        if request.method == "POST":
            form = request.form
            old_dir = cfg["share_dir"]
            old_ve = cfg["visitor_enabled"]
            old_se = cfg["share_enabled"]
            old_ze = cfg["zip_enabled"]
            d = form.get("share_dir", "").strip().strip('"')
            if d:
                p = Path(d).expanduser().resolve()
                if not p.is_dir():
                    toasts.append((f"分享目录不存在：{p}", "error"))
                else:
                    cfg["share_dir"] = str(p)
            if not toasts:
                vp = form.get("visitor_password", "")
                ve = form.get("visitor_enabled") == "on"
                if ve and not vp and not cfg["visitor_password"]:
                    toasts.append(("启用访问密码前，请先设置一个访问密码", "error"))
                else:
                    cfg["visitor_enabled"] = ve
                    if vp:
                        cfg["visitor_password"] = vp
                    ap = form.get("admin_password", "")
                    if ap:
                        cfg["admin_password"] = ap
                    cfg["share_enabled"] = form.get("share_enabled") == "on"
                    cfg["zip_enabled"] = form.get("zip_enabled") == "on"
                    save_config(cfg)
                    # 密码变更后轮换会话密钥，使其他已登录会话（含分享链接带入的）立即失效；
                    # 同时刷新当前管理员的会话，避免其本人被登出
                    if vp or ap:
                        app.secret_key = secrets.token_hex(32)
                        session["role"] = "admin"
                    # 按变更项生成对应的通知弹窗
                    if cfg["share_dir"] != old_dir:
                        toasts.append(("分享目录已更新", "success"))
                    if cfg["visitor_enabled"] != old_ve:
                        toasts.append(
                            (
                                "访问密码已" + ("启用" if cfg["visitor_enabled"] else "关闭"),
                                "success",
                            )
                        )
                    if vp:
                        toasts.append(("访问密码已更新", "success"))
                    if cfg["share_enabled"] != old_se:
                        toasts.append(
                            (
                                "分享功能已" + ("开启" if cfg["share_enabled"] else "关闭"),
                                "success",
                            )
                        )
                    if cfg["zip_enabled"] != old_ze:
                        toasts.append(
                            (
                                "打包下载已" + ("开启" if cfg["zip_enabled"] else "关闭"),
                                "success",
                            )
                        )
                    if ap:
                        toasts.append(("管理员密码已更新", "success"))
                    if not toasts:
                        toasts.append(("设置已保存", "success"))
        return render_template(
            "settings.html",
            toasts=toasts,
            share_dir=cfg["share_dir"],
            visitor_enabled=cfg["visitor_enabled"],
            share_enabled=cfg["share_enabled"],
            zip_enabled=cfg["zip_enabled"],
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
                    "rel": sub_rel,
                    "is_dir": p.is_dir(),
                    "is_image": p.is_file() and p.suffix.lower() in IMAGE_EXTS,
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

        return render_template(
            "list.html",
            items=items,
            rel=rel,
            breadcrumb="".join(crumbs),
            refresh_url=request.path,
            notice=session.pop("notice", None),
            is_admin=session.get("role") == "admin",
            password_on=visitor_protection_on() or session.get("role") is not None,
            share_on=cfg["share_enabled"],
            zip_on=cfg["zip_enabled"],
            version=VERSION,
        )

    @app.route("/s/<path:rel>")
    def share(rel):
        """分享链接：校验开关、路径与密码，通过后文件直接下载、文件夹跳转到浏览页。"""
        if not cfg["share_enabled"]:
            return (
                render_template(
                    "share.html", mode="disabled", error=None, path=None
                ),
                410,
            )
        root = Path(cfg["share_dir"]).resolve()
        target = safe_join(root, rel)
        if target is None or not target.exists():
            return (
                render_template("share.html", mode="missing", error=None, path=None),
                404,
            )
        if visitor_protection_on():
            pwd = request.args.get("pwd", "")
            if pwd:
                # URL 显式携带密码：每次都严格校验（即使已有登录会话）
                r = check_role(pwd)
                if not r:
                    return (
                        render_template(
                            "share.html",
                            mode="password",
                            error="密码错误，请重试",
                            path=request.path,
                        ),
                        401,
                    )
                session["role"] = r
            elif session.get("role") not in ("admin", "visitor"):
                return (
                    render_template(
                        "share.html", mode="password", error=None, path=request.path
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
        if request.args.get("inline") == "1":
            return send_file(target)
        return send_file(target, as_attachment=True)

    @app.route("/upload", methods=["POST"])
    def upload():
        """管理员上传文件到分享目录（可指定当前子目录），文件名冲突时自动加序号。"""
        if session.get("role") != "admin":
            abort(403)
        root = Path(cfg["share_dir"]).resolve()
        rel = request.form.get("rel", "")
        folder = safe_join(root, rel) if rel else root
        if folder is None or not folder.is_dir():
            abort(404)
        saved = []
        for f in request.files.getlist("files"):
            # 只取文件名部分，防止路径穿越
            name = Path(f.filename or "").name
            if not name or name in (".", ".."):
                continue
            target = unique_path(folder, name)
            f.save(target)
            saved.append(target.name)
        if not saved:
            return {"ok": False, "error": "没有可保存的文件"}, 400
        return {"ok": True, "saved": saved}

    @app.route("/mkdir", methods=["POST"])
    def mkdir():
        """管理员在当前浏览的目录下新建文件夹。"""
        if session.get("role") != "admin":
            abort(403)
        root = Path(cfg["share_dir"]).resolve()
        rel = request.form.get("rel", "")
        folder = safe_join(root, rel) if rel else root
        name = request.form.get("name", "").strip()
        if folder is None or not folder.is_dir():
            abort(404)
        if not valid_name(name):
            return {"ok": False, "error": "名称不合法（不能包含 \\ / : * ? \" < > | 字符）"}, 400
        try:
            (folder / name).mkdir()
        except FileExistsError:
            return {"ok": False, "error": "已存在同名文件或文件夹"}, 400
        except OSError as e:
            return {"ok": False, "error": f"创建失败：{e}"}, 500
        return {"ok": True, "name": name}

    @app.route("/qr")
    def qr_image():
        """把传入的文本（分享链接）生成二维码图片（SVG），供手机扫码。"""
        data = request.args.get("d", "")
        if not data or len(data) > 500:
            abort(400)
        try:
            import io

            import qrcode
            import qrcode.image.svg
        except ImportError:
            abort(503)
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return app.response_class(buf.getvalue(), mimetype="image/svg+xml")

    @app.route("/download-zip", methods=["POST"])
    def download_zip():
        """把勾选的文件/文件夹打包成 zip 下载，文件夹递归包含。"""
        import io
        import zipfile

        if not cfg["zip_enabled"]:
            abort(403)
        root = Path(cfg["share_dir"]).resolve()
        rels = request.form.getlist("path")
        if not rels:
            abort(400)
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in rels:
                target = safe_join(root, rel)
                if target is None or not target.exists():
                    continue
                if target.is_dir():
                    for f in sorted(target.rglob("*")):
                        if f.is_file():
                            zf.write(f, str(Path(target.name) / f.relative_to(target)))
                            count += 1
                else:
                    zf.write(target, target.name)
                    count += 1
        if count == 0:
            abort(404)
        buf.seek(0)
        name = "打包下载-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
        return send_file(buf, as_attachment=True, download_name=name)

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="文件或目录不存在"), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="没有权限访问"), 403

    return app


def unique_path(folder, name):
    """同一目录下已有同名文件时，自动追加 (1)(2)… 序号，避免覆盖。"""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        candidate = folder / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{secrets.token_hex(4)}{suffix}"


def valid_name(name):
    """校验新建/重命名的名称：非空、无路径分隔符和 Windows 非法字符。"""
    if not name or len(name) > 255:
        return False
    if name in (".", ".."):
        return False
    return not any(c in name for c in '\\/:*?"<>|')


def print_qr(url):
    """在控制台打印访问二维码，手机扫码即可打开（缺 qrcode 库时静默跳过）。

    用 ▀/▄ 半块字符 + ANSI 颜色渲染：两个模块合并为一行，宽高接近 1:1，
    且强制白底黑码（标准二维码配色），不受终端深浅主题影响。
    """
    try:
        import qrcode
    except ImportError:
        return
    if not sys.stdout.isatty():
        # 输出被重定向（日志文件等）时切换为 UTF-8，避免 ▀/▄ 字符按 GBK 编码失败
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    if sys.platform == "win32":
        # 启用控制台 ANSI 颜色支持（Windows 10+）
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    # ANSI 颜色：前景黑 / 背景白
    FG_DARK, BG_LIGHT, BG_DARK, RESET = "\033[30m", "\033[47m", "\033[40m", "\033[0m"
    print("  手机扫码访问：")
    try:
        for y in range(0, len(m), 2):
            parts = []
            for x in range(len(m[0])):
                top = m[y][x]
                bottom = m[y + 1][x] if y + 1 < len(m) else False
                if top and bottom:
                    parts.append(FG_DARK + BG_DARK + "▀")
                elif top:
                    parts.append(FG_DARK + BG_LIGHT + "▀")
                elif bottom:
                    parts.append(FG_DARK + BG_LIGHT + "▄")
                else:
                    parts.append(BG_LIGHT + " ")
            print("  " + "".join(parts) + RESET)
    except UnicodeEncodeError:
        # 极端编码环境下放弃二维码，不影响服务启动
        pass


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


# ---------------------------------------------------------------- 入口


def main():
    parser = argparse.ArgumentParser(description="局域网文件共享工具")
    parser.add_argument("--dir", help="要共享的文件夹路径")
    parser.add_argument("--port", type=int, help="服务端口（默认 8000）")
    parser.add_argument("--password", help="设置访问密码并开启访问密码保护")
    parser.add_argument("--admin-password", help="设置管理员密码")
    parser.add_argument("--no-password", action="store_true", help="关闭访问密码保护")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
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
        print_qr(f"http://{ip}:{port}/")
    print(f"  访问密码：{'已开启' if cfg['visitor_enabled'] else '未开启'}")
    print(f"  管理员密码：{'已设置' if cfg['admin_password'] else '未设置'}")
    print(f"  分享功能：{'已开启' if cfg['share_enabled'] else '已关闭'}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 50)

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

    serve(app, host="0.0.0.0", port=port, threads=16)


if __name__ == "__main__":
    main()
