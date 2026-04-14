"""
TDnet 通知モジュール
キーワード検索と各種通知手段（デスクトップ・Webhook・メール）を提供します。
"""

import configparser
import logging
import os
import smtplib
import subprocess
import unicodedata
import urllib.request
import json
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# キーワード検索
# ---------------------------------------------------------------------------

def load_keywords(path: str) -> list[str]:
    """keywords.txt を読み込む。# 始まり行と空行は無視。"""
    if not os.path.exists(path):
        logger.warning("キーワードファイルが見つかりません: %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f
                if line.strip() and not line.startswith("#")]


def _normalize(text: str) -> str:
    """全角英数字・記号を半角に統一する（比較用）。"""
    return unicodedata.normalize("NFKC", text)


def search(rows: list[dict], keywords: list[str]) -> list[dict]:
    """
    rows の中からキーワードにマッチした行を返す。
    会社名・表題を対象に部分一致で検索。

    カンマ区切りで AND 条件（例: 「決算短信,フジクラ」→両方含む行にマッチ）。
    同一行に複数キーワードが当たっても1件として扱う。
    """
    if not keywords:
        return []
    matches = []
    for row in rows:
        text = _normalize(row.get("company", "") + " " + row.get("title", ""))
        for kw_line in keywords:
            conditions = [_normalize(k.strip()) for k in kw_line.split(",") if k.strip()]
            if all(c in text for c in conditions):
                matches.append({**row, "matched_keyword": kw_line})
                break
    return matches


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, config_path: str = "notify.ini"):
        self._cfg = configparser.ConfigParser()
        if os.path.exists(config_path):
            self._cfg.read(config_path, encoding="utf-8")
        else:
            logger.warning("通知設定ファイルが見つかりません: %s", config_path)

    def _enabled(self, section: str) -> bool:
        return self._cfg.getboolean(section, "enabled", fallback=False)

    def send(self, matches: list[dict]) -> None:
        """マッチした行一覧を各通知手段へ送信する。"""
        if not matches:
            return

        # デスクトップ通知: 1件ずつバルーンを出す（クリックで個別PDF表示）
        if self._enabled("desktop"):
            for m in matches:
                title = f"TDnet: {m['company']}  [{m['matched_keyword']}]"
                body = f"{m['time']}  {m['code']}\n{m['title']}"
                self._desktop(title, body, [m])

        # Webhook・メール: まとめて1通
        summary_title = f"TDnet アラート: {len(matches)}件のキーワードマッチ"
        summary_lines = [
            f"[{r['time']}] {r['code']} {r['company']}"
            f"\n  表題: {r['title']}"
            f"\n  キーワード: {r['matched_keyword']}"
            f"\n  URL: {r['pdf_url']}"
            for r in matches
        ]
        summary_body = "\n\n".join(summary_lines)

        if self._enabled("webhook"):
            self._webhook(summary_title, summary_body, matches)

        if self._enabled("email"):
            self._email(summary_title, summary_body)

    # ------------------------------------------------------------------
    # デスクトップ通知（Windows・インストール不要）
    # ------------------------------------------------------------------

    def _desktop(self, title: str, body: str, matches: list[dict]) -> None:
        import xml.sax.saxutils as saxutils

        t_xml = saxutils.escape(title)
        b_xml = saxutils.escape(body[:200].replace("\n", " / "))
        url = next((m["pdf_url"] for m in matches if m.get("pdf_url")), "")
        launch_attr = f' activationType="protocol" launch="{saxutils.escape(url)}"' if url else ""

        xml_str = (
            f'<toast{launch_attr}>'
            f'<visual><binding template="ToastGeneric">'
            f'<text>{t_xml}</text>'
            f'<text>{b_xml}</text>'
            f'</binding></visual>'
            f'</toast>'
        )
        ps_xml = xml_str.replace("'", "''")

        # PowerShell の登録済み AppUserModelID を使用（未登録アプリでも通知を表示するため）
        app_id = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$xml.LoadXml('{ps_xml}');"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
            f'$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{app_id}");'
            "$notifier.Show($toast)"
        )
        try:
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-NonInteractive", "-Command", script],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.info("デスクトップ通知送信（クリックでPDF表示）")
        except Exception as e:
            logger.error("デスクトップ通知エラー: %s", e)

    # ------------------------------------------------------------------
    # Webhook（Slack / Discord / 汎用）
    # ------------------------------------------------------------------

    def _webhook(self, title: str, body: str, matches: list[dict]) -> None:
        url = self._cfg.get("webhook", "url", fallback="").strip()
        if not url:
            logger.warning("Webhook URL が設定されていません")
            return

        # Slack と Discord は両方 {"text": "..."} で動作する
        payload = json.dumps({"text": f"*{title}*\n```{body}```"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
            logger.info("Webhook通知送信")
        except Exception as e:
            logger.error("Webhook通知エラー: %s", e)

    # ------------------------------------------------------------------
    # メール
    # ------------------------------------------------------------------

    def _email(self, title: str, body: str) -> None:
        host = self._cfg.get("email", "smtp_host", fallback="")
        port = self._cfg.getint("email", "smtp_port", fallback=587)
        user = self._cfg.get("email", "smtp_user", fallback="")
        password = self._cfg.get("email", "smtp_password", fallback="")
        to = self._cfg.get("email", "to", fallback="")

        if not all([host, user, password, to]):
            logger.warning("メール設定が不完全です")
            return

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = user
        msg["To"] = to

        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(msg)
            logger.info("メール通知送信 -> %s", to)
        except Exception as e:
            logger.error("メール通知エラー: %s", e)
