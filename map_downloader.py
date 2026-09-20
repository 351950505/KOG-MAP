#!/usr/bin/env python3
import json
import os
import re
import shutil
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

# ================= 生产环境绝对路径 =================
BASE_DIR = "/opt/ddnet" if os.path.exists("/opt/ddnet") else os.path.dirname(os.path.abspath(__file__))
OUTPUT_MAPS_DIR = os.path.join(BASE_DIR, "maps")
VOTES_DIR = os.path.join(BASE_DIR, "votes")
ROOT_VOTES_CFG = os.path.join(BASE_DIR, "votes.cfg")
MASTER_URL = "https://master1.ddnet.org/ddnet/15/servers.json"

# ===== 下载失败冷却（防 CDN 404 死循环刷日志；冷却到期自动复查，防止漏图）=====
FAIL_STATE_FILE = os.path.join(BASE_DIR, "map_download_failures.json")
FAIL_MAX_ATTEMPTS = 5      # 连续失败 N 次后进入冷却
FAIL_COOLDOWN_HOURS = 6    # 冷却 N 小时后自动重新复查

# ===== Git 自动发布（新图 + 分类投票文件同步到 /opt/KOG-MAP 仓库并推送 GitHub）=====
GIT_REPO_DIR = "/opt/KOG-MAP"
GIT_ENABLED = os.path.isdir(os.path.join(GIT_REPO_DIR, ".git"))
GIT_AUTHOR_NAME = "me 2"
GIT_AUTHOR_EMAIL = "58302265+351950505@users.noreply.github.com"

# 备选扫描路径 (包含当前目录与本地 Git 仓库)
SCAN_DIRS = [
    OUTPUT_MAPS_DIR,
    "/opt/KOG-MAP/maps",
    os.path.expanduser("~/.local/share/ddnet/downloadedmaps")
]

# 官方真实 CDN 与备用镜像
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
    """【白名单过滤】屏蔽所有 TEST / BETA 沙盒房间，只收割正式房间"""
    s_upper = server_name.upper()
    for blackword in ["TEST", "BETA", "DEV", "EVALUATE", "SUBMISSION"]:
        if blackword in s_upper:
            return False

    if "KOG" not in s_upper and "KOG.TW" not in s_upper:
        return False

    formal_categories = ["MAIN", "HARD", "EASY", "SOLO", "INSANE", "EXTREME", "MODS"]
    return any(cat in s_upper for cat in formal_categories)

def extract_category_from_server(server_name):
    """从正式服房间名解析分类"""
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

def get_map_rating(map_name):
    """【评分草案 · 暂未启用 —— 不改变现有任何行为】

    存量 votes 里每张图都有真实星级（如 ★★✰✰✰），但新图目前没有评分数据源。
    后续接入时本函数应返回 (stars_str, score_int) 或 None，
    format_vote_title() 会自动使用其结果，届时无需再改投票逻辑。

    TODO(评分草案): 数据源待定，可选：
      1) KoG 官方 / DDNet ratings 接口
      2) 人工维护 ratings.json（启动时加载，键为地图名小写）
    """
    return None

def format_vote_title(map_name):
    """生成投票行标题；评分草案未启用时保持固定占位 ★★★✰✰"""
    rating = get_map_rating(map_name)
    stars = rating[0] if rating else "★★★✰✰"
    return f"{map_name} | {stars} | {time.strftime('%Y-%m-%d')}"

def load_fail_state():
    """读取下载失败状态（JSON 持久化，服务重启不丢计数）"""
    try:
        with open(FAIL_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_fail_state(state):
    try:
        with open(FAIL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def fail_in_cooldown(map_name, state):
    """失败次数达到上限且仍在冷却期内 → True；冷却期满自动清零进入复查"""
    st = state.get(map_name.lower())
    if not st:
        return False
    if st.get("count", 0) >= FAIL_MAX_ATTEMPTS:
        if time.time() - st.get("last", 0) < FAIL_COOLDOWN_HOURS * 3600:
            return True
        st["count"] = 0  # 冷却期满，重新复查
    return False

def fail_record(map_name, state):
    st = state.setdefault(map_name.lower(), {"count": 0, "last": 0})
    st["count"] += 1
    st["last"] = time.time()
    save_fail_state(state)
    return st["count"]

def fail_clear(map_name, state):
    if state.pop(map_name.lower(), None) is not None:
        save_fail_state(state)

def git_publish_map_update(map_name, category):
    """新图 + 全部分类 votes 同步到 /opt/KOG-MAP 仓库，commit 并 push 到 GitHub。

    任何失败只打日志，绝不影响收割主流程；
    push 失败的提交留在本地仓库，凭据就绪后随下次发布一并补推。
    """
    if not GIT_ENABLED:
        print("    [git] 未找到 /opt/KOG-MAP 仓库，跳过发布")
        return False
    try:
        repo_maps = os.path.join(GIT_REPO_DIR, "maps")
        repo_votes = os.path.join(GIT_REPO_DIR, "votes")
        os.makedirs(repo_maps, exist_ok=True)
        os.makedirs(repo_votes, exist_ok=True)

        # 1) 新地图 → 仓库 maps/
        shutil.copy2(os.path.join(OUTPUT_MAPS_DIR, f"{map_name}.map"),
                     os.path.join(repo_maps, f"{map_name}.map"))

        # 2) 分类投票文件 + 根 votes.cfg（refresh 已全量重写，整目录覆盖即可）
        for fn in os.listdir(VOTES_DIR):
            if fn.endswith(".cfg"):
                shutil.copy2(os.path.join(VOTES_DIR, fn), os.path.join(repo_votes, fn))
        shutil.copy2(ROOT_VOTES_CFG, os.path.join(GIT_REPO_DIR, "votes.cfg"))

        # 3) 提交并推送（仅限定 maps/votes 路径，避免带入仓库内无关文件）
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        msg = f"auto: add map [{category}] {map_name} ({time.strftime('%Y-%m-%d %H:%M')})"

        subprocess.run(["git", "-C", GIT_REPO_DIR, "add", "-A", "--",
                        "maps", "votes", "votes.cfg"],
                       capture_output=True, timeout=60, env=env)

        commit = subprocess.run(
            ["git", "-C", GIT_REPO_DIR, "-c", f"user.name={GIT_AUTHOR_NAME}",
             "-c", f"user.email={GIT_AUTHOR_EMAIL}", "commit", "-m", msg],
            capture_output=True, timeout=60, env=env)

        if commit.returncode != 0:
            out = ((commit.stdout or b"") + (commit.stderr or b"")).decode("utf-8", "replace")
            if "nothing to commit" in out:
                print("    [git] 无变更可提交")
                return True
            print(f"    [git] commit 失败: {out.strip()[:200]}")
            return False

        push = subprocess.run(["git", "-C", GIT_REPO_DIR, "push", "origin", "HEAD"],
                              capture_output=True, timeout=120, env=env)
        if push.returncode == 0:
            print(f"    [git] 已提交并推送 GitHub: {msg}")
            return True
        print(f"    [git] push 失败(提交已留在本地，凭据就绪后自动补推): "
              f"{(push.stderr or b'').decode('utf-8', 'replace').strip()[:200]}")
        return False
    except Exception as e:
        print(f"    [git] 发布异常: {type(e).__name__}: {e}")
        return False

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

                # 分隔行必须用 20 个减号：纯空格标题的 add_vote 会被服务器拒绝（见交接文档坑 #8）
                f.write('add_vote "--------------------" "info"\n')
                f.write(f'add_vote "🎲 随机一张 {current_cat} 地图" "random_map"\n')
                f.write('add_vote "--------------------" "info"\n\n')

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

def append_map_and_refresh_votes(cat, mname):
    os.makedirs(VOTES_DIR, exist_ok=True)
    target_cfg = os.path.join(VOTES_DIR, f"{cat.lower()}.cfg")
    vote_line = f'add_vote "{format_vote_title(mname)}" "change_map {mname}"\n'

    try:
        with open(target_cfg, "a", encoding="utf-8") as wf:
            wf.write(vote_line)
        os.chmod(target_cfg, 0o644)
    except Exception as e:
        print(f"写入 {target_cfg} 失败: {e}")

    refresh_all_votes_system()

def run_sniper():
    print("=" * 70)
    print("KoG Linux 挂机自动收割服务启动 (Ubuntu 20.04 守护版)")
    print(f"地图物理存放目录: {OUTPUT_MAPS_DIR}")
    print(f"投票配置存放目录: {VOTES_DIR}")
    print(f"Git 自动发布: {'开启 -> ' + GIT_REPO_DIR if GIT_ENABLED else '未找到仓库，已停用'}")
    print("=" * 70)

    clean_existing_corrupted_maps()
    os.makedirs(OUTPUT_MAPS_DIR, exist_ok=True)
    os.makedirs(VOTES_DIR, exist_ok=True)

    refresh_all_votes_system()
    existing_maps = get_existing_maps()
    print(f"本地目前有效纯正地图总数: {len(existing_maps)} 张")
    print("已开启全天候监听（屏蔽一切测试服与非正式地图）...\n" + "=" * 70)

    round_count = 1
    new_downloaded_count = 0
    fail_state = load_fail_state()
    cooling = sum(1 for st in fail_state.values() if st.get("count", 0) >= FAIL_MAX_ATTEMPTS)
    print(f"下载失败冷却状态已加载: {len(fail_state)} 条记录（冷却中 {cooling} 张）")

    while True:
        try:
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
                    # 下载失败冷却中 → 静默跳过（防 404 死循环刷日志），到期自动复查防漏图
                    if fail_in_cooldown(curr_map, fail_state):
                        continue

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
                        print(f"    已登记至 votes/{detected_cat.lower()}.cfg 并刷新全部分类数量！")

                        fail_clear(curr_map, fail_state)
                        git_publish_map_update(curr_map, detected_cat)

                        existing_maps.add(curr_map.lower())
                        new_downloaded_count += 1
                        print(f"   累计新抓取: {new_downloaded_count} 张 | 总库容量: {len(existing_maps)} 张")
                    else:
                        fail_count = fail_record(curr_map, fail_state)
                        if fail_count >= FAIL_MAX_ATTEMPTS:
                            print(f"   ❌ 连续失败 {fail_count} 次，进入 {FAIL_COOLDOWN_HOURS} 小时冷却，到期自动复查防漏图")
                        else:
                            print(f"   ❌ 该地图 CDN 暂未就绪或非合法二进制 (失败 {fail_count}/{FAIL_MAX_ATTEMPTS})，下轮重试")

            now_time = time.strftime("%Y-%m-%d %H:%M:%S")
            # Linux journalctl 友好输出，每 5 轮打印一次心跳
            if round_count % 5 == 0:
                print(f"[{now_time}] 监听中... 活跃正式服: {len(formal_servers)} 个 | 本地图库: {len(existing_maps)} 张 | 新捕获: {new_downloaded_count} 张")

            round_count += 1
            time.sleep(15)

        except KeyboardInterrupt:
            print("\n收到退出指令，服务停止。")
            break
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ROUND-ERROR] {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            time.sleep(5)
            continue

if __name__ == "__main__":
    try:
        run_sniper()
    except Exception as e:
        print(f"\n[运行异常]: {e}")
        traceback.print_exc()
