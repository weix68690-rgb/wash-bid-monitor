# -*- coding: utf-8 -*-
"""
全国洗车机招标监控（中国政府采购网 ccgp）
- 每天抓取全国范围内含洗车设备关键词的公告
- seen.json 增量去重，首跑建立基线并把本轮快照推送一次（用于验证云端可抓到数据）
- PushPlus 微信推送：新增清单 / 无更新心跳 / 抓取失败报警
- 设计为可在 GitHub Actions（境外 IP）运行，ccgp 无地域硬限制
"""
import os
import re
import json
import time
import random
import hashlib
import datetime as dt
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ============ 配置（环境变量可覆盖）============
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
DAYS_BACK = int(os.getenv("DAYS_BACK", "7"))          # 回看近 N 天
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))          # 每个关键词最多翻几页
SEND_HEARTBEAT = os.getenv("SEND_HEARTBEAT", "1") == "1"
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")
RESULT_FILE = os.getenv("RESULT_FILE", "latest_result.json")

# 搜索关键词
# 标题检索：公告标题里直接带这些词的（相关性最高）
TITLE_KEYWORDS = ["洗车机", "自动洗车设备", "隧道式洗车机", "洗车设备", "全自动洗车"]
# 正文兜底检索：标题不带"洗车机"、但采购内容正文里含的单（如设备采购打包、配套工程）
FULLTEXT_KEYWORDS = ["洗车机", "洗车设备"]
# 组装检索任务 (关键词, searchtype, 命中方式)；searchtype=1标题，2全文
SEARCH_QUERIES = ([(k, 1, "标题") for k in TITLE_KEYWORDS]
                  + [(k, 2, "正文") for k in FULLTEXT_KEYWORDS])

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "http://search.ccgp.gov.cn/",
}

CCGP_SEARCH = "http://search.ccgp.gov.cn/bxsearch"

# 统一会话：trust_env=False 强制忽略系统/环境代理，避免请求被本地代理劫持
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update(HEADERS)


# ============ 工具函数 ============
def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def make_id(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            log("警告: seen.json 解析失败，按空集合处理")
    return set()


def save_seen(seen):
    # 只保留最近 3000 条
    arr = sorted(seen)[-3000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False, indent=0)


def daterange():
    """ccgp 时间格式用冒号 YYYY:MM:DD"""
    end = dt.date.today()
    start = end - dt.timedelta(days=DAYS_BACK)
    return start.strftime("%Y:%m:%d"), end.strftime("%Y:%m:%d")


# ============ 抓取 ccgp ============
def save_sample(tag, html):
    """无条件把原始HTML落盘（含被限流页），用于事后自证：是真0、被拦截、还是解析失败"""
    try:
        os.makedirs("debug", exist_ok=True)
        safe = re.sub(r"\W+", "_", tag)
        with open(os.path.join("debug", f"sample_{safe}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        log(f"  [{tag}] 样本落盘失败: {e}")


def fetch_ccgp(keyword, searchtype, match):
    """
    返回 (items, status)
    status: ok / blocked / error / parse_fail
    items: [{title, href, date, match}]
    """
    start_time, end_time = daterange()
    items = []
    status = "ok"
    tag = f"{match}_{keyword}"

    for page in range(1, MAX_PAGES + 1):
        params = (
            f"?searchtype={searchtype}&page_index={page}&bidSort=0"
            f"&kw={quote(keyword)}"
            f"&start_time={start_time}&end_time={end_time}"
            f"&timeType=6&displayZone=&zoneId=&pppStatus=0&agentName="
        )
        url = CCGP_SEARCH + params
        try:
            resp = SESSION.get(url, timeout=20)
            resp.encoding = "utf-8"
            html = resp.text
        except Exception as e:
            log(f"  [{tag}] 第{page}页请求异常: {e}")
            status = "error"
            break

        # 第1页原始HTML无条件落盘（必须在限流判定之前），保证三种状态可区分
        if page == 1:
            save_sample(tag, html)

        # 限流 / 拦截判定（fail-closed，不得把空页当正常）
        if ("频繁访问" in html) or ("您的访问" in html) or len(html) < 5000:
            log(f"  [{tag}] 第{page}页疑似被限流 (html长度={len(html)})")
            status = "blocked"
            break

        page_items, has_next, declared = parse_ccgp_html(html)
        for it in page_items:
            it["match"] = match
        items.extend(page_items)
        log(f"  [{tag}] 第{page}页 声明{declared}条/解析{len(page_items)}条")

        # 交叉校验：页面声明有结果、却一条没解析出来 -> 解析器异常，不能当"没有"
        if declared is not None and declared > 0 and not page_items:
            log(f"  [{tag}] 页面声明{declared}条但解析0条，判定解析失败")
            status = "parse_fail"
            break

        if not has_next:
            break
        time.sleep(random.uniform(0.6, 1.4))  # 礼貌延时，降低被封概率

    return items, status


def diagnose(keyword, html):
    """[保留] 打印页面结构线索，供需要时手工调用"""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "(无title)"
    all_links = soup.find_all("a", href=True)
    ccgp_links = [a["href"].strip() for a in all_links if "ccgp.gov.cn" in a["href"]]
    uls = soup.find_all("ul", class_=True)
    li_count = len(soup.find_all("li"))
    log(f"  [{keyword}] 诊断 title={title!r}")
    log(f"  [{keyword}] 诊断 总链接={len(all_links)} ccgp链接={len(ccgp_links)} li={li_count} ulClass={[u.get('class') for u in uls][:6]}")
    for s in ccgp_links[:5]:
        log(f"  [{keyword}] 诊断 href样例: {s[:110]}")


DATE_RE = re.compile(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")
HREF_RE = re.compile(r"ccgp\.gov\.cn/(cggg|zbgg|zhongbiao|jggg)")
DECLARED_RE = re.compile(r"共找到\s*<span[^>]*>\s*(\d+)\s*</span>\s*条")


def extract_date(text):
    """支持 2026.10.29 / 2026-10-29 / 2026/10/29，统一归一化为 2026-10-29"""
    m = DATE_RE.search(text or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def parse_ccgp_html(html):
    """
    解析 ccgp 搜索结果页
    返回 (items, has_next, declared)
      items: [{title, href, date}]
      declared: 页面"共找到 N 条"中的 N（无法识别时 None），用于交叉校验
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_href = set()

    # 精确锁定结果列表容器；找不到则回退到全局公告链接
    ul = soup.select_one("ul.vT-srch-result-list-bid")
    if ul:
        anchors = [li.find("a") for li in ul.find_all("li")]
        anchors = [a for a in anchors if a and a.get("href")]
    else:
        anchors = soup.find_all("a", href=HREF_RE)

    for a in anchors:
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not href or not title or href in seen_href or len(title) < 6:
            continue
        seen_href.add(href)

        # 日期：从所在 li（含日期 span）整体文本里取
        host = a.find_parent("li") or a.parent
        date = extract_date(host.get_text(" ", strip=True) if host else "")

        items.append({"title": title, "href": href, "date": date})

    dm = DECLARED_RE.search(html)
    declared = int(dm.group(1)) if dm else None

    # 是否有下一页
    has_next = bool(soup.find("a", string=re.compile(r"下一页"))) or \
        bool(re.search(r"page_index=\d+[^>]*>下一页", html)) or \
        bool(re.search(r"下一页", html)) and (declared or 0) > len(items)
    return items, has_next, declared


# ============ 推送 ============
def push(title, content):
    if not PUSHPLUS_TOKEN:
        log("未配置 PUSHPLUS_TOKEN，仅打印不推送：")
        print(f"---- {title} ----\n{content}\n")
        return False
    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=20,
        )
        data = r.json()
        if str(data.get("code")) == "200":
            log("推送成功")
            return True
        log(f"推送失败: {data}")
        return False
    except Exception as e:
        log(f"推送异常: {e}")
        return False


def build_content(new_items):
    # 有日期的按日期倒序
    new_items.sort(key=lambda x: x.get("date", ""), reverse=True)
    lines = [f"共 {len(new_items)} 条新公告（来源：中国政府采购网·全国，近{DAYS_BACK}天）", ""]
    for i, it in enumerate(new_items, 1):
        date = f"[{it['date']}] " if it.get("date") else ""
        tag = "（正文命中）" if it.get("match") == "正文" else ""
        lines.append(f"{i}. {date}{it['title']}{tag}")
        lines.append(f"   {it['href']}")
    return "\n".join(lines)


# ============ 主流程 ============
def main():
    log("=== 全国洗车机招标监控启动 ===")
    seen = load_seen()
    first_run = len(seen) == 0
    if first_run:
        log("检测到首跑：本次只建立基线，不推送新增。")

    all_items = {}
    source_status = {}

    for kw, searchtype, match in SEARCH_QUERIES:
        tag = f"{match}_{kw}"
        items, status = fetch_ccgp(kw, searchtype, match)
        source_status[tag] = status
        for it in items:
            old = all_items.get(it["href"])
            # 标题命中优先于正文命中；否则保留先出现的
            if (not old) or (old.get("match") != "标题" and it.get("match") == "标题"):
                all_items[it["href"]] = it
        time.sleep(random.uniform(1.0, 2.0))

    log(f"合并去重后本轮回抓到 {len(all_items)} 条")

    # 计算新增
    # 首跑也把本轮抓到的全部作为"基线快照"推送，便于当场验证云端能否抓到数据
    new_items = []
    for href, it in all_items.items():
        uid = make_id("ccgp_" + href)
        if uid not in seen:
            new_items.append(it)
        seen.add(uid)

    save_seen(seen)
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"time": dt.datetime.now().isoformat(), "total": len(all_items),
             "new": len(new_items), "status": source_status,
             "items": list(all_items.values())},
            f, ensure_ascii=False, indent=2,
        )

    # 状态汇总
    blocked_kws = [k for k, v in source_status.items() if v == "blocked"]
    error_kws = [k for k, v in source_status.items() if v == "error"]
    parsefail_kws = [k for k, v in source_status.items() if v == "parse_fail"]
    bad_kws = blocked_kws + error_kws + parsefail_kws
    any_ok = any(v == "ok" for v in source_status.values())

    today = dt.date.today().strftime("%m-%d")

    # 失败显性化：没有任何一个检索成功 且 一条数据都没有 -> 明确报失败（不是"今天没招标"）
    if not any_ok and not all_items:
        if blocked_kws:
            reason = "被限流/验证码拦截"
        elif parsefail_kws:
            reason = "页面结构变化导致解析失败（需修脚本）"
        else:
            reason = "网络请求异常"
        push(f"【报警】洗车机监控抓取失败 {today}",
             f"本次所有检索均未成功（原因：{reason}）。\n"
             f"限流: {blocked_kws}\n解析失败: {parsefail_kws}\n异常: {error_kws}\n"
             f"请人工查看 http://search.ccgp.gov.cn/ ，这不是'今天没有招标'。"
             f"debug/sample_*.html 已随本次提交，可直接核对云端拿到的原始页面。")
        log("本轮抓取失败，已报警。")
        return

    if first_run:
        head = (f"首跑完成，已建立基线 {len(all_items)} 条（全国，近{DAYS_BACK}天）。\n"
                f"下面是本轮抓到的全部公告；明天起每天这个时间只推送新增，无新增则推心跳。\n")
        if bad_kws:
            head += f"注意：部分检索异常 {bad_kws}（基线可能不全，debug已留样本）。\n"
        head += "\n"
        push(f"洗车机监控已启动·基线 {len(all_items)} 条 {today}",
             head + (build_content(new_items) if new_items else "（本轮未抓到任何公告）"))
        log("首跑基线已建立并推送。")
        return

    if new_items:
        push(f"洗车机招标新增 {len(new_items)} 条 {today}",
             build_content(new_items))
        log(f"本轮新增 {len(new_items)} 条，已推送。")
    else:
        msg = (f"今日无新增（全国，近{DAYS_BACK}天在线 {len(all_items)} 条）。")
        if bad_kws:
            msg += f"\n注意：部分检索异常，结果可能不全: {bad_kws}"
        if SEND_HEARTBEAT:
            push(f"洗车机监控心跳 {today}", msg)
        log("本轮无新增。")


if __name__ == "__main__":
    main()
