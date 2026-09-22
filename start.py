"""Start the local service and open its page in the default browser."""

from threading import Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


HOST = "127.0.0.1"
PORT = 7860
URL = f"http://{HOST}:{PORT}/"


def service_ready():
    try:
        with urlopen(URL, timeout=1) as response:
            return response.status == 200 and b'id="transcribe-form"' in response.read(32768)
    except (URLError, OSError):
        return False


def open_page():
    try:
        if webbrowser.open(URL, new=2):
            return
    except (webbrowser.Error, OSError):
        pass
    print(f"请在浏览器中手动打开：{URL}", flush=True)


def open_when_ready():
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if service_ready():
            open_page()
            return
        sleep(0.25)
    print(f"服务尚未就绪，请检查终端提示。页面地址：{URL}", flush=True)


def main():
    if service_ready():
        print(f"服务已经运行，正在打开 {URL}", flush=True)
        open_page()
        return 0

    try:
        from app import app
    except ImportError as error:
        print(f"无法加载程序：{error}\n请先按 README 安装 requirements.txt 中的依赖。", flush=True)
        return 1

    print(f"正在启动：{URL}\n使用期间请保留此窗口；按 Ctrl+C 或关闭窗口可停止服务。", flush=True)
    Thread(target=open_when_ready, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
