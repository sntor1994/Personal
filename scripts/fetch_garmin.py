import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# リポジトリルートパスの取得
ROOT_DIR = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")

HEALTH_DIR = ROOT_DIR / "70_HealthData" / "Garmin_health"
ACTIVITY_DIR = ROOT_DIR / "70_HealthData" / "Garmin_activity"


def save_json(path: Path, data: dict) -> None:
    """データを整形したJSONとして保存する"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  -> Saved: {path.relative_to(ROOT_DIR)}")


def init_garmin_client(api_key: str):
    """Garmin Personal API クライアントを初期化する"""
    try:
        from garmin_personal_api import GarminPersonalApiClient
        return GarminPersonalApiClient(api_key=api_key)
    except ImportError:
        try:
            # 別名モジュール・フォールバック対応
            from garmin_connect_personal_api import GarminPersonalApiClient
            return GarminPersonalApiClient(api_key=api_key)
        except ImportError as e:
            print("Error: Garmin Personal API client package is not installed.", file=sys.stderr)
            raise e


def fetch_daily_health(client, target_date: datetime.date) -> None:
    """指定日のデイリー健康データ（睡眠、HRV、心拍、ストレス、Body Battery）を取得・保存"""
    day_str = target_date.isoformat()
    print(f"Collecting Daily Health: {day_str}")

    daily_record = {
        "date": day_str,
        "fetched_at": datetime.now(JST).isoformat(),
        "sleep": None,
        "hrv": None,
        "heart_rate": None,
        "daily_summary": None,
        "stress": None,
        "body_battery": None,
    }

    # 各項目ごとに例外を個別キャッチし、一部欠損しても全体を落とさない
    data_fetchers = [
        ("sleep", lambda: client.get_daily_sleep(day_str)),
        ("hrv", lambda: client.get_daily_hrv(day_str)),
        ("heart_rate", lambda: client.get_daily_heart_rate(day_str)),
        ("daily_summary", lambda: client.get_daily_summaries(day_str, day_str)),
        ("stress", lambda: client.get_daily_stress(day_str, day_str)),
        ("body_battery", lambda: client.get_daily_body_battery(day_str, day_str)),
    ]

    for field, fetcher in data_fetchers:
        try:
            daily_record[field] = fetcher()
        except Exception as e:
            print(f"  [Warning] Failed to fetch {field} for {day_str}: {e}")

    output_path = HEALTH_DIR / f"{day_str}.json"
    save_json(output_path, daily_record)


def fetch_activities(client, start_date: datetime.date, end_date: datetime.date) -> None:
    """期間内のアクティビティ（ランニング等）を取得・保存"""
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()
    print(f"Collecting Activities: {start_str} to {end_str}")

    try:
        activities = client.get_activities(start_str, end_str)
    except Exception as e:
        print(f"  [Warning] Failed to fetch activities: {e}")
        return

    if not activities:
        print("  No activities found in range.")
        return

    # リスト形式で返ってくる想定
    activity_list = activities if isinstance(activities, list) else activities.get("activities", [])

    for act in activity_list:
        activity_id = act.get("activityId") or act.get("id")
        if not activity_id:
            continue

        # プライバシー保護: 位置情報の詳細ポリラインやGPS座標配列は除外（存在する場合）
        act.pop("geoPolylineDTO", None)
        act.pop("splitSummaries", None)

        output_path = ACTIVITY_DIR / f"{activity_id}.json"
        save_json(output_path, act)


def main():
    api_key = os.getenv("GARMIN_PERSONAL_API_KEY")
    if not api_key:
        print("Error: Environment variable 'GARMIN_PERSONAL_API_KEY' is not set.", file=sys.stderr)
        sys.exit(1)

    print("Initializing Garmin Personal API client...")
    client = init_garmin_client(api_key)

    today = datetime.now(JST).date()

    # 1. デイリー健康データ（直近3日間分を取得して同期漏れ・更新を反映）
    for i in range(3):
        target_date = today - timedelta(days=i)
        fetch_daily_health(client, target_date)

    # 2. アクティビティデータ（直近7日間分を取得）
    activity_start_date = today - timedelta(days=7)
    fetch_activities(client, activity_start_date, today)

    print("Garmin data collection completed successfully.")


if __name__ == "__main__":
    main()
