"""只读探测：验证登录、阅览室列表、座位布局，不提交任何预约。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import api as rest

cfg = __import__("src.config", fromlist=["load_config"]).Config.load(
    Path(__file__).resolve().parents[2] / "config.json"
)

print("== 1) 登录 ==")
token = rest.get_token(cfg.username, cfg.password)
print("token:", token)

if not token:
    sys.exit("登录失败，停止探测。")

print("\n== 2) 阅览室列表 ==")
rooms = rest.get_rooms(token)
print(f"共 {len(rooms)} 个阅览室")
for r in rooms[:8]:
    print(f"  roomId={r['roomId']:>3} floor={r.get('floor')} "
          f"name={r.get('room')} 余位={r.get('free')}/{r.get('totalSeats')}")

print("\n== 3) 查目标阅览室座位布局（仅查询，不预约）==")
room_id = rest.get_room_id(token, cfg.get("room"))
date = rest._resolve_date(cfg.get("date"))
print("room_id:", room_id, "date:", date)
if room_id:
    resp = rest._http_get(f"/rest/v2/room/layoutByDate/{room_id}/{date}",
                          {"token": token})
    if resp.get("status") == "success":
        layout = resp["data"]["layout"]
        seats = [v for v in layout.values() if v.get("type") == "seat"]
        print(f"  共 {len(seats)} 个座位，样例：")
        for v in seats[:12]:
            print(f"    seatId={v.get('id')} 编号={v.get('name')} 状态={v.get('status')}")
    else:
        print("  布局查询失败:", resp.get("message"))
