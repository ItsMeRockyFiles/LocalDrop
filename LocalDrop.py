import io
import re
import sys
import socket
import secrets
import pathlib
import logging
import urllib.parse
import http.server
import socketserver
import html

try:
    import qrcode
    import qrcode.image.svg
    HAVE_QR = True
except ImportError:
    HAVE_QR = False

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False

TOKEN = secrets.token_urlsafe(8)

if len(sys.argv) > 1:
    arg = sys.argv[1].strip()
    if arg == "":
        print("Empty path given.")
        sys.exit(1)
    FILE = pathlib.Path(arg).resolve()
else:
    logging.warning("No path given.")
    sys.exit(1)

if not FILE.is_file():
    sys.exit(f"Not a file: {FILE}")

_SKIP_ADAPTERS = (
    "vmware", "vmnet", "virtualbox", "vbox",
    "docker", "wsl", "hyper-v", "hyperv", "vethernet",
    "loopback", "bluetooth", "tailscale", "zerotier",
    "tap", "tun", "wireguard", "openvpn",
)


def _is_private_ipv4(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    if ip.startswith("127.") or ip.startswith("169.254."):
        return False
    return a.is_private


def get_ip():
    if HAVE_PSUTIL:
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()

            fallback = None
            for name, snaplist in addrs.items():
                if name in stats and not stats[name].isup:
                    continue
                low = name.lower()
                is_virtual = any(x in low for x in _SKIP_ADAPTERS)

                for sn in snaplist:
                    if sn.family != socket.AF_INET:
                        continue
                    ip = sn.address
                    if not _is_private_ipv4(ip):
                        continue
                    if is_virtual:
                        if fallback is None:
                            fallback = ip
                        continue
                    return ip

            if fallback:
                return fallback
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect("8.8.8.8", 80)
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


HOST_IP = get_ip()


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def build_qr_svg(data: str) -> str:
    """Return an inline <svg> string for the given data. No extra deps."""
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(data, image_factory=factory, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # Strip fixed width/height so CSS can size it responsively.
    svg = re.sub(r'(<svg[^>]*?)\swidth="[^"]*"', r'\1', svg, count=1)
    svg = re.sub(r'(<svg[^>]*?)\sheight="[^"]*"', r'\1', svg, count=1)
    return svg


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocalDrop/1.0"

    def _authed_rest(self):
        parts = urllib.parse.urlparse(self.path).path.lstrip("/").split("/", 1)
        if not parts or parts[0] != TOKEN:
            self.send_error(404)
            return None
        return parts[1] if len(parts) > 1 else ""

    def _send_bytes(self, ctype, data, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, as_attachment):
        try:
            size = FILE.stat().st_size
            f = open(FILE, "rb")
        except OSError:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        disp = "attachment" if as_attachment else "inline"
        self.send_header("Content-Disposition",
                         f'{disp}; filename="{FILE.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def do_GET(self):
        rest = self._authed_rest()
        if rest is None:
            return
        if rest in ("download", "dl", "file"):
            return self._send_file(as_attachment=True)
        if rest in ("", "/"):
            return self._send_landing()
        self.send_error(404)

    def do_HEAD(self):
        rest = self._authed_rest()
        if rest is None:
            return
        if rest in ("download", "dl", "file"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(FILE.stat().st_size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{FILE.name}"')
            self.end_headers()
            return
        self.send_error(404)

    def _send_landing(self):
        port = self.server.server_address[1]
        base = f"http://{HOST_IP}:{port}/{TOKEN}"
        name = html.escape(FILE.name)
        size = human_size(FILE.stat().st_size)
        dl_url = f"{base}/download"

        if HAVE_QR:
            qr_svg = build_qr_svg(dl_url)
            qr_block = f'<div class="qr">{qr_svg}</div>'
        else:
            qr_block = (
                '<p class="warn">Install <code>qrcode</code> to show a QR code: '
                '<code>python -m pip install qrcode</code></p>'
            )

        page = f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LocalDrop</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0d0e10;
    --card: #141619;
    --line: #24272c;
    --text: #e9eaec;
    --muted: #82868d;
    --accent: #8ed6a8;
  }}

  * {{ box-sizing: border-box; }}

  body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}

  .card {{
    width: 100%;
    max-width: 380px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 28px;
  }}

  .top {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 16px;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--line);
  }}

  .brand {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: .06em;
    text-transform: uppercase;
  }}

  .brand span {{ color: var(--muted); font-weight: 500; }}

  .scope {{
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }}

  h1 {{
    margin: 0 0 4px;
    font-size: 17px;
    font-weight: 600;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }}

  .meta {{
    margin: 0 0 20px;
    font-size: 13px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}

  .btn {{
    display: block;
    width: 100%;
    padding: 12px 16px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: #0b0d0e;
    font: inherit;
    font-weight: 600;
    text-align: center;
    text-decoration: none;
    cursor: pointer;
    transition: background .15s ease;
  }}

  .btn:hover {{ background: #a3e2ba; }}
  .btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  .qr {{
    width: 172px;
    height: 172px;
    margin: 24px auto 10px;
    padding: 8px;
    background: #fff;
    border-radius: 8px;
  }}

  .qr svg {{ display: block; width: 100%; height: 100%; }}

  .hint {{
    margin: 0;
    text-align: center;
    font-size: 12.5px;
    color: var(--muted);
  }}

  .foot {{
    margin: 22px 0 0;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-size: 12px;
    color: #62666d;
    text-align: center;
  }}

  code {{
    padding: 1px 5px;
    border-radius: 4px;
    background: #1c1f24;
    font-size: .92em;
  }}

  .warn {{ color: #d9a05b; }}
</style>
</head>
<body>
  <main class="card">
    <div class="top">
      <div class="brand">Local<span>Drop</span></div>
    </div>

    <h1>{name}</h1>
    <p class="meta">{size}</p>

    <a class="btn" href="{dl_url}" download>Download</a>

    {qr_block}
    <p class="hint">Scan with your phone camera</p>
  </main>
</body>
</html>"""
        self._send_bytes("text/html; charset=utf-8", page.encode("utf-8"))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    for port in range(8000, 8080):
        try:
            http = Server(("", port), Handler)
            break
        except OSError:
            continue
    else:
        print("no free ports available right now.")
        return

    landing = f"http://{HOST_IP}:{port}/{TOKEN}/"
    direct = f"http://{HOST_IP}:{port}/{TOKEN}/download"

    #print("LocalDrop")
    #print(f"  ip:       {HOST_IP}  (psutil: {'yes' if HAVE_PSUTIL else 'no, fallback'})")
    print(f"  page:     {landing}")
    #print(f"  download: {direct}")
    #print(f"  file:     {FILE} ({human_size(FILE.stat().st_size)})")

    if HAVE_QR:
        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(direct)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception:
            pass
    else:
        print("  (pip install qrcode for a terminal QR)")

    http.serve_forever()


if __name__ == "__main__":
    main()