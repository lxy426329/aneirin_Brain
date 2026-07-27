# ============================================================
# Module: Menstrual Cycle Tracker (cycle_tracker.py)
# 模块：例假周期追踪器
#
# Tracks menstrual cycle data and provides predictions.
# 追踪例假周期数据并提供预测。
#
# Core design:
# 核心逻辑：
#   - Store cycle records in a JSON file
#     将周期记录存储在 JSON 文件中
#   - Calculate average cycle length from historical data
#     根据历史数据计算平均周期长度
#   - Predict next cycle start date
#     预测下次例假开始日期
#   - Calculate days until next cycle (for reminders)
#     计算距离下次例假的天数（用于提醒）
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict

logger = logging.getLogger("ombre_brain.cycle")


class CycleTracker:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.join(data_dir, "cycle")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "cycle_data.json")
        self.data = self._load_data()

    def _load_data(self) -> Dict:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load cycle data: {e}")
                return {"records": [], "settings": {}}
        return {"records": [], "settings": {}}

    def _save_data(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save cycle data: {e}")

    def add_record(
        self,
        start_date: str,
        symptoms: str = "",
        duration: int = 5,
        notes: str = "",
        flow_level: str = "normal",
        pain_level: int = 0,
    ) -> tuple:
        """Returns (success: bool, error_msg: str)"""
        try:
            date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            try:
                date_obj = datetime.strptime(start_date, "%Y/%m/%d").date()
            except ValueError:
                return False, f"日期格式无效: {start_date}，请使用 YYYY-MM-DD 或 YYYY/MM/DD"

        # Reject future dates (more than 1 day ahead)
        today = datetime.now().date()
        if date_obj > today + timedelta(days=1):
            return False, f"开始日期 {start_date} 是未来日期，无法记录"

        # Check for overlapping cycles
        for existing in self.data["records"]:
            exist_date = datetime.fromordinal(existing["date_timestamp"]).date()
            exist_end = exist_date + timedelta(days=existing["duration"])
            if date_obj >= exist_date and date_obj < exist_end:
                return False, f"日期 {start_date} 与已有记录 {existing['start_date']} 重叠"

        record = {
            "start_date": start_date,
            "date_timestamp": date_obj.toordinal(),
            "symptoms": symptoms.strip(),
            "duration": max(1, min(14, int(duration))),
            "notes": notes.strip(),
            "flow_level": flow_level.strip().lower(),
            "pain_level": max(0, min(10, int(pain_level))),
            "recorded_at": datetime.now().isoformat(),
        }

        self.data["records"].append(record)
        self.data["records"].sort(key=lambda r: r["date_timestamp"])

        self._save_data()
        logger.info(f"Added cycle record: {start_date}")
        return True, ""

    def get_all_records(self) -> List[Dict]:
        return self.data["records"]

    def get_record_by_date(self, start_date: str) -> Optional[Dict]:
        for record in self.data["records"]:
            if record["start_date"] == start_date:
                return record
        return None

    def delete_record(self, start_date: str) -> bool:
        original_count = len(self.data["records"])
        self.data["records"] = [
            r for r in self.data["records"]
            if r["start_date"] != start_date
        ]
        if len(self.data["records"]) < original_count:
            self._save_data()
            return True
        return False

    def update_record(
        self,
        start_date: str,
        symptoms: str = None,
        duration: int = None,
        notes: str = None,
        flow_level: str = None,
        pain_level: int = None,
    ) -> bool:
        for record in self.data["records"]:
            if record["start_date"] == start_date:
                if symptoms is not None:
                    record["symptoms"] = symptoms.strip()
                if duration is not None:
                    record["duration"] = max(1, min(14, int(duration)))
                if notes is not None:
                    record["notes"] = notes.strip()
                if flow_level is not None:
                    record["flow_level"] = flow_level.strip().lower()
                if pain_level is not None:
                    record["pain_level"] = max(0, min(10, int(pain_level)))
                self._save_data()
                return True
        return False

    def get_recent_records(self, count: int = 5) -> List[Dict]:
        records = sorted(self.data["records"], key=lambda r: r["date_timestamp"], reverse=True)
        return records[:count]

    def _parse_date(self, date_str: str) -> Optional[datetime.date]:
        for fmt in ["%Y-%m-%d", "%Y/%m/%d"]:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    def calculate_average_cycle(self) -> Optional[float]:
        records = sorted(self.data["records"], key=lambda r: r["date_timestamp"])
        if len(records) < 2:
            return None

        intervals = []
        for i in range(1, len(records)):
            prev_date = self._parse_date(records[i-1]["start_date"])
            curr_date = self._parse_date(records[i]["start_date"])
            if prev_date is None or curr_date is None:
                continue
            interval = (curr_date - prev_date).days
            if 20 <= interval <= 45:
                intervals.append(interval)

        if not intervals:
            return None

        return sum(intervals) / len(intervals)

    def predict_next_cycle(self) -> Optional[str]:
        records = sorted(self.data["records"], key=lambda r: r["date_timestamp"])
        if len(records) < 2:
            return None

        avg_cycle = self.calculate_average_cycle()
        if avg_cycle is None:
            return None

        last_date = self._parse_date(records[-1]["start_date"])
        if last_date is None:
            return None

        next_date = last_date + timedelta(days=round(avg_cycle))
        return next_date.strftime("%Y-%m-%d")

    def days_until_next_cycle(self) -> Optional[int]:
        next_date_str = self.predict_next_cycle()
        if next_date_str is None:
            return None

        next_date = self._parse_date(next_date_str)
        if next_date is None:
            return None

        today = datetime.now().date()
        delta = (next_date - today).days
        return delta

    def get_cycle_summary(self) -> Dict:
        records = sorted(self.data["records"], key=lambda r: r["date_timestamp"])
        avg_cycle = self.calculate_average_cycle()
        next_date = self.predict_next_cycle()
        days_until = self.days_until_next_cycle()

        last_record = records[-1] if records else None

        return {
            "total_records": len(records),
            "average_cycle_days": round(avg_cycle) if avg_cycle else None,
            "last_start_date": last_record["start_date"] if last_record else None,
            "last_duration": last_record["duration"] if last_record else None,
            "last_symptoms": last_record["symptoms"] if last_record else None,
            "predicted_next_date": next_date,
            "days_until_next": days_until,
        }