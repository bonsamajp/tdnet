"""
TDnet 更新監視ウォッチャー
一覧の先頭行（最新データ）だけを定期チェックし、変化があった時だけ全データを取得します。

使い方:
  python tdnet_watcher.py                          # 今日を10秒間隔で監視
  python tdnet_watcher.py --date 20260410          # 指定日付
  python tdnet_watcher.py --interval 30            # 間隔を30秒に変更
  python tdnet_watcher.py --no-pdf                 # PDFダウンロードしない
  python tdnet_watcher.py --output-dir ./data      # 出力先を指定
  python tdnet_watcher.py --keywords keywords.txt  # キーワードファイルを指定
  python tdnet_watcher.py --notify-config notify.ini  # 通知設定ファイルを指定
"""

import time
import logging
import argparse
from datetime import date
import tdnet_downloader as dl
import notifier as nt


logger = logging.getLogger(__name__)


def get_first_row(target_date: str) -> dict | None:
    """
    1ページ目だけ取得し、先頭行（最新データ）を返す。
    サイト更新の検出用なので1リクエストのみ。
    """
    rows = dl.fetch_list_page(target_date, page=1)
    return rows[0] if rows else None


def _rotate_log(log_dir: str, new_date: str, console: bool) -> None:
    """ログハンドラを新しい日付のファイルに切り替える。"""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)
    dl.setup_logging(log_dir, new_date, console=console)


def watch(
    target_date: str,
    interval: int,
    output_dir: str,
    no_pdf: bool,
    keywords_path: str,
    notify_config_path: str,
    console_log: bool = False,
    fixed_date: bool = False,
) -> None:
    import os
    log_dir = f"{output_dir}/logs"
    pdf_dir = None if no_pdf else f"{output_dir}/pdf"
    csv_path = f"{output_dir}/tdnet_{target_date}.csv"

    notifier = nt.Notifier(notify_config_path)
    keywords = nt.load_keywords(keywords_path)
    if keywords:
        logger.info("キーワード読み込み: %d件 %s", len(keywords), keywords)
    else:
        logger.info("キーワード未設定（通知なし）")

    last_first_row: dict | None = None
    known_pdf_urls: set[str] = set()  # 把握済みのPDF URL（新規検出に使用）
    is_first_run = True
    check_count = 0

    logger.info("監視開始: %s | 間隔 %d秒 | 出力: %s", target_date, interval, output_dir)

    while True:
        # 日またがり検出（--date 指定時はスキップ）
        today = date.today().strftime("%Y%m%d")
        if not fixed_date and today != target_date:
            logger.info("日付変更: %s → %s", target_date, today)
            target_date = today
            csv_path = f"{output_dir}/tdnet_{target_date}.csv"
            last_first_row = None
            known_pdf_urls = set()
            is_first_run = True
            _rotate_log(log_dir, target_date, console_log)
            # キーワードファイルも再読み込み（日をまたいで変更されている可能性）
            keywords = nt.load_keywords(keywords_path)
            logger.info("新しい日付で監視再開: %s", target_date)

        # キーワードファイルを毎回読み直す（起動中の変更に即対応）
        keywords = nt.load_keywords(keywords_path)

        check_count += 1
        logger.info("チェック #%d", check_count)

        try:
            first_row = get_first_row(target_date)
        except Exception as e:
            logger.error("取得エラー: %s", e)
            time.sleep(interval)
            continue

        if first_row is None:
            logger.info("データなし（開示なし、または日付違い）")
            time.sleep(interval)
            continue

        if first_row == last_first_row:
            logger.info("変化なし（先頭: %s %s %s）",
                        first_row["time"], first_row["code"], first_row["title"][:20])
            time.sleep(interval)
            continue

        # 先頭行が変わった → 全ページ取得
        if is_first_run:
            logger.info("初回取得（ベースライン確立）")
        else:
            logger.info("更新検出！")
            logger.info("  変更前: %s %s %s",
                        last_first_row["time"], last_first_row["code"], last_first_row["title"][:30])
            logger.info("  変更後: %s %s %s",
                        first_row["time"], first_row["code"], first_row["title"][:30])

        logger.info("全データ取得中...")
        try:
            os.makedirs(output_dir, exist_ok=True)

            rows = dl.fetch_all_pages(target_date)
            dl.save_csv(rows, csv_path)

            # 新規行を特定
            if is_first_run:
                # 初回: PDFはDLしない（ベースライン確立のみ）、キーワード検索は全行対象
                pdf_rows = []
                search_rows = rows
                known_pdf_urls = {r["pdf_url"] for r in rows if r["pdf_url"]}
                logger.info("ベースライン記録: %d件", len(known_pdf_urls))
            else:
                # 更新検出時: 前回からの新規行のみ対象
                new_rows = [r for r in rows if r["pdf_url"] and r["pdf_url"] not in known_pdf_urls]
                known_pdf_urls = {r["pdf_url"] for r in rows if r["pdf_url"]}
                pdf_rows = new_rows
                search_rows = new_rows
                logger.info("新規行: %d件", len(new_rows))

            # PDF差分ダウンロード
            if not no_pdf and pdf_rows:
                dl.download_pdfs(pdf_rows, pdf_dir)

            # キーワード検索 → 通知
            if search_rows and keywords:
                matches = nt.search(search_rows, keywords)
                if matches:
                    logger.info("キーワードマッチ: %d件", len(matches))
                    for m in matches:
                        logger.info("  [%s] %s %s「%s」キーワード=%s",
                                    m["time"], m["code"], m["company"],
                                    m["title"][:30], m["matched_keyword"])
                    notifier.send(matches)
                else:
                    logger.info("キーワードマッチなし")

            last_first_row = first_row
            is_first_run = False
            logger.info("完了")

        except Exception as e:
            logger.error("全取得エラー: %s", e)

        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TDnet 更新監視ウォッチャー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date",
        default=None,
        help="監視対象の日付 YYYYMMDD (デフォルト: 今日、指定時は日またがり自動切替なし)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="チェック間隔（秒）(デフォルト: config.ini の値、なければ10)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="出力先ディレクトリ (デフォルト: config.ini の値、なければカレント)",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="PDFダウンロードを行わない",
    )
    parser.add_argument(
        "--keywords",
        default="keywords.txt",
        help="キーワードファイルパス (デフォルト: keywords.txt)",
    )
    parser.add_argument(
        "--notify-config",
        default="notify.ini",
        help="通知設定ファイルパス (デフォルト: notify.ini)",
    )
    args = parser.parse_args()

    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
    console = cfg.getboolean("logging", "console", fallback=False)
    # interval: コマンドライン引数 > config.ini > デフォルト10秒
    interval = args.interval or cfg.getint("watcher", "interval", fallback=10)
    # output_dir: コマンドライン引数 > config.ini > デフォルト"."
    output_dir = args.output_dir or cfg.get("watcher", "output_dir", fallback=".")
    # date未指定なら今日、指定あり→固定（日またがり自動切替なし）
    fixed_date = args.date is not None
    target_date = args.date or date.today().strftime("%Y%m%d")

    log_dir = f"{output_dir}/logs"
    # ログファイル名は実行日（データ取得日ではなく今日の日付）
    dl.setup_logging(log_dir, date.today().strftime("%Y%m%d"), console=console)

    try:
        watch(
            target_date=target_date,
            interval=interval,
            output_dir=output_dir,
            no_pdf=args.no_pdf,
            keywords_path=args.keywords,
            notify_config_path=args.notify_config,
            console_log=console,
            fixed_date=fixed_date,
        )
    except KeyboardInterrupt:
        logger.info("停止しました。")
