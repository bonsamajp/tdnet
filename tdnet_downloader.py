"""
TDnet (適時開示情報閲覧サービス) ダウンローダー
https://www.release.tdnet.info/inbs/I_main_00.html の一覧を取得・保存します。

動作仕様:
  - 同日の CSV は毎回上書き（最新状態に更新）
  - PDF は未取得のものだけダウンロード（差分取得）
  - 1日に何度実行しても安全
"""

import requests
from bs4 import BeautifulSoup
import csv
import json
import logging
import os
import time
from datetime import date


BASE_URL = "https://www.release.tdnet.info"
LIST_URL = BASE_URL + "/inbs/I_list_{page:03d}_{date}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL + "/inbs/I_main_00.html",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ロギング設定
# ---------------------------------------------------------------------------

def setup_logging(log_dir: str, target_date: str, console: bool = False) -> None:
    """ログ設定。logs/tdnet_YYYYMMDD.log に追記。console=True でターミナルにも出力。"""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"tdnet_{target_date}.log")

    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    if console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------

def fetch_list_page(target_date: str, page: int = 1) -> list[dict]:
    """指定日付・ページの開示一覧を取得する。"""
    url = LIST_URL.format(date=target_date, page=page)
    logger.info("Fetching: %s", url)

    resp = SESSION.get(url, timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="list-body-box")
    if table is None:
        return []

    rows = []
    for td_time in table.select("td.kjTime"):
        tr = td_time.parent

        title_td = tr.select_one("td.kjTitle")
        title = title_td.get_text(strip=True) if title_td else ""
        pdf_a = title_td.find("a") if title_td else None
        pdf_url = _abs_url(pdf_a["href"]) if pdf_a and pdf_a.get("href") else ""

        xbrl_a = tr.select_one("td.kjXbrl a")
        xbrl_url = _abs_url(xbrl_a["href"]) if xbrl_a and xbrl_a.get("href") else ""

        rows.append({
            "date":     target_date,
            "time":     td_time.get_text(strip=True),
            "code":     _text(tr, "td.kjCode"),
            "company":  _text(tr, "td.kjName"),
            "title":    title,
            "pdf_url":  pdf_url,
            "xbrl_url": xbrl_url,
            "place":    _text(tr, "td.kjPlace"),
        })

    return rows


def fetch_all_pages(target_date: str, max_pages: int = 30) -> list[dict]:
    """指定日付の全ページを取得する。"""
    all_rows = []
    for page in range(1, max_pages + 1):
        rows = fetch_list_page(target_date, page)
        if not rows:
            break
        all_rows.extend(rows)
        time.sleep(0.5)
    logger.info("合計 %d 件取得", len(all_rows))
    return all_rows


def _text(tag, selector: str) -> str:
    el = tag.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _abs_url(href: str) -> str:
    if not href:
        return ""
    return href if href.startswith("http") else BASE_URL + "/inbs/" + href


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

FIELDNAMES = ["date", "time", "code", "company", "title", "pdf_url", "xbrl_url", "place"]


def save_csv(rows: list[dict], output_path: str) -> None:
    """CSV形式で保存する（同日は上書き）。"""
    if not rows:
        logger.warning("データがありません。")
        return
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CSV保存: %s (%d 件)", output_path, len(rows))


def save_json(rows: list[dict], output_path: str) -> None:
    """JSON形式で保存する（同日は上書き）。"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    logger.info("JSON保存: %s (%d 件)", output_path, len(rows))


def download_pdfs(rows: list[dict], pdf_dir: str) -> None:
    """
    PDFを差分ダウンロードする。
    既に存在するファイルはスキップするため、1日に複数回実行しても安全。
    """
    os.makedirs(pdf_dir, exist_ok=True)
    new_count = skip_count = error_count = 0

    for row in rows:
        url = row["pdf_url"]
        if not url:
            continue
        filename = url.split("/")[-1]
        dest = os.path.join(pdf_dir, filename)

        if os.path.exists(dest):
            skip_count += 1
            continue

        try:
            resp = SESSION.get(url, timeout=30)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
            logger.info("PDF DL: %s", filename)
            new_count += 1
            time.sleep(0.3)
        except Exception as e:
            logger.error("PDF エラー: %s -> %s", filename, e)
            error_count += 1

    logger.info("PDF: 新規 %d 件 / スキップ %d 件 / エラー %d 件", new_count, skip_count, error_count)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="TDnet 適時開示情報ダウンローダー（差分対応）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python tdnet_downloader.py                        # 今日の一覧を取得（CSV + PDF）
  python tdnet_downloader.py --date 20260409        # 指定日付
  python tdnet_downloader.py --format both          # CSV + JSON 両方
  python tdnet_downloader.py --no-pdf               # PDFダウンロードしない
  python tdnet_downloader.py --output-dir ./data    # 出力先を指定
""",
    )
    parser.add_argument(
        "--date",
        default=date.today().strftime("%Y%m%d"),
        help="取得日付 YYYYMMDD (デフォルト: 今日)",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "both"],
        default="csv",
        help="一覧の出力フォーマット (デフォルト: csv)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="出力先ディレクトリ (デフォルト: カレント)",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="PDFダウンロードを行わない",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=30,
        help="最大ページ数 (デフォルト: 30)",
    )
    args = parser.parse_args()

    output_dir = args.output_dir or "."

    log_dir = os.path.join(output_dir, "logs")
    setup_logging(log_dir, args.date)

    logger.info("=== TDnet ダウンローダー 開始 ===")
    logger.info("対象日付: %s", args.date)

    rows = fetch_all_pages(args.date, max_pages=args.max_pages)
    os.makedirs(output_dir, exist_ok=True)

    if args.format in ("csv", "both"):
        csv_path = os.path.join(output_dir, f"tdnet_{args.date}.csv")
        save_csv(rows, csv_path)

    if args.format in ("json", "both"):
        json_path = os.path.join(output_dir, f"tdnet_{args.date}.json")
        save_json(rows, json_path)

    if not args.no_pdf:
        pdf_dir = os.path.join(output_dir, "pdf")
        logger.info("PDFフォルダ: %s", pdf_dir)
        download_pdfs(rows, pdf_dir)

    logger.info("=== 完了 ===")
