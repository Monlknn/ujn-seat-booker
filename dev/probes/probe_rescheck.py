"""单独验证 _reservation_exists 是否能命中已有预约，并确认未命中时回到首页。"""
from src.config import load_config
from src.session import login_browser
from src import booker


def main():
    cfg = load_config()
    sess = login_browser(cfg)
    try:
        page = sess.page
        found = booker._reservation_exists(page, "2026-08-21", "第一阅览室", "08:00", "12:00")
        print("命中已有预约?", found)
        print("当前 URL:", page.url)
    finally:
        sess.close()


if __name__ == "__main__":
    main()
