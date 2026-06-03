"""
TDnet 開示 XBRL 解析・業績比較モジュール

対応開示種別:
  本決算      : 実績 vs 会社予想・コンセンサス乖離率
  四半期(Q1〜Q3): 累計実績の前年同期比・通期会社予想/コンセンサス進捗率
  業績予想修正  : 修正率(修正後 vs 修正前) + コンセンサス乖離率

使い方（単体テスト）:
  python analyzer.py --code 2593 --xbrl-url https://... --title "2026年4月期 本決算"
  python analyzer.py --code 2593 --xbrl-url https://... --title "2027年4月期 第1四半期決算短信"
  python analyzer.py --code 2593 --xbrl-url https://... --title "2027年4月期 業績予想の修正に関するお知らせ"
"""

import io
import json
import logging
import os
import re
import urllib.request
import zipfile

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "consensus")

ITEMS = [
    ("売上高",   "sales"),
    ("営業利益", "operating_income"),
    ("経常利益", "ordinary_income"),
    ("純利益",   "net_income"),
    ("1株利益",  "eps"),
    ("配当",     "dividend"),
]
ITEM_KEYS   = [k for _, k in ITEMS]
ITEM_LABELS = {k: label for label, k in ITEMS}

# ---------------------------------------------------------------------------
# XBRLタグ候補
# ---------------------------------------------------------------------------

XBRL_TAG_MAP = {
    "sales": [
        "OperatingRevenuesIFRS",
        "NetSalesAndOperatingRevenues",
        "OperatingRevenues",
        "NetSales",
        "Revenues",
    ],
    "operating_income": [
        "OperatingIncomeIFRS",
        "OperatingIncome",
        "OperatingProfit",
    ],
    "ordinary_income": [
        "ProfitBeforeTaxIFRS",
        "OrdinaryIncome",
        "OrdinaryProfit",
    ],
    "net_income": [
        "ProfitAttributableToOwnersOfParentIFRS",
        "ProfitAttributableToOwnersOfParent",
        "ProfitIFRS",
        "NetIncome",
        "Profit",
    ],
    "eps": [
        "BasicEarningsPerShareIFRS",
        "BasicEarningsLossPerShare",
        "EarningsPerShare",
        "NetIncomePerShare",
    ],
    "dividend": [
        "DividendPerShare",
        "AnnualDividendPerShare",
        "DividendsPerShare",
    ],
}

NON_CONSOLIDATED_KEYWORDS = ["NonConsolidated", "nonConsolidated"]

# ---------------------------------------------------------------------------
# XBRLコンテキスト定義
# ---------------------------------------------------------------------------

# 本決算: 当期実績
ANNUAL_RESULT_CONTEXTS = [
    "CurrentYearDuration_ConsolidatedMember_ResultMember",
    "CurrentYearDuration_ConsolidatedMember",
    "CurrentYearDuration",
    "CurrentYearInstant_ConsolidatedMember",
    "CurrentYearInstant",
    "FilingDateInstant",
    "PriorYearDuration",
]

# 本決算: 翌期会社予想
NEXT_YEAR_FORECAST_CONTEXTS = [
    "NextYearDuration_ConsolidatedMember_ForecastMember",
    "NextYearDuration_ConsolidatedMember",
    "NextYearDuration",
    "NextYearInstant_ConsolidatedMember",
    "NextYearInstant",
]

# 四半期: 通期会社予想（四半期XBRLに含まれる）
ANNUAL_FORECAST_CONTEXTS = [
    "CurrentYearDuration_ConsolidatedMember_ForecastMember",
    "CurrentYearDuration_ForecastMember",
    "CurrentFiscalYearDuration_ConsolidatedMember_ForecastMember",
    "CurrentFiscalYearDuration_ForecastMember",
]

# 四半期: 当期累計・前年同期
QUARTERLY_CONTEXTS = {
    "q1": {
        "current": [
            "AccumulatedQ1Duration_ConsolidatedMember_ResultMember",
            "AccumulatedQ1Duration_ConsolidatedMember",
            "AccumulatedQ1Duration_ResultMember",
            "AccumulatedQ1Duration",
            "Q1YTDDuration_ConsolidatedMember_ResultMember",
            "Q1YTDDuration_ConsolidatedMember",
            "Q1YTDDuration",
        ],
        "prior": [
            "Prior1YearAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
            "Prior1YearAccumulatedQ1Duration_ConsolidatedMember",
            "Prior1YearAccumulatedQ1Duration",
            "Prior1YearQ1YTDDuration_ConsolidatedMember",
            "Prior1YearQ1YTDDuration",
        ],
    },
    "q2": {
        "current": [
            "AccumulatedQ2Duration_ConsolidatedMember_ResultMember",
            "AccumulatedQ2Duration_ConsolidatedMember",
            "AccumulatedQ2Duration",
            "HalfYearDuration_ConsolidatedMember_ResultMember",
            "HalfYearDuration_ConsolidatedMember",
            "HalfYearDuration",
        ],
        "prior": [
            "Prior1YearAccumulatedQ2Duration_ConsolidatedMember_ResultMember",
            "Prior1YearAccumulatedQ2Duration_ConsolidatedMember",
            "Prior1YearAccumulatedQ2Duration",
            "Prior1YearHalfYearDuration_ConsolidatedMember_ResultMember",
            "Prior1YearHalfYearDuration_ConsolidatedMember",
            "Prior1YearHalfYearDuration",
        ],
    },
    "q3": {
        "current": [
            "AccumulatedQ3Duration_ConsolidatedMember_ResultMember",
            "AccumulatedQ3Duration_ConsolidatedMember",
            "AccumulatedQ3Duration",
        ],
        "prior": [
            "Prior1YearAccumulatedQ3Duration_ConsolidatedMember_ResultMember",
            "Prior1YearAccumulatedQ3Duration_ConsolidatedMember",
            "Prior1YearAccumulatedQ3Duration",
        ],
    },
}

# 業績予想修正: 修正後・修正前
REVISED_FORECAST_CONTEXTS = [
    "CurrentYearDuration_ConsolidatedMember_RevisedForecastMember",
    "CurrentYearDuration_RevisedForecastMember",
    "CurrentYearDuration_ConsolidatedMember_ForecastMember",  # fallback
    "CurrentYearDuration_ForecastMember",
]

PREVIOUS_FORECAST_CONTEXTS = [
    "CurrentYearDuration_ConsolidatedMember_PreviousForecastMember",
    "CurrentYearDuration_PreviousForecastMember",
    "CurrentYearDuration_ConsolidatedMember_BeforeRevisionForecastMember",
    "CurrentYearDuration_BeforeRevisionForecastMember",
]


# ---------------------------------------------------------------------------
# 登録データ読み込み
# ---------------------------------------------------------------------------

def _load_consensus(code: str) -> dict | None:
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]
    path = os.path.join(DATA_DIR, f"{code}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _extract_year_from_title(title: str) -> str | None:
    m = re.search(r"(\d{4})年(\d{1,2})月", title)
    if m:
        return f"{m.group(1)}年{m.group(2).zfill(2)}月"
    m = re.search(r"(\d{4})年度", title)
    if m:
        return f"{m.group(1)}年03月"
    return None


def _next_fiscal_year(year_str: str) -> str | None:
    m = re.match(r"(\d{4})年(\d{2})月", year_str)
    if not m:
        return None
    return f"{int(m.group(1)) + 1}年{m.group(2)}月"


# ---------------------------------------------------------------------------
# 開示種別判定
# ---------------------------------------------------------------------------

def _detect_disclosure_type(title: str) -> str:
    """
    開示タイトルから種別を返す。
      'annual'   : 本決算
      'q1'〜'q3' : 四半期
      'revision' : 業績予想修正
    """
    if re.search(r'業績予想.*修正|修正.*業績予想|業績.{0,10}修正', title):
        return "revision"
    if re.search(r'第[1１一]四半期', title):
        return "q1"
    if re.search(r'第[2２二]四半期|中間決算|中間報告', title):
        return "q2"
    if re.search(r'第[3３三]四半期', title):
        return "q3"
    return "annual"


_Q_LABEL = {"q1": "第1Q", "q2": "第2Q", "q3": "第3Q"}


# ---------------------------------------------------------------------------
# XBRL取得・解析
# ---------------------------------------------------------------------------

def _fetch_xbrl_zip(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _extract_xbrl_from_zip(data: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xbrl_files = [n for n in zf.namelist() if n.endswith(".xbrl") or n.endswith("-ixbrl.htm")]
        if not xbrl_files:
            logger.warning("ZIPにXBRLファイルが見つかりません")
            return None
        xbrl_files.sort(key=lambda x: len(x))
        with zf.open(xbrl_files[0]) as f:
            return f.read().decode("utf-8", errors="replace")


def _parse_xbrl_values(xbrl_text: str, ctx_priority: list[str]) -> dict[str, float]:
    """指定コンテキスト優先順位で各指標を抽出する。"""
    results = {}

    def is_non_consolidated(ctx: str) -> bool:
        return any(kw in ctx for kw in NON_CONSOLIDATED_KEYWORDS)

    def context_rank(ctx: str) -> float:
        penalty = len(ctx_priority) * 100 if is_non_consolidated(ctx) else 0
        for i, pat in enumerate(ctx_priority):
            if ctx == pat:
                return penalty + i
            if ctx.startswith(pat + "_") or ctx.startswith(pat + "-"):
                return penalty + i + 0.5
        return penalty + len(ctx_priority) * 10

    def in_ctx_priority(ctx: str) -> bool:
        return any(ctx == p or ctx.startswith(p + "_") or ctx.startswith(p + "-")
                   for p in ctx_priority)

    # iXBRL プリパース
    ixbrl_map: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(r'<ix:nonFraction\b([^>]+)>([^<]*)', xbrl_text, re.IGNORECASE):
        attrs, raw_val = m.group(1), m.group(2).strip()
        name_m = re.search(r'\bname="(?:[^"]*:)?([^"]+)"', attrs)
        ctx_m  = re.search(r'\bcontextRef="([^"]*)"', attrs)
        if not name_m or not ctx_m:
            continue
        ixbrl_map.setdefault(name_m.group(1).lower(), []).append((ctx_m.group(1), raw_val))

    for key, tag_candidates in XBRL_TAG_MAP.items():
        best_val: float | None = None
        best_rank = float("inf")
        best_info: tuple[str, str, str] = ("", "", "")

        for tag_idx, tag in enumerate(tag_candidates):
            std_matches = re.findall(
                rf'<[^>]*?{re.escape(tag)}[^>]*contextRef="([^"]*)"[^>]*>([^<]*)<',
                xbrl_text, re.IGNORECASE,
            )
            ix_matches = ixbrl_map.get(tag.lower(), [])
            all_matches = [(c, v) for c, v in std_matches + ix_matches if in_ctx_priority(c)]

            for ctx, val in all_matches:
                val = val.strip().replace(",", "")
                if not val:
                    continue
                try:
                    fval = float(val)
                except ValueError:
                    continue
                rank = context_rank(ctx) + tag_idx * 0.001
                if rank < best_rank:
                    best_rank = rank
                    best_val = fval
                    best_info = (tag, ctx, val)

        if best_val is not None:
            results[key] = best_val
            tag, ctx, val = best_info
            logger.info("XBRL抽出: %s = %s (tag=%s, ctx=%s, nonConsol=%s)",
                        key, val, tag, ctx, is_non_consolidated(ctx))

    return results


def _download_xbrl(xbrl_url: str) -> str | None:
    logger.info("XBRL取得: %s", xbrl_url)
    try:
        return _extract_xbrl_from_zip(_fetch_xbrl_zip(xbrl_url))
    except Exception as e:
        logger.error("XBRL取得エラー: %s", e)
        return None


# ---------------------------------------------------------------------------
# 計算ユーティリティ
# ---------------------------------------------------------------------------

def _divergence(actual: float, forecast: float) -> float | None:
    if forecast == 0:
        return None
    return (actual - forecast) / abs(forecast) * 100


def _progress(actual: float, forecast: float) -> float | None:
    if forecast == 0:
        return None
    return actual / abs(forecast) * 100


def _fmt_val(v: float | None, key: str) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}" if key in ("eps", "dividend") else f"{v:,.0f}"


def _fmt_pct(pct: float | None, show_sign: bool = True) -> str:
    if pct is None:
        return "    -  "
    if show_sign:
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}%"
    return f"{pct:.1f}%"


# ---------------------------------------------------------------------------
# 本決算レポート
# ---------------------------------------------------------------------------

def build_report(match: dict, actuals: dict, consensus_data: dict, year: str) -> str:
    entry        = consensus_data["years"][year]
    company_fc   = entry.get("company_forecast", {})
    consensus_fc = entry.get("consensus_forecast", {})
    company_name = consensus_data.get("company", match.get("company", ""))

    lines = [
        "━" * 56,
        f"📊 業績比較レポート: {company_name} [{match.get('code', '')}]",
        f"   {match.get('time', '')}  {match.get('title', '')[:40]}",
        f"   対象年度: {year}",
        "━" * 56,
        f"{'項目':<10} {'実績':>12} {'会社予想比':>10} {'コンセンサス比':>14}",
        "─" * 56,
    ]
    for key in ITEM_KEYS:
        label   = ITEM_LABELS[key]
        actual  = actuals.get(key)
        c_pct   = _divergence(actual, company_fc.get(key))   if actual is not None else None
        n_pct   = _divergence(actual, consensus_fc.get(key)) if actual is not None else None
        lines.append(f"{label:<10} {_fmt_val(actual, key):>12} {_fmt_pct(c_pct):>10} {_fmt_pct(n_pct):>14}")

    lines += ["─" * 56, f"PDF: {match.get('pdf_url', '-')}", "━" * 56]
    return "\n".join(lines)


def build_forecast_report(match: dict, forecasts: dict, consensus_data: dict, next_year: str) -> str:
    entry        = consensus_data["years"][next_year]
    consensus_fc = entry.get("consensus_forecast", {})
    company_name = consensus_data.get("company", match.get("company", ""))

    lines = [
        "━" * 56,
        f"📅 翌期予想 vs コンセンサス: {company_name} [{match.get('code', '')}]",
        f"   対象年度: {next_year}",
        "━" * 56,
        f"{'項目':<10} {'会社予想':>12} {'コンセンサス比':>14}",
        "─" * 56,
    ]
    for key in ITEM_KEYS:
        fc_val  = forecasts.get(key)
        n_pct   = _divergence(fc_val, consensus_fc.get(key)) if fc_val is not None else None
        lines.append(f"{ITEM_LABELS[key]:<10} {_fmt_val(fc_val, key):>12} {_fmt_pct(n_pct):>14}")

    lines += ["─" * 56, "━" * 56]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 四半期レポート
# ---------------------------------------------------------------------------

def build_quarterly_report(
    match: dict,
    actuals: dict,
    prior: dict,
    annual_fc_xbrl: dict,
    consensus_data: dict,
    year: str,
    qtype: str,
) -> str:
    """
    四半期累計実績レポート。
    annual_fc_xbrl : 四半期XBRL内の通期会社予想（前年同期比はXBRL由来なので登録不要）
    """
    qlabel       = _Q_LABEL.get(qtype, qtype.upper())
    entry        = consensus_data["years"].get(year, {})
    consensus_fc = entry.get("consensus_forecast", {})
    company_name = consensus_data.get("company", match.get("company", ""))

    lines = [
        "━" * 64,
        f"📈 {qlabel}累計実績: {company_name} [{match.get('code', '')}]",
        f"   {match.get('time', '')}  {match.get('title', '')[:45]}",
        f"   対象年度: {year}",
        "━" * 64,
        f"{'項目':<10} {qlabel+'累計':>10} {'前年同期比':>10} {'会予進捗':>9} {'コンセ進捗':>10}",
        "─" * 64,
    ]
    for key in ITEM_KEYS:
        actual    = actuals.get(key)
        prior_val = prior.get(key)
        fc_xbrl   = annual_fc_xbrl.get(key)
        fc_con    = consensus_fc.get(key)

        yoy_pct  = _divergence(actual, prior_val) if actual is not None and prior_val is not None else None
        c_prog   = _progress(actual, fc_xbrl)     if actual is not None and fc_xbrl   is not None else None
        n_prog   = _progress(actual, fc_con)      if actual is not None and fc_con     is not None else None

        lines.append(
            f"{ITEM_LABELS[key]:<10} {_fmt_val(actual, key):>10}"
            f" {_fmt_pct(yoy_pct):>10}"
            f" {_fmt_pct(c_prog, show_sign=False):>9}"
            f" {_fmt_pct(n_prog, show_sign=False):>10}"
        )

    lines += ["─" * 64, f"PDF: {match.get('pdf_url', '-')}", "━" * 64]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 業績予想修正レポート
# ---------------------------------------------------------------------------

def build_revision_report(
    match: dict,
    revised: dict,
    previous: dict,
    consensus_data: dict,
    year: str,
) -> str:
    entry        = consensus_data["years"].get(year, {})
    consensus_fc = entry.get("consensus_forecast", {})
    company_name = consensus_data.get("company", match.get("company", ""))

    lines = [
        "━" * 62,
        f"🔄 業績予想修正: {company_name} [{match.get('code', '')}]",
        f"   {match.get('time', '')}  {match.get('title', '')[:45]}",
        f"   対象年度: {year}",
        "━" * 62,
        f"{'項目':<10} {'修正後予想':>12} {'修正前予想':>12} {'修正率':>8} {'コンセンサス比':>14}",
        "─" * 62,
    ]
    for key in ITEM_KEYS:
        rev  = revised.get(key)
        prev = previous.get(key)
        con  = consensus_fc.get(key)

        chg_pct = _divergence(rev, prev) if rev is not None and prev is not None else None
        con_pct = _divergence(rev, con)  if rev is not None and con  is not None else None

        lines.append(
            f"{ITEM_LABELS[key]:<10} {_fmt_val(rev, key):>12} {_fmt_val(prev, key):>12}"
            f" {_fmt_pct(chg_pct):>8} {_fmt_pct(con_pct):>14}"
        )

    lines += ["─" * 62, f"PDF: {match.get('pdf_url', '-')}", "━" * 62]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メインエントリ（notifier.py から呼ばれる）
# ---------------------------------------------------------------------------

def analyze(match: dict) -> str | None:
    code           = match.get("code", "")
    consensus_data = _load_consensus(code)
    if consensus_data is None:
        logger.info("登録データなし: %s", code)
        return None

    xbrl_url = match.get("xbrl_url", "")
    if not xbrl_url:
        logger.info("XBRL URLなし: %s %s", code, match.get("title", ""))
        return None

    title      = match.get("title", "")
    title_year = _extract_year_from_title(title)
    if title_year is None:
        logger.info("タイトルから年度を抽出できません: %s", title)
        return None

    years_data = consensus_data.get("years", {})
    dtype      = _detect_disclosure_type(title)
    logger.info("開示種別: %s  年度: %s", dtype, title_year)

    xbrl_text = _download_xbrl(xbrl_url)
    if not xbrl_text:
        return (
            f"⚠ {consensus_data.get('company', '')} [{code}]: XBRL取得失敗\n"
            f"   PDF確認: {match.get('pdf_url', '')}"
        )

    # ── 本決算 ──────────────────────────────────────────────────────────────
    if dtype == "annual":
        year = title_year if title_year in years_data else None
        if year is None:
            logger.info("登録データに該当年度なし: %s (登録: %s)", title_year, list(years_data.keys()))
            return None

        logger.info("年度マッチ: %s", year)
        actuals = _parse_xbrl_values(xbrl_text, ANNUAL_RESULT_CONTEXTS)
        logger.info("XBRL解析完了（実績）: %d項目", len(actuals))
        report  = build_report(match, actuals, consensus_data, year)

        next_year = _next_fiscal_year(year)
        if next_year and next_year in years_data and years_data[next_year].get("consensus_forecast"):
            forecasts = _parse_xbrl_values(xbrl_text, NEXT_YEAR_FORECAST_CONTEXTS)
            logger.info("XBRL解析完了（翌期予想）: %d項目", len(forecasts))
            if forecasts:
                report += "\n" + build_forecast_report(match, forecasts, consensus_data, next_year)

        return report

    # ── 四半期 ──────────────────────────────────────────────────────────────
    if dtype in ("q1", "q2", "q3"):
        year = title_year if title_year in years_data else None
        if year is None:
            logger.info("登録データに該当年度なし: %s (登録: %s)", title_year, list(years_data.keys()))
            return None

        logger.info("年度マッチ: %s  四半期: %s", year, dtype)
        q_ctx = QUARTERLY_CONTEXTS[dtype]

        actuals       = _parse_xbrl_values(xbrl_text, q_ctx["current"])
        logger.info("XBRL解析完了（当期累計）: %d項目", len(actuals))

        prior         = _parse_xbrl_values(xbrl_text, q_ctx["prior"])
        logger.info("XBRL解析完了（前年同期）: %d項目", len(prior))

        annual_fc_xbrl = _parse_xbrl_values(xbrl_text, ANNUAL_FORECAST_CONTEXTS)
        logger.info("XBRL解析完了（通期会社予想）: %d項目", len(annual_fc_xbrl))

        return build_quarterly_report(match, actuals, prior, annual_fc_xbrl, consensus_data, year, dtype)

    # ── 業績予想修正 ─────────────────────────────────────────────────────────
    if dtype == "revision":
        year = title_year if title_year in years_data else None
        if year is None:
            logger.info("登録データに該当年度なし: %s (登録: %s)", title_year, list(years_data.keys()))
            return None

        logger.info("年度マッチ: %s  業績予想修正", year)
        revised  = _parse_xbrl_values(xbrl_text, REVISED_FORECAST_CONTEXTS)
        logger.info("XBRL解析完了（修正後予想）: %d項目", len(revised))

        previous = _parse_xbrl_values(xbrl_text, PREVIOUS_FORECAST_CONTEXTS)
        logger.info("XBRL解析完了（修正前予想）: %d項目", len(previous))

        return build_revision_report(match, revised, previous, consensus_data, year)

    return None


# ---------------------------------------------------------------------------
# 単体テスト用
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="analyzer 単体テスト")
    parser.add_argument("--code",     required=True)
    parser.add_argument("--xbrl-url", required=True)
    parser.add_argument("--title",    default="2026年04月期 本決算")
    parser.add_argument("--pdf-url",  default="")
    args = parser.parse_args()

    result = analyze({
        "code": args.code, "company": "", "title": args.title,
        "time": "15:30", "pdf_url": args.pdf_url, "xbrl_url": args.xbrl_url,
    })
    print(result or "レポートなし")
