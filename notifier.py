"""
TDnet 通知モジュール
キーワード検索と各種通知処理（デスクトップ・Webhook・メール）を提供します。

analyzer.py と連携し、登録済み銘柄の開示時には業績比較レポートを
デスクトップ通知に含めます。
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

def load_registered_codes(data_dir: str | None = None) -> set[str]:
    """
    consensus/ に登録済みの証券コード一覧を返す（4桁・5桁両対応）。
    これを使うことで keywords.txt への追加なしに登録銘柄を自動監視できる。
    """
    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consensus")
    if not os.path.exists(data_dir):
        return set()
    codes: set[str] = set()
    for fname in os.listdir(data_dir):
        if fname.endswith(".json"):
            code4 = fname[:-5]
            codes.add(code4)
            codes.add(code4 + "0")  # TDnetは5桁コード
    return codes


def load_keywords(path: str) -> list[str]:
    """keywords.txt を読み込む。頭末の空白と空行は無視。"""
    if not os.path.exists(path):
        logger.warning("キーワードファイルが見つかりません: %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f
                if line.strip() and not line.startswith("#")]


def _normalize(text: str) -> str:
    """全角英数字・記号を半角に統一する（検索用）。"""
    return unicodedata.normalize("NFKC", text)


def search(
    rows: list[dict],
    keywords: list[str],
    registered_codes: set[str] | None = None,
) -> list[dict]:
    """
    rows の中からマッチした行を返す。

    優先順①: registered_codes に含まれる証券コードは自動マッチ
             （本決算・四半期・業績予想修正すべてをキーワード登録なしで検知）
    優先順②: keywords.txt によるキーワードマッチ
             （未登録銘柄の監視や特定ワードを含む全開示の監視に使用）

    カンマ区切りで AND 条件。同一行に複数キーワードが当たっても1件として扱う。
    """
    matches = []
    for row in rows:
        # ① 登録銘柄はコードで自動マッチ
        if registered_codes and row.get("code") in registered_codes:
            matches.append({**row, "matched_keyword": "(登録銘柄)"})
            continue
        # ② keywords.txt によるキーワードマッチ
        if not keywords:
            continue
        text = _normalize(row.get("company", "") + " " + row.get("title", ""))
        for kw_line in keywords:
            conditions = [_normalize(k.strip()) for k in kw_line.split(",") if k.strip()]
            if all(c in text for c in conditions):
                matches.append({**row, "matched_keyword": kw_line})
                break
    return matches


# ---------------------------------------------------------------------------
# 業績分析（analyzer.py 連携）
# ---------------------------------------------------------------------------

def _try_analyze(match: dict) -> str | None:
    """
    analyzer.py が存在すれば analyze() を呼び出してレポートを返す。
    存在しない・エラーの場合は None を返す（通知は継続する）。
    """
    try:
        import analyzer
        return analyzer.analyze(match)
    except ImportError:
        logger.debug("analyzer.py が見つかりません。業績比較をスキップします。")
        return None
    except Exception as e:
        logger.error("analyzer エラー: %s", e)
        return None


# ---------------------------------------------------------------------------
# 業績レポートサマリー（トースト通知用）
# ---------------------------------------------------------------------------

def _reorder_report_for_display(report: str) -> str:
    """
    HTMLレポート表示用に節の順序を入れ替える。
    四半期累計実績（📈）と通期予想vsコンセンサス（📅、四半期開示に伴う修正時のみ付記）
    が両方含まれる場合、通期予想セクションを先頭に表示する。
    トースト通知の要約解析（_compact_report_summary）はこの関数を経由しない元の
    report文字列を使うため、ここでの並び替えは影響しない。
    """
    import re

    if "📈" not in report or "📅" not in report:
        return report

    sections = re.split(r'(?=━+\n[📈📅])', report)
    forecast_sections, other_sections = [], []
    for s in sections:
        lines = s.splitlines()
        if len(lines) > 1 and lines[1].startswith("📅"):
            forecast_sections.append(s)
        else:
            other_sections.append(s)
    return "".join(forecast_sections + other_sections)


def _compact_report_summary(report: str) -> tuple[str, str, str]:
    """
    報告種別を自動判定してトースト用3行サマリーを返す。
      📊 業績比較レポート → 本決算: 会社予想比・コンセ比・翌期
      📈 XQ累計実績      → 四半期: 前年同期比・会予進捗・コンセ進捗
      🔄 業績予想修正     → 修正率・コンセ比
    """
    if report.startswith("📈"):
        return _compact_quarterly_summary(report)
    if report.startswith("★"):
        return _compact_revision_summary(report)
    return _compact_annual_summary(report)


def _compact_annual_summary(report: str) -> tuple[str, str, str]:
    """本決算: (会社予想比行, コンセンサス比行, 翌期行)"""
    import re
    short_labels = {"売上高": "売上", "営業利益": "営利", "経常利益": "経常", "純利益": "純利"}
    score_map    = {"売上": 3, "営利": 1, "経常": 1, "純利": 2}

    sections         = re.split(r'(?=📅)', report)
    current_section  = sections[0]
    forecast_section = sections[1] if len(sections) > 1 else ""

    def _extract(section: str, col_index: int) -> list[str]:
        parts = []
        for line in section.splitlines():
            s = line.strip()
            for full, short in short_labels.items():
                if s.startswith(full):
                    pcts = re.findall(r'[△▼]\d+\.\d+%', s)
                    if col_index < len(pcts):
                        parts.append(f"{short}{pcts[col_index]}")
                    break
        return parts

    def _score(parts: list[str]) -> int:
        s = 0
        for p in parts:
            for lbl, pts in score_map.items():
                if p.startswith(lbl):
                    s += pts if "△" in p else -pts
                    break
        return s

    def _line(prefix: str, parts: list[str]) -> str:
        if not parts:
            return ""
        sc = _score(parts)
        return f"{prefix}  " + "  ".join(parts) + f"  【{'+' if sc >= 0 else ''}{sc}】"

    return (
        _line("会予", _extract(current_section, 0)),
        _line("市場", _extract(current_section, 1)),
        _line("翌期", _extract(forecast_section, 0)),
    )


def _compact_quarterly_summary(report: str) -> tuple[str, str, str]:
    """四半期: (前年同期比行, 会予進捗行, コンセ進捗行)"""
    import re
    short_labels = {"売上高": "売上", "営業利益": "営利", "経常利益": "経常", "純利益": "純利"}

    def _extract_col(col_index: int) -> list[str]:
        parts = []
        for line in report.splitlines():
            s = line.strip()
            for full, short in short_labels.items():
                if s.startswith(full):
                    # 数値列: 累計実績 前年同期比 会予進捗 コンセ進捗
                    nums = re.findall(r'[△▼]?\d+\.?\d*%', s)
                    if col_index < len(nums):
                        parts.append(f"{short}{nums[col_index]}")
                    break
        return parts

    def _line(prefix: str, parts: list[str]) -> str:
        return (f"{prefix}  " + "  ".join(parts)) if parts else ""

    return (
        _line("前年", _extract_col(0)),   # 前年同期比
        _line("会進", _extract_col(1)),   # 会社予想進捗
        _line("コン", _extract_col(2)),   # コンセンサス進捗
    )


def _compact_revision_summary(report: str) -> tuple[str, str, str]:
    """業績予想修正: (修正率行, コンセ比行, '')"""
    import re
    short_labels = {"売上高": "売上", "営業利益": "営利", "経常利益": "経常", "純利益": "純利"}
    score_map    = {"売上": 3, "営利": 1, "経常": 1, "純利": 2}

    # ヘッダー部の修正率サマリー行は項目ラベルと%値を含むため、
    # 詳細テーブル本体（「項目」見出し行より下）だけを対象にする
    lines = report.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("項目")), -1)
    table_lines = lines[header_idx + 1:]

    def _extract_col(col_index: int) -> list[str]:
        parts = []
        for line in table_lines:
            s = line.strip()
            for full, short in short_labels.items():
                if s.startswith(full):
                    pcts = re.findall(r'[△▼]\d+\.\d+%', s)
                    if col_index < len(pcts):
                        parts.append(f"{short}{pcts[col_index]}")
                    break
        return parts

    def _score(parts: list[str]) -> int:
        s = 0
        for p in parts:
            for lbl, pts in score_map.items():
                if p.startswith(lbl):
                    s += pts if "△" in p else -pts
                    break
        return s

    def _line(prefix: str, parts: list[str]) -> str:
        if not parts:
            return ""
        sc = _score(parts)
        return f"{prefix}  " + "  ".join(parts) + f"  【{'+' if sc >= 0 else ''}{sc}】"

    return (
        _line("修正", _extract_col(0)),   # 修正率
        _line("市場", _extract_col(1)),   # コンセンサス比
        "",
    )


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
        """マッチした行一覧を各通知先へ送信する。"""
        if not matches:
            return

        # 同一銘柄・同時刻に複数開示（決算短信＋業績予想修正のお知らせ等）が
        # 並ぶことがあるため、レポートは証券コードではなく行ごとに紐付ける。
        for m in matches:
            report = _try_analyze(m)
            if report:
                m["_report"] = report
                logger.info("業績比較レポート生成: %s", m.get("code", ""))

        if self._enabled("desktop"):
            for m in matches:
                title = f"TDnet: {m['company']}  [{m['matched_keyword']}]"
                body  = f"{m['time']}  {m['code']}\n{m['title']}"
                report = m.get("_report")
                self._desktop(title, body, [m], report=report)

        summary_title = f"TDnet アラート: {len(matches)}件のキーワードマッチ"
        summary_parts = []
        for r in matches:
            part = (
                f"[{r['time']}] {r['code']} {r['company']}"
                f"\n  タイトル: {r['title']}"
                f"\n  キーワード: {r['matched_keyword']}"
                f"\n  URL: {r['pdf_url']}"
            )
            report = r.get("_report")
            if report:
                part += f"\n\n{_reorder_report_for_display(report)}"
            summary_parts.append(part)

        summary_body = "\n\n" + ("─" * 40 + "\n\n").join(summary_parts)

        if self._enabled("webhook"):
            self._webhook(summary_title, summary_body, matches)

        if self._enabled("email"):
            self._email(summary_title, summary_body)

    # ------------------------------------------------------------------
    # デスクトップ通知（Windows・インストール不要）
    # ------------------------------------------------------------------

    def _desktop(self, title: str, body: str, matches: list[dict], report: str | None = None) -> None:
        import xml.sax.saxutils as saxutils
        import datetime

        t_xml = saxutils.escape(title)
        b_xml = saxutils.escape(body[:200].replace("\n", " / "))

        # レポートがある場合はHTMLファイルに保存してクリックで開く
        if report:
            m0 = matches[0]
            out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            code = m0.get("code", "unknown")
            report_path = os.path.join(out_dir, f"report_{code}_{ts}.html")
            pdf_url = m0.get("pdf_url", "")
            report_display = _reorder_report_for_display(report)
            report_html = report_display.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            html = (
                "<!DOCTYPE html><html><head>"
                '<meta charset="utf-8">'
                '<style>body{font-family:monospace;font-size:14px;padding:20px;line-height:1.8}'
                'h2{font-size:16px} a{color:#0066cc}</style>'
                f"</head><body>"
                f"<h2>{saxutils.escape(title)}</h2>"
                f"<p>{saxutils.escape(body).replace(chr(10), '<br>')}</p>"
            )
            if pdf_url:
                html += f'<p><a href="{saxutils.escape(pdf_url)}" target="_blank">決算短信PDFを開く</a></p>'
            html += f"<hr><pre>{report_html}</pre></body></html>"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(html)
            file_uri = "file:///" + report_path.replace("\\", "/").replace(" ", "%20")
            launch_attr = f' activationType="protocol" launch="{saxutils.escape(file_uri)}"'
        else:
            url = next((m["pdf_url"] for m in matches if m.get("pdf_url")), "")
            launch_attr = f' activationType="protocol" launch="{saxutils.escape(url)}"' if url else ""

        extra_xml = ""
        if report:
            c_line, n_line, f_line = _compact_report_summary(report)
            for line in (c_line, n_line, f_line):
                if line:
                    extra_xml += f"<text>{saxutils.escape(line)}</text>"

        xml_str = (
            f'<toast{launch_attr}>'
            f'<visual><binding template="ToastGeneric">'
            f'<text>{t_xml}</text>'
            f'<text>{b_xml}</text>'
            f'{extra_xml}'
            f'</binding></visual>'
            f'</toast>'
        )
        ps_xml = xml_str.replace("'", "''")

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
            logger.info("デスクトップ通知送信（クリックでPDF表示）%s", "＋業績レポート" if report else "")
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
        host     = self._cfg.get("email", "smtp_host",     fallback="")
        port     = self._cfg.getint("email", "smtp_port",  fallback=587)
        user     = self._cfg.get("email", "smtp_user",     fallback="")
        password = self._cfg.get("email", "smtp_password", fallback="")
        to       = self._cfg.get("email", "to",            fallback="")

        if not all([host, user, password, to]):
            logger.warning("メール設定が不完全です")
            return

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"]    = user
        msg["To"]      = to

        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(msg)
            logger.info("メール通知送信 -> %s", to)
        except Exception as e:
            logger.error("メール通知エラー: %s", e)
