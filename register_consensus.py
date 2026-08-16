"""
業績ﾃﾞｰﾀ登録ﾂｰﾙ

ﾃｷｽﾄﾌｧｲﾙﾆ記載ｼﾀ業績ﾃｰﾌﾞﾙｦ読ﾐ込ﾝﾃﾞ登録ｼﾏｽ｡
ﾍｯﾀﾞｰｶﾞ複数行ﾆ分ｶﾚﾃｲﾃﾓ自動ﾃﾞ処理ｼﾏｽ｡

使ｲ方:
  python register_consensus.py --file 味ﾉ素.txt   # ﾌｧｲﾙｶﾗ登録
  python register_consensus.py --list              # 登録済ﾐ一覧ｦ表示
  python register_consensus.py --show 2802         # 指定ｺｰﾄﾞﾉ内容ｦ表示
  python register_consensus.py --delete 2802       # 指定ｺｰﾄﾞｦ削除

登録ﾃﾞｰﾀﾊ consensus/ ﾌｫﾙﾀﾞﾆ証券ｺｰﾄﾞｺﾞﾄﾉ JSON ﾃﾞ保存ｻﾚﾏｽ｡
"""

import argparse
import json
import os
import re
import sys

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


def _data_path(code):
    return os.path.join(DATA_DIR, f"{code}.json")


def _load(code):
    path = _data_path(code)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(code, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_data_path(code), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存ｼﾏｼﾀ: {_data_path(code)}")


def _clean_number(s):
    s = s.strip().replace(",", "").replace(" ", "").replace("\u3000", "")
    if s in ("", "-", "-", "—", "N/A", "na"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _extract_year(text):
    m = re.search(r"(\d{4})年(\d{1,2})月", text)
    if not m:
        return None
    return f"{m.group(1)}年{m.group(2).zfill(2)}月"


def _fmt(v, key):
    if v is None:
        return "-"
    if key in ("eps", "dividend"):
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def _is_item_line(line):
    return any(label in line for label, _ in ITEMS)


def _assemble_columns(parts):
    columns = []
    current_col_parts = []
    year_pattern = re.compile(r"\d{4}年\d{1,2}月")
    for part in parts:
        if year_pattern.search(part):
            if current_col_parts:
                columns.append(" ".join(current_col_parts))
            current_col_parts = [part]
        else:
            current_col_parts.append(part)
    if current_col_parts:
        columns.append(" ".join(current_col_parts))
    return columns


def _parse_header(header_lines):
    tab_lines = [l for l in header_lines if "\t" in l]
    all_parts = []
    if tab_lines:
        for line in header_lines:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            all_parts.extend(parts)
    else:
        for line in header_lines:
            line = line.strip()
            if line:
                all_parts.append(line)
    return _assemble_columns(all_parts)


def _parse_data_lines(data_lines, num_cols):
    item_values = {}
    for line in data_lines:
        matched_key = None
        for label, key in ITEMS:
            if label in line:
                matched_key = key
                break
        if matched_key is None:
            continue
        value_part = re.sub(r"^[^\t]+\t", "", line)
        if not value_part.strip():
            value_part = line
            for label, _ in ITEMS:
                value_part = value_part.replace(label, "")
        if "\t" in value_part:
            raw_values = [v.strip() for v in value_part.split("\t")]
        else:
            raw_values = [v.strip() for v in re.split(r"\s{2,}", value_part)]
        raw_values = [v for v in raw_values if v]
        item_values[matched_key] = [_clean_number(v) for v in raw_values]
    return item_values


def _build_table_data(columns, item_values):
    table_data = {}
    for col_idx, col_name in enumerate(columns):
        year = _extract_year(col_name)
        if year is None:
            continue
        is_company   = any(s in col_name for s in ["会社予想", "莨夂､ｾ莠域Φ", "ｶｲｼｬﾖｿｳ"])
        is_consensus = any(s in col_name for s in ["コンセンサス", "繧ｳ繝ｳ繧ｻ繝ｳ繧ｵ繧ｹ", "ｺﾝｾﾝｻｽ"])
        if not is_company and not is_consensus:
            continue
        forecast_type = "company_forecast" if is_company else "consensus_forecast"
        if year not in table_data:
        	table_data[year] = {}
        if forecast_type in table_data[year]:
            continue
        values = {}
        for key in ITEM_KEYS:
            vals = item_values.get(key, [])
            v = vals[col_idx] if col_idx < len(vals) else None
            if v is not None:
                values[key] = v
        if values:
            table_data[year][forecast_type] = values
    return table_data


def _parse_excel_format(lines):
    """
    Excelからコピペしたタブ区切り形式を解析する。
    CODE\t証券コード
    COMPANY\t会社名
    \t列名1\t列名2...
    売上高\t数値1\t数値2...
    """
    code = ""
    company = ""
    header_lines = []
    data_lines = []

    for line in lines:
        cols = line.split("\t")
        first = cols[0].strip()
        if re.match(r"CODE", first, re.IGNORECASE):
            code = cols[1].strip() if len(cols) > 1 else ""
        elif re.match(r"COMPANY", first, re.IGNORECASE):
            company = cols[1].strip() if len(cols) > 1 else ""
        elif _is_item_line(line):
            data_lines.append(line)
        elif any(_extract_year(c) for c in cols):
            header_lines.append(line)

    return code, company, header_lines, data_lines


def _is_excel_format(text):
    """1行目か2行目に CODE\t や COMPANY\t があればExcel貼り付け形式と判断する。"""
    for line in text.splitlines()[:5]:
        if re.match(r"CODE\t", line, re.IGNORECASE) or re.match(r"COMPANY\t", line, re.IGNORECASE):
            return True
    return False


def parse_input_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()

    if _is_excel_format(text):
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        code, company, header_lines, data_lines = _parse_excel_format(lines)
    else:
        code = ""
        company = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if re.match(r"CODE\s*=", line, re.IGNORECASE):
                code = re.sub(r"^CODE\s*=\s*", "", line, flags=re.IGNORECASE).strip()
            elif re.match(r"COMPANY\s*=", line, re.IGNORECASE):
                company = re.sub(r"^COMPANY\s*=\s*", "", line, flags=re.IGNORECASE).strip()
        table_match = re.search(r"\[TABLE\](.*?)\[/TABLE\]", text, re.DOTALL)
        if not table_match:
            raise ValueError("[TABLE] ～ [/TABLE] ブロックが見つかりません")
        table_text = table_match.group(1).strip()
        lines = [l.rstrip() for l in table_text.splitlines()]
        lines = [l for l in lines if l.strip()]
        header_lines = []
        data_lines = []
        for line in lines:
            if _is_item_line(line):
                data_lines.append(line)
            else:
                header_lines.append(line)

    if not code:
        raise ValueError("CODE が見つかりません")
    if not company:
        raise ValueError("COMPANY が見つかりません")
    if not header_lines:
        raise ValueError("ヘッダー行が見つかりません")
    if not data_lines:
        raise ValueError("データ行が見つかりません")

    columns = _parse_header(header_lines)
    print(f"  ヘッダー解析: {len(columns)} 列検出")
    for i, col in enumerate(columns):
        print(f"    [{i}] {col}")
    item_values = _parse_data_lines(data_lines, len(columns))
    table_data = _build_table_data(columns, item_values)
    return code, company, table_data


def cmd_register(args):
    print("解析中...")
    try:
        code, company, table_data = parse_input_file(args.file)
    except Exception as e:
        print(f"エラー: {e}")
        sys.exit(1)
    if not table_data:
        print("エラー: 会社予想またはコンセンサスの列が見つかりません")
        sys.exit(1)
    print()
    print("=" * 60)
    print(f"登録内容: {company} ({code})")
    print("=" * 60)
    for year in sorted(table_data.keys(), reverse=True):
        entry = table_data[year]
        c_fc = entry.get("company_forecast", {})
        n_fc = entry.get("consensus_forecast", {})
        print(f"\n【{year}】")
        print(f"  {'項目':<12} {'会社予想':>15} {'コンセンサス':>15}")
        print(f"  {'-' * 44}")
        for key in ITEM_KEYS:
            label = ITEM_LABELS[key]
            print(f"  {label:<12} {_fmt(c_fc.get(key), key):>15} {_fmt(n_fc.get(key), key):>15}")
    print()
    existing = _load(code)
    if existing:
        print(f"※ 既存データあり（{existing.get('company', '')}）。上書きします。")
    print("登録しますか？ [Y/n]: ", end="")
    if input().strip().lower() == "n":
        print("キャンセルしました")
        return
    data = {"code": code, "company": company, "years": table_data}
    _save(code, data)
    print("登録完了！")


def cmd_list(args):
    if not os.path.exists(DATA_DIR):
        print("登録データがありません")
        return
    files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".json"))
    if not files:
        print("登録データがありません")
        return
    print(f"{'コード':<8} {'会社名':<20} 登録年度")
    print("-" * 55)
    for fname in files:
        code = fname.replace(".json", "")
        data = _load(code)
        years = sorted(data.get("years", {}).keys(), reverse=True)
        print(f"{code:<8} {data.get('company', ''):<20} {', '.join(years)}")


def cmd_show(args):
    data = _load(args.show)
    if not data:
        print(f"データが見つかりません: {args.show}")
        return
    print(f"\n{data.get('company', '')} ({args.show})")
    for year in sorted(data.get("years", {}).keys(), reverse=True):
        entry = data["years"][year]
        c_fc = entry.get("company_forecast", {})
        n_fc = entry.get("consensus_forecast", {})
        print(f"\n【{year}】")
        print(f"  {'項目':<12} {'会社予想':>15} {'コンセンサス':>15}")
        print(f"  {'-' * 44}")
        for key in ITEM_KEYS:
            label = ITEM_LABELS[key]
            print(f"  {label:<12} {_fmt(c_fc.get(key), key):>15} {_fmt(n_fc.get(key), key):>15}")


def cmd_delete(args):
    path = _data_path(args.delete)
    if not os.path.exists(path):
        print(f"データが見つかりません: {args.delete}")
        return
    data = _load(args.delete)
    print(f"削除します: {data.get('company', '')} ({args.delete})")
    print("本当に削除しますか？ [y/N]: ", end="")
    if input().strip().lower() != "y":
        print("キャンセルしました")
        return
    os.remove(path)
    print("削除しました")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="業績データ登録ツール")
    parser.add_argument("--file",   metavar="FILE", help="入力ファイルパス")
    parser.add_argument("--list",   action="store_true", help="登録済み一覧を表示")
    parser.add_argument("--show",   metavar="CODE", help="指定コードの内容を表示")
    parser.add_argument("--delete", metavar="CODE", help="指定コードを削除")
    args = parser.parse_args()

    if args.list:
        cmd_list(args)
    elif args.show:
        cmd_show(args)
    elif args.delete:
        cmd_delete(args)
    elif args.file:
        cmd_register(args)
    else:
        parser.print_help() 