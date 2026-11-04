import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# リポジトリルートパスの取得
ROOT_DIR = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
ISSUE_LOG_HEADER_REGEX = r"(?:^|\n)(?:---\s*\n+)?# Daily Issue Log:[^\n]*"

# 継続更新型イシューや除外用デフォルトラベル（小文字で比較）
DEFAULT_EXCLUDE_LABELS = {
    "ongoing",
    "persistent",
    "evergreen",
    "常設",
    "継続更新",
    "集約除外",
    "exclude-daily",
    "no-daily-log",
}


def get_exclude_labels() -> set[str]:
    """除外対象のラベルセット（小文字）を取得（環境変数 EXCLUDE_LABELS があれば優先）"""
    env_val = os.environ.get("EXCLUDE_LABELS")
    if env_val is not None:
        return {item.strip().lower() for item in env_val.split(",") if item.strip()}
    return DEFAULT_EXCLUDE_LABELS


def should_exclude_issue(issue: dict, exclude_labels: set[str]) -> tuple[bool, str]:
    """Issueが除外ラベルを持っているか判定する"""
    if not exclude_labels:
        return False, ""
    for lbl in issue.get("labels", []):
        if isinstance(lbl, dict) and "name" in lbl:
            name = lbl["name"].strip()
            if name.lower() in exclude_labels:
                return True, name
    return False, ""


def get_target_date() -> datetime.date:
    """集約対象日を取得（環境変数 TARGET_DATE、コマンドライン引数、または前日JST）"""
    target_str = os.environ.get("TARGET_DATE")
    if not target_str and len(sys.argv) > 1:
        target_str = sys.argv[1]

    if target_str:
        try:
            return datetime.strptime(target_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            print(f"[Error] Invalid TARGET_DATE format: {target_str}. Expected YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    now_jst = datetime.now(JST)
    return (now_jst - timedelta(days=1)).date()


def get_repo_name() -> str:
    """リポジトリ名 (owner/repo) を取得"""
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo.strip()

    # ローカル実行時のフォールバック: git remote origin から取得
    try:
        import subprocess

        output = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(ROOT_DIR),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", output)
        if m:
            return m.group(1)
    except Exception:
        pass

    print("[Error] Environment variable 'GH_REPO' or 'GITHUB_REPOSITORY' is not set.", file=sys.stderr)
    sys.exit(1)


def github_api_get(url: str, token: str | None) -> list | dict:
    """GitHub REST APIを呼び出す"""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "Personal-DailyIssueAggregator")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"[Error] GitHub API HTTP {e.code} for {url}: {error_body}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"[Error] Request failed for {url}: {e}", file=sys.stderr)
        raise


def get_issues_for_date(
    repo: str, target_date: datetime.date, token: str | None, exclude_labels: set[str]
) -> list[dict]:
    """
    指定日（JST）に作成されたIssueを取得する。
    作成日時（createdAt）の降順でAPIから取得し、JSTの日付が一致するものを収集する。
    除外ラベルを持つIssueはスキップする。
    """
    target_issues = []
    page = 1
    per_page = 100

    while True:
        url = (
            f"https://api.github.com/repos/{repo}/issues"
            f"?state=all&sort=created&direction=desc&per_page={per_page}&page={page}"
        )
        print(f"Fetching issues from GitHub API (page {page})...")
        items = github_api_get(url, token)

        if not isinstance(items, list) or len(items) == 0:
            break

        stop_pagination = False
        for item in items:
            # Pull Request は除外（Issueのみ対象）
            if "pull_request" in item:
                continue

            created_raw = item.get("created_at")
            if not created_raw:
                continue

            created_utc = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            created_jst = created_utc.astimezone(JST)
            created_date_jst = created_jst.date()

            if created_date_jst == target_date:
                is_excluded, matched_label = should_exclude_issue(item, exclude_labels)
                if is_excluded:
                    print(
                        f"  [Skip] Issue #{item.get('number')} '{item.get('title')}' "
                        f"is excluded by label '{matched_label}'."
                    )
                    continue
                target_issues.append(item)
            elif created_date_jst < target_date:
                # 降順で取得しているため、作成日がtarget_dateより古くなったら以降は存在しない
                stop_pagination = True
                break
            # target_date より新しい場合はスキップして次へ

        if stop_pagination or len(items) < per_page:
            break

        page += 1

    return target_issues


def get_comments_for_issue(repo: str, issue_number: int, token: str | None) -> list[dict]:
    """Issueに紐づくすべてのコメントを取得する"""
    comments = []
    page = 1
    per_page = 100

    while True:
        url = (
            f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
            f"?per_page={per_page}&page={page}"
        )
        items = github_api_get(url, token)
        if not isinstance(items, list) or len(items) == 0:
            break

        comments.extend(items)
        if len(items) < per_page:
            break
        page += 1

    return comments


def format_issue(issue: dict, comments: list[dict]) -> list[str]:
    """単一のIssueとそのコメントをMarkdownフォーマットの行リストに変換する"""
    created_utc = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
    created_jst = created_utc.astimezone(JST)
    created_str = created_jst.strftime("%Y-%m-%d %H:%M")

    state_str = issue.get("state", "").upper()
    labels = [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict) and "name" in lbl]
    labels_str = ", ".join(labels) if labels else "なし"
    url = issue.get("html_url", "")
    body = (issue.get("body") or "").strip()
    if not body:
        body = "本文なし"

    lines = [
        f"## Issue #{issue['number']}: {issue.get('title', '')}",
        "",
        f"- 作成日時：{created_str}",
        f"- 状態：{state_str}",
        f"- ラベル：{labels_str}",
        f"- URL：{url}",
        "",
        "### Issue本文",
        "",
        body,
        "",
        "### コメント",
        "",
    ]

    if comments:
        for comment in comments:
            c_created_utc = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
            c_created_jst = c_created_utc.astimezone(JST)
            c_str = c_created_jst.strftime("%Y-%m-%d %H:%M")
            author = comment.get("user", {}).get("login", "unknown")
            c_body = (comment.get("body") or "").strip()
            lines.append(f"#### {c_str} / {author}")
            lines.append("")
            lines.append(c_body if c_body else "(本文なし)")
            lines.append("")
    else:
        lines.append("コメントなし。")
        lines.append("")

    return lines


def generate_issue_log_markdown(target_date: datetime.date, issues_with_comments: list[tuple[dict, list[dict]]]) -> str:
    """日次Issue集約のMarkdown全体を生成する"""
    date_str = target_date.isoformat()
    generated_at_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# Daily Issue Log: {date_str}",
        "",
        f"- 集約対象日：{date_str}",
        f"- Issue数：{len(issues_with_comments)}",
        f"- 集約実行日時：{generated_at_str}",
        "",
    ]

    if issues_with_comments:
        for issue, comments in issues_with_comments:
            lines.append("---")
            lines.append("")
            lines.extend(format_issue(issue, comments))
    else:
        lines.append("対象日に作成されたIssueはありません。")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_log(target_date: datetime.date, markdown_text: str) -> Path:
    """
    本リポジトリの構成に合わせて 40_DailyLog/{YYYY}/{YYYY-MM-DD}.md に保存する。
    既存ファイルが存在する場合:
    - 既存の Issue Log セクションがあれば置換
    - 手動メモ等がある場合は温存してマージ
    """
    year_str = str(target_date.year)
    date_str = target_date.isoformat()
    output_dir = ROOT_DIR / "40_DailyLog" / year_str
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{date_str}.md"

    if output_file.exists():
        existing_text = output_file.read_text(encoding="utf-8")
        match = re.search(ISSUE_LOG_HEADER_REGEX, existing_text)
        if match:
            # 既存の Issue Log 開始位置より前のコンテンツ（手動メモ等）を保持
            prefix = existing_text[: match.start()].rstrip()
            if prefix:
                final_content = f"{prefix}\n\n---\n\n{markdown_text}"
            else:
                final_content = markdown_text
        else:
            # Issue Log ヘッダーがない既存ファイル（手書きメモ等）がある場合
            prefix = existing_text.rstrip()
            if prefix:
                final_content = f"{prefix}\n\n---\n\n{markdown_text}"
            else:
                final_content = markdown_text
    else:
        final_content = markdown_text

    output_file.write_text(final_content, encoding="utf-8")
    return output_file


def main():
    repo = get_repo_name()
    target_date = get_target_date()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    exclude_labels = get_exclude_labels()

    print(f"Repository: {repo}")
    print(f"Target date (JST): {target_date.isoformat()}")
    if exclude_labels:
        print(f"Excluded labels: {', '.join(sorted(exclude_labels))}")
    else:
        print("Excluded labels: (None)")

    # 対象日のIssue取得（除外ラベルを適用）
    issues = get_issues_for_date(repo, target_date, token, exclude_labels)
    # 作成日時昇順にソート
    issues.sort(key=lambda x: x["created_at"])
    print(f"Found {len(issues)} issue(s) created on {target_date.isoformat()}.")

    # コメント取得
    issues_with_comments = []
    for issue in issues:
        issue_number = issue["number"]
        comment_count = issue.get("comments", 0)
        comments = []
        if comment_count > 0:
            print(f"  Fetching {comment_count} comment(s) for Issue #{issue_number}...")
            comments = get_comments_for_issue(repo, issue_number, token)
            # コメント作成日時昇順にソート
            comments.sort(key=lambda x: x["created_at"])
        issues_with_comments.append((issue, comments))

    # Markdown生成
    md_content = generate_issue_log_markdown(target_date, issues_with_comments)

    # ファイル保存 (40_DailyLog/{YYYY}/{YYYY-MM-DD}.md)
    saved_file = save_log(target_date, md_content)
    print(f"Saved log to: {saved_file.relative_to(ROOT_DIR)}")
    print("Done.")


if __name__ == "__main__":
    main()
