"""
tdnet_watcher の即時通知フローをモックで検証するテストスクリプト。

実際にTDnetへアクセスせず、fetch_list_page をモックして
「更新検出 → 1ページ目で即通知 → 残りページ取得」の流れと
所要時間を確認します。

実行:
  python test_watcher.py
"""

import time
import logging
import unittest
from unittest.mock import patch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ダミーデータ
ROW_BASE = {
    "date": "20260601", "time": "15:00", "code": "99999",
    "company": "テスト株式会社", "title": "適時開示テスト",
    "pdf_url": "https://example.com/test_base.pdf", "xbrl_url": "", "place": "東証",
}
ROW_ITOEN = {
    "date": "20260601", "time": "15:30", "code": "25930",
    "company": "伊藤園", "title": "2026年4月期決算短信[日本基準]（連結）",
    "pdf_url": "https://example.com/itoen.pdf", "xbrl_url": "", "place": "東証",
}
ROW_OTHER = {
    "date": "20260601", "time": "15:30", "code": "12345",
    "company": "その他株式会社", "title": "自己株式の取得",
    "pdf_url": "https://example.com/other.pdf", "xbrl_url": "", "place": "東証",
}

# fetch_list_page の呼び出し回数で返すデータを切り替える
call_count = 0

def mock_fetch_list_page(target_date: str, page: int = 1) -> list[dict]:
    global call_count
    call_count += 1

    if page == 1:
        if call_count == 1:
            # 初回チェック: ベースライン（伊藤園なし）
            logger.info("[MOCK] page1 初回（ベースライン）")
            return [ROW_BASE]
        else:
            # 2回目以降: 伊藤園が追加された状態
            logger.info("[MOCK] page1 更新後（伊藤園あり）")
            return [ROW_ITOEN, ROW_OTHER, ROW_BASE]
    elif page == 2:
        logger.info("[MOCK] page2（残りページ）")
        time.sleep(0.3)  # 実際のネットワーク遅延を模擬
        return [
            {**ROW_BASE, "code": "11111", "pdf_url": "https://example.com/p2.pdf"},
        ]
    else:
        return []  # ページ終端


def mock_download_pdfs(rows, pdf_dir):
    logger.info("[MOCK] PDFダウンロード %d件（スキップ）", len(rows))


def mock_analyze(match):
    return None  # analyzer は使わない


class TestWatcher(unittest.TestCase):

    def test_fast_notification(self):
        """更新検出から通知までが全ページ取得より先に来ることを確認する。"""
        global call_count
        call_count = 0

        notify_time = None
        remaining_fetch_time = None

        # Notifier.send をフック
        import notifier as nt
        original_send = nt.Notifier.send

        def hooked_send(self, matches):
            nonlocal notify_time
            notify_time = time.time()
            logger.info("[HOOK] 通知発火！ matches=%d件", len(matches))
            for m in matches:
                logger.info("  → %s %s「%s」", m["code"], m["company"], m["title"][:40])

        # fetch_all_pages をフック（2ページ目以降の取得タイミングを記録）
        import tdnet_downloader as dl
        original_fetch_all = dl.fetch_all_pages

        def hooked_fetch_all(target_date, max_pages=30, start_page=1):
            nonlocal remaining_fetch_time
            if start_page >= 2:
                remaining_fetch_time = time.time()
                logger.info("[HOOK] 残りページ取得開始（start_page=%d）", start_page)
            return original_fetch_all(target_date, max_pages=max_pages, start_page=start_page)

        with patch("tdnet_downloader.fetch_list_page", side_effect=mock_fetch_list_page), \
             patch("tdnet_downloader.download_pdfs", side_effect=mock_download_pdfs), \
             patch.object(nt.Notifier, "send", hooked_send), \
             patch("tdnet_downloader.fetch_all_pages", side_effect=hooked_fetch_all):

            import tdnet_watcher as tw

            # interval=0 で2回だけ回す（初回ベースライン + 1回更新）
            loop_count = 0
            original_sleep = tw._sleep_until_next

            def no_sleep(interval, offset):
                nonlocal loop_count
                loop_count += 1
                # watch()はループ前に1回初期スリープするので、
                # 3回目（=2イテレーション完了後）で終了
                if loop_count >= 3:
                    raise KeyboardInterrupt

            tw._sleep_until_next = no_sleep

            try:
                tw.watch(
                    target_date="20260601",
                    interval=2,
                    output_dir="output_test",
                    no_pdf=False,
                    keywords_path="keywords.txt",
                    notify_config_path="notify.ini",
                    console_log=False,
                    fixed_date=True,
                    second_offset=10,
                )
            except KeyboardInterrupt:
                pass
            finally:
                tw._sleep_until_next = original_sleep

        # 結果を検証
        logger.info("=" * 50)
        if notify_time and remaining_fetch_time:
            diff = remaining_fetch_time - notify_time
            if diff > 0:
                logger.info("結果: 通知が残りページ取得より %.3f秒 早かった ✓", diff)
            else:
                logger.info("結果: 残りページ取得が通知より %.3f秒 早かった ✗", -diff)
            self.assertLess(
                notify_time, remaining_fetch_time,
                "通知は残りページ取得より先に発火すべき"
            )
        elif notify_time is None:
            self.fail("通知が発火しませんでした（キーワードマッチなし？）")
        else:
            logger.info("残りページ取得がなかった（1ページのみ）")

        logger.info("=" * 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
