"""
TDnet 更新監視ウォッチャー
一覧の先頭行（最新データ）だけを定期チェックし、変化があった時だけ全データを取得します。

使い方:
  python tdnet_watcher.py                          # 今日を5秒間隔でチェック
  python tdnet_watcher.py --date 20260410          # 指定日付
  python tdnet_watcher.py --interval 30            # チェック間隔を30秒に変更（デフォルト5）
  python tdnet_watcher.py --second-offset 10       # チェック秒位置を揃える（デフォルト10）
  python tdnet_watcher.py --no-pdf                 # PDFダウンロードしない
  python tdnet_watcher.py --output-dir ./data      # 出力先を指定
  python tdnet_watcher.py --keywords keywords.txt  # キーワードファイルを指定
  python tdnet_watcher.py --notify-config notify.ini  # 通知設定ファイルを指定
"""

import threading
import time
import logging
import argparse
from datetime import date
import tdnet_downloader as dl
import notifier as nt


logger = logging.getLogger(__name__)


def get_first_page(target_date: str) -> list[dict]:
    """
    1ページ目だけ取得して全行返す。
    更新検出と即時キーワード通知の両方に使う。
    """
    return dl.fetch_list_page(target_date, page=1)


def _rotate_log(log_dir: str, new_date: str, console: bool) -> None:
    """ログハンドラを新しい日付のファイルに切り替える。"""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)
    dl.setup_logging(log_dir, new_date, console=console)


def _sleep_until_next(interval: int, offset: int) -> None:
    """
    次のチェック時刻までスリープする。
    interval秒ごと、毎分の offset 秒の位置にチェックが走るように時刻を揃える。

    例:
      interval=20, offset=10 → :10, :30, :50
      interval=30, offset=10 → :10, :40
      interval=60, offset=10 → 毎分 :10
    """
    now = time.time()
    # 現在より後の、最初の「interval秒境界 + offset」の時刻を求める
    next_time = ((now - offset) // interval + 1) * interval + offset
    sleep_sec = next_time - now
    if sleep_sec > 0:
        time.sleep(sleep_sec)


def watch(
    target_date: str,
    interval: int,
    output_dir: str,
    no_pdf: bool,
    keywords_path: str,
    notify_config_path: str,
    console_log: bool = False,
    fixed_date: bool = False,
    second_offset: int = 10,
) -> None:
    import os
    log_dir = f"{output_dir}/logs"
    pdf_dir = None if no_pdf else f"{output_dir}/pdf"
    csv_path = f"{output_dir}/tdnet_{target_date}.csv"

    notifier         = nt.Notifier(notify_config_path)
    keywords         = nt.load_keywords(keywords_path)
    registered_codes = nt.load_registered_codes()
    if registered_codes:
        logger.info("登録銘柄: %d件 %s", len(registered_codes) // 2,
                    sorted(c for c in registered_codes if len(c) == 4))
    if keywords:
        logger.info("キーワード: %d件 %s", len(keywords), keywords)
    if not registered_codes and not keywords:
        logger.info("登録銘柄・キーワードともに未設定（通知なし）")

    last_first_row: dict | None = None
    known_pdf_urls: set[str] = set()  # 把握済みのPDF URL（新規検出に使用）
    is_first_run = True
    check_count = 0

    logger.info(
        "監視開始: %s | 間隔 %d秒 | 毎分 %02d秒に揃えてチェック | 出力: %s",
        target_date, interval, second_offset, output_dir,
    )

    # 最初のチェックも :offset に揃えて開始する
    _sleep_until_next(interval, second_offset)

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
            # キーワード・登録銘柄も再読み込み
            keywords         = nt.load_keywords(keywords_path)
            registered_codes = nt.load_registered_codes()
            logger.info("新しい日付で監視再開: %s", target_date)

        # キーワード・登録銘柄を毎回読み直す（起動中の変更に即対応）
        keywords         = nt.load_keywords(keywords_path)
        registered_codes = nt.load_registered_codes()

        check_count += 1
        logger.info("チェック #%d", check_count)

        try:
            page1_rows = get_first_page(target_date)
        except Exception as e:
            logger.error("取得エラー: %s", e)
            _sleep_until_next(interval, second_offset)
            continue

        if not page1_rows:
            logger.info("データなし（開示なし、または日付違い）")
            _sleep_until_next(interval, second_offset)
            continue

        first_row = page1_rows[0]

        if first_row == last_first_row:
            logger.info("変化なし（先頭: %s %s %s）",
                        first_row["time"], first_row["code"], first_row["title"][:20])
            _sleep_until_next(interval, second_offset)
            continue

        # 先頭行が変わった
        if is_first_run:
            logger.info("初回取得（ベースライン確立）")
        else:
            logger.info("更新検出！")
            logger.info("  変更前: %s %s %s",
                        last_first_row["time"], last_first_row["code"], last_first_row["title"][:30])
            logger.info("  変更後: %s %s %s",
                        first_row["time"], first_row["code"], first_row["title"][:30])

        try:
            os.makedirs(output_dir, exist_ok=True)

            if not is_first_run:
                # ① 1ページ目の新規行でキーワード検索 → 即通知（全ページ取得を待たない）
                page1_new = [r for r in page1_rows if r["pdf_url"] and r["pdf_url"] not in known_pdf_urls]
                logger.info("1ページ目新規行: %d件 → 即時キーワードチェック", len(page1_new))
                if page1_new and keywords:
                    matches = nt.search(page1_new, keywords, registered_codes)
                    if matches:
                        logger.info("キーワードマッチ: %d件", len(matches))
                        for m in matches:
                            logger.info("  [%s] %s %s「%s」キーワード=%s",
                                        m["time"], m["code"], m["company"],
                                        m["title"][:30], m["matched_keyword"])
                        notifier.send(matches)
                    else:
                        logger.info("キーワードマッチなし")

            # ② 2ページ目以降を取得してCSV保存・PDF DL
            logger.info("残りページ取得中...")
            remaining_rows = dl.fetch_all_pages(target_date, start_page=2)
            rows = page1_rows + remaining_rows
            dl.save_csv(rows, csv_path)

            if is_first_run:
                pdf_rows = rows
                known_pdf_urls = {r["pdf_url"] for r in rows if r["pdf_url"]}
                logger.info("ベースライン記録: %d件", len(known_pdf_urls))
                # 初回は全行をキーワード検索
                if rows and keywords:
                    matches = nt.search(rows, keywords, registered_codes)
                    if matches:
                        logger.info("キーワードマッチ: %d件", len(matches))
                        for m in matches:
                            logger.info("  [%s] %s %s「%s」キーワード=%s",
                                        m["time"], m["code"], m["company"],
                                        m["title"][:30], m["matched_keyword"])
                        notifier.send(matches)
                    else:
                        logger.info("キーワードマッチなし")
            else:
                new_rows = [r for r in rows if r["pdf_url"] and r["pdf_url"] not in known_pdf_urls]
                known_pdf_urls = {r["pdf_url"] for r in rows if r["pdf_url"]}
                pdf_rows = new_rows
                logger.info("新規行合計: %d件", len(new_rows))

            if not no_pdf and pdf_rows:
                # PDFダウンロードは監視ループをブロックしないようバックグラウンドで実行
                threading.Thread(
                    target=dl.download_pdfs, args=(pdf_rows, pdf_dir), daemon=True,
                ).start()

            last_first_row = first_row
            is_first_run = False
            logger.info("完了")

        except Exception as e:
            logger.error("全取得エラー: %s", e)

        _sleep_until_next(interval, second_offset)


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
        help="チェック間隔（秒）(デフォルト: config.ini の値、なければ5)",
    )
    parser.add_argument(
        "--second-offset",
        type=int,
        default=None,
        help="毎分のチェック秒位置 0-59 (デフォルト: config.ini の値、なければ10)",
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
    # interval: コマンドライン引数 > config.ini > デフォルト5秒
    interval = args.interval or cfg.getint("watcher", "interval", fallback=5)
    # second_offset: コマンドライン引数 > config.ini > デフォルト10秒
    second_offset = (
        args.second_offset
        if args.second_offset is not None
        else cfg.getint("watcher", "second_offset", fallback=10)
    )
    if not (0 <= second_offset < 60):
        parser.error("--second-offset は 0〜59 の範囲で指定してください")
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
            second_offset=second_offset,
        )
    except KeyboardInterrupt:
        logger.info("停止しました。")
