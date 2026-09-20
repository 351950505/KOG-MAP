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

OUTPUT_MAPS_DIR = "./maps"
VOTES_DIR = "./votes"
ROOT_VOTES_CFG = "./votes.cfg"
MASTER_URL = "https://master1.ddnet.org/ddnet/15/servers.json"

# 官方正式 CDN
CDN_TEMPLATES = [
    "https://maps.kog.tw/teeworlds/maps/{name}_{sha}.map",
    "https://maps.ddnet.org/compilations/maps/{name}_{sha}.map",
    "https://maps2.ddnet.org/compilations/maps/{name}_{sha}.map",
    "https://maps.ddnet.org/compilations/maps/{name}.map",
]

CATEGORY_DISPLAY_NAMES = {
    "Easy": "Eᴀsʏ",
    "Main": "Mᴀɪɴ",
    "Hard": "Hᴀʀᴅ",
    "Solo": "Sᴏʟᴏ",
    "Insane": "Iɴsᴀɴᴇ",
    "Extreme": "Exᴛʀᴇᴍᴇ",
    "Mods": "Mᴏᴅs",
    "Unknown": "Uɴᴋɴᴏᴡɴ",
}

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def is_valid_teeworlds_binary(data_bytes):
    """校验 Teeworlds 原生二进制魔数 (DATA 或 ATAD)"""
    if not data_bytes or len(data_bytes) < 1024:
        return False
    magic = data_bytes[:4]
    if magic in [b"DATA", b"ATAD"]:
        return True
    header_preview = data_bytes[:64].lower()
    if (
        b"<html" in header_preview
        or b"<!doc" in header_preview
        or b"404" in header_preview
    ):
        return False
    return False


def clean_existing_corrupted_maps():
    """清理伪装成 .map 的 404 网页假文件"""
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
    """
    【核心过滤防御】仅允许正统官方常驻房间，彻底过滤一切 TEST、BETA 和测试沙盒
    """
    s_upper = server_name.upper()

    # 1. 强行剔除一切测试、评估、BETA 和开发房间
    for blackword in ["TEST", "BETA", "DEV", "EVALUATE", "SUBMISSION"]:
        if blackword in s_upper:
            return False

    # 2. 确保是官方 KoG 服务器
    if "KOG" not in s_upper and "KOG.TW" not in s_upper:
        return False

    # 3. 确保是正统分类专区
    formal_categories = [
        "MAIN",
        "HARD",
        "EASY",
        "SOLO",
        "INSANE",
        "EXTREME",
        "MODS",
    ]
    if any(cat in s_upper for cat in formal_categories):
        return True

    return False


def extract_category_from_server(server_name):
    """从正规官方服务器名字中提取难度分类"""
    m = re.search(
        r'-\s*(Easy|Main|Hard|Solo|Insane|Extreme|Mods)\b',
        server_name,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).capitalize()
    for kw in ["Easy", "Main", "Hard", "Solo", "Insane", "Extreme", "Mods"]:
        if kw.lower() in server_name.lower():
            return kw
    return "Main"  # 正式服若未写明，默认归入主线


def get_existing_maps():
    existing = set()
    desktop = os.path.expandvars(r"%USERPROFILE%\Desktop")
    scan_dirs = [
        OUTPUT_MAPS_DIR,
        os.path.join(desktop, "opengores-maps"),
        "./KoG_Maps_Categorized",
    ]

    for d in scan_dirs:
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
                        pure = re.sub(
                            r"_[^_]+ \d+星_发布时间.*$", "", raw
                        ).strip().lower()
                    else:
                        pure = raw.lower()
                    existing.add(pure)
    return existing


def try_extract_from_client_cache(map_name):
    client_cache = os.path.expandvars(r"%APPDATA%\DDNet\downloadedmaps")
    if os.path.exists(client_cache):
        for f in os.listdir(client_cache):
            if f.lower().startswith(map_name.lower()) and f.endswith(".map"):
                f_path = os.path.join(client_cache, f)
                try:
                    with open(f_path, "rb") as cf:
                        data = cf.read()
                    if is_valid_teeworlds_binary(data):
                        return data
                except Exception:
                    pass
    return None


def download_with_curl_strict(target_url):
    temp_file = "./temp_curl_test.tmp"
    try:
        cmd = [
            "curl",
            "-s",
            "-f",
            "-L",
            "--connect-timeout",
            "10",
            "-A",
            "Mozilla/5.0 DDNet/18.8",
            "-o",
            temp_file,
            target_url,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if (
            res.returncode == 0
            and os.path.exists(temp_file)
            and os.path.getsize(temp_file) > 1024
        ):
            with open(temp_file, "rb") as tf:
                data = tf.read()
            os.remove(temp_file)
            if is_valid_teeworlds_binary(data):
                return data
    except Exception:
        pass
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
    return None


def download_map_strictly(map_name, map_sha):
    cached = try_extract_from_client_cache(map_name)
    if cached:
        return cached, "本地客户端 UDP 缓存"

    encoded_name = urllib.parse.quote(map_name)
    headers = {
        "User-Agent": "Mozilla/5.0 DDNet/18.8",
        "Connection": "close",
        "Accept-Encoding": "identity",
    }

    for tpl in CDN_TEMPLATES:
        url = tpl.format(name=encoded_name, sha=map_sha)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(
                    req, context=ssl_ctx, timeout=10
                ) as resp:
                    data = resp.read()
                    if is_valid_teeworlds_binary(data):
                        return data, url.split("/")[2]
            except Exception:
                time.sleep(0.5)

        curl_data = download_with_curl_strict(url)
        if curl_data:
            return curl_data, f"curl({url.split('/')[2]})"

    return None, "None"


def vote_sort_key(line):
    """投票列表排序：有日期的按日期倒序（新的在上，同日期按图名）；
    无日期的 Official 组沉底（组内按图名）。与线上 map_downloader.py 同逻辑。"""
    target = line.split("change_map ")[-1].replace('"', "").strip().lower()
    m = re.search(r"\|\s*(\d{4})-(\d{1,2})-(\d{1,2})", line)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (0, -y, -mo, -d, target)
    return (1, target)


def refresh_all_votes_system():
    """扫描 votes/ 目录下所有分类，精确重算实际数量并刷新头部导航与标题"""
    if not os.path.exists(VOTES_DIR):
        os.makedirs(VOTES_DIR, exist_ok=True)
        return

    category_maps = defaultdict(list)

    for fname in os.listdir(VOTES_DIR):
        if fname.endswith(".cfg") and fname != "all.cfg":
            raw_cat = fname.replace(".cfg", "").capitalize()
            cat_name = (
                "Extreme"
                if "Ext" in raw_cat
                else "Insane"
                if "Insane" in raw_cat
                else "Hard"
                if "Hard" in raw_cat
                else "Easy"
                if "Easy" in raw_cat
                else "Solo"
                if "Solo" in raw_cat
                else "Main"
                if "Main" in raw_cat
                else "Mods"
                if "Mod" in raw_cat
                else "Unknown"
            )

            cfg_path = os.path.join(VOTES_DIR, fname)
            try:
                with open(
                    cfg_path, "r", encoding="utf-8", errors="ignore"
                ) as f:
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
            m_target = (
                m.split("change_map ")[-1].replace('"', "").strip().lower()
            )
            if m_target not in seen:
                seen.add(m_target)
                unique_maps.append(m)

        unique_maps.sort(key=vote_sort_key)
        category_maps[cat] = unique_maps
        real_counts[cat] = len(unique_maps)

    priority_order = [
        "Main",
        "Easy",
        "Hard",
        "Solo",
        "Insane",
        "Extreme",
        "Mods",
        "Unknown",
    ]
    active_categories = [c for c in priority_order if c in real_counts]
    for c in real_counts:
        if c not in active_categories:
            active_categories.append(c)

    for current_cat in active_categories:
        cfg_file = os.path.join(VOTES_DIR, f"{current_cat.lower()}.cfg")
        real_total = real_counts[current_cat]

        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                f.write(
                    "# ==================================================\n"
                )
                f.write(
                    f"# KoG 投票系统 - 【{current_cat}】专区 (共 {real_total}"
                    " 张)\n"
                )
                f.write("# 说明: 仅展示本分类地图，无任何冗余大列表\n")
                f.write(
                    "# ==================================================\n\n"
                )

                f.write(
                    "# -------------- 【分类无缝切换】 --------------\n"
                )
                for cat in active_categories:
                    styled = CATEGORY_DISPLAY_NAMES.get(cat, cat)
                    count = real_counts[cat]
                    if cat == current_cat:
                        f.write(
                            f'add_vote "☒ {styled} Mᴀᴘs ({count})" "info"\n'
                        )
                    else:
                        f.write(
                            f'add_vote "☐ {styled} Mᴀᴘs ({count})" "clear_votes;'
                            f' exec votes/{cat.lower()}.cfg"\n'
                        )

                f.write('add_vote " " "info"\n')
                f.write(
                    f'add_vote "🎲 随机一张 {current_cat} 地图" "random_map"\n'
                )
                f.write('add_vote " " "info"\n\n')

                f.write(
                    f"# -------------- 【{current_cat} 地图列表】"
                    " --------------\n"
                )
                for m_line in category_maps[current_cat]:
                    f.write(f"{m_line}\n")
        except Exception as e:
            print(f"刷新 {cfg_file} 失败: {e}")

    default_cat = (
        "main" if "Main" in active_categories else active_categories[0].lower()
    )
    try:
        with open(ROOT_VOTES_CFG, "w", encoding="utf-8") as rf:
            rf.write(f"exec votes/{default_cat}.cfg\n")
    except Exception:
        pass


def append_map_and_refresh_votes(cat, mname):
    os.makedirs(VOTES_DIR, exist_ok=True)
    target_cfg = os.path.join(VOTES_DIR, f"{cat.lower()}.cfg")
    current_date = time.strftime("%Y-%m-%d")
    vote_line = (
        f'add_vote "{mname} | ★★★✰✰ | {current_date}" "change_map {mname}"\n'
    )

    try:
        with open(target_cfg, "a", encoding="utf-8") as wf:
            wf.write(vote_line)
    except Exception as e:
        print(f"写入 {target_cfg} 失败: {e}")

    refresh_all_votes_system()


def run_sniper():
    print("=" * 70)
    print("KoG 挂机自动收割器 (严格防范 TEST 房间・纯正式服地图收割版)")
    print("防御规则: 强行屏蔽一切带有 TEST / BETA / EVALUATE 的沙盒测试房间")
    print("收割目标: 仅从 Main/Hard/Easy/Solo/Insane/Extreme 正式房间拉取地图")
    print("=" * 70)

    clean_existing_corrupted_maps()
    os.makedirs(OUTPUT_MAPS_DIR, exist_ok=True)
    os.makedirs(VOTES_DIR, exist_ok=True)

    print("正在校准已有投票配置...")
    refresh_all_votes_system()

    existing_maps = get_existing_maps()
    print(f"本地有效纯正地图总数: {len(existing_maps)} 张")
    print(
        "正在开启全自动静默监听...（已开启测试服防火墙）\n" + "=" * 70
    )

    round_count = 1
    new_downloaded_count = 0

    while True:
        try:
            req = urllib.request.Request(
                MASTER_URL,
                headers={"User-Agent": "DDNet", "Connection": "close"},
            )
            with urllib.request.urlopen(
                req, context=ssl_ctx, timeout=6
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            raw_servers = data.get("servers", [])
            formal_servers = []

            # 核心过滤：只挑选正统正式官方服
            for s in raw_servers:
                s_name = s.get("info", {}).get("name", "")
                if is_official_formal_kog_server(s_name):
                    formal_servers.append(s)

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

                    print(
                        f"\n🎯 [捕获正版新图] 正式服轮换中: {curr_map} (专区: {detected_cat})"
                    )
                    print(f"   来源正式房间: {server_name[:38]}...")
                    print(f"   正在多源下载并执行 DATA 魔数校验...")

                    map_bytes, source_info = download_map_strictly(
                        curr_map, curr_sha
                    )

                    if map_bytes and is_valid_teeworlds_binary(map_bytes):
                        with open(dest_path, "wb") as f:
                            f.write(map_bytes)

                        print(
                            f"    合法正式图落盘: maps/{curr_map}.map (大小:"
                            f" {len(map_bytes)/1024:.1f} KB, 源: {source_info})"
                        )

                        append_map_and_refresh_votes(detected_cat, curr_map)
                        print(
                            f"    已登记至 votes/{detected_cat.lower()}.cfg"
                            " 并全自动刷新所有分类导航栏数量！"
                        )

                        existing_maps.add(curr_map.lower())
                        new_downloaded_count += 1
                        print(
                            f"   正式新图累计抓取: {new_downloaded_count}"
                            f" 张 | 总库容量: {len(existing_maps)} 张"
                        )
                    else:
                        print(
                            f"   ❌ 该地图 CDN 暂未就绪或非合法二进制，下轮轮换重试"
                        )

            now_time = time.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{now_time}] 监听中... 在线正式房间: {len(formal_servers)} 个 |"
                f" 本地图库: {len(existing_maps)} 张 | 正版新图捕获:"
                f" {new_downloaded_count} 张   "
            )
            sys.stdout.flush()

            round_count += 1
            time.sleep(15)

        except KeyboardInterrupt:
            print("\n收到退出指令，监听结束。")
            break
        except Exception:
            time.sleep(5)
            continue


if __name__ == "__main__":
    try:
        run_sniper()
    except Exception as e:
        print(f"\n[运行异常]: {e}")
        traceback.print_exc()
    finally:
        print("\n" + "-" * 70)
        input("程序已结束，按【回车键 (Enter)】退出...")