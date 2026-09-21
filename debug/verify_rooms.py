"""验证 refresh_rooms 等卡片稳定后能读到所有 7层 阅览室。"""
import sys, time
sys.path.insert(0, '.')
from src.config import load_config
from src.session import login_browser
from src.gui import App

# 只 import App 类即可（不需要实例化 Tk，只要它的静态方法）
App._wait_room_list_stable  # 确保方法存在

cfg = load_config('config.json')
# 模拟用户选了 7层
cfg._data['floor'] = '7层'
cfg._data['campus'] = '主校区'

sess = login_browser(cfg)
page = sess.page

print('=== 直接调用 App._wait_room_list_stable + _collect_rooms_across_pages ===')
App._wait_room_list_stable(page, timeout_ms=6000)
rooms = App._collect_rooms_across_pages(page)
print(f'  共 {len(rooms)} 项: {rooms}')

# 模拟 GUI 过滤（floor=7层 → kw=七）
FLOOR_NAME_MAP = {"1层":"一","2层":"二","3层":"三","4层":"四","5层":"五","6层":"六","7层":"七"}
filtered = [r for r in rooms if "七" in r]
print(f'  FLOOR_NAME_MAP["7层"]="七" 过滤后: {len(filtered)} 项: {filtered}')

sess.close()