#!/usr/bin/env python3
import json
import os
import re
import ssl
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from collections import defaultdict

# 强制 UTF-8 编码
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 生产环境路径
BASE_DIR = "/opt/ddnet" if os.path.exists("/opt/ddnet") else os.path.dirname(os.path.abspath(__file__))
OUTPUT_MAPS_DIR = os.path.join(BASE_DIR, "maps")
VOTES_DIR = os.path.join(BASE_DIR, "votes")
ROOT_VOTES_CFG = os.path.join(BASE_DIR, "votes.cfg")
FIFO_PATH = os.path.join(BASE_DIR, "server.fifo")
MASTER_URL = "https://master1.ddnet.org/ddnet/15/servers.json"

SCAN_DIRS = [
    OUTPUT_MAPS_DIR,
    "/opt/KOG-MAP/maps",
    os.path.expanduser("~/.local/share/ddnet/downloadedmaps")
]

CDN_TEMPLATES = [
    "https://maps.kog.tw/teeworlds/maps/{name}_{sha}.map",
    "https://maps.ddnet.org/compilations/maps/{name}_{sha}.map",
    "https://maps2.ddnet.org/compilations/maps/{name}_{sha}.map",
    "https://maps.ddnet.org/compilations/maps/{name}.map"
]

CATEGORY_DISPLAY_NAMES = {
    "Easy": "Eᴀsʏ",
    "Main": "Mᴀɪɴ",
    "Hard": "Hᴀʀᴅ",
    "Solo": "Sᴏʟᴏ",
    "Insane": "Iɴsᴀɴᴇ",
    "Extreme": "Exᴛʀᴇᴍᴇ",
    "Mods": "Mᴏᴅs",
    "Unknown": "Uɴᴋɴᴏᴡɴ"
}

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# 记录当前通知绑定的日期
last_checked_date = ""

def is_valid_teeworlds_binary(data_bytes):
    """严格校验 Teeworlds/DDNet 原生二进制魔数 (DATA 或 ATAD)"""
    if not data_bytes or len(data_bytes) < 1024:
        return False
    magic = data_bytes[:4]
    if magic in [b"DATA", b"ATAD"]:
        return True
    header_preview = data_bytes[:64].lower()
    if b"<html" in header_preview or b"<!doc" in header_preview or b"404" in header_preview:
        return False
    return False

def clean_existing_corrupted_maps():
    """扫描 maps 目录，清除伪装成 .map 的 404 网页假文件"""
    if not os.path.exists(OUTPUT_MAPS_DIR):
        return
    corrupted_files = []
    for root, dirs, files in os.walk(OUTPUT_MAPS_DIR):
        for f in files:
            if f.endswith(".map"):
                fp = os.path.join(root, f)
                try:
                    with open(fp, "rb") as mf:
                        header = mf.read(16)
                    if header[:4] not in [b"DATA", b"ATAD"]:
                        corrupted_files.append(fp)
                except Exception:
                    corrupted_files.append(fp)

    if corrupted_files:
        print("=" * 70)
        print(f" [体检清理] 发现 {len(corrupted_files)} 个假文件：")
        for c in corrupted_files:
            print(f"  --> 删除假文件: {os.path.basename(c)}")
            try:
                os.remove(c)
            except Exception:
                pass
        print(" 所有假文件已清理完毕！\n" + "=" * 70)

def is_official_formal_kog_server(server_name):
    """【白名单过滤】屏蔽一切 TEST / BETA 沙盒房间"""
    s_upper = server_name.upper()
    for blackword in ["TEST", "BETA", "DEV", "EVALUATE", "SUBMISSION"]:
        if blackword in s_upper:
            return False

    if "KOG" not in s_upper and "KOG.TW" not in s_upper:
        return False

    formal_categories = ["MAIN", "HARD", "EASY", "SOLO", "INSANE", "EXTREME", "MODS"]
    return any(cat in s_upper for cat in formal_categories)

def extract_category_from_server(server_name):
    m = re.search(r'-\s*(Easy|Main|Hard|Solo|Insane|Extreme|Mods)\b', server_name, re.IGNORECASE)
    if m:
        return m.group(1).capitalize()
    for kw in ["Easy", "Main", "Hard", "Solo", "Insane", "Extreme", "Mods"]:
        if kw.lower() in server_name.lower():
            return kw
    return "Main"

def get_existing_maps():
    existing = set()
    for d in SCAN_DIRS:
        if os.path.exists(d):
            for root, dirs, files in os.walk(d):
                for f in files:
                    if not f.endswith(".map"):
                        continue
                    fp = os.path.join(root, f)
                    try:
                        with open(fp, "rb") as mf:
                            head = mf.read(4)
                        if head not in [b"DATA", b"ATAD"]:
                            continue
                    except Exception:
                        continue

                    raw = f.replace(".map", "").strip()
                    if "｜" in raw or "|" in raw:
                        pure = re.split(r"\s*[|｜]\s*", raw)[0].strip().lower()
                    elif "_发布时间" in raw:
                        pure = re.sub(r"_[^_]+ \d+星_发布时间.*$", "", raw).strip().lower()
                    else:
                        pure = raw.lower()
                    existing.add(pure)
    return existing

def download_with_curl_strict(target_url):
    temp_file = "/tmp/temp_curl_test.tmp"
    try:
        cmd = [
            "curl", "-s", "-f", "-L", "--connect-timeout", "10",
            "-A", "Mozilla/5.0 DDNet/18.8",
            "-o", temp_file, target_url
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if res.returncode == 0 and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1024:
            with open(temp_file, "rb") as tf:
                data = tf.read()
            os.remove(temp_file)
            if is_valid_teeworlds_binary(data):
                return data
    except Exception:
        pass
    finally:
        if os.path.exists(temp_file):
            try: os.remove(temp_file)
            except Exception: pass
    return None

def download_map_strictly(map_name, map_sha):
    encoded_name = urllib.parse.quote(map_name)
    headers = {
        "User-Agent": "Mozilla/5.0 DDNet/18.8",
        "Connection": "close",
        "Accept-Encoding": "identity"
    }

    for tpl in CDN_TEMPLATES:
        url = tpl.format(name=encoded_name, sha=map_sha)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                    data = resp.read()
                    if is_valid_teeworlds_binary(data):
                        return data, url.split("/")[2]
            except Exception:
                time.sleep(0.5)

        curl_data = download_with_curl_strict(url)
        if curl_data:
            return curl_data, f"curl({url.split('/')[2]})"

    return None, "None"

def refresh_all_votes_system():
    """扫描并更新 votes 目录下各分类的导航栏数量统计"""
    if not os.path.exists(VOTES_DIR):
        os.makedirs(VOTES_DIR, exist_ok=True)
        return

    category_maps = defaultdict(list)

    for fname in os.listdir(VOTES_DIR):
        if fname.endswith(".cfg") and fname != "all.cfg":
            raw_cat = fname.replace(".cfg", "").capitalize()
            cat_name = (
                "Extreme" if "Ext" in raw_cat else
                "Insane" if "Insane" in raw_cat else
                "Hard" if "Hard" in raw_cat else
                "Easy" if "Easy" in raw_cat else
                "Solo" if "Solo" in raw_cat else
                "Main" if "Main" in raw_cat else
                "Mods" if "Mod" in raw_cat else "Unknown"
            )

            cfg_path = os.path.join(VOTES_DIR, fname)
            try:
                with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        l = line.strip()
                        if l.startswith("add_vote") and "change_map " in l:
                            category_maps[cat_name].append(l)
            except Exception:
                pass

    real_counts = {}
    for cat, maps in category_maps.items():
        seen = set()
        unique_maps = []
        for m in maps:
            m_target = m.split("change_map ")[-1].replace('"', '').strip().lower()
            if m_target not in seen:
                seen.add(m_target)
                unique_maps.append(m)

        unique_maps.sort(key=lambda x: x.split("change_map ")[-1].replace('"', '').lower())
        category_maps[cat] = unique_maps
        real_counts[cat] = len(unique_maps)

    priority_order = ["Main", "Easy", "Hard", "Solo", "Insane", "Extreme", "Mods", "Unknown"]
    active_categories = [c for c in priority_order if c in real_counts]
    for c in real_counts:
        if c not in active_categories:
            active_categories.append(c)

    for current_cat in active_categories:
        cfg_file = os.path.join(VOTES_DIR, f"{current_cat.lower()}.cfg")
        real_total = real_counts[current_cat]

        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                f.write("# ==================================================\n")
                f.write(f"# KoG 投票系统 - 【{current_cat}】专区 (共 {real_total} 张)\n")
                f.write("# 说明: 仅展示本分类地图，无任何冗余大列表\n")
                f.write("# ==================================================\n\n")

                f.write("# -------------- 【分类无缝切换】 --------------\n")
                for cat in active_categories:
                    styled = CATEGORY_DISPLAY_NAMES.get(cat, cat)
                    count = real_counts[cat]
                    if cat == current_cat:
                        f.write(f'add_vote "☒ {styled} Mᴀᴘs ({count})" "info"\n')
                    else:
                        f.write(f'add_vote "☐ {styled} Mᴀᴘs ({count})" "clear_votes; exec votes/{cat.lower()}.cfg"\n')

                f.write('add_vote " " "info"\n')
                f.write(f'add_vote "🎲 随机一张 {current_cat} 地图" "random_map"\n')
                f.write('add_vote " " "info"\n\n')

                f.write(f"# -------------- 【{current_cat} 地图列表】 --------------\n")
                for m_line in category_maps[current_cat]:
                    f.write(f"{m_line}\n")
            os.chmod(cfg_file, 0o644)
        except Exception as e:
            print(f"刷新 {cfg_file} 失败: {e}")

    default_cat = "main" if "Main" in active_categories else active_categories[0].lower()
    try:
        with open(ROOT_VOTES_CFG, "w", encoding="utf-8") as rf:
            rf.write(f"exec votes/{default_cat}.cfg\n")
        os.chmod(ROOT_VOTES_CFG, 0o644)
    except Exception:
        pass

def send_to_server_fifo(cmd_text):
    """向服务端 FIFO 管道写入指令"""
    if os.path.exists(FIFO_PATH):
        try:
            with open(FIFO_PATH, "w", encoding="utf-8") as f:
                f.write(cmd_text + "\n")
        except Exception:
            pass

def sync_today_new_maps_motd():
    """【进服弹窗核心】只在当天有新图时设置进服提示，隔天自动清空静音"""
    global last_checked_date
    today_str = time.strftime("%Y-%m-%d")
    today_maps = []

    # 扫描 votes/ 目录下所有记录今天日期的地图
    if os.path.exists(VOTES_DIR):
        for fname in os.listdir(VOTES_DIR):
            if fname.endswith(".cfg") and fname != "all.cfg":
                cat = fname.replace(".cfg", "").capitalize()
                fp = os.path.join(VOTES_DIR, fname)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if today_str in line and "change_map " in line:
                                mname = line.split("change_map ")[-1].replace('"', '').strip()
                                today_maps.append((mname, cat))
                except Exception:
                    pass

    # 如果今天有新入库的地图，配置进服弹窗
    if today_maps:
        # 去重
        seen = set()
        unique_today = []
        for m, c in today_maps:
            if m not in seen:
                seen.add(m)
                unique_today.append((m, c))

        map_lines = "\\n".join([f"• {m} ({c})" for m, c in unique_today[:6]])
        if len(unique_today) > 6:
            map_lines += f"\\n... 等共 {len(unique_today)} 张"

        motd_text = (
            f"==============================\\n"
            f"📢【今日正版新图速递 ({today_str})】\\n"
            f"{map_lines}\\n"
            f"按 ESC -> 选项投票 即可发起体验！\\n"
            f"=============================="
        )
        send_to_server_fifo(f'sv_motd "{motd_text}"')
        print(f"[{today_str}] 已同步进服弹窗 (今日新增 {len(unique_today)} 张新图)")
    else:
        # 今天没有新图（或日期已切换到新的一天），自动清空弹窗不打扰
        send_to_server_fifo('sv_motd ""')

    last_checked_date = today_str

def append_map_and_refresh_votes(cat, mname):
    os.makedirs(VOTES_DIR, exist_ok=True)
    target_cfg = os.path.join(VOTES_DIR, f"{cat.lower()}.cfg")
    current_date = time.strftime("%Y-%m-%d")
    vote_line = f'add_vote "{mname} | ★★★✰✰ | {current_date}" "change_map {mname}"\n'

    try:
        with open(target_cfg, "a", encoding="utf-8") as wf:
            wf.write(vote_line)
        os.chmod(target_cfg, 0o644)
    except Exception as e:
        print(f"写入 {target_cfg} 失败: {e}")

    # 1. 刷新投票菜单数量
    refresh_all_votes_system()

    # 2. 实时广播（通知正在玩的玩家）
    send_to_server_fifo(f'say 📢 [KoG 新图] 已自动入库: {mname} ({cat})')
    send_to_server_fifo(f'broadcast 🎯 新地图 [{mname}] 已上线，快去投票体验！')

    # 3. 更新进服弹窗（通知稍后/今天进服的玩家）
    sync_today_new_maps_motd()

def run_sniper():
    print("=" * 70)
    print("KoG Linux 挂机自动收割服务启动 (含今日新图进服自动弹窗)")
    print(f"地图物理存放目录: {OUTPUT_MAPS_DIR}")
    print(f"投票配置存放目录: {VOTES_DIR}")
    print("=" * 70)

    clean_existing_corrupted_maps()
    os.makedirs(OUTPUT_MAPS_DIR, exist_ok=True)
    os.makedirs(VOTES_DIR, exist_ok=True)

    refresh_all_votes_system()
    # 启动时先核对一次今天的进服弹窗状态
    sync_today_new_maps_motd()

    existing_maps = get_existing_maps()
    print(f"本地目前有效纯正地图总数: {len(existing_maps)} 张")
    print("已开启全天候监听...\n" + "=" * 70)

    round_count = 1
    new_downloaded_count = 0

    while True:
        try:
            # 每天午夜日期切换时，自动复位昨天的提示
            current_day = time.strftime("%Y-%m-%d")
            if current_day != last_checked_date:
                sync_today_new_maps_motd()

            req = urllib.request.Request(MASTER_URL, headers={"User-Agent": "DDNet", "Connection": "close"})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            raw_servers = data.get("servers", [])
            formal_servers = [s for s in raw_servers if is_official_formal_kog_server(s.get("info", {}).get("name", ""))]

            for s in formal_servers:
                info = s.get("info", {})
                map_info = info.get("map", {})
                curr_map = map_info.get("name", "")
                curr_sha = map_info.get("sha256", "")
                server_name = info.get("name", "Unknown Server")

                if not curr_map or not curr_sha:
                    continue

                if curr_map.lower() not in existing_maps:
                    detected_cat = extract_category_from_server(server_name)
                    dest_path = os.path.join(OUTPUT_MAPS_DIR, f"{curr_map}.map")

                    print(f"\n🎯 [捕获新图] 官方正式服轮换中: {curr_map} (专区: {detected_cat})")
                    print(f"   来源房间: {server_name[:38]}...")

                    map_bytes, source_info = download_map_strictly(curr_map, curr_sha)

                    if map_bytes and is_valid_teeworlds_binary(map_bytes):
                        with open(dest_path, "wb") as f:
                            f.write(map_bytes)
                        os.chmod(dest_path, 0o644)

                        print(f"    合法正式图落盘: maps/{curr_map}.map (大小: {len(map_bytes)/1024:.1f} KB, 源: {source_info})")

                        append_map_and_refresh_votes(detected_cat, curr_map)
                        print(f"    已登记至 votes/{detected_cat.lower()}.cfg 并同步配置今日进服提示！")

                        existing_maps.add(curr_map.lower())
                        new_downloaded_count += 1
                        print(f"   累计新抓取: {new_downloaded_count} 张 | 总库容量: {len(existing_maps)} 张")
                    else:
                        print(f"   ❌ 该地图 CDN 暂未就绪或非合法二进制，下轮重试")

            now_time = time.strftime("%Y-%m-%d %H:%M:%S")
            if round_count % 5 == 0:
                print(f"[{now_time}] 监听中... 活跃正式服: {len(formal_servers)} 个 | 本地图库: {len(existing_maps)} 张 | 新捕获: {new_downloaded_count} 张")

            round_count += 1
            time.sleep(15)

        except KeyboardInterrupt:
            print("\n收到退出指令，服务停止。")
            break
        except Exception as e:
            time.sleep(5)
            continue

if __name__ == "__main__":
    try:
        run_sniper()
    except Exception as e:
        print(f"\n[运行异常]: {e}")
        traceback.print_exc()
